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


def _svg(corpo, w=18, h=18, classe=""):
    return (f'<svg class="{classe}" viewBox="0 0 24 24" width="{w}" height="{h}" aria-hidden="true" '
            f'focusable="false" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
            f'stroke-linejoin="round">{corpo}</svg>')


ICONE_INFO = _svg('<circle cx="12" cy="12" r="9.5"/><path d="M12 11v6"/><circle cx="12" cy="7.6" r=".6" '
                  'fill="currentColor"/>')
ICONE_OLHO = _svg('<path d="M1.5 12S5.5 4.5 12 4.5 22.5 12 22.5 12 18.5 19.5 12 19.5 1.5 12 1.5 12Z"/>'
                  '<circle cx="12" cy="12" r="3.2" fill="currentColor" stroke="none"/>', classe="olho")
ICONE_ALERTA = _svg('<path d="M12 3 2.5 20h19L12 3Z"/><path d="M12 10v4.5"/><circle cx="12" cy="17.2" r=".6" '
                    'fill="currentColor"/>', 20, 20)
# Telescópio em tripé (setup completo), luneta pequena (setup leve), telescópio riscado (guardado)
ICONE_SETUP = {
    "Sim": _svg('<path d="m3.5 11.5 11-5 1.8 3.8-11 5Z"/><path d="m14.5 6.5 3.2-1.5 1.8 3.8-3.2 1.5"/>'
                '<path d="M10 14.5 6.5 21M10 14.5l3.5 6.5M10 14.5V21"/>', 40, 40),
    "Oportunista": _svg('<path d="m5 12.5 8-3.6 1.3 2.8-8 3.6Z"/><path d="M9.5 14.5 8 19M9.5 14.5 11.5 19"/>'
                        '<path d="M17 7.5h3M18.5 6v3" opacity=".7"/>', 40, 40),
    "Não": _svg('<path d="m3.5 11.5 11-5 1.8 3.8-11 5Z" opacity=".55"/><path d="M10 14.5 6.5 21M10 14.5l3.5 6.5" '
                'opacity=".55"/><path d="M3 3l18 18"/>', 40, 40),
}
EQUIPAMENTO = {
    "Sim": ("Setup completo", "Monte o equipamento principal: há janela útil para ao menos uma modalidade."),
    "Oportunista": ("Setup leve e rápido", "Algo fácil de montar e desmontar; confira o satélite antes de sair."),
    "Não": ("Equipamento guardado", "Não vale levar o equipamento para a serra nesta noite."),
}


def montar_html(itens, site, tz, url_repo, dias, tz_nome="horário de Brasília (UTC−3)"):
    e = html.escape
    ult = itens[-1] if itens else None
    partes = []
    banner = ""

    if ult:
        d = ult["d"]
        tl = ult["t"].astimezone(tz)
        alvo = datetime.fromisoformat(d["alvo"]).astimezone(tz)
        chave = d.get("montar") if d.get("montar") in VEREDITO else "Oportunista"
        frase, classe = VEREDITO[chave]
        det = d.get("transparencia_detalhe") or {}
        fonte = ('radiossonda e satélite <span class="nb">GOES-19</span>' if "peso_sat" in det
                 else "radiossonda (sem satélite)")
        val = d.get("validade") or {}

        def hl(k):
            v = val.get(k)
            return datetime.fromisoformat(v).astimezone(tz) if v else None

        vn, vs, vo = hl("nuvens"), hl("seeing"), hl("orvalho")
        selo = ""
        if vn:
            selo = (f'<span class="selo mono" data-ate="{e(val["nuvens"])}" data-hora="{vn:%H:%M}">'
                    f'válido até {vn:%H:%M}</span>')
            banner = (f'<div class="banner-expirado" role="alert" hidden>{ICONE_ALERTA}<p>Este relatório passou '
                      f'da validade às <span class="mono">{vn:%H:%M}</span>. Aguarde a próxima atualização antes '
                      'de decidir.</p></div>')
        lis = "".join(f"<li>{e(m)}</li>" for m in motivos(d))
        eq_nome, eq_txt = EQUIPAMENTO[chave]
        cdp, pla = str(d.get("ceu_profundo", "—")), str(d.get("planetaria", "—"))

        def chip(nome, status):
            return (f'<li><span class="led {nivel_cor(NIVEL_ALVO.get(status, 3))}" aria-hidden="true"></span>'
                    f'<span class="chip-nome">{nome}</span><span class="chip-val">{e(status)}</span></li>')

        partes.append(f"""
<section class="decisao {classe}" aria-labelledby="veredito">
  <div class="decisao-principal">
    <p class="meta"><span>Atualizado às <span class="mono">{tl:%H:%M}</span> de <span class="mono">{tl:%d/%m}</span>
      ({e(tz_nome.split(" (")[0])})</span>{selo}</p>
    <p class="status"><span class="led-status" aria-hidden="true"></span><span id="veredito-rot">Veredito</span></p>
    <h2 id="veredito">{e(frase)}</h2>
    {f'<ul class="motivos">{lis}</ul>' if lis else ''}
    <p class="fonte">Previsão para a noite, com alvo às <span class="mono">{alvo:%H:%M}</span>. Fontes: {fonte}.</p>
    <a class="botao" href="relatorios/{e(ult["pdf"].name)}">Abrir o relatório completo (PDF)</a>
  </div>
  <aside class="equipamento" aria-label="Equipamento recomendado">
    <p class="eq-rot">Equipamento recomendado</p>
    <div class="eq-icone">{ICONE_SETUP[chave]}</div>
    <p class="eq-nome">{eq_nome}</p>
    <p class="eq-txt">{eq_txt}</p>
    <ul class="chips">{chip("Céu profundo", cdp)}{chip("Planetária e lunar", pla)}</ul>
  </aside>
</section>""")

        def medidor(n):
            seg = "".join(f'<span class="{nivel_cor(n) if i < n else "vazio"}"></span>' for i in range(5))
            return f'<span class="medidor" role="img" aria-label="{n} de 5">{seg}</span>'

        def card(rot, valor_html, rodape="", extra_cls=""):
            return (f'<div class="card {extra_cls}"><p class="rot">{rot}</p>{valor_html}'
                    f'{f"<p class=rodape>{rodape}</p>" if rodape else ""}</div>')

        def nota(n):
            return (f'<div class="valor-linha"><p class="valor mono">{n}<span class="de"> de 5</span></p>'
                    f'{medidor(n)}</div>')

        def risco(txt, nivel):
            return (f'<div class="valor-linha"><p class="valor"><span class="led {nivel_cor(nivel)}" '
                    f'aria-hidden="true"></span>risco {e(txt)}</p></div>')

        T, S = int(d.get("transparencia", 1)), int(d.get("seeing", 1))
        chuva, orvalho = str(d.get("chuva", "—")), str(d.get("orvalho", "—"))
        ate = lambda x: f'válido até <span class="mono">{x:%H:%M}</span>' if x else ""  # noqa: E731
        atm = "".join([
            card("Transparência", nota(T), ate(vn)),
            card("Seeing", nota(S), ate(vs)),
            card("Chuva", risco(chuva, NIVEL_RISCO_CHUVA.get(chuva, 3)), ate(vn)),
            card("Orvalho", risco(orvalho, NIVEL_RISCO_ORVALHO.get(orvalho, 3)),
                 f'até o amanhecer (<span class="mono">{vo:%H:%M}</span>)' if vo else ""),
        ])
        lua = float(d.get("lua_iluminada", 0))
        crescente = idade_lua(datetime.fromisoformat(d["alvo"])) < 29.530588853 / 2
        fase = ("nova" if lua < 0.03 else "cheia" if lua > 0.97 else ("crescente" if crescente else "minguante"))

        def alvo_val(status):
            return (f'<div class="valor-linha"><p class="valor"><span class="led {nivel_cor(NIVEL_ALVO.get(status, 3))}" '
                    f'aria-hidden="true"></span>{e(status)}</p></div>')

        alvos = "".join([
            card("Céu profundo", alvo_val(cdp), "galáxias, nebulosas, longa exposição"),
            card("Planetária e lunar", alvo_val(pla), "planetas, Lua, alta ampliação"),
            card("Lua", f'<div class="valor-linha">{icone_lua(lua, crescente)}<p class="valor">'
                        f'<span class="mono">{round(100 * lua)}%</span> iluminada</p></div>', f"fase {fase}", "card-lua"),
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
        partes.append('<section class="decisao meio"><div class="decisao-principal"><h2>Nenhum relatório nos '
                      'últimos dias</h2><p class="fonte">A página volta a mostrar a previsão assim que o próximo '
                      'relatório for gerado.</p></div></section>')

    # Legenda: barra sempre visível + explicação recolhida
    partes.append(f"""
<section class="legenda" aria-labelledby="g-noites">
  <h3 id="g-noites">Noites anteriores e de hoje</h3>
  <ul class="escala" aria-label="Cores: condição para observação">
    <li><span class="sw n5"></span>muito favorável</li><li><span class="sw n4"></span>favorável</li>
    <li><span class="sw n3"></span>neutro</li><li><span class="sw n2"></span>desfavorável</li>
    <li><span class="sw n1"></span>muito desfavorável</li>
  </ul>
  <details id="como-ler">
    <summary>{ICONE_INFO}<span>Como ler a tabela e as cores</span></summary>
    <div class="explica">
      <p><strong>Cores (condição para observação).</strong> Quanto mais forte o tom, melhor para observar.
      Em Chuva e Orvalho, o tom de "muito favorável" indica risco baixo.</p>
      <p><strong>Siglas dentro dos blocos</strong></p>
      <ul>
        <li><strong class="mono">1 a 5:</strong> nota geral da condição</li>
        <li><strong class="mono">B / M / A / MA:</strong> risco baixo, moderado, alto ou muito alto</li>
        <li><strong class="mono">Op.</strong> <strong>(em Observar):</strong> oportunista (apenas um setup leve, se abrir)</li>
      </ul>
      <p>Todos os horários da página estão no {e(tz_nome)}.</p>
      <p>Cada coluna é um horário em que a previsão foi atualizada; a coluna destacada é a do horário-alvo.
      Toque em qualquer bloco para abrir o relatório completo daquele horário.</p>
    </div>
  </details>
</section>""")

    # Matriz horária por noite (mais recente primeiro)
    noites = {}
    for it in itens:
        noites.setdefault(noite_de(it["t"].astimezone(tz)), []).append(it)
    for nd in sorted(noites, reverse=True):
        runs = noites[nd]
        dia2 = nd + timedelta(days=1)
        try:
            alvo_n = datetime.fromisoformat(runs[-1]["d"]["alvo"])
            dif = [abs((r["t"] - alvo_n).total_seconds()) for r in runs]
            i_alvo = dif.index(min(dif)) if min(dif) <= 20 * 60 else -1
        except Exception:  # noqa: BLE001
            i_alvo = -1

        cab = "".join(
            f'<th scope="col" class="{"alvo" if i == i_alvo else ""}">'
            f'<a class="mono" href="relatorios/{e(r["pdf"].name)}">{r["t"].astimezone(tz):%H:%M}</a>'
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
                cels.append(f'<td class="{"alvo" if i == i_alvo else ""}"><a class="b mono {nivel_cor(n)}" '
                            f'href="relatorios/{e(r["pdf"].name)}" title="{e(nome)}: {e(txt)} ({hora}). '
                            f'Abrir relatório" aria-label="{e(nome)}: {e(txt)}, {hora}. Abrir relatório">'
                            f'{e(curto)}</a></td>')
            return (f'<tr><th scope="row">{nome}<span class="dica">{dica}</span></th>'
                    f'{"".join(cels)}</tr>')

        partes.append(f"""
<section class="noite">
  <h4>Noite de <span class="mono">{nd:%d/%m}</span> para <span class="mono">{dia2:%d/%m}</span></h4>
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
<meta name="theme-color" content="#080B12">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
:root {{
  --fundo:#080B12; --card:#111622; --borda:rgba(255,255,255,0.08); --borda-forte:rgba(255,255,255,0.16);
  --texto:#E7ECF5; --sec:#8A94A8; --foco:#7CB3FF;
  --n5:#1D4ED8; --n4:#3B82F6; --n3:#7FA8EA; --n2:#B9C7DF; --n1:#E3E8F0; --vazio:#1C2333;
  --t5:#F5F8FF; --t4:#F5F8FF; --t3:#0A1020; --t2:#0A1020; --t1:#0A1020;
  --bom:#34D399; --meio:#FBBF24; --ruim:#F87171;
  --ambar:#F59E0B; --ambar-fundo:rgba(245,158,11,0.12); --ambar-texto:#FCD34D;
  --mono:"JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}}
body.noturno {{
  --fundo:#050000; --card:#0C0101; --borda:rgba(239,68,68,0.16); --borda-forte:rgba(239,68,68,0.32);
  --texto:#EF4444; --sec:#991B1B; --foco:#EF4444;
  --n5:#EF4444; --n4:#B91C1C; --n3:#7F1D1D; --n2:#4A0E0E; --n1:#2A0707; --vazio:#140202;
  --t5:#050000; --t4:#050000; --t3:#EF4444; --t2:#EF4444; --t1:#EF4444;
  --bom:#EF4444; --meio:#DC2626; --ruim:#B91C1C;
  --ambar:#B91C1C; --ambar-fundo:rgba(185,28,28,0.14); --ambar-texto:#EF4444;
}}
* {{ box-sizing:border-box; }}
html {{ background:#080B12; scroll-behavior:smooth; }}
body {{ margin:0; background:var(--fundo); color:var(--texto); font-family:Inter, system-ui, sans-serif;
  font-size:16px; line-height:1.55; -webkit-font-smoothing:antialiased; transition:background .2s, color .2s; }}
.mono {{ font-family:var(--mono); font-variant-numeric:tabular-nums; letter-spacing:-.01em; }}
main, header, footer {{ max-width:900px; margin:0 auto; padding:0 20px; }}
:focus-visible {{ outline:2px solid var(--foco); outline-offset:2px; border-radius:6px; }}
.sr {{ position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }}
.nb {{ white-space:nowrap; }}

/* Cabeçalho */
header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px 16px; padding-top:24px; flex-wrap:wrap; }}
header > div {{ flex:1 1 320px; min-width:0; }}
header h1 {{ font-size:1rem; font-weight:600; margin:0; letter-spacing:-.005em; }}
header p {{ margin:2px 0 0; color:var(--sec); font-size:.86rem; }}
button.modo {{ flex:none; display:inline-flex; align-items:center; gap:8px; font:inherit; font-size:.84rem; font-weight:500;
  color:var(--texto); background:var(--card); border:1px solid var(--borda-forte); border-radius:999px; padding:7px 14px; cursor:pointer; }}
button.modo:hover {{ border-color:var(--texto); }}
button.modo[aria-pressed="true"] {{ background:var(--texto); color:var(--fundo); border-color:var(--texto); }}

/* Banner de relatório vencido */
.banner-expirado {{ display:flex; gap:12px; align-items:flex-start; margin:20px 0 0; padding:12px 16px; border-radius:12px;
  background:var(--ambar-fundo); border:1px solid var(--ambar); color:var(--ambar-texto); font-weight:600; }}
.banner-expirado[hidden] {{ display:none; }}
.banner-expirado svg {{ flex:none; margin-top:2px; }}
.banner-expirado p {{ margin:0; }}

/* Painel de decisão */
.decisao {{ --cor:var(--meio); display:grid; grid-template-columns:minmax(0, 1fr) 280px; margin:20px 0 0;
  background:var(--card); border:1px solid var(--borda); border-radius:18px; overflow:hidden; position:relative; }}
.decisao::before {{ content:""; position:absolute; inset:0 0 auto 0; height:3px; background:var(--cor); }}
.decisao.bom {{ --cor:var(--bom); }} .decisao.meio {{ --cor:var(--meio); }} .decisao.ruim {{ --cor:var(--ruim); }}
.decisao-principal {{ padding:26px 26px 24px; }}
.meta {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px 12px; margin:0 0 18px; color:var(--sec); font-size:.86rem; }}
.selo {{ display:inline-block; padding:2px 10px; border-radius:999px; border:1px solid var(--borda-forte); color:var(--texto); font-size:.78rem; }}
.selo.vencido {{ border-color:var(--ambar); color:var(--ambar-texto); background:var(--ambar-fundo); }}
.status {{ display:flex; align-items:center; gap:8px; margin:0 0 6px; color:var(--cor); font-size:.86rem; font-weight:600; }}
.led-status {{ width:10px; height:10px; border-radius:50%; background:var(--cor); box-shadow:0 0 0 4px color-mix(in srgb, var(--cor) 22%, transparent); }}
.decisao h2 {{ font-size:clamp(2rem, 5.6vw, 3.1rem); line-height:1.04; font-weight:800; letter-spacing:-.025em; margin:0 0 16px; }}
.decisao.vencida h2, .decisao.vencida .motivos {{ opacity:.5; }}
.motivos {{ list-style:none; margin:0 0 18px; padding:0; display:grid; gap:6px; }}
.motivos li {{ position:relative; padding-left:18px; }}
.motivos li::before {{ content:""; position:absolute; left:2px; top:.62em; width:7px; height:7px; border-radius:2px; background:var(--cor); }}
.fonte {{ margin:0 0 18px; color:var(--sec); font-size:.86rem; max-width:62ch; }}
.botao {{ display:inline-block; color:var(--fundo); background:var(--texto); text-decoration:none; font-weight:600;
  padding:11px 18px; border-radius:10px; }}
.botao:hover {{ opacity:.9; }}
.equipamento {{ padding:26px 22px 22px; border-left:1px solid var(--borda); background:color-mix(in srgb, var(--cor) 6%, transparent); }}
.eq-rot {{ margin:0 0 10px; color:var(--sec); font-size:.8rem; }}
.eq-icone {{ width:58px; height:58px; display:grid; place-items:center; border-radius:14px; color:var(--cor);
  border:1px solid color-mix(in srgb, var(--cor) 40%, transparent); background:color-mix(in srgb, var(--cor) 10%, transparent); }}
.eq-nome {{ margin:12px 0 2px; font-weight:700; font-size:1.05rem; }}
.eq-txt {{ margin:0 0 16px; color:var(--sec); font-size:.84rem; }}
.chips {{ list-style:none; margin:0; padding:12px 0 0; border-top:1px solid var(--borda); display:grid; gap:8px; font-size:.84rem; }}
.chips li {{ display:grid; grid-template-columns:auto 1fr auto; align-items:center; gap:8px; }}
.chip-nome {{ color:var(--sec); }} .chip-val {{ font-weight:600; }}

/* Cartões de métricas */
h3 {{ font-size:.95rem; font-weight:600; margin:32px 0 10px; }}
.cards {{ display:grid; gap:10px; }}
.cards.c4 {{ grid-template-columns:repeat(4, minmax(0, 1fr)); }}
.cards.c3 {{ grid-template-columns:repeat(3, minmax(0, 1fr)); }}
.card {{ background:var(--card); border:1px solid var(--borda); border-radius:14px; padding:14px 16px; min-width:0; }}
.card .rot {{ margin:0 0 8px; color:var(--sec); font-size:.82rem; }}
.valor-linha {{ display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }}
.card-lua .valor-linha {{ justify-content:flex-start; }}
.valor {{ margin:0; font-weight:700; font-size:1.08rem; display:flex; align-items:center; gap:8px; }}
.valor.mono {{ font-size:1.5rem; line-height:1; }}
.valor .de {{ font-size:.8rem; font-weight:500; color:var(--sec); font-family:Inter, system-ui, sans-serif; }}
.card .rodape {{ margin:10px 0 0; color:var(--sec); font-size:.76rem; }}
.medidor {{ display:inline-flex; gap:3px; }}
.medidor span {{ width:10px; height:22px; border-radius:3px; }}
.led {{ width:9px; height:9px; border-radius:50%; flex:none; }}
.lua {{ flex:none; }} .lua-sombra {{ fill:var(--vazio); stroke:var(--borda-forte); stroke-width:1; }} .lua-luz {{ fill:var(--texto); }}

/* Escala de cores */
.n5 {{ background:var(--n5); }} .n4 {{ background:var(--n4); }} .n3 {{ background:var(--n3); }}
.n2 {{ background:var(--n2); }} .n1 {{ background:var(--n1); }} .vazio {{ background:var(--vazio); }}
body.noturno .n1, body.noturno .n2, body.noturno .vazio {{ box-shadow:inset 0 0 0 1px rgba(239,68,68,.22); }}
.escala {{ list-style:none; margin:0 0 10px; padding:0; display:flex; flex-wrap:wrap; gap:6px 16px; color:var(--sec); font-size:.84rem; }}
.escala li {{ display:inline-flex; align-items:center; gap:7px; white-space:nowrap; }}
.sw {{ width:18px; height:12px; border-radius:3px; display:inline-block; }}
details {{ color:var(--sec); font-size:.88rem; background:var(--card); border:1px solid var(--borda); border-radius:12px; }}
summary {{ display:flex; align-items:center; gap:8px; cursor:pointer; color:var(--texto); padding:10px 14px; list-style:none; font-weight:500; }}
summary::-webkit-details-marker {{ display:none; }}
summary::after {{ content:"+"; margin-left:auto; color:var(--sec); font-family:var(--mono); }}
details[open] summary::after {{ content:"−"; }}
.explica {{ max-width:68ch; padding:0 16px 12px 40px; }}
.explica p {{ margin:6px 0; }} .explica ul {{ margin:4px 0 8px; padding-left:1.1em; }}
.explica strong {{ color:var(--texto); font-weight:600; }}

/* Matriz horária */
h4 {{ font-size:.9rem; font-weight:600; margin:26px 0 8px; }}
.rolagem {{ overflow-x:auto; scroll-behavior:smooth; -webkit-overflow-scrolling:touch; scrollbar-width:thin;
  background: linear-gradient(to right, var(--fundo) 30%, transparent) left / 40px 100% no-repeat local,
              linear-gradient(to left, var(--fundo) 30%, transparent) right / 40px 100% no-repeat local,
              linear-gradient(to right, rgba(0,0,0,.6), transparent) left / 14px 100% no-repeat scroll,
              linear-gradient(to left, rgba(0,0,0,.6), transparent) right / 14px 100% no-repeat scroll; }}
table {{ border-collapse:separate; border-spacing:4px 4px; font-size:.8rem; }}
th {{ font-weight:400; color:var(--sec); text-align:left; white-space:nowrap; }}
tbody th, thead th.canto {{ position:sticky; left:0; z-index:2; background:var(--fundo); padding-right:12px; }}
tbody th {{ color:var(--texto); font-weight:500; }}
thead th {{ text-align:center; padding:0 0 2px; vertical-align:top; min-width:44px; }}
thead a {{ color:var(--sec); text-decoration:none; font-size:.76rem; }}
thead th.alvo a {{ color:var(--texto); font-weight:700; }}
.marca {{ display:block; font-size:.64rem; line-height:1.1; color:var(--cor-alvo, var(--texto)); }}
td {{ padding:0; position:relative; }}
td.alvo::before {{ content:""; position:absolute; inset:-3px -2px; border-left:1px solid var(--borda-forte);
  border-right:1px solid var(--borda-forte); background:rgba(255,255,255,.05); pointer-events:none; }}
body.noturno td.alvo::before {{ background:rgba(239,68,68,.06); }}
tr:first-child td.alvo::before {{ border-top:1px solid var(--borda-forte); border-radius:6px 6px 0 0; }}
tr:last-child td.alvo::before {{ border-bottom:1px solid var(--borda-forte); border-radius:0 0 6px 6px; }}
a.b {{ position:relative; display:flex; align-items:center; justify-content:center; width:44px; height:30px; border-radius:6px;
  text-decoration:none; font-weight:700; font-size:.76rem; cursor:pointer; transition:transform .12s ease, box-shadow .12s ease; }}
a.b:hover {{ transform:translateY(-1px); box-shadow:0 0 0 2px var(--foco); }}
a.b.n5 {{ color:var(--t5); }} a.b.n4 {{ color:var(--t4); }} a.b.n3 {{ color:var(--t3); }}
a.b.n2 {{ color:var(--t2); }} a.b.n1 {{ color:var(--t1); }}
th .dica {{ display:block; font-size:.7rem; color:var(--sec); font-weight:400; }}

footer {{ color:var(--sec); font-size:.82rem; padding-top:40px; padding-bottom:40px; }}
footer p {{ max-width:68ch; }}
footer a {{ color:var(--texto); }}

@media (max-width:720px) {{
  .decisao {{ grid-template-columns:1fr; }}
  .equipamento {{ border-left:0; border-top:1px solid var(--borda); display:grid; grid-template-columns:58px 1fr;
    column-gap:14px; align-items:center; }}
  .eq-rot {{ grid-column:1 / -1; }} .eq-icone {{ grid-row:span 2; }} .eq-nome {{ margin-top:0; }} .eq-txt {{ margin-bottom:0; }}
  .chips {{ grid-column:1 / -1; margin-top:14px; }}
  .cards.c4 {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }}
  .cards.c3 {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }}
  .cards.c3 .card:last-child {{ grid-column:1 / -1; }}
}}
@media (max-width:480px) {{
  .decisao-principal {{ padding:22px 18px 20px; }} .equipamento {{ padding:18px; }}
  table {{ border-spacing:3px; font-size:.72rem; }}
  a.b {{ width:36px; height:28px; font-size:.68rem; }} thead th {{ min-width:36px; }}
  thead a {{ font-size:.68rem; }} th .dica {{ font-size:.62rem; }}
}}
@media (prefers-reduced-motion:reduce) {{ * {{ transition:none !important; scroll-behavior:auto !important; }}
  a.b:hover {{ transform:none; }} }}
</style>
</head>
<body>
<script>
try {{ if (localStorage.getItem("modoVermelho") === "1") document.body.classList.add("noturno"); }} catch (e) {{}}
</script>
<header>
  <div>
    <h1>{e(titulo)}</h1>
    <p>Previsão automática a partir da radiossonda de Confins e do satélite <span class="nb">GOES-19</span></p>
  </div>
  <button class="modo" type="button" aria-pressed="false" id="modo"
          title="Cores vermelhas para preservar a visão no escuro">{ICONE_OLHO}<span>Modo noturno</span></button>
</header>
<main>
{banner}
{''.join(partes)}
</main>
<footer>
  <p>As notas são estimativas automáticas feitas por regras heurísticas, ainda em fase experimental.
  Antes de subir a serra, confira também o céu no satélite e as condições locais.</p>
  <p>Relatórios dos últimos {dias} dias. Página gerada às <span class="mono">{gerado:%H:%M}</span> de
  <span class="mono">{gerado:%d/%m/%Y}</span>, {e(tz_nome)}.
  {f'Código-fonte e documentação: <a href="{e(url_repo)}">{e(url_repo)}</a> (GPL-3.0).' if url_repo else ''}</p>
</footer>
<script>
(function () {{
  var c = document.body, b = document.getElementById("modo");
  function sync() {{ var on = c.classList.contains("noturno"); b.setAttribute("aria-pressed", on ? "true" : "false");
    b.title = on ? "Voltar às cores normais" : "Cores vermelhas para preservar a visão no escuro";
    var m = document.querySelector('meta[name="theme-color"]'); if (m) m.content = on ? "#050000" : "#080B12"; }}
  b.addEventListener("click", function () {{ c.classList.toggle("noturno");
    try {{ localStorage.setItem("modoVermelho", c.classList.contains("noturno") ? "1" : "0"); }} catch (e) {{}}
    sync(); }});
  sync();

  // Validade: banner âmbar no topo e selo "vencido" quando o relatório passa da validade
  var selo = document.querySelector(".selo"), banner = document.querySelector(".banner-expirado"),
      dec = document.querySelector(".decisao");
  if (selo && new Date(selo.getAttribute("data-ate")) < new Date()) {{
    selo.classList.add("vencido"); selo.textContent = "vencido às " + selo.getAttribute("data-hora");
    if (banner) banner.hidden = false;
    if (dec) dec.classList.add("vencida");
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
    ap.add_argument("--tz-nome", default="horário de Brasília (UTC−3)", help="como o fuso aparece na página")
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
    (args.saida / "index.html").write_text(montar_html(itens, args.site, tz, args.url_repo, args.dias, args.tz_nome),
                                           encoding="utf-8")
    (args.saida / ".nojekyll").write_text("")
    print(f"Página gerada em {args.saida} com {len(itens)} relatório(s).")


if __name__ == "__main__":
    main()
