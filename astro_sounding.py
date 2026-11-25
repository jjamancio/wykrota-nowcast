#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 João — projeto wykrota-nowcast
"""
astro_sounding.py — Previsão para astronomia a partir de um log do radiosonde_auto_rx.

Lê o *_sonde.log (CSV) gerado pelo radiosonde_auto_rx, monta o perfil vertical,
calcula índices termodinâmicos e de turbulência e aplica regras heurísticas
(configuráveis) para dar notas de Transparência e Seeing, risco de chuva e
de orvalho, e uma recomendação de uso do equipamento.

Dependências: numpy, pandas, matplotlib, reportlab  (Python >= 3.11)
    pip install numpy pandas matplotlib reportlab

Uso básico:
    python3 astro_sounding.py 20261124-113405_X3812737_RS41_404001_sonde.log

Opções úteis:
    --target 2026-11-25T00:00Z   horário-alvo da observação (padrão: próxima 00Z)
    --site SBCF                  nome do local no relatório
    --sfc-elev 827               elevação da superfície (m)
    --sfc-t 21 --sfc-td 19       T e Td de superfície (ex.: METAR) para a parcela e o orvalho
    --sfc-p 915                  pressão de superfície (hPa), usada só se a sonda não medir pressão
    --config astro_config.toml   limiares das regras
    --no-descent                 não usar a descida para preencher os níveis baixos
    --pdf / --json / --png       caminhos de saída (padrão: derivados do nome do log)
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Constantes físicas
# --------------------------------------------------------------------------
RD = 287.04      # J/(kg K)
CP = 1005.7      # J/(kg K)
LV = 2.501e6     # J/kg
G = 9.80665      # m/s2
EPS = 0.622
KAPPA = RD / CP
MS2KT = 1.943844

# Unidade de vento nas saídas: "km/h" (padrão), "m/s" ou "kt"
WIND_UNIT = "km/h"
_WIND_FACTOR = {"km/h": 3.6, "m/s": 1.0, "kt": MS2KT}
# Barbelas no Skew-T por unidade: (meia, cheia, flâmula)
_BARB_INC = {"km/h": (10, 20, 100), "m/s": (2.5, 5, 25), "kt": (5, 10, 50)}


def wind(ms, digits=0):
    """Formata uma velocidade em m/s na unidade configurada."""
    return f"{ms * _WIND_FACTOR[WIND_UNIT]:.{digits}f} {WIND_UNIT}"

# --------------------------------------------------------------------------
# Configuração padrão (sobrescreva em um arquivo TOML com as mesmas chaves)
# --------------------------------------------------------------------------
DEFAULTS = {
    "processamento": {
        "bin_m": 100.0,             # espessura das camadas de média (m)
        "camada_cisalhamento_m": 500.0,
        "p_topo_hpa": 100.0,        # topo usado para seeing/estabilidade (ignora estratosfera)
    },
    "nuvens": {
        # Água líquida/mista (T >= limite_gelo_c): UR em relação à água
        "limite_gelo_c": -20.0,
        "ur_agua_provavel": 93.0,
        "ur_agua_possivel": 85.0,
        # Gelo (T < limite_gelo_c): UR em relação ao gelo
        "ur_gelo_provavel": 100.0,
        "ur_gelo_possivel": 90.0,
        "espessura_min_m": 200.0,
    },
    "transparencia": {
        "desconto_baixa": 1.5,      # nuvem provável p > 700 hPa
        "desconto_media": 2.0,      # 700–400 hPa
        "desconto_alta": 1.0,       # p < 400 hPa (cirrus)
        "fator_possivel": 0.4,      # fração do desconto se só "possível"
        "pw_mm": [[35.0, 0.5], [45.0, 1.0]],      # água precipitável
        "ur_baixa_media": [90.0, 0.5],            # UR média no 1º km
    },
    "seeing": {
        "jato_ms": [[25.0, 0.5], [35.0, 1.0], [50.0, 2.0]],
        "cisalhamento_ms_km": [[8.0, 0.5], [12.0, 1.0], [18.0, 1.5]],
        "ri_camadas": [[1, 0.5], [3, 1.0]],       # nº de camadas com Ri < 0,25
        "cape_jkg": [[300.0, 0.5], [1000.0, 1.0]],
        "k_index": [[35.0, 0.5]],                 # ambiente convectivo (outflows, nuvens)
        "agl_min_m": 1000.0,                      # ignora camada limite
        "vento_local_ms": [[8.0, 0.5], [12.0, 1.0]],  # vento na altitude do observatório
    },
    "chuva": {
        "cape_jkg": [[500.0, 1], [1500.0, 2]],
        "pw_mm": [[40.0, 1]],
        "ur_700_500": [[80.0, 1]],
        "k_index": [[30.0, 1], [35.0, 2]],
    },
    "orvalho": {"alto_c": 2.0, "moderado_c": 4.0},
    "lua": {"limite_ceu_profundo": 0.35},
    "unidades": {"vento": "km/h"},        # "km/h", "m/s" ou "kt"
    "satelite": {
        "horizonte_h": 6.0,        # peso do satélite cai a zero após este intervalo até o alvo
        "dist_max_km": 20.0,       # aviso se o JSON do satélite for de outro ponto
        "peso_min_l2": 0.5,        # peso mínimo do satélite para usar CAPE (DSI) e TPW
        "cobertura_min_l2": 0.3,   # fração mínima de pixels válidos em 50 km
        "lst_orvalho_c": 2.0,      # LST − Td abaixo disso → alerta de orvalho
    },
}


def deep_update(base: dict, new: dict) -> dict:
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v
    return base


def step_penalty(value, table):
    """Retorna o desconto do maior limiar ultrapassado em [[limiar, desconto], ...]."""
    pen = 0
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0
    for thr, p in table:
        if value >= thr:
            pen = p
    return pen


# --------------------------------------------------------------------------
# Termodinâmica
# --------------------------------------------------------------------------
def es_water(t_c):
    return 6.112 * np.exp(17.67 * t_c / (t_c + 243.5))


def es_ice(t_c):
    return 6.112 * np.exp(22.46 * t_c / (t_c + 272.62))


def dewpoint_from_rh(t_c, rh):
    rh = np.clip(rh, 0.5, 100.0)
    gam = np.log(rh / 100.0) + 17.67 * t_c / (t_c + 243.5)
    return 243.5 * gam / (17.67 - gam)


def mixing_ratio(e, p):
    return EPS * e / (p - e)


def virtual_temp_k(t_k, r):
    return t_k * (1 + r / EPS) / (1 + r)


def theta_e_bolton(p, t_c, td_c):
    t = t_c + 273.15
    td = td_c + 273.15
    r = mixing_ratio(es_water(td_c), p)
    tl = 1.0 / (1.0 / (td - 56.0) + np.log(t / td) / 800.0) + 56.0
    th_l = t * (1000.0 / (p - es_water(td_c))) ** KAPPA * (t / tl) ** (0.28 * r)
    return th_l * np.exp((3036.0 / tl - 1.78) * r * (1 + 0.448 * r))


def lcl(p0, t0_c, td0_c):
    t = t0_c + 273.15
    td = td0_c + 273.15
    tl = 1.0 / (1.0 / (td - 56.0) + math.log(t / td) / 800.0) + 56.0
    pl = p0 * (tl / t) ** (1.0 / KAPPA)
    return pl, tl - 273.15


def moist_lapse_dTdp(p, t_k):
    rs = mixing_ratio(es_water(t_k - 273.15), p)
    num = RD * t_k + LV * rs
    den = CP + (LV * LV * rs * EPS) / (RD * t_k * t_k)
    return num / (den * p)


def moist_adiabat(p_start, t_start_c, p_levels):
    """Integra a pseudoadiabática (RK4) de p_start até cada nível (decrescente)."""
    out = np.empty(len(p_levels))
    p = p_start
    t = t_start_c + 273.15
    for i, pt in enumerate(p_levels):
        while p - pt > 1e-6:
            h = -min(5.0, p - pt)
            k1 = moist_lapse_dTdp(p, t)
            k2 = moist_lapse_dTdp(p + h / 2, t + h / 2 * k1)
            k3 = moist_lapse_dTdp(p + h / 2, t + h / 2 * k2)
            k4 = moist_lapse_dTdp(p + h, t + h * k3)
            t += h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
            p += h
        out[i] = t - 273.15
    return out


@dataclass
class ParcelResult:
    name: str
    p0: float
    t0: float
    td0: float
    p_lcl: float = float("nan")
    p_lfc: float = float("nan")
    p_el: float = float("nan")
    cape: float = 0.0
    cin: float = 0.0
    li: float = float("nan")
    tp: np.ndarray = field(default_factory=lambda: np.array([]))
    pp: np.ndarray = field(default_factory=lambda: np.array([]))


def parcel_analysis(name, p, t, td, p0, t0, td0):
    """CAPE/CIN com correção de temperatura virtual. p decrescente."""
    res = ParcelResult(name, p0, t0, td0)
    m = p <= p0
    pp = np.concatenate([[p0], p[m]])
    te = np.concatenate([[t0], t[m]])
    tde = np.concatenate([[td0], td[m]])
    if len(pp) < 3:
        return res
    p_lcl, t_lcl = lcl(p0, t0, td0)
    r0 = mixing_ratio(es_water(td0), p0)
    tp = np.empty(len(pp))
    dry = pp >= p_lcl
    tp[dry] = (t0 + 273.15) * (pp[dry] / p0) ** KAPPA - 273.15
    if (~dry).any():
        tp[~dry] = moist_adiabat(p_lcl, t_lcl, pp[~dry])
    rp = np.where(dry, r0, mixing_ratio(es_water(tp), pp))
    tvp = virtual_temp_k(tp + 273.15, rp)
    tve = virtual_temp_k(te + 273.15, mixing_ratio(es_water(tde), pp))
    buoy = tvp - tve
    lnp = np.log(pp)
    # Índices dos cruzamentos acima do NCL
    above = np.where(pp <= p_lcl)[0]
    lfc_i = el_i = None
    if len(above):
        pos = above[buoy[above] > 0]
        if len(pos):
            lfc_i = pos[0]
            el_i = pos[-1]
    cape = cin = 0.0
    for i in range(len(pp) - 1):
        seg = RD * 0.5 * (buoy[i] + buoy[i + 1]) * (lnp[i] - lnp[i + 1])
        if lfc_i is not None and lfc_i <= i < el_i:
            cape += max(seg, 0.0)
        elif lfc_i is None or i < lfc_i:
            cin += min(seg, 0.0)
    res.p_lcl = p_lcl
    if lfc_i is not None:
        res.p_lfc = pp[lfc_i]
        res.p_el = pp[el_i]
    else:
        cin = float("nan")  # sem LFC: CIN indefinido
    res.cape = cape
    res.cin = cin
    if pp.min() <= 500 <= pp.max():
        x = -lnp
        res.li = float(np.interp(-math.log(500), x, te) - np.interp(-math.log(500), x, tp))
    res.tp, res.pp = tp, pp
    return res


# --------------------------------------------------------------------------
# Leitura e preparação do perfil
# --------------------------------------------------------------------------
def hypsometric_pressure(alt, t_c, rh, p_start):
    p = np.empty(len(alt))
    p[0] = p_start
    for i in range(1, len(alt)):
        tk = t_c[i - 1] + 273.15
        r = mixing_ratio(es_water(t_c[i - 1]) * rh[i - 1] / 100, p[i - 1])
        tv = virtual_temp_k(tk, r)
        p[i] = p[i - 1] * math.exp(-G * (alt[i] - alt[i - 1]) / (RD * tv))
    return p


def std_atm_pressure(z):
    return 1013.25 * (1 - 2.25577e-5 * z) ** 5.25588


def load_log(path: Path, args, cfg):
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    notes = []
    serial = str(df["serial"].iloc[0])
    stype = df["type"].mode().iloc[0]
    burst_i = df["alt"].idxmax()
    burst = df.loc[burst_i]

    valid = (df["temp"] > -150) & (df["humidity"] >= 0) & (df["humidity"] <= 100.5)
    has_p = (df["pressure"] > 0).sum() > 0.5 * valid.sum()

    asc = df[valid & (df.index <= burst_i) & (df["vel_v"] > 0)].copy()
    asc["fonte"] = "subida"
    desc = df[valid & (df.index > burst_i)].copy()
    desc["fonte"] = "descida"

    if not has_p:
        notes.append("A sonda não mediu pressão: pressão calculada pela equação hipsométrica "
                     + ("a partir da pressão de superfície informada." if args.sfc_p
                        else "a partir da atmosfera padrão (informe --sfc-p para mais precisão)."))
        for part in (asc, desc):
            if len(part):
                part.sort_values("alt", inplace=True)
                z0 = part["alt"].iloc[0]
                p0 = std_atm_pressure(z0)
                if args.sfc_p and args.sfc_elev is not None:
                    p0 = args.sfc_p * math.exp(-G * (z0 - args.sfc_elev) / (RD * 290.0))
                part["pressure"] = hypsometric_pressure(part["alt"].values, part["temp"].values,
                                                        part["humidity"].values, p0)
    else:
        asc = asc[asc["pressure"] > 0]
        desc = desc[desc["pressure"] > 0]

    # Sondagem incompleta: sem burst (ainda subindo no último frame) ou topo baixo
    last_rows = df.index[-1] - burst_i
    if last_rows < 30 and df["vel_v"].iloc[-30:].median() > 1.0:
        notes.append(f"ATENÇÃO: sondagem INCOMPLETA — a sonda ainda subia no último registro "
                     f"({float(df['alt'].iloc[-1]):.0f} m). Níveis acima disso não foram medidos.")
    elif float(burst["alt"]) < 15000:
        notes.append(f"ATENÇÃO: topo da sondagem em apenas {float(burst['alt']):.0f} m; níveis altos "
                     "(cirrus, jato) sem dados.")

    parts = [asc]
    asc_min = asc["alt"].min()
    used_desc = False
    if not args.no_descent and len(desc):
        low = desc[desc["alt"] < asc_min - 50]
        if len(low) > 20:
            parts.append(low)
            used_desc = True
            t_low = low["timestamp"].median()
            notes.append(f"A subida só tem dados válidos a partir de {asc_min:.0f} m "
                         f"({asc['pressure'].max():.0f} hPa). Os níveis abaixo foram preenchidos com a "
                         f"DESCIDA da sonda (~{t_low:%H:%MZ}, a alguns km do ponto de lançamento).")
    if not used_desc:
        notes.append(f"Perfil começa em {asc_min:.0f} m ({asc['pressure'].max():.0f} hPa); "
                     "não há dados abaixo disso.")
    raw = pd.concat(parts)

    # Vento: o 'heading' é o rumo do deslocamento → o vento VEM de heading+180
    wdir = np.deg2rad((raw["heading"].values + 180.0) % 360.0)
    spd = raw["vel_h"].values
    raw["u"] = -spd * np.sin(wdir)
    raw["v"] = -spd * np.cos(wdir)

    # Média em camadas de altitude
    b = cfg["processamento"]["bin_m"]
    raw["bin"] = (raw["alt"] // b) * b + b / 2
    agg = raw.groupby("bin").agg(p=("pressure", "median"), t=("temp", "median"),
                                 rh=("humidity", "median"), u=("u", "mean"), v=("v", "mean"),
                                 fonte=("fonte", "first"), n=("temp", "size")).reset_index()
    agg = agg[agg["n"] >= 3].rename(columns={"bin": "z"})
    agg = agg.sort_values("p", ascending=False).reset_index(drop=True)
    # remove eventuais inversões de pressão (ruído) mantendo p estritamente decrescente
    keep = [0]
    for i in range(1, len(agg)):
        if agg.loc[i, "p"] < agg.loc[keep[-1], "p"]:
            keep.append(i)
    agg = agg.loc[keep].reset_index(drop=True)

    agg["td"] = dewpoint_from_rh(agg["t"], agg["rh"])
    agg["rhi"] = agg["rh"] * es_water(agg["t"]) / es_ice(agg["t"])
    agg["spd"] = np.hypot(agg["u"], agg["v"])
    agg["dir"] = (np.rad2deg(np.arctan2(-agg["u"], -agg["v"])) + 360) % 360
    agg["theta"] = (agg["t"] + 273.15) * (1000 / agg["p"]) ** KAPPA
    r = mixing_ratio(es_water(agg["td"]), agg["p"])
    agg["thetav"] = agg["theta"] * (1 + r / EPS) / (1 + r)
    agg["r"] = r

    meta = dict(serial=serial, type=stype, launch=asc["timestamp"].min(),
                burst_alt=float(burst["alt"]), burst_time=burst["timestamp"],
                burst_p=float(burst["pressure"]) if burst["pressure"] > 0 else float("nan"),
                lat=float(asc["lat"].iloc[0]), lon=float(asc["lon"].iloc[0]),
                has_pressure=bool(has_p), used_descent=used_desc, asc_min_alt=float(asc_min))
    return agg, meta, notes


# --------------------------------------------------------------------------
# Análises
# --------------------------------------------------------------------------
def interp_at(prof, p_target, col):
    p = prof["p"].values
    if not (p.min() <= p_target <= p.max()):
        return float("nan")
    return float(np.interp(-math.log(p_target), -np.log(p), prof[col].values))


def find_cloud_layers(prof, cfg):
    c = cfg["nuvens"]
    liquid = prof["t"] >= c["limite_gelo_c"]
    prov = np.where(liquid, prof["rh"] >= c["ur_agua_provavel"], prof["rhi"] >= c["ur_gelo_provavel"])
    poss = np.where(liquid, prof["rh"] >= c["ur_agua_possivel"], prof["rhi"] >= c["ur_gelo_possivel"])
    layers = []
    flag = np.where(prov, 2, np.where(poss, 1, 0))
    cls = np.where(prof["p"] > 700, 0, np.where(prof["p"] > 400, 1, 2))  # baixa/média/alta
    i, n = 0, len(prof)
    while i < n:
        if flag[i] == 0:
            i += 1
            continue
        j = i
        while (j + 1 < n and cls[j + 1] == cls[i]
               and (flag[j + 1] > 0 or (j + 2 < n and flag[j + 2] > 0 and cls[j + 2] == cls[i]))):
            j += 1
        seg = prof.iloc[i:j + 1]
        thick = float(seg["z"].max() - seg["z"].min() + cfg["processamento"]["bin_m"])
        if thick >= c["espessura_min_m"]:
            frac_prov = float((flag[i:j + 1] == 2).mean())
            nivel = ("baixa", "média", "alta")[cls[i]]
            fase = "gelo" if seg["t"].median() < c["limite_gelo_c"] else "água/mista"
            layers.append(dict(
                base_m=float(seg["z"].min()), topo_m=float(seg["z"].max()),
                p_base=float(seg["p"].max()), p_topo=float(seg["p"].min()),
                espessura_m=thick, nivel=nivel, fase=fase,
                status="provável" if frac_prov >= 0.5 else "possível",
                ur_max=float(seg["rh"].max()), uri_max=float(seg["rhi"].max()),
                fonte=",".join(sorted(set(seg["fonte"])))))
        i = j + 1
    return layers


def shear_and_ri(prof, sfc_elev, cfg):
    dz = cfg["processamento"]["camada_cisalhamento_m"]
    z = prof["z"].values
    out = []
    edges = np.arange(z.min(), z.max() - dz, dz)
    for z0 in edges:
        z1 = z0 + dz
        def at(col, zz):
            return float(np.interp(zz, z, prof[col].values))
        du = at("u", z1) - at("u", z0)
        dv = at("v", z1) - at("v", z0)
        dth = at("thetav", z1) - at("thetav", z0)
        thm = 0.5 * (at("thetav", z1) + at("thetav", z0))
        shear = math.hypot(du, dv) / dz          # 1/s
        n2 = G / thm * dth / dz
        ri = n2 / shear ** 2 if shear > 1e-4 else float("inf")
        out.append(dict(z0=z0, z1=z1, p0=at("p", z0), p1=at("p", z1),
                        shear_ms_km=shear * 1000, ri=ri, n2=n2, agl0=z0 - sfc_elev))
    return pd.DataFrame(out)


def lapse_features(prof, sfc_elev):
    z = prof["z"].values
    t = prof["t"].values
    feats = []
    # inversões / camadas isotérmicas (≥ 200 m)
    dtdz = np.gradient(t, z) * 1000  # K/km
    i, n = 0, len(z)
    while i < n:
        if dtdz[i] > -0.5:
            j = i
            while j + 1 < n and dtdz[j + 1] > -0.5:
                j += 1
            if z[j] - z[i] >= 200:
                kind = "inversão" if t[j] - t[i] > 0.3 else "quase isotérmica"
                feats.append(dict(tipo=kind, base_m=float(z[i]), topo_m=float(z[j]),
                                  p_base=float(prof["p"].iloc[i]), p_topo=float(prof["p"].iloc[j]),
                                  dT=float(t[j] - t[i])))
            i = j + 1
        else:
            i += 1
    # tropopausa (critério OMM simplificado)
    trop = None
    for k in range(n):
        if prof["p"].iloc[k] > 500:
            continue
        if dtdz[k] > -2.0:
            m = (z > z[k]) & (z <= z[k] + 2000)
            if m.sum() >= 3 and np.all((t[m] - t[k]) / ((z[m] - z[k]) / 1000) > -2.0):
                trop = dict(z=float(z[k]), p=float(prof["p"].iloc[k]), t=float(t[k]))
                break
    return feats, trop


def precipitable_water(prof):
    p = prof["p"].values * 100
    r = prof["r"].values
    q = r / (1 + r)
    return float(np.trapezoid(q[::-1], p[::-1]) / G)  # kg/m2 = mm


def moon_illumination(dt: datetime):
    """Fração iluminada aproximada (±5%) e idade da Lua em dias."""
    ref = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    syn = 29.530588853
    age = ((dt - ref).total_seconds() / 86400.0) % syn
    return (1 - math.cos(2 * math.pi * age / syn)) / 2, age


# --------------------------------------------------------------------------
# Regras de pontuação
# --------------------------------------------------------------------------
def score_all(prof, layers, shear_df, parcels, pw, sfc, moon_frac, cfg, sfc_elev, obs=None, cape_sat=None):
    rep = {"transparencia": {}, "seeing": {}, "chuva": {}, "orvalho": {}}
    # Transparência
    tcfg = cfg["transparencia"]
    score = 5.0
    det = []
    for L in layers:
        base = {"baixa": tcfg["desconto_baixa"], "média": tcfg["desconto_media"],
                "alta": tcfg["desconto_alta"]}[L["nivel"]]
        f = 1.0 if L["status"] == "provável" else tcfg["fator_possivel"]
        f *= min(1.0, L["espessura_m"] / 1000.0) ** 0.5
        d = base * f
        score -= d
        det.append(f"nuvem {L['nivel']} {L['status']} {L['p_base']:.0f}–{L['p_topo']:.0f} hPa: −{d:.1f}")
    d = step_penalty(pw, tcfg["pw_mm"])
    if d:
        score -= d
        det.append(f"água precipitável {pw:.0f} mm: −{d:.1f}")
    low = prof[prof["z"] <= sfc_elev + 1000]
    if len(low):
        rhm = float(low["rh"].mean())
        if rhm >= tcfg["ur_baixa_media"][0]:
            score -= tcfg["ur_baixa_media"][1]
            det.append(f"UR média {rhm:.0f}% no 1º km: −{tcfg['ur_baixa_media'][1]:.1f}")
    rep["transparencia"] = dict(nota=int(max(1, min(5, math.floor(score + 0.5)))), bruto=score, detalhes=det)

    # Seeing
    scfg = cfg["seeing"]
    score = 5.0
    det = []
    upper = prof[(prof["p"] <= 400) & (prof["p"] >= 150)]
    jet = float(upper["spd"].max()) if len(upper) else float("nan")
    jet_p = float(upper.loc[upper["spd"].idxmax(), "p"]) if len(upper) else float("nan")
    d = step_penalty(jet, scfg["jato_ms"])
    if d:
        score -= d
    det.append(f"vento máx. 400–150 hPa: {wind(jet)} em {jet_p:.0f} hPa" + (f": −{d:.1f}" if d else ""))
    free = shear_df[shear_df["agl0"] >= scfg["agl_min_m"]]
    smax = float(free["shear_ms_km"].max()) if len(free) else float("nan")
    srow = free.loc[free["shear_ms_km"].idxmax()] if len(free) else None
    d = step_penalty(smax, scfg["cisalhamento_ms_km"])
    if d:
        score -= d
    if srow is not None:
        det.append(f"cisalhamento máx.: {smax:.1f} m/s/km entre {srow.p0:.0f} e {srow.p1:.0f} hPa"
                   + (f": −{d:.1f}" if d else ""))
    nri = int((free["ri"] < 0.25).sum()) if len(free) else 0
    d = step_penalty(nri, scfg["ri_camadas"])
    if d:
        score -= d
    det.append(f"camadas com Ri < 0,25 (acima de {scfg['agl_min_m']:.0f} m AGL): {nri}" + (f": −{d:.1f}" if d else ""))
    mu = max(parcels.values(), key=lambda r: r.cape)
    cape_use = mu.cape if cape_sat is None else cape_sat
    cape_lbl = "CAPE" if cape_sat is None else "CAPE do satélite (DSI)"
    d = step_penalty(cape_use, scfg["cape_jkg"])
    if d:
        score -= d
        det.append(f"instabilidade ({cape_lbl} {cape_use:.0f} J/kg): −{d:.1f}")
    if obs and obs.get("vento_ms") is not None:
        d = step_penalty(obs["vento_ms"], scfg["vento_local_ms"])
        if d:
            score -= d
        det.append(f"vento a {obs['elev']:.0f} m (observatório): {wind(obs['vento_ms'])} de "
                   f"{obs['dir']:.0f}°" + (f": −{d:.1f}" if d else ""))
    kk = k_index(prof)
    d = step_penalty(kk, scfg["k_index"])
    if d:
        score -= d
        det.append(f"ambiente convectivo (índice K {kk:.0f}): −{d:.1f}")
    rep["seeing"] = dict(nota=int(max(1, min(5, math.floor(score + 0.5)))), bruto=score, detalhes=det,
                         jato_ms=jet, jato_p=jet_p, cis_max=smax, n_ri=nri)

    # Chuva
    ccfg = cfg["chuva"]
    pts = 0
    det = []
    for label, val, table in [(cape_lbl, cape_use, ccfg["cape_jkg"]), ("PW (mm)", pw, ccfg["pw_mm"]),
                              ("UR média 700–500 hPa (%)", rep_ur(prof, 700, 500), ccfg["ur_700_500"]),
                              ("Índice K", k_index(prof), ccfg["k_index"])]:
        p = step_penalty(val, table)
        pts += p
        det.append(f"{label}: {val:.0f}" + (f" (+{p})" if p else ""))
    nivel = "baixo" if pts <= 1 else ("moderado" if pts <= 3 else "alto")
    rep["chuva"] = dict(nivel=nivel, pontos=pts, detalhes=det)

    # Orvalho
    spread = sfc["t"] - sfc["td"]
    o = cfg["orvalho"]
    nivel = "muito alto" if spread <= 1 else ("alto" if spread <= o["alto_c"] else
                                              ("moderado" if spread <= o["moderado_c"] else "baixo"))
    rep["orvalho"] = dict(nivel=nivel, spread=spread, **sfc)

    rep["veredito"] = verdict(rep["transparencia"]["nota"], rep["seeing"]["nota"], moon_frac, cfg)
    return rep


def verdict(T, S, moon_frac, cfg):
    lim = cfg["lua"]["limite_ceu_profundo"]
    if T >= 4 and moon_frac < lim:
        dso = ("Favorável", "Céu limpo e Lua fraca.")
    elif T >= 4:
        dso = ("Marginal", f"Céu bom, mas a Lua está {moon_frac:.0%} iluminada: prefira banda estreita (narrowband).")
    elif T == 3:
        dso = ("Marginal", "Nebulosidade parcial/umidade: integrações curtas e descarte de subs.")
    else:
        dso = ("Desfavorável", "Nuvens e umidade comprometem a transparência.")
    if S >= 4 and T >= 2:
        pla = ("Favorável", "Atmosfera estável; aproveite alta ampliação.")
    elif S >= 3 and T >= 2:
        pla = ("Marginal", "Seeing médio; ampliações moderadas e lucky imaging.")
    elif S >= 2 and T >= 2:
        pla = ("Marginal", "Só visual/oportunista, em brechas.")
    else:
        pla = ("Desfavorável", "Turbulência e/ou nuvens.")
    if "Favorável" in (dso[0], pla[0]):
        mont = ("Sim", "Há janela útil para ao menos uma modalidade.")
    elif "Marginal" in (dso[0], pla[0]):
        mont = ("Oportunista", "Só um setup leve e rápido, conferindo o satélite antes.")
    else:
        mont = ("Não", "Não compensa montar o equipamento principal.")
    return dict(ceu_profundo=dso, planetaria=pla, montar=mont)


def rep_ur(prof, p1, p2):
    m = (prof["p"] <= p1) & (prof["p"] >= p2)
    return float(prof.loc[m, "rh"].mean()) if m.any() else float("nan")


def k_index(prof):
    t850, t700, t500 = (interp_at(prof, p, "t") for p in (850, 700, 500))
    td850, td700 = (interp_at(prof, p, "td") for p in (850, 700))
    return (t850 - t500) + td850 - (t700 - td700)


# --------------------------------------------------------------------------
# Skew-T
# --------------------------------------------------------------------------
SKEW = 35.0


def skx(t, p):
    return t + SKEW * np.log(1000.0 / np.asarray(p))


def plot_skewt(prof, parcel, meta, title, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FixedLocator, NullLocator, FixedFormatter

    fig = plt.figure(figsize=(8.2, 8.6), dpi=130)
    ax = fig.add_axes([0.09, 0.07, 0.74, 0.86])
    axb = fig.add_axes([0.84, 0.07, 0.10, 0.86], sharey=ax)
    pgrid = np.geomspace(1050, 100, 120)
    ax.set_yscale("log")
    for T0 in range(-130, 60, 10):
        ax.plot(skx(T0, pgrid), pgrid, color="#b0b0b0", lw=0.6, zorder=1)
    for th in range(250, 470, 10):
        ax.plot(skx(th * (pgrid / 1000) ** KAPPA - 273.15, pgrid), pgrid, color="#d9a07a", lw=0.5, ls="--", zorder=1)
    for T0 in range(-10, 40, 5):
        pm = pgrid[pgrid <= 1000]
        ax.plot(skx(moist_adiabat(1000, T0, pm), pm), pm, color="#7fb08a", lw=0.5, ls=":", zorder=1)
    ax.axvline(0, color="none")
    ax.plot(skx(np.zeros_like(pgrid), pgrid), pgrid, color="#4a90d9", lw=1.0, zorder=2)  # 0 °C

    for fonte, ls, a in (("subida", "-", 1.0), ("descida", "--", 0.8)):
        s = prof[prof["fonte"] == fonte]
        if len(s):
            ax.plot(skx(s["t"], s["p"]), s["p"], color="#d62728", lw=1.8, ls=ls, alpha=a, zorder=4,
                    label=f"T ({fonte})")
            ax.plot(skx(s["td"], s["p"]), s["p"], color="#2ca02c", lw=1.8, ls=ls, alpha=a, zorder=4,
                    label=f"Td ({fonte})")
    if parcel is not None and len(parcel.pp):
        ax.plot(skx(parcel.tp, parcel.pp), parcel.pp, color="black", lw=1.1, ls="-.", zorder=3,
                label=f"parcela {parcel.name}")
    ax.set_ylim(1050, 100)
    ax.set_xlim(-40, 50)
    ticks = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100]
    ax.yaxis.set_major_locator(FixedLocator(ticks))
    ax.yaxis.set_major_formatter(FixedFormatter([str(t) for t in ticks]))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_ylabel("Pressão (hPa)")
    ax.set_xlabel("Temperatura (°C) — eixo inclinado")
    ax.grid(axis="y", color="#cccccc", lw=0.5)
    ax.legend(loc="upper right", fontsize=7, framealpha=0.9)
    ax.set_title(title, fontsize=10)

    # barbelas a cada ~25 hPa, na unidade configurada
    targets = np.arange(1000, 99, -25)
    ps, us, vs = [], [], []
    for pt in targets:
        if prof["p"].min() <= pt <= prof["p"].max():
            i = (prof["p"] - pt).abs().idxmin()
            ps.append(prof.loc[i, "p"])
            us.append(prof.loc[i, "u"] * _WIND_FACTOR[WIND_UNIT])
            vs.append(prof.loc[i, "v"] * _WIND_FACTOR[WIND_UNIT])
    half, full, flag = _BARB_INC[WIND_UNIT]
    axb.barbs(np.zeros(len(ps)), ps, us, vs, length=5.5, lw=0.7,
              barb_increments=dict(half=half, full=full, flag=flag))
    axb.set_xlim(-1, 1)
    axb.set_xticks([])
    axb.tick_params(labelleft=False)
    axb.set_title(f"vento ({WIND_UNIT})", fontsize=8)
    axb.text(0, 1.035, f"meia={half:g} cheia={full:g}\nflâmula={flag:g}", transform=axb.transAxes,
             fontsize=5.5, ha="center", va="bottom")
    for s in ("top", "right", "left", "bottom"):
        axb.spines[s].set_visible(False)
    fig.savefig(out_png)
    plt.close(fig)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def find_font():
    import matplotlib
    cands = [Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu-sans-fonts"),
             Path("/usr/share/fonts/dejavu"), Path(matplotlib.get_data_path()) / "fonts/ttf"]
    for d in cands:
        if (d / "DejaVuSans.ttf").exists() and (d / "DejaVuSans-Bold.ttf").exists():
            return d
    return None


def build_pdf(out_pdf, png, ctx):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    fdir = find_font()
    if fdir:
        pdfmetrics.registerFont(TTFont("DV", str(fdir / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DVB", str(fdir / "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("DV", normal="DV", bold="DVB", italic="DV", boldItalic="DVB")
        reg, bold, star_on, star_off = "DV", "DVB", "★", "★"
    else:
        reg, bold, star_on, star_off = "Helvetica", "Helvetica-Bold", "*", "·"

    NAVY = colors.HexColor("#1b2a4a")
    ACC = colors.HexColor("#3b6ea5")
    LIGHT = colors.HexColor("#eef2f7")
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["Normal"], fontName=reg, fontSize=9.4, leading=13, spaceAfter=4)
    small = ParagraphStyle("s", parent=body, fontSize=7.8, leading=10, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=body, fontName=bold, fontSize=17, leading=21, textColor=NAVY)
    sub = ParagraphStyle("sub", parent=body, fontSize=9.5, textColor=ACC, spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=body, fontName=bold, fontSize=12, leading=15, textColor=NAVY,
                        spaceBefore=9, spaceAfter=4)
    cell = ParagraphStyle("c", parent=body, fontSize=8, leading=10, spaceAfter=0)
    cellb = ParagraphStyle("cb", parent=cell, fontName=bold, textColor=colors.white)

    def P(t, s=body):
        return Paragraph(t, s)

    def B(t):
        return Paragraph(t, ParagraphStyle("bl", parent=body, leftIndent=12, bulletIndent=2), bulletText="•")

    def table(rows, widths):
        data = [[P(str(c), cellb if i == 0 else cell) for c in r] for i, r in enumerate(rows)]
        t = Table(data, colWidths=widths, repeatRows=1)
        st = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c5cfdb")),
              ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "TOP")]
        st += [("BACKGROUND", (0, i), (-1, i), LIGHT) for i in range(2, len(rows), 2)]
        t.setStyle(TableStyle(st))
        return t

    def box(text, color):
        t = Table([[P(text)]], colWidths=[17 * cm])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                               ("LINEBEFORE", (0, 0), (0, -1), 4, color),
                               ("LEFTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 6),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        return t

    def stars(n):
        return (f"<font color='#c98a12'>{star_on * n}</font>"
                f"<font color='#bbbbbb'>{star_off * (5 - n)}</font>")

    c = ctx
    rep = c["rep"]
    T, S = rep["transparencia"]["nota"], rep["seeing"]["nota"]
    color = {"Sim": colors.HexColor("#2e7d4f"), "Oportunista": colors.HexColor("#c98a12"),
             "Não": colors.HexColor("#b03a2e")}[rep["veredito"]["montar"][0]]
    if c.get("sat"):
        titulo = "Previsão Astronômica — Sondagem + Satélite GOES-19"
        fontes = (f"Fontes: radiossonda {c['meta']['serial']} ({c['meta']['launch']:%d/%m %H:%MZ}) e "
                  f"GOES-19 ({len(c['sat']['quadros'])} quadro(s), último "
                  f"{c['sat']['ultimo_quadro'][8:10]}/{c['sat']['ultimo_quadro'][5:7]} "
                  f"{c['sat']['ultimo_quadro'][11:16]}Z, peso {c['rep']['transparencia'].get('peso_sat', 0):.0%} "
                  "na transparência)")
    else:
        titulo = "Previsão Astronômica — Análise de Sondagem"
        fontes = (f"Fonte: apenas radiossonda {c['meta']['serial']} ({c['meta']['launch']:%d/%m %H:%MZ}); "
                  "sem dados de satélite (use --sat-json)")
    s = [P(titulo, h1),
         P(f"{c['site']} · Alvo: {c['target']:%d/%m/%Y %H:%MZ} · Sonda {c['meta']['type']} "
           f"{c['meta']['serial']} (lançamento {c['meta']['launch']:%d/%m %H:%MZ})", sub),
         P(fontes, small), Spacer(1, 4),
         box(f"<b>Transparência {T}/5 · Seeing {S}/5 · Chuva: {rep['chuva']['nivel']} · "
             f"Orvalho: {rep['orvalho']['nivel']}</b><br/>"
             f"Montar o equipamento? <b>{rep['veredito']['montar'][0]}</b>: {rep['veredito']['montar'][1]}",
             color), Spacer(1, 5), P("Ressalvas", h2)]
    for n in c["notes"]:
        s.append(B(n))
    s.append(KeepTogether([Image(png, width=14.5 * cm, height=14.5 * cm * 8.6 / 8.2),
                           P("Skew-T gerado a partir do log (média em camadas de "
                             f"{c['cfg']['processamento']['bin_m']:.0f} m).", small)]))

    s.append(P("Índices", h2))
    pr = c["parcels"]
    rows = [["Parcela", "NCL (hPa)", "NCE/LFC (hPa)", "NE/EL (hPa)", "CAPE (J/kg)", "CIN (J/kg)", "LI (°C)"]]
    for r in pr.values():
        rows.append([f"{r.name} ({r.p0:.0f} hPa, T {r.t0:.1f} / Td {r.td0:.1f})", f"{r.p_lcl:.0f}",
                     "—" if math.isnan(r.p_lfc) else f"{r.p_lfc:.0f}",
                     "—" if math.isnan(r.p_el) else f"{r.p_el:.0f}", f"{r.cape:.0f}",
                     "—" if math.isnan(r.cin) else f"{r.cin:.0f}",
                     "—" if math.isnan(r.li) else f"{r.li:+.1f}"])
    s.append(table(rows, [5.4 * cm, 1.6 * cm, 2.1 * cm, 2 * cm, 2 * cm, 1.9 * cm, 2 * cm]))
    s.append(Spacer(1, 4))
    trop = c["trop"]
    s.append(P(f"Água precipitável: <b>{c['pw']:.0f} mm</b> · Índice K: <b>{c['k']:.0f}</b> · "
               f"Isoterma 0 °C: <b>{c['frz']}</b> · Tropopausa: <b>"
               + (f"{trop['p']:.0f} hPa ({trop['z'] / 1000:.1f} km, {trop['t']:.0f} °C)" if trop else "n/d")
               + f"</b> · Burst: {c['meta']['burst_alt'] / 1000:.1f} km"))

    s.append(P("Perfil nos níveis-padrão", h2))
    rows = [["hPa", "Alt (m)", "T (°C)", "Td (°C)", "T−Td", "UR água/gelo (%)", "Vento"]]
    for p in c["levels"]:
        rows.append(p)
    s.append(table(rows, [1.4 * cm, 1.8 * cm, 1.7 * cm, 1.7 * cm, 1.5 * cm, 3.2 * cm, 3.4 * cm]))

    s.append(P("1. Nebulosidade e chuva (transparência)", h2))
    if c["layers"]:
        rows = [["Camada", "Altitude (m)", "Espessura", "Nível / fase", "Status", "UR máx. água/gelo"]]
        for L in c["layers"]:
            rows.append([f"{L['p_base']:.0f}–{L['p_topo']:.0f} hPa", f"{L['base_m']:.0f}–{L['topo_m']:.0f}",
                         f"{L['espessura_m']:.0f} m", f"{L['nivel']} / {L['fase']}", f"<b>{L['status']}</b>",
                         f"{L['ur_max']:.0f}% / {L['uri_max']:.0f}%"])
        s.append(table(rows, [2.8 * cm, 2.6 * cm, 2.3 * cm, 3.4 * cm, 2.1 * cm, 3.8 * cm]))
    else:
        s.append(P("Nenhuma camada úmida significativa detectada."))
    s.append(Spacer(1, 3))
    for d in rep["transparencia"]["detalhes"]:
        s.append(B(d))
    s.append(P(f"<b>Risco de chuva/convecção: {rep['chuva']['nivel']}</b> ("
               + "; ".join(rep["chuva"]["detalhes"]) + ")."))

    sat = c.get("sat")
    if sat:
        s.append(P("Satélite GOES-19 (nowcasting)", h2))
        q = sat["quadros"][-1]
        rows = [["Raio", "Claro", "Nuvem/opaca", "Cirrus fino", "Nevoeiro/estrato"]]
        for R in sat["raios_km"]:
            st = q.get(f"r{R}")
            if st:
                rows.append([f"{R} km", f"{st['claro']:.0%}", f"{st['nuvem'] + st['opaca fria']:.0%}",
                             f"{st['cirrus fino']:.0%}", f"{st['nevoeiro/estrato']:.0%}"])
        s.append(table(rows, [2.5 * cm, 3 * cm, 3.5 * cm, 3.5 * cm, 4.5 * cm]))
        s.append(Spacer(1, 3))
        s.append(B(f"Último quadro: {sat['ultimo_quadro'][:16].replace('T', ' ')}Z "
                   f"({'noite' if q.get('noite') else 'dia'}), {len(sat['quadros'])} quadro(s); "
                   f"no local: {q['local']['classe']} (BT13 {q['local']['bt13_k'] - 273.15:.1f} °C)."))
        if sat.get("tendencia"):
            tdn = sat["tendencia"]
            s.append(B(f"Tendência da nebulosidade (50 km): {tdn['leitura']} ({tdn['pontos_por_h']:+.0f} pontos "
                       f"percentuais/h; série {', '.join(f'{x:.0%}' for x in tdn['nublado_50km'])})."))
        mv = sat.get("movimento")
        if mv:
            s.append(B(f"Deslocamento das nuvens: de {mv['dir_de']:.0f}° a {mv['vel_kmh']:.0f} km/h"
                       + ("" if mv["valido"] else " (pouco confiável: " + "; ".join(mv.get("motivos") or []) + ")")
                       + (f"; coerente com o vento da sonda em {mv['coerente_com']}" if mv.get("coerente_com") else "")
                       + "."))
        for k, v in (sat.get("previsoes") or {}).items():
            s.append(B(f"{k}: {v['evento']}" + (f" em ~{v['h']:.1f} h" if v.get("h") else "") + "."))
        tr = rep["transparencia"]
        s.append(P(f"<b>Transparência pelo satélite: {sat['transparencia_sat']['nota']}/5</b> · peso na nota final "
                   f"{tr.get('peso_sat', 0):.0%} (quadro a {sat['dh']:.1f} h do alvo) · nota final "
                   f"<b>{tr['nota']}/5</b> (sondagem {tr.get('sondagem')}/5)."))
        l2 = sat.get("l2") or {}
        if l2:
            rows = [["Produto L2", "Horário", "Raio", "Válidos", "p10 / mediana / p90"]]
            for pk in ("ACHA", "ACHT", "DSI", "TPW", "LST"):
                it = l2.get(pk)
                if not it:
                    continue
                R = "r25" if pk in ("ACHA", "ACHT", "LST") else "r50"
                st = it[R]
                un = it["unidade"]
                def fv(x, un=un):
                    if x is None:
                        return "—"
                    if un == "K":
                        return f"{x - 273.15:.1f} °C"
                    if un == "m":
                        return f"{x / 1000:.1f} km"
                    return f"{x:.0f} {un}"
                rows.append([f"{pk} — {it['nome']}", it["tempo"][11:16] + "Z", R[1:] + " km", f"{st['validos']:.0%}",
                             f"{fv(st['p10'])} / <b>{fv(st['mediana'])}</b> / {fv(st['p90'])}"])
            s.append(Spacer(1, 3))
            s.append(table(rows, [5.3 * cm, 1.9 * cm, 1.5 * cm, 1.6 * cm, 6.7 * cm]))
            cam = (l2.get("ACHA") or {}).get("camadas_25km")
            if cam:
                s.append(P(f"Topos das nuvens em 25 km (ACHA): abaixo de 3 km {cam['baixa_lt3km']:.0%} · "
                           f"3–7 km {cam['media_3_7km']:.0%} · acima de 7 km {cam['alta_ge7km']:.0%}. "
                           "DSI, TPW e LST só existem em céu claro; ACHA e ACHT só onde há nuvem.", small))
        mvc = sat.get("movimento") or {}
        ql_caption = ("Canal 13 com classificação (vermelho: opaca fria; laranja: nuvem; azul: cirrus fino; "
                      "amarelo: nevoeiro). Setas: de onde as nuvens chegariam ao observatório em 1,5 h")
        if mvc and mvc.get("valido"):
            ql_caption += " — verde contínua: deslocamento observado"
        elif mvc:
            ql_caption += " — cinza tracejada: estimativa pouco confiável"
        ql_caption += "; pontilhadas: vento da sonda em 700 hPa (ciano) e 250 hPa (magenta)."
        if sat.get("png") and Path(sat["png"]).exists():
            s.append(KeepTogether([Image(sat["png"], width=11 * cm, height=11 * cm),
                                   P(ql_caption, small)]))

    s.append(P("2. Seeing (turbulência)", h2))
    for d in rep["seeing"]["detalhes"]:
        s.append(B(d))
    if c["lapse"]:
        s.append(P("Camadas estáveis detectadas: " + "; ".join(
            f"{f['tipo']} {f['p_base']:.0f}–{f['p_topo']:.0f} hPa (ΔT {f['dT']:+.1f} °C)" for f in c["lapse"]) + "."))
    sh = c["shear"]
    if len(sh):
        top = sh[sh["agl0"] >= c["cfg"]["seeing"]["agl_min_m"]].nlargest(4, "shear_ms_km")
        rows = [["Camada", "Cisalhamento", "Ri", "Leitura"]]
        for _, r in top.iterrows():
            leit = "turbulência provável" if r.ri < 0.25 else ("marginal" if r.ri < 1 else "estável")
            rows.append([f"{r.p0:.0f}–{r.p1:.0f} hPa", f"{r.shear_ms_km:.1f} m/s/km",
                         "∞" if math.isinf(r.ri) else f"{r.ri:.2f}", leit])
        s.append(table(rows, [4 * cm, 3.5 * cm, 2.5 * cm, 7 * cm]))

    s.append(P("3. Orvalho no equipamento", h2))
    o = rep["orvalho"]
    s.append(P(f"Superfície ({o['origem']}): T = {o['t']:.1f} °C, Td = {o['td']:.1f} °C, diferença = "
               f"<b>{o['spread']:.1f} °C</b> → risco <b>{o['nivel']}</b>. À noite a diferença costuma "
               "diminuir; com risco moderado ou maior, use aquecedores e dew shield."))

    s.append(P("4. Veredito", h2))
    s.append(table([["Critério", "Nota"],
                    ["Transparência", f"{stars(T)}  <b>{T}/5</b>  (pontuação {rep['transparencia']['bruto']:.1f})"],
                    ["Seeing", f"{stars(S)}  <b>{S}/5</b>  (pontuação {rep['seeing']['bruto']:.1f})"]],
                   [4 * cm, 13 * cm]))
    s.append(Spacer(1, 5))
    v = rep["veredito"]
    s.append(table([["Modalidade", "Recomendação"],
                    ["Céu profundo (longa exposição)", f"<b>{v['ceu_profundo'][0]}</b>: {v['ceu_profundo'][1]}"],
                    ["Planetária/lunar (alta ampliação)", f"<b>{v['planetaria'][0]}</b>: {v['planetaria'][1]}"],
                    ["Montar o equipamento?", f"<b>{v['montar'][0]}</b>: {v['montar'][1]}"]],
                   [5.5 * cm, 11.5 * cm]))
    s.append(Spacer(1, 4))
    s.append(P(f"Lua: {c['moon'][0]:.0%} iluminada (idade {c['moon'][1]:.1f} dias) no horário-alvo.", small))
    s.append(P("Notas geradas por regras heurísticas (limiares em astro_config.toml). Confira satélite "
               "(GOES-19 IR/vapor d'água), radar e METAR antes de decidir.", small))

    def deco(cv, d):
        cv.saveState()
        cv.setFillColor(NAVY)
        cv.rect(0, A4[1] - 0.45 * cm, A4[0], 0.45 * cm, stroke=0, fill=1)
        cv.setFont(reg, 7.5)
        cv.setFillColor(colors.HexColor("#777777"))
        cv.drawString(2 * cm, 1.1 * cm, f"astro_sounding.py · {c['site']} · {c['meta']['serial']}")
        cv.drawRightString(A4[0] - 2 * cm, 1.1 * cm, f"Página {d.page}")
        cv.restoreState()

    doc = SimpleDocTemplate(str(out_pdf), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=1.5 * cm, bottomMargin=1.8 * cm,
                            title=f"Previsão astronômica {c['site']} {c['target']:%Y-%m-%d %HZ}")
    doc.build(s, onFirstPage=deco, onLaterPages=deco)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def parse_target(txt, launch):
    if txt:
        txt = txt.replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    nxt = (launch + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if launch.hour < 12:
        nxt = launch.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return nxt


def main():
    ap = argparse.ArgumentParser(description="Previsão astronômica a partir de log do radiosonde_auto_rx")
    ap.add_argument("log", type=Path)
    ap.add_argument("--site", default="SBCF (Confins/MG)")
    ap.add_argument("--target", help="horário-alvo ISO, ex. 2026-11-25T00:00Z")
    ap.add_argument("--sfc-elev", type=float, help="elevação da superfície (m)")
    ap.add_argument("--sfc-t", type=float, help="temperatura de superfície (°C)")
    ap.add_argument("--sfc-td", type=float, help="ponto de orvalho de superfície (°C)")
    ap.add_argument("--sfc-p", type=float, help="pressão de superfície (hPa)")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--no-descent", action="store_true")
    ap.add_argument("--lat", type=float, help="latitude do observatório")
    ap.add_argument("--lon", type=float, help="longitude do observatório")
    ap.add_argument("--obs-elev", type=float, help="altitude do observatório (m); condições lidas no perfil")
    ap.add_argument("--sat-json", type=Path, help="JSON do goes_nowcast.py")
    ap.add_argument("--wind-unit", choices=["km/h", "m/s", "kt"], help="unidade de vento nas saídas")
    ap.add_argument("--pdf", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--png", type=Path)
    args = ap.parse_args()

    cfg = json.loads(json.dumps(DEFAULTS))
    if args.config:
        with open(args.config, "rb") as fh:
            deep_update(cfg, tomllib.load(fh))
    global WIND_UNIT
    WIND_UNIT = cfg["unidades"]["vento"] if args.wind_unit is None else args.wind_unit
    if WIND_UNIT not in _WIND_FACTOR:
        sys.exit("unidade de vento inválida: use km/h, m/s ou kt")

    prof, meta, notes = load_log(args.log, args, cfg)
    if len(prof) < 10:
        sys.exit("Perfil insuficiente no log.")
    if args.sfc_elev is not None:
        sfc_elev = args.sfc_elev
    elif args.obs_elev is not None:
        sfc_elev = args.obs_elev
    else:
        sfc_elev = float(prof["z"].min())
    launch = meta["launch"].to_pydatetime()
    target = parse_target(args.target, launch)
    dh = (target - launch).total_seconds() / 3600
    if abs(dh) > 3:
        notes.insert(0, f"Sondagem lançada às {launch:%H:%MZ} de {launch:%d/%m}; horário-alvo {target:%d/%m %H:%MZ} "
                        f"({dh:+.0f} h). O perfil não captura a evolução diurna (aquecimento, convecção, "
                        "resfriamento noturno): trate as notas como tendência.")

    p = prof["p"].values
    t = prof["t"].values
    td = prof["td"].values

    # Parcelas
    parcels = {}
    if args.sfc_t is not None and args.sfc_td is not None:
        p_sfc = args.sfc_p or float(p.max())
        parcels["SFC"] = parcel_analysis("superfície (informada)", p, t, td, p_sfc, args.sfc_t, args.sfc_td)
    base_name = "base dos dados" + (" (descida)" if meta["used_descent"] else "")
    parcels["BASE"] = parcel_analysis(base_name, p, t, td, p[0], t[0], td[0])
    low = prof[prof["p"] >= p[0] - 300]
    the = theta_e_bolton(low["p"].values, low["t"].values, low["td"].values)
    k = int(np.argmax(the))
    if k != 0:
        parcels["MU"] = parcel_analysis("mais instável", p, t, td, float(low["p"].iloc[k]),
                                        float(low["t"].iloc[k]), float(low["td"].iloc[k]))
    best = max(parcels.values(), key=lambda r: r.cape)

    layers = find_cloud_layers(prof, cfg)
    tropo = prof[prof["p"] >= cfg["processamento"]["p_topo_hpa"]].reset_index(drop=True)
    shear = shear_and_ri(tropo, sfc_elev, cfg)
    lapse, _ = lapse_features(tropo, sfc_elev)
    _, trop = lapse_features(prof, sfc_elev)
    pw = precipitable_water(prof)
    kidx = k_index(prof)
    zc = prof[prof["t"] <= 0]
    frz = f"{zc['p'].iloc[0]:.0f} hPa ({zc['z'].iloc[0] / 1000:.1f} km)" if len(zc) else "n/d"
    obs = None
    if args.obs_elev is not None:
        z = prof["z"].values
        if z.min() <= args.obs_elev <= z.max():
            def at(col):
                return float(np.interp(args.obs_elev, z, prof[col].values))
            u_o, v_o = at("u"), at("v")
            obs = dict(elev=args.obs_elev, t=at("t"), td=at("td"), rh=at("rh"), p=at("p"),
                       vento_ms=math.hypot(u_o, v_o), dir=(math.degrees(math.atan2(-u_o, -v_o)) + 360) % 360)
            for L in layers:
                if L["base_m"] - 50 <= args.obs_elev <= L["topo_m"] + 50:
                    notes.append(f"O observatório ({args.obs_elev:.0f} m) está DENTRO da camada úmida "
                                 f"{L['base_m']:.0f}–{L['topo_m']:.0f} m ({L['status']}): neblina/nuvem no local "
                                 "no horário da sondagem.")
        else:
            notes.append(f"Altitude do observatório ({args.obs_elev:.0f} m) fora do perfil medido.")
    if args.sfc_t is not None and args.sfc_td is not None:
        sfc = dict(t=args.sfc_t, td=args.sfc_td, origem="informada")
    elif obs:
        sfc = dict(t=obs["t"], td=obs["td"],
                   origem=f"perfil interpolado na altitude do observatório, {obs['elev']:.0f} m / {obs['p']:.0f} hPa")
    else:
        sfc = dict(t=float(t[0]), td=float(td[0]),
                   origem=f"nível mais baixo da sonda, {prof['z'].iloc[0]:.0f} m / {p[0]:.0f} hPa"
                   + (", descida" if prof["fonte"].iloc[0] == "descida" else ""))
    moon = moon_illumination(target)
    rep = score_all(prof, layers, shear, parcels, pw, sfc, moon[0], cfg, sfc_elev, obs)

    # Combinação com o satélite
    sat = None
    if args.sat_json:
        sat = json.loads(args.sat_json.read_text())
        t_sat = datetime.fromisoformat(sat["ultimo_quadro"])
        dh_sat = abs((target - t_sat).total_seconds()) / 3600
        w = max(0.0, 1.0 - dh_sat / cfg["satelite"]["horizonte_h"])
        # Produtos L2: CAPE (DSI) e água precipitável (TPW) substituem os da sonda quando o
        # satélite está perto do alvo e há cobertura suficiente (só existem em céu claro)
        l2 = sat.get("l2") or {}
        sc = cfg["satelite"]
        cape_sat = pw_use = None
        if w >= sc["peso_min_l2"]:
            dsi = (l2.get("DSI") or {}).get("r50") or {}
            if dsi.get("mediana") is not None and dsi.get("validos", 0) >= sc["cobertura_min_l2"]:
                cape_sat = dsi["mediana"]
                notes.append(f"CAPE do satélite (DSI, mediana em 50 km) = {cape_sat:.0f} J/kg usado no lugar do "
                             f"da sondagem ({max(r.cape for r in parcels.values()):.0f} J/kg).")
            tpw = (l2.get("TPW") or {}).get("r50") or {}
            if tpw.get("mediana") is not None and tpw.get("validos", 0) >= sc["cobertura_min_l2"]:
                pw_use = tpw["mediana"]
                notes.append(f"Água precipitável do satélite (TPW) = {pw_use:.0f} mm usada no lugar da sondagem "
                             f"({pw:.0f} mm).")
        if cape_sat is not None or pw_use is not None:
            rep = score_all(prof, layers, shear, parcels, pw if pw_use is None else pw_use, sfc, moon[0], cfg,
                            sfc_elev, obs, cape_sat)
        lst = l2.get("LST") or {}
        if lst.get("lst_menos_td_c") is not None:
            dd = lst["lst_menos_td_c"]
            notes.append(f"LST ({lst['tempo'][11:16]}Z) − Td da sondagem no observatório = {dd:+.1f} °C"
                         + (": superfície no ponto de orvalho ou abaixo → condensação muito provável."
                            if dd <= sc["lst_orvalho_c"] else "."))
        T_snd = rep["transparencia"]["nota"]
        T_sat = sat["transparencia_sat"]["nota"]
        T_fin = int(max(1, min(5, math.floor(w * T_sat + (1 - w) * T_snd + 0.5))))
        rep["transparencia"].update(sondagem=T_snd, satelite=T_sat, peso_sat=w, nota=T_fin)
        rep["veredito"] = verdict(T_fin, rep["seeing"]["nota"], moon[0], cfg)
        sat["peso"] = w
        sat["dh"] = dh_sat
        if args.lat is not None and args.lon is not None:
            dkm = math.hypot((sat["lat"] - args.lat) * 111.32,
                             (sat["lon"] - args.lon) * 111.32 * math.cos(math.radians(args.lat)))
            if dkm > cfg["satelite"]["dist_max_km"]:
                notes.append(f"O JSON do satélite é de um ponto a {dkm:.0f} km do observatório.")
        notes.append(f"Satélite: último quadro {t_sat:%d/%m %H:%MZ}, a {dh_sat:.1f} h do alvo → peso {w:.0%} na "
                     f"transparência (sondagem {T_snd}/5, satélite {T_sat}/5, final {T_fin}/5).")

    levels = []
    for pl in (925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100):
        if not (p.min() <= pl <= p.max()):
            continue
        tt, dd = interp_at(prof, pl, "t"), interp_at(prof, pl, "td")
        u, v = interp_at(prof, pl, "u"), interp_at(prof, pl, "v")
        spd = math.hypot(u, v)
        wd = (math.degrees(math.atan2(-u, -v)) + 360) % 360
        levels.append([pl, f"{interp_at(prof, pl, 'z'):.0f}", f"{tt:.1f}", f"{dd:.1f}", f"{tt - dd:.1f}",
                       f"{interp_at(prof, pl, 'rh'):.0f} / {interp_at(prof, pl, 'rhi'):.0f}",
                       f"{wd:03.0f}° / {wind(spd)}"])

    winds = {}
    for pl in (850, 700, 500, 300, 250, 200):
        if p.min() <= pl <= p.max():
            u, v = interp_at(prof, pl, "u"), interp_at(prof, pl, "v")
            winds[str(pl)] = dict(u=round(u, 2), v=round(v, 2), vel_ms=round(math.hypot(u, v), 2),
                                  dir=round((math.degrees(math.atan2(-u, -v)) + 360) % 360))

    stem = args.log.with_suffix("")
    out_png = args.png or Path(f"{stem}_skewt.png")
    out_pdf = args.pdf or Path(f"{stem}_astro.pdf")
    out_json = args.json or Path(f"{stem}_astro.json")
    plot_skewt(prof, best, meta,
               f"Skew-T — {meta['type']} {meta['serial']} — {launch:%Y-%m-%d %H:%MZ}", out_png)
    ctx = dict(site=args.site, target=target, meta=meta, notes=notes, rep=rep, parcels=parcels, pw=pw, k=kidx,
               frz=frz, trop=trop, levels=levels, layers=layers, shear=shear, lapse=lapse, moon=moon, cfg=cfg,
               sat=sat, obs=obs)
    build_pdf(out_pdf, str(out_png), ctx)

    summary = dict(
        site=args.site, alvo=target.isoformat(), lancamento=launch.isoformat(), sonda=meta["serial"],
        transparencia=rep["transparencia"]["nota"], seeing=rep["seeing"]["nota"],
        chuva=rep["chuva"]["nivel"], orvalho=rep["orvalho"]["nivel"],
        montar=rep["veredito"]["montar"][0], ceu_profundo=rep["veredito"]["ceu_profundo"][0],
        planetaria=rep["veredito"]["planetaria"][0], lua_iluminada=round(moon[0], 2),
        pw_mm=round(pw, 1), k_index=round(kidx, 1),
        cape={k2: round(r.cape) for k2, r in parcels.items()},
        camadas_nuvem=layers, notas=notes,
        sfc_t_c=round(sfc["t"], 2), sfc_td_c=round(sfc["td"], 2), sfc_origem=sfc["origem"],
        observatorio=None if obs is None else {k: round(v, 2) for k, v in obs.items()},
        lat=args.lat, lon=args.lon,
        transparencia_detalhe={k: v for k, v in rep["transparencia"].items() if k != "detalhes"},
        ventos=winds)
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    print(f"{args.site} — alvo {target:%d/%m %H:%MZ}")
    print(f"  Transparência {rep['transparencia']['nota']}/5 | Seeing {rep['seeing']['nota']}/5 | "
          f"Chuva {rep['chuva']['nivel']} | Orvalho {rep['orvalho']['nivel']}")
    print(f"  Montar? {rep['veredito']['montar'][0]} — {rep['veredito']['montar'][1]}")
    print(f"  PDF: {out_pdf}\n  JSON: {out_json}")


if __name__ == "__main__":
    main()
