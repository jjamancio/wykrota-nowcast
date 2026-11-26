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
import math
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Frase principal da página para cada veredito do JSON
VEREDITO = {
    "Sim": ("A noite está boa para observar", "bom"),
    "Oportunista": ("Só um setup leve, se abrir", "meio"),
    "Não": ("Não compensa observar hoje", "ruim"),
}

TITULO = "Observatório {site}: condições do céu para observação"


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


# Níveis 1–5 para itens que não são nota
NIVEL_RISCO_CHUVA = {"baixo": 5, "moderado": 3, "alto": 1}
NIVEL_RISCO_ORVALHO = {"baixo": 5, "moderado": 3, "alto": 2, "muito alto": 1}
NIVEL_ALVO = {"Favorável": 5, "Marginal": 3, "Desfavorável": 1}


def idade_lua(dt: datetime):
    ref = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    return ((dt - ref).total_seconds() / 86400.0) % 29.530588853


def icone_lua(frac: float, crescente: bool, r=11):
    """SVG da fase da Lua como vista do hemisfério sul (crescente iluminada à esquerda)."""
    c = r + 1
    k = max(0.0, min(1.0, frac))
    rx = r * abs(1 - 2 * k)
    lado_esq = crescente  # hemisfério sul
    s1 = 0 if lado_esq else 1
    s2 = (1 if k < 0.5 else 0) if lado_esq else (0 if k < 0.5 else 1)
    caminho = (f"M{c},{c - r} A{r},{r} 0 0 {s1} {c},{c + r} A{rx:.2f},{r} 0 0 {s2} {c},{c - r} Z")
    return (f'<svg class="lua" viewBox="0 0 {2 * c} {2 * c}" width="{2 * c}" height="{2 * c}" aria-hidden="true">'
            f'<circle cx="{c}" cy="{c}" r="{r}" class="lua-sombra"/>'
            f'<path d="{caminho}" class="lua-luz"/></svg>')


def motivos(d):
    """Até três motivos curtos para o veredito, a partir do JSON do relatório."""
    T, S = int(d.get("transparencia", 1)), int(d.get("seeing", 1))
    chuva, orvalho = d.get("chuva"), d.get("orvalho")
    lua = d.get("lua_iluminada", 0)
    neg, pos = [], []
    if T <= 2:
        neg.append("Transparência baixa: nuvens ou muita umidade")
    elif T >= 4:
        pos.append("Céu transparente")
    if chuva == "alto":
        neg.append("Risco alto de chuva ou trovoada")
    elif chuva == "moderado":
        neg.append("Risco moderado de chuva")
    if S <= 2:
        neg.append("Seeing ruim: imagem tremida em alta ampliação")
    elif S >= 4:
        pos.append("Seeing bom para alta ampliação")
    if lua >= 0.6 and T >= 3:
        neg.append(f"Lua {round(100 * lua)}% iluminada atrapalha o céu profundo")
    if orvalho in ("alto", "muito alto"):
        neg.append(f"Orvalho {orvalho}: use aquecedores nas ópticas")
    lista = (pos + neg) if d.get("montar") == "Sim" else (neg + pos)
    return lista[:3]


ICONE_INFO = ('<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">'
              '<circle cx="12" cy="12" r="9.5" fill="none" stroke="currentColor" stroke-width="1.8"/>'
              '<rect x="11" y="10.5" width="2" height="6.5" rx="1" fill="currentColor"/>'
              '<circle cx="12" cy="7.6" r="1.25" fill="currentColor"/></svg>')

ICONE_OLHO = ('<svg class="olho" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">'
              '<path d="M1.5 12S5.5 4.5 12 4.5 22.5 12 22.5 12 18.5 19.5 12 19.5 1.5 12 1.5 12Z" fill="none" '
              'stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>'
              '<circle cx="12" cy="12" r="3.4" fill="currentColor"/></svg>')


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
        fonte = "radiossonda e satélite GOES-19" if "peso_sat" in det else "radiossonda (sem satélite)"
        val = d.get("validade") or {}

        def hl(chave):
            v = val.get(chave)
            return datetime.fromisoformat(v).astimezone(tz) if v else None

        vn, vs, vo = hl("nuvens"), hl("seeing"), hl("orvalho")
        selo = ""
        alerta = ""
        if vn:
            selo = (f'<span class="selo" data-ate="{e(val["nuvens"])}" data-hora="{vn:%H:%M}">'
                    f'válido até {vn:%H:%M}</span>')
            alerta = ('<p class="expirado" role="alert" hidden>Este relatório passou da validade às '
                      f'{vn:%H:%M}. Aguarde a próxima atualização antes de decidir.</p>')
        lis = "".join(f"<li>{e(m)}</li>" for m in motivos(d))

        partes.append(f"""
<section class="hero {classe}" aria-labelledby="veredito">
  <p class="meta"><span>Atualizado às {tl:%H:%M} de {tl:%d/%m}</span>{selo}</p>
  {alerta}
  <h2 id="veredito">{e(frase)}</h2>
  {f'<ul class="motivos">{lis}</ul>' if lis else ''}
  <p class="fonte">Previsão para a noite, com alvo às {alvo:%H:%M}. Fontes: {fonte}.</p>
  <a class="botao" href="relatorios/{e(ult["pdf"].name)}">Abrir o relatório completo (PDF)</a>
</section>""")

        def barra(n):
            return "".join(f'<span class="c {nivel_cor(n) if i < n else "vazio"}"></span>' for i in range(5))

        def card(rot, valor, nivel, extra="", rodape=""):
            return (f'<div class="card"><p class="rot">{rot}</p>'
                    f'<p class="valor"><span class="ponto {nivel_cor(nivel)}" aria-hidden="true"></span>{valor}</p>'
                    f'{extra}{f"<p class=rodape>{rodape}</p>" if rodape else ""}</div>')

        T, S = int(d.get("transparencia", 1)), int(d.get("seeing", 1))
        chuva, orvalho = str(d.get("chuva", "—")), str(d.get("orvalho", "—"))
        atm = "".join([
            card("Transparência", f"{T} de 5", T, f'<span class="barra" aria-hidden="true">{barra(T)}</span>',
                 f"válido até {vn:%H:%M}" if vn else ""),
            card("Seeing", f"{S} de 5", S, f'<span class="barra" aria-hidden="true">{barra(S)}</span>',
                 f"válido até {vs:%H:%M}" if vs else ""),
            card("Chuva", f"risco {e(chuva)}", NIVEL_RISCO_CHUVA.get(chuva, 3), "",
                 f"válido até {vn:%H:%M}" if vn else ""),
            card("Orvalho", f"risco {e(orvalho)}", NIVEL_RISCO_ORVALHO.get(orvalho, 3), "",
                 f"até o amanhecer ({vo:%H:%M})" if vo else ""),
        ])
        lua = float(d.get("lua_iluminada", 0))
        idade = idade_lua(datetime.fromisoformat(d["alvo"]))
        crescente = idade < 29.530588853 / 2
        fase = ("nova" if lua < 0.03 else "cheia" if lua > 0.97 else
                ("crescente" if crescente else "minguante"))
        cdp, pla = str(d.get("ceu_profundo", "—")), str(d.get("planetaria", "—"))
        alvos = "".join([
            card("Céu profundo", e(cdp), NIVEL_ALVO.get(cdp, 3), "", "galáxias, nebulosas, longa exposição"),
            card("Planetária e lunar", e(pla), NIVEL_ALVO.get(pla, 3), "", "planetas, Lua, alta ampliação"),
            (f'<div class="card"><p class="rot">Lua</p><p class="valor">{icone_lua(lua, crescente)}'
             f'{round(100 * lua)}% iluminada</p><p class="rodape">fase {fase}</p></div>'),
        ])
        partes.append(f"""
<section class="grupo" aria-labelledby="g-atm">
  <h3 id="g-atm">Condições atmosféricas</h3>
  <div class="cards c4">{atm}</div>
</section>
<section class="grupo" aria-labelledby="g-alvos">
  <h3 id="g-alvos">Alvos e céu</h3>
  <div class="cards c3">{alvos}</div>
</section>""")
    else:
        partes.append('<section class="hero meio"><h2>Nenhum relatório nos últimos dias</h2>'
                      '<p class="fonte">A página volta a mostrar a previsão assim que o próximo relatório '
                      'for gerado.</p></section>')

    # Legenda: barra sempre visível + explicação recolhível
    partes.append(f"""
<section class="legenda" aria-labelledby="g-noites">
  <h3 id="g-noites">Noites anteriores e de hoje</h3>
  <ul class="escala" aria-label="Cores: condição para observação">
    <li><span class="c n5"></span>muito favorável</li><li><span class="c n4"></span>favorável</li>
    <li><span class="c n3"></span>neutro</li><li><span class="c n2"></span>desfavorável</li>
    <li><span class="c n1"></span>muito desfavorável</li>
  </ul>
  <details id="como-ler">
    <summary>{ICONE_INFO}<span>Como ler a tabela e as cores</span></summary>
    <div class="explica">
      <p><strong>Cores (condição para observação).</strong> Quanto mais forte o tom, melhor para observar.
      Em Chuva e Orvalho, o tom de "muito favorável" indica risco baixo.</p>
      <p><strong>Siglas dentro dos blocos</strong></p>
      <ul>
        <li><strong>1 a 5:</strong> nota geral da condição</li>
        <li><strong>B / M / A / MA:</strong> risco baixo, moderado, alto ou muito alto</li>
        <li><strong>Op. (em Observar):</strong> oportunista (apenas um setup leve, se abrir)</li>
      </ul>
      <p>Cada coluna é um horário em que a previsão foi atualizada; a coluna destacada é a do horário-alvo.
      Toque em qualquer bloco para abrir o relatório completo daquele horário.</p>
    </div>
  </details>
</section>""")

    # Faixas por noite (mais recente primeiro)
    noites = {}
    for it in itens:
        noites.setdefault(noite_de(it["t"].astimezone(tz)), []).append(it)
    for nd in sorted(noites, reverse=True):
        runs = noites[nd]
        dia2 = nd + timedelta(days=1)
        # coluna do horário-alvo: a execução mais próxima do alvo (até 20 min)
        try:
            alvo_n = datetime.fromisoformat(runs[-1]["d"]["alvo"])
            dif = [abs((r["t"] - alvo_n).total_seconds()) for r in runs]
            i_alvo = dif.index(min(dif)) if min(dif) <= 20 * 60 else -1
        except Exception:  # noqa: BLE001
            i_alvo = -1

        cab = "".join(
            f'<th scope="col" class="{"alvo" if i == i_alvo else ""}">'
            f'<a href="relatorios/{e(r["pdf"].name)}">{r["t"].astimezone(tz):%H:%M}</a>'
            f'<span class="marca">{"alvo" if i == i_alvo else "&nbsp;"}</span></th>'
            for i, r in enumerate(runs))

        def linha(nome, chave, dica, mapa=None):
            cels = []
            for i, r in enumerate(runs):
                v = r["d"].get(chave)
                if mapa:
                    n, curto = mapa.get(v, (1, "?"))
                    txt = str(v)
                else:
                    n = int(v or 1)
                    curto = str(n)
                    txt = f"nota {n} de 5"
                hora = f'{r["t"].astimezone(tz):%H:%M}'
                cels.append(f'<td class="{"alvo" if i == i_alvo else ""}"><a class="b {nivel_cor(n)}" '
                            f'href="relatorios/{e(r["pdf"].name)}" title="{e(nome)}: {e(txt)} ({hora}). '
                            f'Abrir relatório" aria-label="{e(nome)}: {e(txt)}, {hora}. Abrir relatório">'
                            f'{e(curto)}</a></td>')
            return (f'<tr><th scope="row">{nome}<span class="dica">{dica}</span></th>'
                    f'{"".join(cels)}</tr>')

        partes.append(f"""
<section class="noite">
  <h4>Noite de {nd:%d/%m} para {dia2:%d/%m}</h4>
  <div class="rolagem" tabindex="0" aria-label="Tabela da noite de {nd:%d/%m}; role para o lado para ver todos os horários">
  <table>
    <thead><tr><th scope="col" class="canto"><span class="sr">Item</span></th>{cab}</tr></thead>
    <tbody>
      {linha("Transparência", "transparencia", "nota de 1 a 5")}
      {linha("Seeing", "seeing", "nota de 1 a 5")}
      {linha("Chuva", "chuva", "risco", {k: (v, k[0].upper()) for k, v in NIVEL_RISCO_CHUVA.items()})}
      {linha("Orvalho", "orvalho", "risco",
             {"baixo": (5, "B"), "moderado": (3, "M"), "alto": (2, "A"), "muito alto": (1, "MA")})}
      {linha("Observar", "montar", "veredito",
             {"Sim": (5, "Sim"), "Oportunista": (3, "Op."), "Não": (1, "Não")})}
    </tbody>
  </table></div>
</section>""")

    gerado = datetime.now(tz)
    titulo = TITULO.format(site=site)
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(titulo)}</title>
<meta name="description" content="Previsão de transparência, seeing, chuva e orvalho para o Observatório {e(site)}, a partir de radiossonda e GOES-19.">
<meta name="theme-color" content="#101B33">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap" rel="stylesheet">
<style>
:root {{
  --fundo:#101B33; --painel:#1A2747; --texto:#E6EAF2; --sec:#9AA6BF; --linha:#2C3B63; --foco:#7FB2FF;
  --n5:#0B3D91; --n4:#2F6BC2; --n3:#6E95CF; --n2:#AFC0DD; --n1:#D9DEE8; --vazio:#24335A;
  --t5:#F2F5FA; --t4:#F2F5FA; --t3:#0E1A33; --t2:#0E1A33; --t1:#0E1A33;
  --bom:#5FB98A; --meio:#E2B14A; --ruim:#E06C5F;
  --hero-bom:#123331; --hero-meio:#2F2A1C; --hero-ruim:#33202B;
  --alerta-fundo:#E06C5F; --alerta-texto:#1A0B0B;
}}
html.vermelho {{
  --fundo:#000000; --painel:#0E0000; --texto:#EF4444; --sec:#B91C1C; --linha:#3B0A0A; --foco:#F87171;
  --n5:#EF4444; --n4:#C12A2A; --n3:#991B1B; --n2:#701414; --n1:#4A0D0D; --vazio:#160303;
  --t5:#000000; --t4:#000000; --t3:#FCA5A5; --t2:#EF4444; --t1:#EF4444;
  --bom:#EF4444; --meio:#991B1B; --ruim:#7F1D1D;
  --hero-bom:#140303; --hero-meio:#100202; --hero-ruim:#0E0202;
  --alerta-fundo:#7F1D1D; --alerta-texto:#FECACA;
}}
* {{ box-sizing:border-box; }}
html {{ background:var(--fundo); color:var(--texto); }}
body {{ margin:0; font-family:"Atkinson Hyperlegible", system-ui, sans-serif; font-size:17px; line-height:1.5; }}
main, header, footer {{ max-width:760px; margin:0 auto; padding:0 20px; }}
:focus-visible {{ outline:3px solid var(--foco); outline-offset:2px; border-radius:4px; }}
.sr {{ position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }}
.nb {{ white-space:nowrap; }}

/* Cabeçalho */
header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; padding-top:26px; flex-wrap:wrap; }}
header > div {{ flex:1 1 300px; min-width:0; }}
header h1 {{ font-size:1.05rem; font-weight:700; margin:0; letter-spacing:.01em; }}
header p {{ margin:2px 0 0; color:var(--sec); font-size:.9rem; }}
button.modo {{ flex:none; display:inline-flex; align-items:center; gap:8px; font:inherit; font-size:.85rem;
  color:var(--texto); background:transparent; border:1px solid var(--linha); border-radius:999px; padding:6px 14px; cursor:pointer; }}
button.modo[aria-pressed="true"] {{ background:var(--texto); color:var(--fundo); border-color:var(--texto); }}

/* Cartão principal */
.hero {{ margin:26px 0 0; padding:24px 24px 22px; border-radius:16px; background:var(--painel);
  border:1px solid var(--linha); }}
.hero.bom {{ background:var(--hero-bom); border-color:color-mix(in srgb, var(--bom) 45%, transparent); }}
.hero.meio {{ background:var(--hero-meio); border-color:color-mix(in srgb, var(--meio) 45%, transparent); }}
.hero.ruim {{ background:var(--hero-ruim); border-color:color-mix(in srgb, var(--ruim) 45%, transparent); }}
.meta {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px 12px; margin:0 0 10px; color:var(--sec); font-size:.92rem; }}
.selo {{ display:inline-block; padding:2px 10px; border-radius:999px; border:1px solid currentColor; font-size:.82rem; color:var(--texto); }}
.selo.vencido {{ background:var(--alerta-fundo); border-color:var(--alerta-fundo); color:var(--alerta-texto); font-weight:700; }}
.expirado {{ margin:0 0 14px; padding:10px 14px; border-radius:10px; background:var(--alerta-fundo);
  color:var(--alerta-texto); font-weight:700; }}
.hero h2 {{ font-size:clamp(2rem, 6.4vw, 3rem); line-height:1.08; margin:0 0 14px; letter-spacing:-.01em; }}
.hero.vencida h2, .hero.vencida .motivos {{ opacity:.55; }}
.motivos {{ margin:0 0 16px; padding-left:1.2em; }}
.motivos li {{ margin:2px 0; }}
.fonte {{ margin:0 0 18px; color:var(--sec); font-size:.9rem; max-width:62ch; }}
.botao {{ display:inline-block; color:var(--fundo); background:var(--texto); text-decoration:none; font-weight:700;
  padding:11px 18px; border-radius:10px; }}

/* Grupos de cartões */
h3 {{ font-size:1rem; margin:30px 0 10px; }}
.cards {{ display:grid; gap:10px; }}
.cards.c4 {{ grid-template-columns:repeat(4, 1fr); }}
.cards.c3 {{ grid-template-columns:repeat(3, 1fr); }}
.card {{ background:var(--painel); border:1px solid var(--linha); border-radius:12px; padding:12px 14px; min-width:0; }}
.card .rot {{ margin:0; color:var(--sec); font-size:.85rem; }}
.card .valor {{ margin:2px 0 6px; font-weight:700; font-size:1.05rem; display:flex; align-items:center; gap:8px; }}
.card .rodape {{ margin:4px 0 0; color:var(--sec); font-size:.76rem; }}
.ponto {{ width:10px; height:10px; border-radius:50%; flex:none; }}
.barra {{ display:flex; gap:3px; }}
.barra .c {{ flex:1; height:8px; border-radius:2px; }}
.lua {{ flex:none; }} .lua-sombra {{ fill:var(--vazio); }} .lua-luz {{ fill:var(--texto); }}

/* Escala de cores */
.c {{ display:inline-block; border-radius:3px; }}
.n5 {{ background:var(--n5); }} .n4 {{ background:var(--n4); }} .n3 {{ background:var(--n3); }}
.n2 {{ background:var(--n2); }} .n1 {{ background:var(--n1); }} .vazio {{ background:var(--vazio); }}
.escala {{ list-style:none; margin:0 0 8px; padding:0; display:flex; flex-wrap:wrap; gap:6px 16px; color:var(--sec); font-size:.88rem; }}
.escala li {{ display:inline-flex; align-items:center; gap:7px; white-space:nowrap; }}
.escala .c {{ width:20px; height:14px; }}
details {{ color:var(--sec); font-size:.9rem; }}
summary {{ display:inline-flex; align-items:center; gap:8px; cursor:pointer; color:var(--texto); padding:4px 0; list-style:none; }}
summary::-webkit-details-marker {{ display:none; }}
summary::after {{ content:"+"; color:var(--sec); }}
details[open] summary::after {{ content:"−"; }}
.explica {{ max-width:64ch; padding:4px 0 2px 26px; }}
.explica p {{ margin:6px 0; }} .explica ul {{ margin:4px 0 8px; padding-left:1.1em; }}

/* Tabelas das noites */
h4 {{ font-size:.98rem; margin:26px 0 8px; }}
.rolagem {{ overflow-x:auto;
  background: linear-gradient(to right, var(--fundo) 30%, transparent) left / 40px 100% no-repeat local,
              linear-gradient(to left, var(--fundo) 30%, transparent) right / 40px 100% no-repeat local,
              linear-gradient(to right, rgba(0,0,0,.45), transparent) left / 14px 100% no-repeat scroll,
              linear-gradient(to left, rgba(0,0,0,.45), transparent) right / 14px 100% no-repeat scroll; }}
table {{ border-collapse:separate; border-spacing:4px 4px; font-size:.82rem; }}
th {{ font-weight:400; color:var(--sec); text-align:left; white-space:nowrap; }}
tbody th, thead th.canto {{ position:sticky; left:0; z-index:1; background:var(--fundo); padding-right:12px; }}
thead th {{ text-align:center; padding:0 2px 2px; vertical-align:top; min-width:42px; }}
thead a {{ color:var(--sec); text-decoration:none; }}
thead th.alvo a {{ color:var(--texto); font-weight:700; }}
.marca {{ display:block; font-size:.66rem; line-height:1.1; color:var(--texto); }}
td {{ padding:0; }}
td.alvo a.b {{ box-shadow:0 0 0 2px var(--texto); }}
a.b {{ display:flex; align-items:center; justify-content:center; width:42px; height:28px; border-radius:4px;
  text-decoration:none; font-weight:700; font-size:.78rem; cursor:pointer; transition:transform .12s ease; }}
a.b:hover {{ transform:translateY(-1px); box-shadow:0 0 0 2px var(--foco); }}
a.b.n5 {{ color:var(--t5); }} a.b.n4 {{ color:var(--t4); }} a.b.n3 {{ color:var(--t3); }}
a.b.n2 {{ color:var(--t2); }} a.b.n1 {{ color:var(--t1); }}
th .dica {{ display:block; font-size:.72rem; color:var(--sec); opacity:.85; }}

footer {{ color:var(--sec); font-size:.85rem; padding-top:40px; padding-bottom:40px; }}
footer p {{ max-width:64ch; }}
footer a {{ color:var(--texto); }}

@media (max-width:640px) {{
  .cards.c4 {{ grid-template-columns:repeat(2, 1fr); }}
  .cards.c3 {{ grid-template-columns:repeat(2, 1fr); }}
  .cards.c3 .card:last-child {{ grid-column:1 / -1; }}
  .hero {{ padding:20px 18px; }}
  table {{ border-spacing:3px; font-size:.74rem; }}
  a.b {{ width:34px; height:26px; font-size:.7rem; }}
  thead th {{ min-width:34px; }}
  th .dica {{ font-size:.64rem; }}
}}
@media (prefers-reduced-motion:reduce) {{ a.b {{ transition:none; }} a.b:hover {{ transform:none; }} }}
</style>
<script>
try {{ if (localStorage.getItem("modoVermelho") === "1") document.documentElement.classList.add("vermelho"); }} catch (e) {{}}
</script>
</head>
<body>
<header>
  <div>
    <h1>{e(titulo)}</h1>
    <p>Previsão automática a partir da radiossonda de Confins e do satélite <span class="nb">GOES-19</span></p>
  </div>
  <button class="modo" type="button" aria-pressed="false" id="modo"
          title="Cores vermelhas para preservar a visão no escuro">{ICONE_OLHO}<span>Modo noturno</span></button>
</header>
<main>
{''.join(partes)}
</main>
<footer>
  <p>As notas são estimativas automáticas feitas por regras heurísticas, ainda em fase experimental.
  Antes de subir a serra, confira também o céu no satélite e as condições locais.</p>
  <p>Relatórios dos últimos {dias} dias. Página gerada às {gerado:%H:%M} de {gerado:%d/%m/%Y}.
  {f'Código-fonte e documentação: <a href="{e(url_repo)}">{e(url_repo)}</a> (GPL-3.0).' if url_repo else ''}</p>
</footer>
<script>
(function () {{
  var h = document.documentElement, b = document.getElementById("modo");
  function sync() {{ var on = h.classList.contains("vermelho"); b.setAttribute("aria-pressed", on ? "true" : "false");
    b.title = on ? "Voltar às cores normais" : "Cores vermelhas para preservar a visão no escuro"; }}
  b.addEventListener("click", function () {{ h.classList.toggle("vermelho");
    try {{ localStorage.setItem("modoVermelho", h.classList.contains("vermelho") ? "1" : "0"); }} catch (e) {{}}
    sync(); }});
  sync();

  // Validade: selo vira alerta quando o relatório vence
  var selo = document.querySelector(".selo"), alerta = document.querySelector(".expirado"),
      hero = document.querySelector(".hero");
  if (selo && new Date(selo.getAttribute("data-ate")) < new Date()) {{
    selo.classList.add("vencido"); selo.textContent = "vencido às " + selo.getAttribute("data-hora");
    if (alerta) alerta.hidden = false;
    if (hero) hero.classList.add("vencida");
  }}

  // "Como ler": aberto só na primeira visita
  var det = document.getElementById("como-ler");
  if (det) {{
    try {{
      if (!localStorage.getItem("comoLerVisto")) {{ det.open = true; localStorage.setItem("comoLerVisto", "1"); }}
    }} catch (e) {{}}
  }}
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
