#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 João — projeto wykrota-nowcast
"""
gerar_site.py — Gera a página estática (GitHub Pages) com os relatórios em PDF.

Lê os pares <SITE>_<AAAAMMDD>T<HHMM>Z.json/.pdf gerados pelo previsao.sh, mantém os
últimos N dias e escreve:
    <saida>/index.html          página com o último veredito e as faixas por noite
    <saida>/relatorios/*.pdf    os relatórios
    <saida>/relatorios/ultimo.pdf
    <saida>/.nojekyll

Uso:
    python3 gerar_site.py --entrada ~/astro_previsoes --saida _site --site Wykrota --dias 7
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

VEREDITO = {
    "Sim": ("Vale montar o equipamento", "bom"),
    "Oportunista": ("Só um setup leve, se abrir", "meio"),
    "Não": ("Não compensa montar hoje", "ruim"),
}


def carregar(entrada: Path, site: str, dias: int, agora: datetime):
    pat = re.compile(rf"^{re.escape(site)}_(\d{{8}}T\d{{4}})Z\.json$")
    itens = []
    for j in entrada.iterdir():
        m = pat.match(j.name)
        if not m:
            continue
        pdf = j.with_suffix(".pdf")
        if not pdf.exists():
            continue
        t = datetime.strptime(m.group(1), "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
        if agora - t > timedelta(days=dias):
            continue
        try:
            d = json.loads(j.read_text())
        except Exception:  # noqa: BLE001
            continue
        itens.append(dict(t=t, pdf=pdf, d=d))
    return sorted(itens, key=lambda x: x["t"])


def noite_de(t_local: datetime):
    """Noite astronômica: execuções entre 12h e 12h do dia seguinte pertencem à mesma noite."""
    return (t_local - timedelta(hours=12)).date()


def nivel_cor(n):
    return f"n{max(1, min(5, int(n)))}"


def montar_html(itens, site, tz, url_repo, dias):
    e = html.escape
    ult = itens[-1] if itens else None
    partes = []
    if ult:
        d = ult["d"]
        tl = ult["t"].astimezone(tz)
        alvo = datetime.fromisoformat(d["alvo"]).astimezone(tz)
        frase, classe = VEREDITO.get(d.get("montar"), ("Sem veredito", "meio"))
        det = d.get("transparencia_detalhe") or {}
        fonte = "radiossonda e satélite GOES-19" if "peso_sat" in det else "radiossonda"

        def barra(n, nome):
            cel = "".join(f'<span class="c {nivel_cor(n) if i < n else "vazio"}"></span>' for i in range(5))
            return (f'<div class="nota"><span class="rot">{nome}</span>'
                    f'<span class="barra" aria-hidden="true">{cel}</span>'
                    f'<span class="val">{n} de 5</span></div>')

        partes.append(f"""
<section class="agora {classe}" aria-labelledby="veredito">
  <p class="quando">Atualizado às {tl:%H:%M} de {tl:%d/%m}, para a noite com alvo às {alvo:%H:%M}.
  Fontes: {fonte}.</p>
  <h2 id="veredito">{e(frase)}</h2>
  <div class="notas">
    {barra(d.get("transparencia", 1), "Transparência")}
    {barra(d.get("seeing", 1), "Seeing")}
  </div>
  <dl class="extras">
    <div><dt>Chuva</dt><dd>{e(str(d.get("chuva", "—")))}</dd></div>
    <div><dt>Orvalho</dt><dd>{e(str(d.get("orvalho", "—")))}</dd></div>
    <div><dt>Céu profundo</dt><dd>{e(str(d.get("ceu_profundo", "—")))}</dd></div>
    <div><dt>Planetária</dt><dd>{e(str(d.get("planetaria", "—")))}</dd></div>
    <div><dt>Lua</dt><dd>{round(100 * d.get("lua_iluminada", 0))}% iluminada</dd></div>
  </dl>
  <a class="botao" href="relatorios/{e(ult["pdf"].name)}">Abrir o relatório completo (PDF)</a>
</section>""")
    else:
        partes.append('<section class="agora"><h2>Nenhum relatório nos últimos dias</h2>'
                      '<p>A página volta a mostrar a previsão assim que o próximo relatório for gerado.</p></section>')

    # Faixas por noite (mais recente primeiro)
    noites = {}
    for it in itens:
        noites.setdefault(noite_de(it["t"].astimezone(tz)), []).append(it)
    blocos = []
    for nd in sorted(noites, reverse=True):
        runs = noites[nd]
        dia2 = nd + timedelta(days=1)
        cab = "".join(f'<th scope="col"><a href="relatorios/{e(r["pdf"].name)}">'
                      f'{r["t"].astimezone(tz):%H:%M}</a></th>' for r in runs)

        def linha(nome, chave, mapa=None):
            cels = []
            for r in runs:
                v = r["d"].get(chave)
                if mapa:
                    n = mapa.get(v, 1)
                    txt = str(v)
                else:
                    n = int(v or 1)
                    txt = f"{n} de 5"
                cels.append(f'<td><a class="b {nivel_cor(n)}" href="relatorios/{e(r["pdf"].name)}" '
                            f'title="{e(nome)} {e(txt)} — {r["t"].astimezone(tz):%H:%M}">'
                            f'<span class="sr">{e(txt)}</span></a></td>')
            return f'<tr><th scope="row">{nome}</th>{"".join(cels)}</tr>'

        blocos.append(f"""
<section class="noite">
  <h3>Noite de {nd:%d/%m} para {dia2:%d/%m}</h3>
  <div class="rolagem"><table>
    <thead><tr><th scope="col"><span class="sr">Item</span></th>{cab}</tr></thead>
    <tbody>
      {linha("Transparência", "transparencia")}
      {linha("Seeing", "seeing")}
      {linha("Chuva", "chuva", {"baixo": 5, "moderado": 3, "alto": 1})}
      {linha("Orvalho", "orvalho", {"baixo": 5, "moderado": 3, "alto": 2, "muito alto": 1})}
      {linha("Montar", "montar", {"Sim": 5, "Oportunista": 3, "Não": 1})}
    </tbody>
  </table></div>
</section>""")

    gerado = datetime.now(tz)
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(site)}: previsão do céu para observação</title>
<meta name="description" content="Previsão de transparência, seeing, chuva e orvalho para o Observatório {e(site)}, a partir de radiossonda e GOES-19.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap" rel="stylesheet">
<style>
:root {{
  --fundo:#101B33; --painel:#1A2747; --texto:#E6EAF2; --sec:#9AA6BF; --linha:#2C3B63; --foco:#7FB2FF;
  --n5:#0B3D91; --n4:#2F6BC2; --n3:#6E95CF; --n2:#AFC0DD; --n1:#D9DEE8; --vazio:#24335A;
  --bom:#5FB98A; --meio:#E2B14A; --ruim:#E06C5F;
}}
html.vermelho {{
  --fundo:#0A0000; --painel:#160202; --texto:#FF5A4F; --sec:#B8322A; --linha:#3A0A08; --foco:#FF8A80;
  --n5:#FF3B30; --n4:#C92D24; --n3:#8F1F19; --n2:#5A130F; --n1:#2E0907; --vazio:#1F0605;
  --bom:#FF3B30; --meio:#C92D24; --ruim:#8F1F19;
}}
* {{ box-sizing:border-box; }}
html {{ background:var(--fundo); color:var(--texto); }}
body {{ margin:0; font-family:"Atkinson Hyperlegible", system-ui, sans-serif; font-size:17px; line-height:1.5; }}
main, header, footer {{ max-width:720px; margin:0 auto; padding:0 20px; }}
header {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; padding-top:28px; flex-wrap:wrap; }}
header h1 {{ font-size:1.05rem; font-weight:700; margin:0; letter-spacing:.01em; }}
header p {{ margin:0; color:var(--sec); font-size:.9rem; }}
button.modo {{ font:inherit; font-size:.85rem; color:var(--texto); background:transparent; border:1px solid var(--linha);
  border-radius:999px; padding:6px 14px; cursor:pointer; }}
:focus-visible {{ outline:3px solid var(--foco); outline-offset:2px; }}
.agora {{ margin:28px 0 8px; padding:26px 24px 24px; background:var(--painel); border-radius:14px; border-left:6px solid var(--meio); }}
.agora.bom {{ border-left-color:var(--bom); }} .agora.ruim {{ border-left-color:var(--ruim); }}
.quando {{ margin:0 0 6px; color:var(--sec); font-size:.92rem; max-width:60ch; }}
.agora h2 {{ font-size:clamp(2rem, 6.5vw, 3.1rem); line-height:1.08; margin:0 0 22px; letter-spacing:-.01em; }}
.notas {{ display:grid; gap:10px; margin-bottom:18px; }}
.nota {{ display:grid; grid-template-columns:8.5em auto 1fr; align-items:center; gap:12px; }}
.rot {{ color:var(--sec); }}
.barra {{ display:inline-flex; gap:4px; }}
.c {{ width:26px; height:18px; border-radius:3px; display:inline-block; }}
.val {{ font-weight:700; }}
.extras {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:10px 18px; margin:0 0 22px; }}
.extras dt {{ color:var(--sec); font-size:.85rem; }} .extras dd {{ margin:0; font-weight:700; }}
.botao {{ display:inline-block; color:var(--fundo); background:var(--texto); text-decoration:none; font-weight:700;
  padding:11px 18px; border-radius:10px; }}
.n5 {{ background:var(--n5); }} .n4 {{ background:var(--n4); }} .n3 {{ background:var(--n3); }}
.n2 {{ background:var(--n2); }} .n1 {{ background:var(--n1); }} .vazio {{ background:var(--vazio); }}
h3 {{ font-size:1rem; margin:34px 0 10px; }}
.legenda {{ color:var(--sec); font-size:.9rem; margin:30px 0 0; }}
.legenda ul {{ list-style:none; margin:0 0 6px; padding:0; display:flex; flex-wrap:wrap; gap:6px 16px; }}
.legenda li {{ display:inline-flex; align-items:center; gap:7px; white-space:nowrap; }}
.legenda p {{ margin:0; }}
.legenda .c {{ width:20px; height:14px; }}
.rolagem {{ overflow-x:auto; }}
table {{ border-collapse:separate; border-spacing:3px; font-size:.85rem; }}
th {{ font-weight:400; color:var(--sec); text-align:left; padding-right:10px; white-space:nowrap; }}
thead th {{ text-align:center; padding:0 0 4px; }}
thead a {{ color:var(--sec); text-decoration:none; }}
td {{ padding:0; }}
a.b {{ display:block; width:34px; height:24px; border-radius:3px; }}
.sr {{ position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }}
footer {{ color:var(--sec); font-size:.85rem; padding-top:40px; padding-bottom:40px; }}
footer p {{ max-width:64ch; }}
footer a {{ color:var(--texto); }}
@media (max-width:480px) {{ .nota {{ grid-template-columns:1fr auto; }} .nota .val {{ grid-column:2; }}
  .nota .barra {{ grid-column:1 / -1; grid-row:2; }}
  table {{ border-spacing:2px; font-size:.72rem; }} a.b {{ width:27px; height:22px; }}
  th {{ padding-right:4px; }} }}
</style>
<script>
try {{ if (localStorage.getItem("modoVermelho") === "1") document.documentElement.classList.add("vermelho"); }} catch (e) {{}}
</script>
</head>
<body>
<header>
  <div>
    <h1>Observatório {e(site)}: céu para observação</h1>
    <p>Previsão automática a partir da radiossonda de Confins e do satélite GOES-19</p>
  </div>
  <button class="modo" type="button" aria-pressed="false" id="modo">Luz vermelha</button>
</header>
<main>
{''.join(partes)}
<div class="legenda">
  <ul aria-label="Escala dos blocos">
    <li><span class="c n5"></span>excelente</li><li><span class="c n4"></span>boa</li>
    <li><span class="c n3"></span>regular</li><li><span class="c n2"></span>ruim</li><li><span class="c n1"></span>péssima</li>
  </ul>
  <p>Cada coluna é um horário da noite. Toque num bloco para abrir o relatório daquele horário.</p>
</div>
{''.join(blocos)}
</main>
<footer>
  <p>As notas são estimativas automáticas feitas por regras heurísticas, ainda em fase experimental.
  Antes de subir a serra, confira também o céu no satélite e as condições locais.</p>
  <p>Relatórios dos últimos {dias} dias. Página gerada às {gerado:%H:%M} de {gerado:%d/%m/%Y}.
  {f'Código-fonte e documentação: <a href="{e(url_repo)}">{e(url_repo)}</a> (GPL-3.0).' if url_repo else ''}</p>
</footer>
<script>
(function () {{
  var b = document.getElementById("modo"), h = document.documentElement;
  function sync() {{ var on = h.classList.contains("vermelho"); b.setAttribute("aria-pressed", on ? "true" : "false");
    b.textContent = on ? "Cores normais" : "Luz vermelha"; }}
  b.addEventListener("click", function () {{ h.classList.toggle("vermelho");
    try {{ localStorage.setItem("modoVermelho", h.classList.contains("vermelho") ? "1" : "0"); }} catch (e) {{}}
    sync(); }});
  sync();
}})();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Gera a GitHub Page com os relatórios")
    ap.add_argument("--entrada", type=Path, required=True)
    ap.add_argument("--saida", type=Path, required=True)
    ap.add_argument("--site", default="Wykrota")
    ap.add_argument("--dias", type=int, default=7)
    ap.add_argument("--tz", default="America/Sao_Paulo")
    ap.add_argument("--url-repo", default="")
    args = ap.parse_args()

    tz = ZoneInfo(args.tz)
    agora = datetime.now(timezone.utc)
    itens = carregar(args.entrada, args.site, args.dias, agora)
    if args.saida.exists():
        shutil.rmtree(args.saida)
    (args.saida / "relatorios").mkdir(parents=True)
    for it in itens:
        shutil.copy2(it["pdf"], args.saida / "relatorios" / it["pdf"].name)
    if itens:
        shutil.copy2(itens[-1]["pdf"], args.saida / "relatorios" / "ultimo.pdf")
    (args.saida / "index.html").write_text(montar_html(itens, args.site, tz, args.url_repo, args.dias),
                                           encoding="utf-8")
    (args.saida / ".nojekyll").write_text("")
    print(f"Página gerada em {args.saida} com {len(itens)} relatório(s).")


if __name__ == "__main__":
    main()
