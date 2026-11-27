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


def nota10(d, chave):
    """Nota de transparência/seeing na escala 1–10 (converte relatórios antigos, de 1 a 5)."""
    v = d.get(chave)
    if v in (None, ""):
        return 1
    v = float(v)
    if d.get("escala_notas") != 10:
        v = 1 + (v - 1) * 2.25
    return int(max(1, min(10, round(v))))


def cor10(n):
    """Cor da escala (5 tons) para uma nota de 1 a 10."""
    return nivel_cor(math.ceil(n / 2))


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
    T, S = nota10(d, "transparencia"), nota10(d, "seeing")
    chuva, orvalho = d.get("chuva"), d.get("orvalho")
    lua = d.get("lua_iluminada", 0)
    neg, pos = [], []
    if T <= 4:
        neg.append("Transparência baixa: nuvens ou muita umidade")
    elif T >= 8:
        pos.append("Céu transparente")
    if chuva == "alto":
        neg.append("Risco alto de chuva ou trovoada")
    elif chuva == "moderado":
        neg.append("Risco moderado de chuva")
    if S <= 4:
        neg.append("Seeing ruim: imagem tremida em alta ampliação")
    elif S >= 8:
        pos.append("Seeing bom para alta ampliação")
    if lua >= 0.6 and T >= 6:
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
            seg = "".join(f'<span class="{cor10(n) if i < n else "vazio"}"></span>' for i in range(10))
            return f'<span class="medidor m10" role="img" aria-label="{n} de 10">{seg}</span>'

        def card(rot, valor_html, rodape="", extra_cls=""):
            return (f'<div class="card {extra_cls}"><p class="rot">{rot}</p>{valor_html}'
                    f'{f"<p class=rodape>{rodape}</p>" if rodape else ""}</div>')

        def nota(n):
            return (f'<div class="valor-linha"><p class="valor mono">{n}<span class="de"> de 10</span></p>'
                    f'{medidor(n)}</div>')

        def risco(txt, nivel):
            return (f'<div class="valor-linha"><p class="valor"><span class="led {nivel_cor(nivel)}" '
                    f'aria-hidden="true"></span>risco {e(txt)}</p></div>')

        T, S = nota10(d, "transparencia"), nota10(d, "seeing")
        chuva, orvalho = str(d.get("chuva", "—")), str(d.get("orvalho", "—"))
        arc, r0 = d.get("seeing_arcsec"), d.get("r0_cm")
        seeing_extra = (f'<p class="fisico mono">{arc:.2f}″ <span>r₀ {r0:.1f} cm</span></p>' if arc else "")
        ate = lambda x: f'válido até <span class="mono">{x:%H:%M}</span>' if x else ""  # noqa: E731
        atm = "".join([
            card("Transparência", nota(T), ate(vn)),
            card("Seeing", nota(S) + seeing_extra, ate(vs)),
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
        <li><strong class="mono">1 a 10:</strong> nota geral da condição (transparência e seeing)</li>
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
                    n10 = nota10(r["d"], chave)
                    n = math.ceil(n10 / 2)
                    curto = str(n10)
                    txt = f"nota {n10} de 10"
                    if chave == "seeing" and r["d"].get("seeing_arcsec"):
                        txt += f", {r['d']['seeing_arcsec']:.2f}″"
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
  <table class="matriz">
    <thead><tr><th scope="col" class="canto"><span class="sr">Item</span></th>{cab}</tr></thead>
    <tbody>
      {linha("Transparência", "transparencia", "nota de 1 a 10")}
      {linha("Seeing", "seeing", "nota de 1 a 10")}
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
/* ===== Tokens ===== */
:root {{
  --fundo:#080B12; --card:#111622; --card-2:#0D121C; --borda:rgba(255,255,255,0.08); --borda-forte:rgba(255,255,255,0.18);
  --texto:#E7ECF5; --sec:#8A94A8; --foco:#7CB3FF;
  --n5:#1D4ED8; --n4:#3B82F6; --n3:#7FA8EA; --n2:#B9C7DF; --n1:#E3E8F0; --vazio:#1C2333;
  --t5:#F5F8FF; --t4:#F5F8FF; --t3:#0A1020; --t2:#0A1020; --t1:#0A1020;
  --bom:#34D399; --meio:#FBBF24; --ruim:#F87171;
  --ambar:#F59E0B; --ambar-fundo:rgba(245,158,11,0.12); --ambar-texto:#FCD34D;
  --alvo-borda:rgba(231,236,245,0.55); --alvo-fundo:rgba(255,255,255,0.045);
  --mono:"JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  --pad:clamp(16px, 4vw, 32px);
  --raio:clamp(14px, 1.6vw, 18px);
  --toque:44px;
}}
body.noturno {{
  --fundo:#050000; --card:#0C0101; --card-2:#080000; --borda:rgba(239,68,68,0.16); --borda-forte:rgba(239,68,68,0.34);
  --texto:#EF4444; --sec:#991B1B; --foco:#EF4444;
  --n5:#EF4444; --n4:#B91C1C; --n3:#7F1D1D; --n2:#4A0E0E; --n1:#2A0707; --vazio:#140202;
  --t5:#050000; --t4:#050000; --t3:#EF4444; --t2:#EF4444; --t1:#EF4444;
  --bom:#EF4444; --meio:#DC2626; --ruim:#B91C1C;
  --ambar:#B91C1C; --ambar-fundo:rgba(185,28,28,0.14); --ambar-texto:#EF4444;
  --alvo-borda:rgba(239,68,68,0.6); --alvo-fundo:rgba(239,68,68,0.06);
}}

/* ===== Base (mobile primeiro: 360 px) ===== */
* {{ box-sizing:border-box; }}
html {{ background:#080B12; -webkit-text-size-adjust:100%; }}
body {{ margin:0; background:var(--fundo); color:var(--texto); font-family:Inter, system-ui, sans-serif;
  font-size:clamp(15px, 0.35vw + 14px, 17px); line-height:1.55; -webkit-font-smoothing:antialiased;
  transition:background-color .2s, color .2s; }}
.mono {{ font-family:var(--mono); font-variant-numeric:tabular-nums; letter-spacing:-.01em; }}
header, main, footer {{ width:100%; max-width:1120px; margin-inline:auto; padding-inline:var(--pad); }}
:focus-visible {{ outline:2px solid var(--foco); outline-offset:2px; border-radius:6px; }}
.sr {{ position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }}
.nb {{ white-space:nowrap; }}

/* Cabeçalho */
header {{ display:flex; align-items:center; justify-content:space-between; gap:10px 16px; flex-wrap:wrap; padding-top:clamp(14px, 3vw, 28px); }}
header > div {{ flex:1 1 240px; min-width:0; }}
header h1 {{ font-size:clamp(.95rem, .4vw + .85rem, 1.1rem); font-weight:600; margin:0; line-height:1.3; }}
header p {{ margin:2px 0 0; color:var(--sec); font-size:.84rem; }}
button.modo {{ flex:none; display:inline-flex; align-items:center; gap:8px; min-height:var(--toque); padding:0 16px;
  font:inherit; font-size:.86rem; font-weight:500; color:var(--texto); background:var(--card);
  border:1px solid var(--borda-forte); border-radius:999px; cursor:pointer; touch-action:manipulation; }}
button.modo:hover {{ border-color:var(--texto); }}
button.modo[aria-pressed="true"] {{ background:var(--texto); color:var(--fundo); border-color:var(--texto); }}

/* Banner de vencido */
.banner-expirado {{ display:flex; gap:10px; align-items:flex-start; margin:14px 0 0; padding:12px 14px; border-radius:12px;
  background:var(--ambar-fundo); border:1px solid var(--ambar); color:var(--ambar-texto); font-weight:600; font-size:.92rem; }}
.banner-expirado[hidden] {{ display:none; }}
.banner-expirado svg {{ flex:none; margin-top:1px; }}
.banner-expirado p {{ margin:0; }}

/* Painel de decisão */
.decisao {{ --cor:var(--meio); display:grid; grid-template-columns:1fr; margin:14px 0 0;
  background:var(--card); border:1px solid var(--borda); border-radius:var(--raio); overflow:hidden; position:relative; }}
.decisao::before {{ content:""; position:absolute; inset:0 0 auto 0; height:3px; background:var(--cor); }}
.decisao.bom {{ --cor:var(--bom); }} .decisao.meio {{ --cor:var(--meio); }} .decisao.ruim {{ --cor:var(--ruim); }}
.decisao-principal {{ padding:18px 16px 18px; min-width:0; }}
.meta {{ display:flex; flex-wrap:wrap; align-items:center; gap:6px 10px; margin:0 0 12px; color:var(--sec); font-size:.8rem; }}
.selo {{ display:inline-block; padding:2px 10px; border-radius:999px; border:1px solid var(--borda-forte); color:var(--texto); font-size:.74rem; }}
.selo.vencido {{ border-color:var(--ambar); color:var(--ambar-texto); background:var(--ambar-fundo); }}
.status {{ display:flex; align-items:center; gap:8px; margin:0 0 4px; color:var(--cor); font-size:.82rem; font-weight:600; }}
.led-status {{ width:9px; height:9px; border-radius:50%; background:var(--cor);
  box-shadow:0 0 0 4px color-mix(in srgb, var(--cor) 22%, transparent); }}
.decisao h2 {{ font-size:clamp(1.85rem, 4.2vw + 1rem, 3.4rem); line-height:1.04; font-weight:800; letter-spacing:-.025em;
  margin:0 0 12px; text-wrap:balance; }}
.decisao.vencida h2, .decisao.vencida .motivos {{ opacity:.5; }}
.motivos {{ list-style:none; margin:0 0 12px; padding:0; display:grid; gap:4px; font-size:.94rem; }}
.motivos li {{ position:relative; padding-left:17px; }}
.motivos li::before {{ content:""; position:absolute; left:2px; top:.6em; width:7px; height:7px; border-radius:2px; background:var(--cor); }}
.fonte {{ margin:0 0 14px; color:var(--sec); font-size:.8rem; max-width:62ch; }}
.botao {{ display:flex; align-items:center; justify-content:center; min-height:var(--toque); width:100%; padding:0 18px;
  color:var(--fundo); background:var(--texto); text-decoration:none; font-weight:600; border-radius:10px; }}
.botao:hover {{ opacity:.9; }}
.equipamento {{ display:grid; grid-template-columns:52px 1fr; column-gap:14px; align-items:center; padding:16px;
  border-top:1px solid var(--borda); background:color-mix(in srgb, var(--cor) 6%, var(--card)); min-width:0; }}
.eq-rot {{ grid-column:1 / -1; margin:0 0 10px; color:var(--sec); font-size:.78rem; }}
.eq-icone {{ grid-row:span 2; width:52px; height:52px; display:grid; place-items:center; border-radius:14px; color:var(--cor);
  border:1px solid color-mix(in srgb, var(--cor) 40%, transparent); background:color-mix(in srgb, var(--cor) 10%, transparent); }}
.eq-icone svg {{ width:34px; height:34px; }}
.eq-nome {{ margin:0; font-weight:700; font-size:clamp(1rem, .4vw + .9rem, 1.15rem); }}
.eq-txt {{ margin:2px 0 0; color:var(--sec); font-size:.82rem; }}
.chips {{ grid-column:1 / -1; list-style:none; margin:14px 0 0; padding:12px 0 0; border-top:1px solid var(--borda);
  display:grid; gap:8px; font-size:.84rem; }}
.chips li {{ display:grid; grid-template-columns:auto 1fr auto; align-items:center; gap:8px; }}
.chip-nome {{ color:var(--sec); }} .chip-val {{ font-weight:600; }}

/* Grupos de cartões */
h3 {{ font-size:clamp(.92rem, .3vw + .85rem, 1.02rem); font-weight:600; margin:clamp(22px, 3vw, 34px) 0 10px; }}
.cards {{ display:grid; gap:clamp(8px, 1.2vw, 12px); }}
.cards.c4 {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }}
.cards.c3 {{ grid-template-columns:1fr; }}
.card {{ background:var(--card); border:1px solid var(--borda); border-radius:14px; padding:12px 14px; min-width:0; }}
.card .rot {{ margin:0 0 6px; color:var(--sec); font-size:.8rem; }}
.valor-linha {{ display:flex; align-items:center; justify-content:space-between; gap:8px 10px; flex-wrap:wrap; }}
.card-lua .valor-linha {{ justify-content:flex-start; }}
.valor {{ margin:0; font-weight:700; font-size:clamp(.98rem, .5vw + .88rem, 1.12rem); display:flex; align-items:center; gap:8px; line-height:1.25; }}
.valor.mono {{ font-size:clamp(1.35rem, 1.2vw + 1.05rem, 1.75rem); line-height:1; }}
.valor .de {{ font-size:.78rem; font-weight:500; color:var(--sec); font-family:Inter, system-ui, sans-serif; }}
.card .rodape {{ margin:8px 0 0; color:var(--sec); font-size:.74rem; }}
.medidor {{ display:inline-flex; gap:3px; }}
.medidor span {{ width:9px; height:20px; border-radius:3px; }}
.medidor.m10 {{ gap:2px; }} .medidor.m10 span {{ width:6px; height:20px; border-radius:2px; }}
.fisico {{ margin:8px 0 0; font-weight:700; font-size:.95rem; }}
.fisico span {{ margin-left:8px; font-weight:500; color:var(--sec); font-size:.8rem; }}
.led {{ width:9px; height:9px; border-radius:50%; flex:none; }}
.lua {{ flex:none; }} .lua-sombra {{ fill:var(--vazio); stroke:var(--borda-forte); stroke-width:1; }} .lua-luz {{ fill:var(--texto); }}

/* Escala e legenda */
.n5 {{ background:var(--n5); }} .n4 {{ background:var(--n4); }} .n3 {{ background:var(--n3); }}
.n2 {{ background:var(--n2); }} .n1 {{ background:var(--n1); }} .vazio {{ background:var(--vazio); }}
body.noturno .n1, body.noturno .n2, body.noturno .vazio {{ box-shadow:inset 0 0 0 1px rgba(239,68,68,.22); }}
.escala {{ list-style:none; margin:0 0 10px; padding:0; display:flex; flex-wrap:wrap; gap:6px 14px; color:var(--sec); font-size:.8rem; }}
.escala li {{ display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }}
.sw {{ width:18px; height:12px; border-radius:3px; display:inline-block; }}
details {{ color:var(--sec); font-size:.88rem; background:var(--card); border:1px solid var(--borda); border-radius:12px; }}
summary {{ display:flex; align-items:center; gap:8px; min-height:var(--toque); padding:0 14px; cursor:pointer;
  color:var(--texto); list-style:none; font-weight:500; }}
summary::-webkit-details-marker {{ display:none; }}
summary::after {{ content:"+"; margin-left:auto; color:var(--sec); font-family:var(--mono); font-size:1.1rem; }}
details[open] summary::after {{ content:"−"; }}
.explica {{ max-width:68ch; padding:0 14px 12px 40px; }}
.explica p {{ margin:6px 0; }} .explica ul {{ margin:4px 0 8px; padding-left:1.1em; }}
.explica strong {{ color:var(--texto); font-weight:600; }}

/* Matriz horária */
.noite {{ margin-top:12px; background:var(--card); border:1px solid var(--borda); border-radius:var(--raio); overflow:hidden; }}
.noite h4 {{ margin:0; padding:12px 14px 4px; font-size:.9rem; font-weight:600; }}
.rolagem {{ overflow-x:auto; -webkit-overflow-scrolling:touch; overscroll-behavior-x:contain; scroll-behavior:smooth;
  scrollbar-width:thin; padding:0 0 10px; }}
table.matriz {{ border-collapse:separate; border-spacing:0; font-size:.76rem; }}
.matriz th, .matriz td {{ padding:3px 2px; }}
.matriz th:first-child, .matriz td:first-child {{ position:sticky; left:0; z-index:2; background:var(--card);
  padding-left:14px; padding-right:12px; text-align:left; white-space:nowrap;
  box-shadow:1px 0 0 var(--borda), 8px 0 10px -8px rgba(0,0,0,.7); }}
.matriz tbody th {{ color:var(--texto); font-weight:500; }}
.matriz th {{ font-weight:400; color:var(--sec); }}
.matriz thead th {{ text-align:center; vertical-align:top; padding-top:6px; padding-bottom:4px; }}
.matriz thead th:first-child {{ vertical-align:bottom; }}
.matriz thead a {{ color:var(--sec); text-decoration:none; font-size:.72rem; display:inline-flex; align-items:center;
  justify-content:center; min-height:24px; }}
.matriz .marca {{ display:block; font-size:.62rem; line-height:1.1; color:var(--texto); }}
.matriz th.alvo, .matriz td.alvo {{ background:var(--alvo-fundo); border-left:1px solid var(--alvo-borda); border-right:1px solid var(--alvo-borda); }}
.matriz thead th.alvo {{ border-top:1px solid var(--alvo-borda); border-radius:8px 8px 0 0; }}
.matriz thead th.alvo a {{ color:var(--texto); font-weight:700; }}
.matriz tbody tr:last-child td.alvo {{ border-bottom:1px solid var(--alvo-borda); border-radius:0 0 8px 8px; }}
.matriz .dica {{ display:block; font-size:.66rem; color:var(--sec); font-weight:400; }}
a.b {{ display:flex; align-items:center; justify-content:center; min-width:40px; min-height:var(--toque); border-radius:7px;
  text-decoration:none; font-weight:700; font-size:.74rem; cursor:pointer; touch-action:manipulation;
  transition:transform .12s ease, box-shadow .12s ease; }}
a.b:hover {{ transform:translateY(-1px); box-shadow:0 0 0 2px var(--foco); }}
a.b:active {{ transform:scale(.97); }}
a.b.n5 {{ color:var(--t5); }} a.b.n4 {{ color:var(--t4); }} a.b.n3 {{ color:var(--t3); }}
a.b.n2 {{ color:var(--t2); }} a.b.n1 {{ color:var(--t1); }}

footer {{ color:var(--sec); font-size:.8rem; padding-top:36px; padding-bottom:36px; }}
footer p {{ max-width:72ch; }}
footer a {{ color:var(--texto); word-break:break-word; }}

/* ===== ≥ 600 px ===== */
@media (min-width:600px) {{
  .decisao-principal {{ padding:24px 24px 22px; }}
  .botao {{ display:inline-flex; width:auto; }}
  .equipamento {{ padding:20px 24px; }}
  .cards.c4 {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }}
  table.matriz {{ font-size:.8rem; }}
  a.b {{ min-width:46px; font-size:.78rem; }}
}}

/* ===== ≥ 900 px (desktop) ===== */
@media (min-width:900px) {{
  .decisao {{ grid-template-columns:1.6fr 1fr; margin-top:20px; }}
  .decisao-principal {{ padding:30px 32px 28px; }}
  .meta {{ font-size:.84rem; margin-bottom:18px; }}
  .motivos {{ font-size:1rem; gap:6px; margin-bottom:16px; }}
  .fonte {{ font-size:.86rem; margin-bottom:18px; }}
  .equipamento {{ display:block; border-top:0; border-left:1px solid var(--borda); padding:30px 28px; }}
  .eq-icone {{ width:60px; height:60px; }} .eq-icone svg {{ width:40px; height:40px; }}
  .eq-nome {{ margin-top:14px; }} .eq-txt {{ margin-bottom:0; }}
  .chips {{ margin-top:18px; }}
  .cards.c4 {{ grid-template-columns:repeat(4, minmax(0, 1fr)); }}
  .cards.c3 {{ grid-template-columns:repeat(3, minmax(0, 1fr)); }}
  .card {{ padding:16px 18px; }}
  /* colunas distribuídas por igual; rolagem só se a noite tiver horários demais para a largura */
  table.matriz {{ width:100%; }}
  .matriz th:first-child, .matriz td:first-child {{ width:150px; box-shadow:1px 0 0 var(--borda); }}
  .matriz td a.b {{ width:100%; min-width:0; }}
  .matriz thead a {{ font-size:.78rem; }}
}}

@media (hover:none) {{ a.b:hover {{ transform:none; box-shadow:none; }} }}
@media (prefers-reduced-motion:reduce) {{ * {{ transition:none !important; scroll-behavior:auto !important; }}
  a.b:hover, a.b:active {{ transform:none; }} }}
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
