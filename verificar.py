#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 João — projeto wykrota-nowcast
"""
verificar.py — Confere as previsões com o que de fato aconteceu.

Passo 1 (automático): para cada relatório final (<SITE>_<AAAAMMDD>T<HHMM>Z.json), olha as
imagens do GOES-19 na janela de observação (do horário-alvo até N horas depois), mede a
fração de céu claro e de cirrus num raio de 25 km do observatório e converte numa
"transparência observada" de 1 a 5, com a mesma fórmula da nota do satélite.

Passo 2 (opcional): se existir o registro manual (registro_observacoes.csv), compara também
transparência e seeing previstos com o que os observadores anotaram.

Os resultados por quadro do GOES ficam num cache, então a verificação continua valendo
mesmo depois que as imagens antigas forem apagadas. Rode todo dia de manhã, por exemplo.

Saídas na pasta --saida:
    verificacao.csv          uma linha por previsão verificada
    verificacao_resumo.md    viés, erro médio, acertos e matriz de confusão
    verificacao_cache.json   resultado de cada quadro do GOES já processado

Uso:
    python3 verificar.py --entrada ~/astro_previsoes --goes-root ".../IMAGES/GOES-19/Full Disk" \
        --lat -19.824167 --lon -43.690278 --site Wykrota --config astro_config.toml \
        --registro registro_observacoes.csv --saida ~/astro_previsoes/verificacao
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import tomllib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import goes_nowcast as gn  # noqa: E402

CAMPOS_REGISTRO = ["data_noite", "hora_inicio", "hora_fim", "observador", "local", "transparencia",
                   "seeing", "nuvens_pct", "cirrus", "orvalho", "vento", "sqm", "fwhm_arcsec", "notas"]


# --------------------------------------------------------------------------
# Observado pelo satélite
# --------------------------------------------------------------------------
def frame_stats(folder: Path, lat, lon, tsfc_k, cfg):
    fr = gn.Frame(folder, lat, lon, 170, cfg)
    sun = gn.solar_elevation(fr.time, lat, lon)
    cls, info = gn.classify(fr, sun, tsfc_k, cfg)
    s25 = gn.stats(cls, fr.dist <= 25)
    s50 = gn.stats(cls, fr.dist <= 50)
    return dict(tempo=fr.time.isoformat(), noite=bool(info["noite"]),
                claro25=s25["claro"], cirrus25=s25["cirrus fino"], nevoeiro25=s25["nevoeiro/estrato"],
                claro50=s50["claro"])


def nota_observada(claro, cirrus, cfg):
    sc = 1 + 4 * claro - cfg["nota"]["penal_cirrus"] * cirrus
    return int(max(1, min(5, math.floor(sc + 0.5)))), sc


# --------------------------------------------------------------------------
# Registro manual
# --------------------------------------------------------------------------
def ler_registro(path: Path | None):
    if not path or not path.exists():
        return {}, []
    noites, avisos = {}, []
    with open(path, newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        faltando = [c for c in ("data_noite", "transparencia", "seeing") if c not in (rd.fieldnames or [])]
        if faltando:
            return {}, [f"registro sem as colunas obrigatórias: {', '.join(faltando)}"]
        for i, row in enumerate(rd, start=2):
            if not (row.get("data_noite") or "").strip() or row["data_noite"].startswith("#"):
                continue
            try:
                d = datetime.strptime(row["data_noite"].strip(), "%Y-%m-%d").date()
            except ValueError:
                avisos.append(f"linha {i}: data_noite inválida ({row['data_noite']})")
                continue
            def num(k, lo=None, hi=None):
                v = (row.get(k) or "").strip().replace(",", ".")
                if not v:
                    return None
                try:
                    x = float(v)
                except ValueError:
                    avisos.append(f"linha {i}: {k} não numérico ({v})")
                    return None
                if (lo is not None and x < lo) or (hi is not None and x > hi):
                    avisos.append(f"linha {i}: {k} fora da faixa ({v})")
                    return None
                return x
            reg = dict(transparencia=num("transparencia", 1, 5), seeing=num("seeing", 1, 5),
                       nuvens_pct=num("nuvens_pct", 0, 100), sqm=num("sqm", 10, 23),
                       fwhm=num("fwhm_arcsec", 0, 20), observador=(row.get("observador") or "").strip(),
                       orvalho=(row.get("orvalho") or "").strip().lower())
            noites.setdefault(d, []).append(reg)
    # média por noite quando houver mais de um observador
    out = {}
    for d, regs in noites.items():
        def media(k):
            v = [r[k] for r in regs if r[k] is not None]
            return sum(v) / len(v) if v else None
        out[d] = dict(transparencia=media("transparencia"), seeing=media("seeing"),
                      nuvens_pct=media("nuvens_pct"), sqm=media("sqm"), fwhm=media("fwhm"),
                      n_obs=len(regs), observadores=", ".join(sorted({r["observador"] for r in regs if r["observador"]})))
    return out, avisos


# --------------------------------------------------------------------------
# Métricas
# --------------------------------------------------------------------------
def metricas(pares):
    """pares: lista de (previsto, observado) inteiros/float 1–5."""
    if not pares:
        return None
    err = [p - o for p, o in pares]
    n = len(pares)
    return dict(n=n, vies=sum(err) / n, mae=sum(abs(e) for e in err) / n,
                exato=sum(1 for p, o in pares if round(p) == round(o)) / n,
                pm1=sum(1 for e in err if abs(e) <= 1) / n)


def matriz(pares):
    c = Counter((int(round(p)), int(round(o))) for p, o in pares)
    linhas = ["| previsto \\ observado | 1 | 2 | 3 | 4 | 5 |", "|---|---|---|---|---|---|"]
    for p in range(5, 0, -1):
        linhas.append(f"| **{p}** | " + " | ".join(str(c.get((p, o), "")) or "" for o in range(1, 6)) + " |")
    return "\n".join(linhas)


def fmt_m(m):
    if not m:
        return "| — | — | — | — | — |"
    return f"| {m['n']} | {m['vies']:+.2f} | {m['mae']:.2f} | {m['exato']:.0%} | {m['pm1']:.0%} |"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Verificação das previsões com o GOES-19 e o registro manual")
    ap.add_argument("--entrada", type=Path, required=True, help="pasta dos relatórios (JSON finais)")
    ap.add_argument("--goes-root", type=Path, required=True, help=".../IMAGES/GOES-19/Full Disk")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--site", default="Wykrota")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--registro", type=Path, help="CSV do registro manual (opcional)")
    ap.add_argument("--saida", type=Path, required=True)
    ap.add_argument("--janela-h", type=float, default=3.0, help="horas de observação a partir do alvo")
    ap.add_argument("--min-quadros", type=int, default=3, help="quadros mínimos na janela para verificar")
    ap.add_argument("--tz", default="America/Sao_Paulo")
    args = ap.parse_args()

    cfg = json.loads(json.dumps(gn.DEFAULTS))
    if args.config:
        with open(args.config, "rb") as fh:
            gn.deep_update(cfg, tomllib.load(fh).get("goes", {}))
    tz = ZoneInfo(args.tz)
    args.saida.mkdir(parents=True, exist_ok=True)
    cache_p = args.saida / "verificacao_cache.json"
    cache = json.loads(cache_p.read_text()) if cache_p.exists() else {}

    # Previsões finais
    pat = re.compile(rf"^{re.escape(args.site)}_(\d{{8}}T\d{{4}})Z\.json$")
    previsoes = []
    for j in sorted(args.entrada.iterdir()):
        m = pat.match(j.name)
        if not m:
            continue
        try:
            d = json.loads(j.read_text())
        except Exception:  # noqa: BLE001
            continue
        if "alvo" not in d or "transparencia" not in d:
            continue
        emit = datetime.strptime(m.group(1), "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
        previsoes.append((j, emit, d))

    # Quadros do GOES disponíveis
    quadros = gn.list_products(args.goes_root) if args.goes_root.exists() else []
    t_ultimo = quadros[-1][0] if quadros else None

    # Processa (ou recupera do cache) cada quadro necessário
    def stats_quadro(t, folder, tsfc_k):
        k = f"{args.site}|{args.lat:.4f}|{args.lon:.4f}|{folder.name}"
        if k not in cache:
            try:
                cache[k] = frame_stats(folder, args.lat, args.lon, tsfc_k, cfg)
            except Exception as e:  # noqa: BLE001
                print(f"aviso: {folder.name}: {e}", file=sys.stderr)
                cache[k] = None
        return cache[k]

    # Quadros já no cache cujo diretório pode ter sido apagado
    cache_por_tempo = {}
    for k, v in cache.items():
        if v and k.startswith(f"{args.site}|{args.lat:.4f}|{args.lon:.4f}|"):
            cache_por_tempo[datetime.fromisoformat(v["tempo"])] = v

    registro, avisos = ler_registro(args.registro)
    for a in avisos:
        print(f"registro: {a}", file=sys.stderr)

    linhas, pendentes = [], 0
    for j, emit, d in previsoes:
        alvo = datetime.fromisoformat(d["alvo"])
        fim = alvo + timedelta(hours=args.janela_h)
        # Aguarda enquanto a janela ainda pode receber quadros (até 12 h depois do fim dela)
        agora = datetime.now(timezone.utc)
        ainda_chegando = (t_ultimo is None or t_ultimo < fim) and not any(t >= fim for t in cache_por_tempo)
        if ainda_chegando and agora - fim < timedelta(hours=12):
            pendentes += 1
            continue
        tsfc = d.get("sfc_t_c")
        tsfc_k = None if tsfc is None else tsfc + 273.15
        obs = []
        for t, folder in quadros:
            if alvo <= t <= fim:
                s = stats_quadro(t, folder, tsfc_k)
                if s:
                    obs.append(s)
        vistos = {o["tempo"] for o in obs}
        for t, v in cache_por_tempo.items():
            if alvo <= t <= fim and v["tempo"] not in vistos:
                obs.append(v)
        if len(obs) < args.min_quadros:
            continue
        claro = sum(o["claro25"] for o in obs) / len(obs)
        cirrus = sum(o["cirrus25"] for o in obs) / len(obs)
        t_obs, t_obs_bruto = nota_observada(claro, cirrus, cfg)
        det = d.get("transparencia_detalhe") or {}
        noite = (alvo.astimezone(tz) - timedelta(hours=12)).date()
        reg = registro.get(noite) or {}
        linhas.append(dict(
            previsao=j.stem, emitida_utc=emit.isoformat(), alvo_utc=alvo.isoformat(), noite=noite.isoformat(),
            antecedencia_h=round((alvo - emit).total_seconds() / 3600, 1),
            transp_final=d["transparencia"], transp_sondagem=det.get("sondagem", d["transparencia"]),
            transp_satelite=det.get("satelite", ""), peso_sat=round(det.get("peso_sat", 0.0), 2),
            seeing=d.get("seeing"), chuva=d.get("chuva"), orvalho=d.get("orvalho"), montar=d.get("montar"),
            obs_quadros=len(obs), obs_claro_25km=round(claro, 3), obs_cirrus_25km=round(cirrus, 3),
            obs_transp_sat=t_obs, obs_transp_sat_bruto=round(t_obs_bruto, 2),
            reg_transparencia="" if reg.get("transparencia") is None else round(reg["transparencia"], 1),
            reg_seeing="" if reg.get("seeing") is None else round(reg["seeing"], 1),
            reg_nuvens_pct="" if reg.get("nuvens_pct") is None else round(reg["nuvens_pct"]),
            reg_sqm="" if reg.get("sqm") is None else round(reg["sqm"], 2),
            reg_observadores=reg.get("observadores", ""),
        ))

    cache_p.write_text(json.dumps(cache, ensure_ascii=False, indent=1))

    csv_p = args.saida / "verificacao.csv"
    if linhas:
        with open(csv_p, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(linhas[0].keys()))
            w.writeheader()
            w.writerows(linhas)

    # ---- Resumo ----
    def pares(chave_prev, chave_obs, filtro=lambda r: True):
        out = []
        for r in linhas:
            if not filtro(r) or r[chave_prev] in ("", None) or r[chave_obs] in ("", None):
                continue
            out.append((float(r[chave_prev]), float(r[chave_obs])))
        return out

    md = [f"# Verificação das previsões: {args.site}",
          "",
          f"Gerado em {datetime.now(tz):%d/%m/%Y %H:%M}. {len(linhas)} previsão(ões) verificada(s), "
          f"{pendentes} aguardando o fim da janela de observação. Janela: do horário-alvo até "
          f"{args.janela_h:g} h depois; observado = céu claro e cirrus em 25 km pelo GOES-19.",
          "",
          "Viés positivo = previsão mais otimista que o observado. ±1 = acertou com até 1 ponto de diferença.",
          "",
          "## Transparência prevista × observada pelo satélite",
          "",
          "| Nota avaliada | n | viés | erro médio | exato | ±1 |", "|---|---|---|---|---|---|"]
    sem_sat = lambda r: r["peso_sat"] < 0.5  # noqa: E731
    for nome, chave, filt in [("Final (todas)", "transp_final", lambda r: True),
                              ("Final, satélite com peso < 50%", "transp_final", sem_sat),
                              ("Só sondagem", "transp_sondagem", lambda r: True)]:
        md.append(f"| {nome} " + fmt_m(metricas(pares(chave, "obs_transp_sat", filt))))
    md += ["", "A linha \"satélite com peso < 50%\" é a mais honesta para avaliar a previsão: quando o "
           "satélite pesa muito, a nota final já é quase uma observação.", "",
           "### Por antecedência (nota só da sondagem)", "",
           "| Antecedência | n | viés | erro médio | exato | ±1 |", "|---|---|---|---|---|---|"]
    for nome, lo, hi in [("até 2 h", -99, 2), ("2–6 h", 2, 6), ("6–12 h", 6, 12), ("mais de 12 h", 12, 999)]:
        md.append(f"| {nome} " + fmt_m(metricas(pares("transp_sondagem", "obs_transp_sat",
                                                      lambda r, lo=lo, hi=hi: lo < r["antecedencia_h"] <= hi))))
    pm = pares("transp_sondagem", "obs_transp_sat")
    if pm:
        md += ["", "### Matriz de confusão (só sondagem)", "", matriz(pm)]

    md += ["", "## Registro manual dos observadores", ""]
    pt = pares("transp_final", "reg_transparencia")
    ps = pares("seeing", "reg_seeing")
    if pt or ps:
        md += ["| Comparação | n | viés | erro médio | exato | ±1 |", "|---|---|---|---|---|---|",
               "| Transparência final × registro " + fmt_m(metricas(pt)),
               "| Seeing × registro " + fmt_m(metricas(ps)),
               "| Transparência observada pelo satélite × registro " +
               fmt_m(metricas(pares("obs_transp_sat", "reg_transparencia")))]
        md += ["", "A última linha mostra o quanto a \"observação\" do satélite concorda com quem estava lá; "
               "se concordar pouco, a calibração deve se apoiar mais no registro manual."]
    else:
        md.append("Ainda não há noites do registro manual coincidindo com previsões verificadas.")
    if avisos:
        md += ["", "Avisos ao ler o registro:", ""] + [f"- {a}" for a in avisos]
    (args.saida / "verificacao_resumo.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    m = metricas(pares("transp_final", "obs_transp_sat", sem_sat))
    print(f"{len(linhas)} previsão(ões) verificada(s), {pendentes} pendente(s).")
    if m:
        print(f"Transparência (satélite com peso < 50%): viés {m['vies']:+.2f}, erro médio {m['mae']:.2f}, "
              f"±1 em {m['pm1']:.0%} ({m['n']} casos)")
    print(f"Resumo: {args.saida / 'verificacao_resumo.md'}")


if __name__ == "__main__":
    main()
