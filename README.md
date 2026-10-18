# Curadoria do Daniel

Landing page de promoções do Mercado Livre (áudio, transmissão, notebooks, material para eventos e igrejas), com links de afiliado e atualização diária.

## Como funciona

1. `gerar_site.py` busca ofertas nas categorias do `config.json`.
2. Só entram itens **novos**, com **desconto real** (preço original maior que o atual), de **loja oficial** ou vendedor **MercadoLíder Gold/Platinum**, e **bem avaliados** (nota mínima 4,3 com pelo menos 10 avaliações; ajustável no `config.json`).
   A ordem em cada categoria é: **1) qualidade do produto** (faixas de nota: 4,7+, 4,5+, 4,3+), **2) reputação da loja** (oficial, depois Platinum, depois Gold) e **3) menor preço**. Produtos na mesma faixa de nota e com loja do mesmo nível são ordenados do mais barato para o mais caro.
3. Cada item vira um card em `docs/index.html`, com seu link de afiliado.
4. O GitHub Actions roda todo dia às 06:00 e publica a página (GitHub Pages).
5. Se a busca falhar num dia, o site mantém os últimos dados salvos (`docs/dados.json`), nunca fica vazio.

## Configuração (uma vez só)

1. **Afiliado**: no painel de afiliados do Mercado Livre, gere um link de um produto e veja os parâmetros `matt_tool` e `matt_word` na URL. Guarde esses dois valores.
2. **App de desenvolvedor**: em developers.mercadolivre.com.br crie um aplicativo e pegue o `Client ID` e o `Client Secret`.
3. **GitHub**: crie um repositório, envie esta pasta e em *Settings → Secrets and variables → Actions* cadastre:
   `ML_CLIENT_ID`, `ML_CLIENT_SECRET`, `ML_MATT_TOOL`, `ML_MATT_WORD`.
4. Em *Settings → Pages*, escolha *Deploy from a branch*, branch `main`, pasta `/docs`.
5. Em *Actions → Atualizar promoções → Run workflow* para gerar a primeira versão.

## Ajustes do dia a dia

- `config.json`: categorias, termos de busca, desconto mínimo, níveis de vendedor aceitos, nota mínima (`avaliacao_minima`) e número mínimo de avaliações (`avaliacoes_minimas`).
- `produtos_manuais.json`: seus destaques fixos (aparecem no topo da categoria). Exemplo:

```json
[{"categoria": "Microfones", "titulo": "Nome do produto", "preco": 399.9, "preco_original": 499.9,
  "link": "https://produto.mercadolivre.com.br/MLB-...", "imagem": "https://..."}]
```

## Pontos de atenção

- **Erro 403**: a API de busca do Mercado Livre às vezes bloqueia IPs de servidores (inclusive do GitHub Actions). Se o log mostrar `HTTP 403`, rode o script num computador seu (Agendador de Tarefas no Windows ou `cron` no Linux/Mac) e envie a pasta `docs` ao repositório, ou use um proxy residencial.
- **Formato do link**: o script monta `link-do-produto?matt_tool=...&matt_word=...`. Gere um link no painel e confira que o seu tem esses mesmos parâmetros. Se o seu formato for diferente, ajuste a função `link_afiliado`.
- **Regras do programa**: o Mercado Livre só permite link de afiliado em páginas de produto (não em buscas, ofertas do dia ou categorias) e analisa a qualidade dos canais. Não compre pelos seus próprios links, pois a comissão não é paga.
- Teste o link em aba anônima antes de divulgar.
- Ver o visual sem API: `python gerar_site.py --demo` gera `docs/preview.html` com itens fictícios.
