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
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
        return None
    corpo = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}
    ).encode()
    try:
        return http_json(
            f"{API}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data=corpo,
        )["access_token"]
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
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        d = http_json(f"{API}/reviews/item/{item_id}", headers=headers)
        return d.get("rating_average"), (d.get("paging") or {}).get("total", 0) or 0
    except Exception:  # noqa: BLE001
        return None, 0


def faixa_qualidade(nota):
    """Faixas de nota: 4,7+ = 3, 4,5+ = 2, 4,3+ = 1. Notas próximas ficam empatadas e o preço decide."""
    if nota is None:
        return 0
    return 3 if nota >= 4.7 else 2 if nota >= 4.5 else 1 if nota >= 4.3 else 0


def chave_ordem(p):
    """Ordem pedida: 1) qualidade do produto, 2) reputação da loja, 3) menor preço."""
    return (-faixa_qualidade(p.get("nota")), -p.get("loja_nivel", 0), p["preco"])


def coletar(cfg):
    filtros = cfg["filtros"]
    token = obter_token()
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
    if p.get("frete_gratis"):
        tags.append('<span class="tag">Frete grátis</span>')
    link = link_afiliado(p["link"], cfg)
    return f"""<article class="card">
  <div class="img"><span class="off">-{p["desconto"]}%</span><img src="{e(p["imagem"], quote=True)}" alt="{e(p["titulo"], quote=True)}" loading="lazy"></div>
  <div class="body">
    <div class="title">{e(p["titulo"])}</div>
    <div class="old">{brl(p["preco_original"])}</div>
    <div class="price">{brl(p["preco"])}</div>
    <div class="tags">{"".join(tags)}</div>
    <a class="btn" href="{e(link, quote=True)}" target="_blank" rel="sponsored nofollow noopener">Ver no Mercado Livre</a>
  </div>
</article>"""


def renderizar(dados, cfg, saida, aviso_demo=False):
    e = html.escape
    abas, secoes = [], []
    for nome, itens in dados.items():
        slug = "".join(c.lower() if c.isalnum() else "-" for c in nome).strip("-")
        abas.append(f'  <button class="tab" data-cat="{slug}" aria-pressed="false">{e(nome)}</button>')
        corpo = "\n".join(card(p, cfg) for p in itens) if itens else '<p class="empty">Sem ofertas qualificadas hoje nesta categoria. Volte amanhã!</p>'
        secoes.append(f'<section data-cat="{slug}">\n<h2>{e(nome)}</h2>\n<div class="grid">\n{corpo}\n</div>\n</section>')
    agora = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y às %H:%M")
    modelo = (BASE / "template.html").read_text(encoding="utf-8")
    sub = cfg["subtitulo"]
    if aviso_demo:
        sub = "PRÉVIA COM ITENS DE EXEMPLO — preços e produtos fictícios, só para ver o visual."
    pagina = (
        modelo.replace("{{TITULO}}", e(cfg["titulo"]))
        .replace("{{DESCRICAO}}", e(cfg["subtitulo"], quote=True))
        .replace("{{SUBTITULO}}", e(sub))
        .replace("{{ATUALIZADO}}", agora)
        .replace("{{ABAS}}", "\n".join(abas))
        .replace("{{SECOES}}", "\n".join(secoes))
    )
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
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
    dados, tudo_falhou = coletar(cfg)
    total = sum(len(v) for v in dados.values())

    if tudo_falhou or total == 0:
        cache = carregar(cache_path, None)
        if cache:
            print("[aviso] coleta sem resultados; mantendo os últimos dados salvos.", file=sys.stderr)
            dados = cache
        else:
            print("[aviso] coleta sem resultados e sem cache ainda.", file=sys.stderr)
            print("[erro] nada para publicar; o index.html NÃO foi alterado.", file=sys.stderr)
            return 1
    else:
        DOCS.mkdir(exist_ok=True)
        cache_path.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")

    # Destaques manuais (sua curadoria) entram sempre no topo da categoria indicada.
    for m in carregar(BASE / "produtos_manuais.json", []):
        cat = m.get("categoria")
        if cat in dados and m.get("link") and m.get("preco"):
            m.setdefault("preco_original", m["preco"])
            m.setdefault("desconto", max(0, round((1 - m["preco"] / m["preco_original"]) * 100)))
            m.setdefault("imagem", "")
            m.setdefault("selo", "Escolha da curadoria")
            dados[cat].insert(0, m)

    renderizar(dados, cfg, "index.html")
    print(f"Site gerado: {sum(len(v) for v in dados.values())} itens em docs/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
