#!/usr/bin/env python3
"""Gera docs/index.html com as melhores promoções do Mercado Livre.

Uso:
  python gerar_site.py          # busca na API e gera o site
  python gerar_site.py --demo   # gera docs/preview.html com itens de EXEMPLO (só para ver o visual)

Variáveis de ambiente (opcionais, têm prioridade sobre o config.json):
  ML_CLIENT_ID, ML_CLIENT_SECRET   credenciais do app em developers.mercadolivre.com.br
  ML_MATT_TOOL, ML_MATT_WORD       parâmetros do seu link de afiliado
"""
import argparse
import shutil
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python antigo
    ZoneInfo = None


def fuso_brasilia():
    """Horário de Brasília. No Windows o banco de fusos pode faltar; nesse caso usa UTC-3 fixo."""
    try:
        return ZoneInfo("America/Sao_Paulo")
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=-3))

BASE = Path(__file__).parent
DOCS = BASE / "docs"
API = "https://api.mercadolibre.com"
UA = "achados-som/1.0"


def carregar(caminho, padrao):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return padrao


def http_json(url, headers=None, data=None, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def obter_token():
    cid, secret = os.environ.get("ML_CLIENT_ID"), os.environ.get("ML_CLIENT_SECRET")
    if not cid or not secret:
        print("[aviso] chaves ML_CLIENT_ID / ML_CLIENT_SECRET não preenchidas.", file=sys.stderr)
        return None
    corpo = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}
    ).encode()
    try:
        token = http_json(
            f"{API}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data=corpo,
        )["access_token"]
        print("[info] token do Mercado Livre obtido.")
        return token
    except Exception as e:  # noqa: BLE001
        print(f"[aviso] não consegui token do Mercado Livre: {e}", file=sys.stderr)
        return None


def buscar(termo, token, limite):
    qs = urllib.parse.urlencode({"q": termo, "limit": limite, "condition": "new"})
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return http_json(f"{API}/sites/MLB/search?{qs}", headers=headers).get("results", [])


def qualificado(item, filtros):
    """Loja confiável: oficial ou vendedor com reputação alta (MercadoLíder Gold/Platinum)."""
    if item.get("condition") != "new":
        return False
    if filtros.get("aceitar_loja_oficial") and item.get("official_store_id"):
        return True
    rep = (item.get("seller") or {}).get("seller_reputation") or {}
    return rep.get("power_seller_status") in filtros.get("niveis_aceitos", [])


def normalizar(item, categoria):
    preco = item.get("price")
    original = item.get("original_price")
    if not preco or not original or original <= preco:
        return None
    foto = (item.get("thumbnail") or "").replace("http://", "https://").replace("-I.", "-O.")
    rep = (item.get("seller") or {}).get("seller_reputation") or {}
    selo = "Loja oficial" if item.get("official_store_id") else (
        "MercadoLíder " + rep["power_seller_status"].capitalize() if rep.get("power_seller_status") else ""
    )
    nivel = 3 if item.get("official_store_id") else {"platinum": 2, "gold": 1}.get(rep.get("power_seller_status"), 0)
    return {
        "loja_nivel": nivel,
        "nota": None,
        "avaliacoes": 0,
        "id": item.get("id"),
        "categoria": categoria,
        "titulo": item.get("title", ""),
        "preco": float(preco),
        "preco_original": float(original),
        "desconto": round((1 - preco / original) * 100),
        "link": item.get("permalink", ""),
        "imagem": foto,
        "selo": selo,
        "frete_gratis": bool((item.get("shipping") or {}).get("free_shipping")),
        "vendidos": item.get("sold_quantity") or 0,
    }


def obter_avaliacao(item_id, token):
    """Nota média e quantidade de avaliações do produto. Devolve (None, 0) se não conseguir."""
    d = chamar_api("avaliações", f"/reviews/item/{item_id}", token, ignorar=(404,))
    if not d:
        return None, 0
    amostra("avaliações", json.dumps({k: d.get(k) for k in ("rating_average", "paging")}, ensure_ascii=False))
    return d.get("rating_average"), (d.get("paging") or {}).get("total", 0) or 0


def faixa_qualidade(nota):
    """Faixas de nota: 4,7+ = 3, 4,5+ = 2, 4,3+ = 1. Notas próximas ficam empatadas e o preço decide."""
    if nota is None:
        return 0
    return 3 if nota >= 4.7 else 2 if nota >= 4.5 else 1 if nota >= 4.3 else 0


def chave_ordem(p):
    """Ordem pedida: 1) qualidade do produto, 2) reputação da loja, 3) menor preço."""
    q = p["qualidade"] if p.get("qualidade") is not None else faixa_qualidade(p.get("nota"))
    return (-q, -p.get("loja_nivel", 0), p["preco"])


FALHAS = {}
LOG = []


def info(msg):
    print(msg)
    LOG.append(msg)


def chamar_api(rotulo, caminho, token, ignorar=()):
    """GET autenticado. Devolve o JSON ou None; conta as falhas por tipo para o resumo final.
    Se o Mercado Livre responder 429 (muitas consultas), espera e tenta de novo."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    chave = None
    for tentativa in range(5):
        time.sleep(0.1 if tentativa == 0 else 3 * tentativa)
        try:
            return http_json(API + caminho, headers=headers)
        except urllib.error.HTTPError as e:
            if e.code in ignorar:
                return None
            chave = f"{rotulo}: HTTP {e.code}"
            if e.code != 429:
                break
        except Exception as e:  # noqa: BLE001
            chave = f"{rotulo}: {type(e).__name__}"
    FALHAS[chave] = FALHAS.get(chave, 0) + 1
    return None


def resumo_falhas():
    if FALHAS:
        info("\nConsultas que falharam (tipo: quantidade):")
        for k, v in sorted(FALHAS.items()):
            info(f"  {k}  x{v}")
    if LOG:
        try:
            (BASE / "log.txt").write_text("\n".join(LOG), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def nivel_loja(body, token, cache):
    """(nível, selo): 3 = loja oficial, 2 = MercadoLíder Platinum, 1 = Gold,
    0.5 = reputação verde (boa, sem MercadoLíder), 0 = outras."""
    if body.get("official_store_id"):
        return 3, "Loja oficial"
    sid = body.get("seller_id")
    if sid not in cache:
        d = chamar_api("vendedor", f"/users/{sid}", token)
        rep = (d or {}).get("seller_reputation") or {}
        trans = (rep.get("transactions") or {})
        neg = ((rep.get("metrics") or {}).get("claims") or {}).get("rate")
        cache[sid] = {
            "status": rep.get("power_seller_status"),
            "level": rep.get("level_id"),
            "total": trans.get("total") or 0,
            "reclamacoes": neg,
        }
        amostra("vendedor", json.dumps({"seller_id": sid, **cache[sid]}, ensure_ascii=False))
    r = cache[sid]
    if r["status"] == "platinum":
        return 2, "MercadoLíder Platinum"
    if r["status"] == "gold":
        return 1, "MercadoLíder Gold"
    if r["level"] == "5_green" and r["total"] >= 100:
        return 0.5, "Boa reputação"
    return 0, ""


def loja_aceita(nivel, filtros):
    niveis = filtros.get("niveis_aceitos", ["platinum", "gold"])
    return (
        (nivel == 3 and filtros.get("aceitar_loja_oficial", True))
        or (nivel == 2 and "platinum" in niveis)
        or (nivel == 1 and "gold" in niveis)
        or (nivel == 0.5 and "verde" in niveis)
    )


def item_para_dict(b, categoria, nivel, selo):
    preco = float(b["price"])
    orig = b.get("original_price")
    orig = float(orig) if orig and float(orig) > preco else preco
    fotos = b.get("pictures") or []
    foto = (fotos[0].get("secure_url") if fotos else None) or b.get("thumbnail") or ""
    return {
        "id": b.get("id"),
        "categoria": categoria,
        "titulo": b.get("title", ""),
        "preco": preco,
        "preco_original": orig,
        "desconto": round((1 - preco / orig) * 100),
        "link": b.get("permalink", ""),
        "imagem": foto.replace("http://", "https://"),
        "selo": selo,
        "frete_gratis": bool((b.get("shipping") or {}).get("free_shipping")),
        "vendidos": b.get("sold_quantity") or 0,
        "loja_nivel": nivel,
        "nota": None,
        "avaliacoes": 0,
    }


AMOSTRAS = set()
MOTIVOS = {}


def motivo(chave):
    MOTIVOS[chave] = MOTIVOS.get(chave, 0) + 1


def amostra(rotulo, texto):
    """Registra (só a primeira vez) um trecho de cada tipo de resposta, para diagnóstico."""
    if rotulo not in AMOSTRAS:
        AMOSTRAS.add(rotulo)
        info(f"[amostra: {rotulo}] {texto[:500]}")


def ids_de_itens(d, limite=5):
    """Extrai ids de itens de uma resposta de /products/{id}/items (aceita lista ou {'results': [...]})."""
    lista = d.get("results") if isinstance(d, dict) else d
    ids = []
    for e in lista or []:
        i = (e.get("item_id") or e.get("id")) if isinstance(e, dict) else (e if isinstance(e, str) else None)
        if i and i not in ids:
            ids.append(i)
        if len(ids) >= limite:
            break
    return ids


def _txt(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def marca_do_produto(r):
    for a in r.get("attributes") or []:
        if isinstance(a, dict) and a.get("id") == "BRAND":
            return a.get("value_name") or ""
    return ""


def marca_confere(m, marca, nome):
    nm = _txt(m)
    if not nm:
        return False
    if marca:
        mm = _txt(marca)
        return mm == nm or (len(nm) >= 4 and mm.startswith(nm + " "))
    return f" {nm} " in f" {_txt(nome)} "  # produto sem marca cadastrada: procura no nome


def nivel_marca(marca, nome, cat, f):
    """Qualidade definida pela curadoria: 3 = marca premium, 2 = marca confiável, 0 = fora da lista."""
    premium = cat.get("marcas_premium") or []
    confiaveis = cat.get("marcas_confiaveis") or []
    if not premium and not confiaveis:
        return 1  # categoria sem lista de marcas: não filtra
    for nivel, lista in ((3, premium), (2, confiaveis)):
        if any(marca_confere(m, marca, nome) for m in lista):
            return nivel
    return 1 if f.get("incluir_outras_marcas", False) else 0


def link_do_item(item_id):
    """MLB6958053892 -> https://produto.mercadolivre.com.br/MLB-6958053892"""
    m = re.match(r"^([A-Z]{3})(\d+)$", str(item_id))
    return f"https://produto.mercadolivre.com.br/{m.group(1)}-{m.group(2)}" if m else ""


def melhor_oferta(pid, token, f, cache_vendedor):
    """Oferta mais barata, de produto novo e de loja qualificada, para um produto do catálogo."""
    d = chamar_api("ofertas do produto", f"/products/{pid}/items", token, ignorar=(404,))
    if d is None:
        motivo("produto sem anúncios ativos (404)")
        return None
    amostra("itens do produto", json.dumps(d, ensure_ascii=False))
    lista = d.get("results") if isinstance(d, dict) else d
    ofertas = [
        o for o in (lista or [])
        if isinstance(o, dict) and o.get("condition") == "new" and o.get("price") and o.get("item_id")
    ]
    if ofertas:
        amostra("campos da oferta", json.dumps(sorted(ofertas[0].keys())))
    if not ofertas:
        motivo("produto sem oferta nova")
        return None
    ofertas.sort(key=lambda o: o["price"])
    for o in ofertas[: f.get("ofertas_examinadas", 15)]:  # só examina as mais baratas
        nivel, selo = nivel_loja(o, token, cache_vendedor)
        if loja_aceita(nivel, f):
            return o, nivel, selo
        r = cache_vendedor.get(o.get("seller_id")) or {}
        motivo("loja recusada: " + str(r.get("level") or "sem reputação"))
    motivo("produto sem loja aceita")
    return None


def oferta_para_dict(o, titulo, categoria, nivel, selo, pid):
    preco = float(o["price"])
    orig = o.get("original_price")
    orig = float(orig) if orig and float(orig) > preco else preco
    return {
        "id": o["item_id"],
        "pid": pid,
        "categoria": categoria,
        "titulo": titulo,
        "preco": preco,
        "preco_original": orig,
        "desconto": round((1 - preco / orig) * 100),
        "link": link_do_item(o["item_id"]),
        "imagem": "",
        "selo": selo,
        "frete_gratis": bool((o.get("shipping") or {}).get("free_shipping")),
        "vendidos": 0,
        "loja_nivel": nivel,
        "nota": None,
        "avaliacoes": 0,
        "em_oferta": bool(o.get("deal_ids")) or orig > preco,
    }


def coletar_catalogo(cfg, token):
    """Usa só o que o Mercado Livre libera para o app: catálogo -> ofertas do produto -> vendedor."""
    f = cfg["filtros"]
    modo_q = f.get("qualidade", "marcas")  # "marcas" (curadoria) ou "avaliacoes" (se o app tiver acesso às notas)
    resultado = {}
    cache_vendedor = {}
    total_produtos = 0
    for cat in cfg["categorias"]:
        # 1) produtos do catálogo para cada termo de busca (guarda nome e marca)
        produtos = {}
        for termo in cat["buscas"]:
            qs = urllib.parse.urlencode(
                {"status": "active", "site_id": "MLB", "q": termo, "limit": min(f.get("resultados_por_busca", 20), 50)}
            )
            d = chamar_api("busca no catálogo", f"/products/search?{qs}", token)
            for r in (d or {}).get("results") or []:
                if r.get("id") and r["id"] not in produtos:
                    produtos[r["id"]] = {"nome": r.get("name", ""), "marca": marca_do_produto(r), "qualidade": None}
        total_produtos += len(produtos)

        # 2) qualidade pela marca: só analisa o que passa na curadoria, marcas melhores primeiro
        if modo_q == "marcas":
            elegiveis = []
            for pid, pr in produtos.items():
                pr["qualidade"] = nivel_marca(pr["marca"], pr["nome"], cat, f)
                if pr["qualidade"] > 0:
                    elegiveis.append(pid)
            elegiveis.sort(key=lambda pid: -produtos[pid]["qualidade"])
        else:
            elegiveis = list(produtos)
        ids = elegiveis[: cat.get("produtos_analisados", f.get("produtos_analisados_por_categoria", 60))]
        MOTIVOS.clear()

        # 3) para cada produto, a oferta mais barata de uma loja qualificada
        candidatos = []
        for pid in ids:
            achado = melhor_oferta(pid, token, f, cache_vendedor)
            if not achado:
                continue
            o, nivel, selo = achado
            if o["price"] < cat.get("preco_min", 0):
                motivo("abaixo do preço mínimo")
                continue
            n = oferta_para_dict(o, produtos[pid]["nome"], cat["nome"], nivel, selo, pid)
            n["marca"] = produtos[pid]["marca"]
            n["qualidade"] = produtos[pid]["qualidade"]
            if f.get("exigir_promocao", False) and not n["em_oferta"]:
                continue
            candidatos.append(n)
        n_candidatos = len(candidatos)
        candidatos.sort(key=chave_ordem)
        candidatos = candidatos[: f.get("candidatos_por_categoria", 30)]

        # 4) avaliações (só se o app tiver acesso a elas)
        if modo_q == "avaliacoes":
            aprovados = []
            for n in candidatos:
                n["nota"], n["avaliacoes"] = obter_avaliacao(n["id"], token)
                if f.get("exigir_avaliacao", True):
                    if n["nota"] is None or n["nota"] < f.get("avaliacao_minima", 4.3):
                        continue
                    if n["avaliacoes"] < f.get("avaliacoes_minimas", 10):
                        continue
                aprovados.append(n)
        else:
            aprovados = candidatos
        aprovados.sort(key=chave_ordem)
        finais = aprovados[: f["itens_por_categoria"]]

        # 5) imagem só dos que vão para o site
        for n in finais:
            det = chamar_api("detalhe do produto", f"/products/{n['pid']}", token)
            if det is not None:
                resumo = {k: v for k, v in det.items() if k not in ("attributes", "main_features", "description", "pickers")}
                amostra("produto", json.dumps(resumo, ensure_ascii=False))
                fotos = det.get("pictures") or []
                if fotos and isinstance(fotos[0], dict):
                    n["imagem"] = (fotos[0].get("url") or fotos[0].get("secure_url") or "").replace("http://", "https://")
        resultado[cat["nome"]] = finais
        info(
            f"[{cat['nome']}] produtos={len(produtos)} na_curadoria={len(elegiveis)} analisados={len(ids)} "
            f"loja_ok={n_candidatos} no_site={len(finais)}"
        )
        if MOTIVOS:
            info("   descartes: " + "; ".join(f"{k} x{v}" for k, v in sorted(MOTIVOS.items(), key=lambda kv: -kv[1])))
    return resultado, total_produtos == 0


def coletar(cfg):
    filtros = cfg["filtros"]
    token = obter_token()
    if filtros.get("modo", "catalogo") == "catalogo":
        return coletar_catalogo(cfg, token)
    resultado = {}
    erros = 0
    tentativas = 0
    for cat in cfg["categorias"]:
        vistos, itens = set(), []
        for termo in cat["buscas"]:
            tentativas += 1
            try:
                brutos = buscar(termo, token, filtros["resultados_por_busca"])
            except urllib.error.HTTPError as e:
                erros += 1
                print(f"[aviso] busca '{termo}' falhou: HTTP {e.code}", file=sys.stderr)
                continue
            except Exception as e:  # noqa: BLE001
                erros += 1
                print(f"[aviso] busca '{termo}' falhou: {e}", file=sys.stderr)
                continue
            for b in brutos:
                if b.get("id") in vistos or not qualificado(b, filtros):
                    continue
                n = normalizar(b, cat["nome"])
                if not n or n["desconto"] < filtros["desconto_minimo"] or n["preco"] < cat.get("preco_min", 0):
                    continue
                vistos.add(n["id"])
                itens.append(n)
        # Só consulta avaliações dos candidatos mais vendidos, para não fazer chamadas demais.
        itens.sort(key=lambda x: x["vendidos"], reverse=True)
        itens = itens[: filtros.get("candidatos_por_categoria", 30)]
        aprovados = []
        for n in itens:
            n["nota"], n["avaliacoes"] = obter_avaliacao(n["id"], token)
            time.sleep(0.15)
            if filtros.get("exigir_avaliacao", True):
                if n["nota"] is None or n["nota"] < filtros.get("avaliacao_minima", 4.3):
                    continue
                if n["avaliacoes"] < filtros.get("avaliacoes_minimas", 10):
                    continue
            aprovados.append(n)
        aprovados.sort(key=chave_ordem)
        resultado[cat["nome"]] = aprovados[: filtros["itens_por_categoria"]]
    return resultado, (erros == tentativas and tentativas > 0)


def link_afiliado(url, cfg):
    tool = os.environ.get("ML_MATT_TOOL") or cfg["afiliado"].get("matt_tool", "")
    word = os.environ.get("ML_MATT_WORD") or cfg["afiliado"].get("matt_word", "")
    if not url:
        return ""
    base = url.split("?")[0].split("#")[0]
    if not tool or not word:
        return base
    return base + "?" + urllib.parse.urlencode({"matt_tool": tool, "matt_word": word})


def brl(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def card(p, cfg):
    e = html.escape
    tags = []
    if p.get("selo"):
        tags.append(f'<span class="tag ok">{e(p["selo"])}</span>')
    if p.get("nota"):
        tags.insert(0, f'<span class="tag star">★ {p["nota"]:.1f} ({p.get("avaliacoes", 0)})</span>')
    if p.get("marca") and p.get("qualidade"):
        tags.insert(0, f'<span class="tag star">{e(p["marca"])}</span>')
    if p.get("frete_gratis"):
        tags.append('<span class="tag">Frete grátis</span>')
    link = link_afiliado(p["link"], cfg)
    off = f'<span class="off">-{p["desconto"]}%</span>' if p.get("desconto", 0) > 0 else ""
    velho = (
        f'<div class="old">{brl(p["preco_original"])}</div>'
        if p["preco_original"] > p["preco"]
        else '<div class="old" aria-hidden="true">&nbsp;</div>'  # reserva o espaço: todos os cards ficam iguais
    )
    img = (
        f'<img src="{e(p["imagem"], quote=True)}" alt="{e(p["titulo"], quote=True)}" loading="lazy">'
        if p.get("imagem") else ""
    )
    if p.get("em_oferta") and not p.get("desconto"):
        tags.append('<span class="tag star">Em oferta</span>')
    return f"""<article class="card">
  <div class="img">{off}{img}</div>
  <div class="body">
    <div class="title">{e(p["titulo"])}</div>
    {velho}
    <div class="price">{brl(p["preco"])}</div>
    <div class="tags">{"".join(tags)}</div>
    <a class="btn" href="{e(link, quote=True)}" target="_blank" rel="sponsored nofollow noopener">Ver no Mercado Livre</a>
  </div>
</article>"""


def foto_src(nome):
    """Embute a foto no próprio index.html (não depende de enviar a pasta img ao GitHub)."""
    import base64
    arq = BASE / "assets" / nome
    if not arq.is_file():
        return "img/" + nome
    tipo = "image/webp" if arq.suffix.lower() == ".webp" else "image/png" if arq.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{tipo};base64," + base64.b64encode(arq.read_bytes()).decode()


ICONES_PADRAO = ["mixer", "mic", "speaker", "video", "laptop", "cable"]


def slug_de(nome):
    return "".join(c.lower() if c.isalnum() else "-" for c in nome).strip("-")


def renderizar(dados, cfg, saida, aviso_demo=False, atualizado=None):
    e = html.escape
    abas, secoes, cards_cat = [], [], []
    por_nome = {c["nome"]: (i, c) for i, c in enumerate(cfg.get("categorias", []))}
    for nome, itens in dados.items():
        slug = slug_de(nome)
        abas.append(f'  <button class="tab" data-cat="{slug}" aria-pressed="false">{e(nome)}</button>')
        corpo = "\n".join(card(p, cfg) for p in itens) if itens else '<p class="empty">Sem ofertas qualificadas hoje nesta categoria. Volte amanhã!</p>'
        secoes.append(f'<section data-cat="{slug}">\n<h3>{e(nome)}</h3>\n<div class="grid">\n{corpo}\n</div>\n</section>')
        i, c = por_nome.get(nome, (len(cards_cat), {}))
        icone = c.get("icone") or ICONES_PADRAO[i % len(ICONES_PADRAO)]
        foto = (
            f'<img src="img/{e(c["imagem"], quote=True)}" alt="" loading="lazy">' if c.get("imagem") else ""
        )
        cards_cat.append(
            f'      <a class="cat" href="#achados" data-cat="{slug}">'
            f'<div class="cat-img">{foto}<svg class="ico"><use href="#i-{icone}"/></svg></div>'
            f'<div class="cat-info"><svg class="ico"><use href="#i-{icone}"/></svg><b>{e(nome)}</b>'
            f'<svg class="ico go"><use href="#i-arrow"/></svg></div></a>'
        )
    agora = atualizado or datetime.now(fuso_brasilia()).strftime("%d/%m/%Y às %H:%M")
    modelo = (BASE / "template.html").read_text(encoding="utf-8")
    sub = cfg["subtitulo"]
    if aviso_demo:
        sub = "PRÉVIA COM ITENS DE EXEMPLO — preços e produtos fictícios, só para ver o visual."
    site = cfg.get("site", {})
    contato = cfg.get("contato_url", "")
    btn = (
        f'      <a class="ghost" href="{e(contato, quote=True)}" target="_blank" rel="noopener">'
        f'<svg class="ico"><use href="#i-whats"/></svg>{e(cfg.get("contato_rotulo", "Falar comigo"))}</a>'
        if contato else ""
    )
    site_url = cfg.get("url_site", "").rstrip("/") + "/" if cfg.get("url_site") else ""
    perfil = cfg.get("perfil_ml") or "https://www.mercadolivre.com.br/"
    pagina = (
        modelo.replace("{{TITULO_CAPS}}", e(cfg["titulo"].upper()))
        .replace("{{TITULO}}", e(cfg["titulo"]))
        .replace("{{DESCRICAO}}", e(cfg["subtitulo"], quote=True))
        .replace("{{MARCA1}}", e(site.get("marca_linha1", "CURADORIA DO")))
        .replace("{{MARCA2}}", e(site.get("marca_linha2", "DANIEL")))
        .replace("{{HERO1}}", e(site.get("hero_chamada", "Equipamentos escolhidos por quem entende de")))
        .replace("{{HERO2}}", e(site.get("hero_destaque", "áudio e eventos.")))
        .replace("{{HERO_TEXTO}}", e(sub if aviso_demo else site.get("hero_texto", cfg["subtitulo"])))
        .replace("{{FOTO_SRC}}", foto_src(site.get("foto", "daniel-bancada.webp")))
        .replace("{{ASSINATURA}}", e(site.get("assinatura", "Daniel")))
        .replace("{{RODAPE_TEXTO}}", e(site.get("rodape", "")))
        .replace("{{PERFIL_ML}}", e(link_afiliado(perfil, cfg) if "/social/" not in perfil else perfil, quote=True))
        .replace("{{CONTATO_BTN}}", btn)
        .replace("{{SITE_URL}}", e(site_url, quote=True))
        .replace("{{ATUALIZADO}}", agora)
        .replace("{{ABAS}}", "\n".join(abas))
        .replace("{{SECOES}}", "\n".join(secoes))
    )
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    og = BASE / "assets" / "og.jpg"  # imagem do cartão de compartilhamento (WhatsApp etc.)
    if og.is_file():
        shutil.copyfile(og, DOCS / "og.jpg")
    (DOCS / saida).write_text(pagina, encoding="utf-8")


def dados_demo(cfg):
    # Valores fictícios, propositalmente fora de ordem, para conferir o ranking.
    exemplos = [  # (nota, avaliações, nível da loja, preço)
        (4.5, 120, 3, 650.0), (4.8, 300, 1, 990.0), (4.8, 210, 3, 940.0),
        (4.8, 95, 3, 870.0), (4.6, 80, 2, 700.0), (4.4, 60, 2, 560.0),
    ]
    selos = {3: "Loja oficial", 2: "MercadoLíder Platinum", 1: "MercadoLíder Gold"}
    out = {}
    for i, cat in enumerate(cfg["categorias"]):
        itens = []
        for j, (nota, aval, nivel, preco) in enumerate(exemplos):
            original = round(preco * 1.3, 2)
            itens.append({
                "id": f"DEMO{i}{j}", "categoria": cat["nome"],
                "titulo": f"EXEMPLO — {cat['buscas'][j % len(cat['buscas'])]} (item fictício)",
                "preco": preco, "preco_original": original, "desconto": round((1 - preco / original) * 100),
                "link": "https://www.mercadolivre.com.br/", "imagem": "", "selo": selos[nivel],
                "frete_gratis": j % 2 == 0, "vendidos": 0,
                "nota": nota, "avaliacoes": aval, "loja_nivel": nivel,
            })
        out[cat["nome"]] = sorted(itens, key=chave_ordem)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    cfg = carregar(BASE / "config.json", {})

    if args.demo:
        renderizar(dados_demo(cfg), cfg, "preview.html", aviso_demo=True)
        print("Prévia gerada em docs/preview.html")
        return 0

    cache_path = DOCS / "dados.json"
    atualizado = None  # None = agora (coleta nova); com cache, mostra a hora da última coleta de verdade
    dados, tudo_falhou = coletar(cfg)
    resumo_falhas()
    total = sum(len(v) for v in dados.values())

    if tudo_falhou or total == 0:
        cache = carregar(cache_path, None)
        if cache:
            print("[aviso] coleta sem resultados; mantendo os últimos dados salvos.", file=sys.stderr)
            dados = cache
            try:
                atualizado = (DOCS / "atualizado.txt").read_text(encoding="utf-8").strip() or None
            except Exception:  # noqa: BLE001
                atualizado = None
        else:
            print("[aviso] coleta sem resultados e sem cache ainda.", file=sys.stderr)
            print("[erro] nada para publicar; o index.html NÃO foi alterado.", file=sys.stderr)
            return 1
    else:
        DOCS.mkdir(exist_ok=True)
        cache_path.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
        (DOCS / "atualizado.txt").write_text(
            datetime.now(fuso_brasilia()).strftime("%d/%m/%Y às %H:%M"), encoding="utf-8")

    # Destaques manuais (sua curadoria) entram sempre no topo da categoria indicada.
    for m in carregar(BASE / "produtos_manuais.json", []):
        cat = m.get("categoria")
        if cat in dados and m.get("link") and m.get("preco"):
            m.setdefault("preco_original", m["preco"])
            m.setdefault("desconto", max(0, round((1 - m["preco"] / m["preco_original"]) * 100)))
            m.setdefault("imagem", "")
            m.setdefault("selo", "Escolha da curadoria")
            dados[cat].insert(0, m)

    renderizar(dados, cfg, "index.html", atualizado=atualizado)
    print(f"Site gerado: {sum(len(v) for v in dados.values())} itens em docs/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
