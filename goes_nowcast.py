#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 João — projeto wykrota-nowcast
"""
goes_nowcast.py — Nowcasting de nebulosidade para astronomia com imagens GOES-19
recebidas por HRIT e processadas pelo SatDump.

Lê as últimas N pastas de produto do SatDump (cada uma com product.cbor e as PNGs
de 8 bits por canal), calibra para temperatura de brilho, localiza o observatório
no disco e classifica cada pixel ao redor:

    opaca fria   BT13 < 253 K (topos frios: nuvens altas/espessas)
    nuvem        BT13 bem abaixo da referência de céu claro
    cirrus fino  excesso de BT13−BT15 em relação ao céu claro local
    nevoeiro     BT7−BT13 negativo (só à noite)
    claro        o resto

Calcula a fração de céu claro em raios de 25/50/100 km, a tendência das últimas
horas, o deslocamento das nuvens (correlação de fase entre quadros) e uma estimativa
de chegada/saída das nuvens. Opcionalmente usa os ventos da sondagem (JSON do
astro_sounding.py) para olhar "contra o vento" por camada.

Saídas: JSON (para o astro_sounding.py --sat-json) e um PNG de visualização (quicklook).

Dependências: numpy, pillow, matplotlib  (Python >= 3.11)

Exemplo:
    python3 goes_nowcast.py --root "/caminho/para/goesdata/IMAGES/GOES-19/Full Disk" \
        --site Wykrota --lat -19.824167 --lon -43.690278 \
        --sounding-json sonde_astro.json --json goes_wykrota.json --png goes_wykrota.png
"""
from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

DEFAULTS = {
    "nuvens": {
        "bt_opaca_fria_k": 253.0,
        "delta_nuvem_k": 8.0,          # BT13 < Tref - delta → nuvem
        "excesso_btd_cirrus_k": 1.5,   # (BT13-BT15) acima do céu claro local
        "delta_cirrus_bt_k": 2.0,      # cirrus exige BT13 < Tref - isto
        "btd_nevoeiro_k": -2.0,        # BT7 - BT13 <= isto (noite)
        "refl_nuvem_dia": 0.35,        # canal 2 (dia)
        "tref_min_k": 275.0,           # abaixo disso, o p90 local é nuvem
        "tref_padrao_dia_k": 300.0,
        "tref_padrao_noite_k": 290.0,
        "btd_claro_padrao_k": 3.0,
        "elev_sol_noite_graus": -5.0,
    },
    "analise": {
        "raios_km": [25, 50, 100],
        "janela_km": 260,
        "raio_amostra_km": 20,
        "horizontes_h": [0.5, 1.0, 1.5, 2.0, 3.0],
        "desloc_max_kmh": 120.0,
        "pico_min_correlacao": 0.05,
        "dispersao_max_frac": 0.5,     # pares de quadros devem concordar (fração da velocidade)
        "dif_dir_vento_max": 45.0,     # graus: coerência com o vento da sondagem
    },
    "nota": {
        "peso_agora": 0.5,
        "penal_cirrus": 1.0,
    },
}


# --------------------------------------------------------------------------
# CBOR mínimo (o SatDump grava product.cbor) — sem dependências externas
# --------------------------------------------------------------------------
def cbor_loads(b: bytes):
    pos = [0]

    def take(n):
        v = b[pos[0]:pos[0] + n]
        pos[0] += n
        return v

    def arg(info):
        if info < 24:
            return info
        return int.from_bytes(take({24: 1, 25: 2, 26: 4, 27: 8}[info]), "big")

    def item():
        ib = take(1)[0]
        mt, info = ib >> 5, ib & 31
        if mt == 7:
            if info in (20, 21):
                return info == 21
            if info in (22, 23):
                return None
            if info == 25:
                return struct.unpack(">e", take(2))[0]
            if info == 26:
                return struct.unpack(">f", take(4))[0]
            if info == 27:
                return struct.unpack(">d", take(8))[0]
            return info
        if info == 31:
            out = [] if mt == 4 else ({} if mt == 5 else b"")
            while b[pos[0]] != 0xFF:
                if mt == 4:
                    out.append(item())
                elif mt == 5:
                    k = item()
                    out[k] = item()
                else:
                    out += item()
            pos[0] += 1
            return out
        a = arg(info)
        if mt == 0:
            return a
        if mt == 1:
            return -1 - a
        if mt == 2:
            return take(a)
        if mt == 3:
            return take(a).decode("utf-8")
        if mt == 4:
            return [item() for _ in range(a)]
        if mt == 5:
            d = {}
            for _ in range(a):
                k = item()
                d[k] = item()
            return d
        if mt == 6:
            return item()
        raise ValueError(f"tipo CBOR {mt}")

    return item()


# --------------------------------------------------------------------------
# Projeção geoestacionária (convenção GOES-R, sweep x)
# --------------------------------------------------------------------------
class Geos:
    REQ = 6378137.0
    RPOL = 6356752.31414

    def __init__(self, cfg):
        self.h = float(cfg["altitude"])
        self.lon0 = float(cfg["lon0"])
        self.ox, self.oy = float(cfg["offset_x"]), float(cfg["offset_y"])
        self.sx, self.sy = float(cfg["scalar_x"]), float(cfg["scalar_y"])
        self.w, self.hgt = int(cfg["width"]), int(cfg["height"])
        if cfg.get("type", "geos") != "geos":
            raise ValueError("projeção não suportada: " + str(cfg.get("type")))

    def ll2px(self, lat, lon):
        lat = np.asarray(lat, float)
        lon = np.asarray(lon, float)
        phi = np.radians(lat)
        lam = np.radians(lon - self.lon0)
        e2 = 1 - self.RPOL ** 2 / self.REQ ** 2
        phic = np.arctan(self.RPOL ** 2 / self.REQ ** 2 * np.tan(phi))
        rc = self.RPOL / np.sqrt(1 - e2 * np.cos(phic) ** 2)
        H = self.h + self.REQ
        sx = H - rc * np.cos(phic) * np.cos(lam)
        sy = -rc * np.cos(phic) * np.sin(lam)
        sz = rc * np.sin(phic)
        n = np.sqrt(sx * sx + sy * sy + sz * sz)
        x = np.arcsin(-sy / n)
        y = np.arctan(sz / sx)
        col = (x * self.h - self.ox) / self.sx
        row = (y * self.h - self.oy) / self.sy
        return col, row

    def jacobian(self, lat, lon, d=0.05):
        """Matriz px/km: [[dcol/dE, dcol/dN], [drow/dE, drow/dN]]."""
        kmlat = 111.32 * d
        kmlon = 111.32 * math.cos(math.radians(lat)) * d
        c1, r1 = self.ll2px(lat, lon + d)
        c0, r0 = self.ll2px(lat, lon - d)
        c3, r3 = self.ll2px(lat + d, lon)
        c2, r2 = self.ll2px(lat - d, lon)
        return np.array([[(c1 - c0) / (2 * kmlon), (c3 - c2) / (2 * kmlat)],
                         [(r1 - r0) / (2 * kmlon), (r3 - r2) / (2 * kmlat)]], float)


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def solar_elevation(dt: datetime, lat, lon):
    doy = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = hour * 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    cz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1, min(1, cz))))


def deep_update(base, new):
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v
    return base


DIR_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})")


def list_products(root: Path):
    out = []
    for d in root.iterdir():
        if d.is_dir() and (d / "product.cbor").exists():
            m = DIR_RE.search(d.name)
            if m:
                t = datetime(*map(int, m.groups()), tzinfo=timezone.utc)
                out.append((t, d))
    return sorted(out)


# --------------------------------------------------------------------------
# Leitura de um produto
# --------------------------------------------------------------------------
NEEDED = ("13", "15", "7", "2")


class Frame:
    def __init__(self, folder: Path, lat, lon, win_km, cfg):
        meta = cbor_loads((folder / "product.cbor").read_bytes())
        if meta.get("product_source", "").upper().replace(" ", "") not in ("GOES-19", "GOES19"):
            print(f"aviso: {folder.name}: product_source={meta.get('product_source')}", file=sys.stderr)
        self.folder = folder
        ts = meta.get("timestamps") or [meta.get("product_timestamp")]
        self.time = datetime.fromtimestamp(float(ts[0]), timezone.utc)
        self.proj = Geos(meta["projection_cfg"])
        col, row = self.proj.ll2px(lat, lon)
        self.col, self.row = float(col), float(row)
        if not (0 <= self.col < self.proj.w and 0 <= self.row < self.proj.hgt):
            raise ValueError("o ponto está fora da imagem")
        self.J = self.proj.jacobian(lat, lon)
        px_per_km = np.abs(self.J).max()
        self.half = int(math.ceil(win_km * px_per_km)) + 2
        cal = meta["calibration"]
        files = {im["name"]: im["file"] for im in meta["images"]}
        self.bt = {}
        r0, c0 = int(round(self.row)), int(round(self.col))
        self.r0, self.c0 = r0, c0
        for ch in NEEDED:
            if ch not in files or ch not in cal:
                continue
            lut = np.zeros(256, float)
            for cnt, val in cal[ch]:
                lut[int(cnt)] = val
            with Image.open(folder / files[ch]) as im:
                box = (c0 - self.half, r0 - self.half, c0 + self.half + 1, r0 + self.half + 1)
                arr = np.asarray(im.crop(box))
            if arr.ndim == 3:
                arr = arr[..., 0]
            self.bt[ch] = lut[arr]
        for ch in ("13", "15"):
            if ch not in self.bt:
                raise ValueError(f"canal {ch} ausente em {folder}")
        # coordenadas (km Leste, km Norte) de cada pixel da janela
        n = 2 * self.half + 1
        dr, dc = np.mgrid[-self.half:self.half + 1, -self.half:self.half + 1]
        dr = dr + (r0 - self.row)
        dc = dc + (c0 - self.col)
        Jinv = np.linalg.inv(self.J)
        self.E = Jinv[0, 0] * dc + Jinv[0, 1] * dr
        self.N = Jinv[1, 0] * dc + Jinv[1, 1] * dr
        self.dist = np.hypot(self.E, self.N)
        self.space = self.bt["13"] < 150  # pixels fora do disco
        self.km_per_px = 1 / px_per_km
        self.shape = (n, n)


def classify(fr: Frame, sun_el, tsfc_k, cfg):
    c = cfg["nuvens"]
    bt13, bt15 = fr.bt["13"], fr.bt["15"]
    btd = bt13 - bt15
    near = fr.dist <= 150
    noite = sun_el < c["elev_sol_noite_graus"]
    p90 = float(np.percentile(bt13[near], 90))
    tref = p90
    origem = "p90 local"
    if tsfc_k is not None:
        if tref < tsfc_k - 5:
            tref, origem = tsfc_k - 5, "superfície da sondagem − 5 K"
    elif tref < c["tref_min_k"]:
        tref = c["tref_padrao_noite_k"] if noite else c["tref_padrao_dia_k"]
        origem = "padrão (região muito nublada)"
    warm = near & (bt13 >= tref - 2)
    btd_clear = float(np.median(btd[warm])) if warm.sum() > 20 else c["btd_claro_padrao_k"]

    cls = np.zeros(bt13.shape, np.uint8)  # 0 claro
    cirrus = ((btd - btd_clear) >= c["excesso_btd_cirrus_k"]) & (bt13 < tref - c["delta_cirrus_bt_k"])
    cls[cirrus] = 3
    if noite and "7" in fr.bt:
        fog = (fr.bt["7"] - bt13 <= c["btd_nevoeiro_k"]) & (bt13 >= tref - 15)
        cls[fog & (cls == 0)] = 4
    cloud = bt13 < tref - c["delta_nuvem_k"]
    if not noite and "2" in fr.bt and sun_el > 15:
        cloud |= fr.bt["2"] >= c["refl_nuvem_dia"]
    cls[cloud] = 2
    cls[bt13 < c["bt_opaca_fria_k"]] = 1
    cls[fr.space] = 255
    return cls, dict(tref_k=tref, tref_origem=origem, btd_claro_k=btd_clear, noite=noite)


NAMES = {0: "claro", 1: "opaca fria", 2: "nuvem", 3: "cirrus fino", 4: "nevoeiro/estrato"}


def stats(cls, mask):
    m = mask & (cls != 255)
    n = int(m.sum())
    if n == 0:
        return None
    out = {NAMES[k]: float((cls[m] == k).mean()) for k in NAMES}
    out["n_pixels"] = n
    return out


def phase_correlation(a, b):
    """Deslocamento (dr, dc) de b em relação a a."""
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    A = np.fft.fft2((a - a.mean()) * win)
    B = np.fft.fft2((b - b.mean()) * win)
    R = A.conj() * B
    R /= np.abs(R) + 1e-9
    r = np.fft.ifft2(R).real
    i, j = np.unravel_index(np.argmax(r), r.shape)
    peak = float(r[i, j])
    # refinamento subpixel (parábola)
    def sub(m, k, n):
        y0, y1, y2 = m[(k - 1) % n], m[k], m[(k + 1) % n]
        d = y0 - 2 * y1 + y2
        return k + (0.5 * (y0 - y2) / d if abs(d) > 1e-12 else 0.0)
    fi = sub(r[:, j], i, r.shape[0])
    fj = sub(r[i, :], j, r.shape[1])
    if fi > r.shape[0] / 2:
        fi -= r.shape[0]
    if fj > r.shape[1] / 2:
        fj -= r.shape[1]
    return fi, fj, peak


def motion_between(f1: Frame, f2: Frame, cfg):
    dt_h = (f2.time - f1.time).total_seconds() / 3600
    if dt_h <= 0 or dt_h > 1.6:
        return None
    s = min(f1.half, f2.half, 128)
    def cut(f):
        a = f.bt["13"][f.half - s:f.half + s, f.half - s:f.half + s].copy()
        return np.clip(a, 200, 300)  # realça nuvens, limita a superfície
    dr, dc, peak = phase_correlation(cut(f1), cut(f2))
    Jinv = np.linalg.inv(f2.J)
    dE = Jinv[0, 0] * dc + Jinv[0, 1] * dr
    dN = Jinv[1, 0] * dc + Jinv[1, 1] * dr
    vE, vN = dE / dt_h, dN / dt_h
    spd = math.hypot(vE, vN)
    ok = peak >= cfg["analise"]["pico_min_correlacao"] and spd <= cfg["analise"]["desloc_max_kmh"]
    # direção de onde as nuvens VÊM (convenção meteorológica)
    dir_from = (math.degrees(math.atan2(-vE, -vN)) + 360) % 360
    return dict(vE_kmh=vE, vN_kmh=vN, vel_kmh=spd, dir_de=dir_from, pico=peak, valido=bool(ok),
                de=f1.time.isoformat(), para=f2.time.isoformat())


def combined_motion(frames, cfg, snd):
    """Combina todos os pares consecutivos de quadros e confere com o vento da sondagem."""
    a = cfg["analise"]
    pairs = [motion_between(frames[i][0], frames[i + 1][0], cfg) for i in range(len(frames) - 1)]
    pairs = [p for p in pairs if p]
    if not pairs:
        return None
    good = [p for p in pairs if p["pico"] >= a["pico_min_correlacao"] and p["vel_kmh"] <= a["desloc_max_kmh"]]
    use = good or pairs
    vE = float(np.median([p["vE_kmh"] for p in use]))
    vN = float(np.median([p["vN_kmh"] for p in use]))
    spd = math.hypot(vE, vN)
    spread = float(np.median([math.hypot(p["vE_kmh"] - vE, p["vN_kmh"] - vN) for p in use])) if len(use) > 1 else 0.0
    pico = float(np.median([p["pico"] for p in use]))
    dir_de = (math.degrees(math.atan2(-vE, -vN)) + 360) % 360
    motivos = []
    if not good:
        motivos.append(f"correlação fraca (pico {pico:.2f} < {a['pico_min_correlacao']})")
    if len(use) > 1 and spread > a["dispersao_max_frac"] * spd + 10:
        motivos.append(f"pares de quadros discordam (dispersão {spread:.0f} km/h)")
    # coerência com o vento da sondagem (algum nível entre 700 e 200 hPa)
    coerente = None
    for p in ("700", "500", "300", "250", "200"):
        w = (snd.get("ventos") or {}).get(p)
        if not w or w["vel_ms"] < 2:
            continue
        d = abs((dir_de - w["dir"] + 180) % 360 - 180)
        r = spd / (w["vel_ms"] * 3.6)
        if d <= a["dif_dir_vento_max"] and 0.5 <= r <= 2.0:
            coerente = f"{p} hPa"
            break
    valido = not motivos or (coerente is not None and good)
    return dict(vE_kmh=vE, vN_kmh=vN, vel_kmh=spd, dir_de=dir_de, pico=pico, pares=len(pairs),
                pares_bons=len(good), dispersao_kmh=spread, coerente_com=coerente,
                valido=bool(valido), motivos=motivos)


def upwind_scan(fr, cls, vE, vN, classes, cfg):
    """Fração das classes indicadas em círculos 'contra o vento' para cada horizonte."""
    rs = cfg["analise"]["raio_amostra_km"]
    out = []
    for h in cfg["analise"]["horizontes_h"]:
        e0, n0 = -vE * h, -vN * h
        m = (np.hypot(fr.E - e0, fr.N - n0) <= rs) & (cls != 255)
        ok = m.sum() > 10
        frac = float(np.isin(cls[m], classes).mean()) if ok else float("nan")
        tot = float((cls[m] != 0).mean()) if ok else float("nan")
        out.append(dict(h=h, km=math.hypot(e0, n0), frac=frac, frac_total=tot))
    return out


def eta_from_scan(scan, site_cloudy, thr=0.5):
    for s in scan:
        if math.isnan(s["frac"]):
            continue
        if not site_cloudy and s["frac"] >= thr:
            return dict(evento="chegada de nuvens", h=s["h"])
        if site_cloudy and s["frac_total"] <= 1 - thr - 0.1:  # abertura exige céu claro de fato
            return dict(evento="abertura", h=s["h"])
    return dict(evento="sem mudança no horizonte", h=None)


# --------------------------------------------------------------------------
# Produtos L2 recebidos pelo HRIT (ACHA, ACHT, DSI, LST, TPW)
# --------------------------------------------------------------------------
# Calibração padrão (tirada dos cabeçalhos LRIT do GOES-19): valor = v255 + (255 - pixel) * passo.
# Pixel 0 = sem dado (céu claro para ACHA/ACHT; nublado para DSI/LST/TPW; ou falha de recepção).
L2_PADRAO = {
    "ACHA": dict(v255=0.0, passo=78.1321, unidade="m", nome="altura do topo das nuvens"),
    "ACHT": dict(v255=180.0, passo=0.6251, unidade="K", nome="temperatura do topo das nuvens"),
    "DSI": dict(v255=0.0, passo=19.5330, unidade="J/kg", nome="CAPE (superfície)"),
    "LST": dict(v255=190.0, passo=0.64, unidade="K", nome="temperatura da superfície terrestre"),
    "TPW": dict(v255=0.0, passo=0.3907, unidade="mm", nome="água precipitável"),
}


def lrit_lut(path: Path):
    """Lê a tabela pixel→valor (registro tipo 3) do cabeçalho de um arquivo LRIT."""
    with open(path, "rb") as fh:
        head = fh.read(16)
        if len(head) < 16 or head[0] != 0:
            return None
        total = struct.unpack(">I", head[4:8])[0]
        rest = fh.read(total - 16)
    i = 0
    while i + 3 <= len(rest):
        htype, hlen = rest[i], struct.unpack(">H", rest[i + 1:i + 3])[0]
        if hlen < 3:
            break
        if htype == 3:
            txt = rest[i + 3:i + hlen].decode("latin-1", "replace")
            lut = np.full(256, np.nan)
            for line in txt.replace("\r", "").split("\n"):
                if ":=" in line and not line.startswith(("$", "_")):
                    k, v = line.split(":=", 1)
                    try:
                        lut[int(k)] = float(v)
                    except ValueError:
                        pass
            return lut
        i += hlen
    return None


def l2_luts(lrit_dir: Path | None):
    luts, fonte = {}, {}
    for p, c in L2_PADRAO.items():
        lut = c["v255"] + (255 - np.arange(256)) * c["passo"]
        luts[p], fonte[p] = lut, "padrão"
    if lrit_dir and lrit_dir.exists():
        for p in L2_PADRAO:
            cands = sorted(lrit_dir.glob(f"OR_ABI-L2-{p}F-*G19*.lrit"), key=lambda x: x.stat().st_mtime)
            if cands:
                lut = lrit_lut(cands[-1])
                if lut is not None and np.isfinite(lut[1:]).sum() > 200:
                    # completa lacunas por interpolação
                    idx = np.arange(256)
                    ok = np.isfinite(lut)
                    luts[p] = np.interp(idx, idx[ok], lut[ok])
                    fonte[p] = cands[-1].name
    for p in luts:
        luts[p] = luts[p].astype(float)
        luts[p][0] = np.nan
    return luts, fonte


def latest_l2(l2_root: Path, product: str, ref: datetime, max_age_h: float):
    best = None
    for t, d in list_products(l2_root):
        if t > ref + timedelta(minutes=20) or (ref - t).total_seconds() / 3600 > max_age_h:
            continue
        files = list(d.glob(f"G19_{product}_*.png"))
        if files and (best is None or t > best[0]):
            best = (t, d, files[0])
    return best


def l2_sample(png: Path, cbor: Path, lut, lat, lon, raios):
    meta = cbor_loads(cbor.read_bytes())
    pc = dict(meta["projection_cfg"])
    with Image.open(png) as im:
        w, h = im.size
        f = w / float(pc["width"])
        pc.update(scalar_x=pc["scalar_x"] / f, scalar_y=pc["scalar_y"] / f, width=w, height=h)
        g = Geos(pc)
        col, row = (float(x) for x in g.ll2px(lat, lon))
        J = g.jacobian(lat, lon)
        k = int(math.ceil(max(raios) * np.abs(J).max())) + 2
        r0, c0 = int(round(row)), int(round(col))
        arr = np.asarray(im.crop((c0 - k, r0 - k, c0 + k + 1, r0 + k + 1)))
    if arr.ndim == 3:
        arr = arr[..., 0]
    val = lut[arr]
    Ji = np.linalg.inv(J)
    dr, dc = np.mgrid[-k:k + 1, -k:k + 1]
    E = Ji[0, 0] * (dc + c0 - col) + Ji[0, 1] * (dr + r0 - row)
    N = Ji[1, 0] * (dc + c0 - col) + Ji[1, 1] * (dr + r0 - row)
    dist = np.hypot(E, N)
    out = {}
    for R in raios:
        x = val[dist <= R]
        v = x[np.isfinite(x)]
        out[f"r{R}"] = dict(validos=round(len(v) / max(1, len(x)), 2),
                            p10=None if not len(v) else round(float(np.percentile(v, 10)), 1),
                            mediana=None if not len(v) else round(float(np.median(v)), 1),
                            p90=None if not len(v) else round(float(np.percentile(v, 90)), 1),
                            valores=v)
    return out


def analyze_l2(l2_root, lrit_dir, lat, lon, ref, max_age_h, snd):
    luts, fonte = l2_luts(lrit_dir)
    res = {}
    for p, c in L2_PADRAO.items():
        hit = latest_l2(l2_root, p, ref, max_age_h)
        if not hit:
            continue
        t, d, png = hit
        try:
            st = l2_sample(png, d / "product.cbor", luts[p], lat, lon, (25, 50))
        except Exception as e:  # noqa: BLE001
            print(f"aviso: L2 {p}: {e}", file=sys.stderr)
            continue
        item = dict(nome=c["nome"], unidade=c["unidade"], tempo=t.isoformat(), calibracao=fonte[p])
        for R in (25, 50):
            s = dict(st[f"r{R}"])
            s.pop("valores")
            item[f"r{R}"] = s
        if p == "ACHA":
            v = st["r25"]["valores"]
            nt = max(1, int(round(len(v) / max(st["r25"]["validos"], 1e-6)))) if len(v) else 1
            item["camadas_25km"] = dict(
                baixa_lt3km=round(float((v < 3000).sum()) / nt, 2),
                media_3_7km=round(float(((v >= 3000) & (v < 7000)).sum()) / nt, 2),
                alta_ge7km=round(float((v >= 7000).sum()) / nt, 2))
        if p == "LST" and snd.get("sfc_td_c") is not None and item["r25"]["mediana"] is not None:
            item["lst_menos_td_c"] = round(item["r25"]["mediana"] - 273.15 - snd["sfc_td_c"], 1)
        res[p] = item
    return res


def wind_from_sounding(snd, p):
    v = (snd.get("ventos") or {}).get(str(p))
    if not v:
        return None
    # u, v do vento (m/s) → deslocamento das nuvens em km/h
    return v["u"] * 3.6, v["v"] * 3.6


# --------------------------------------------------------------------------
# Quicklook
# --------------------------------------------------------------------------
def quicklook(fr, cls, info, res, out_png, site, snd=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap

    ext = 130
    m = (np.abs(fr.E) <= ext) & (np.abs(fr.N) <= ext)
    rows = np.where(m.any(1))[0]
    cols = np.where(m.any(0))[0]
    sl = (slice(rows.min(), rows.max() + 1), slice(cols.min(), cols.max() + 1))
    fig, ax = plt.subplots(figsize=(7, 7), dpi=120)
    # Mistura canal 13 (cinza) + classificação numa única camada (evita costuras entre camadas)
    bt = fr.bt["13"][sl]
    g = 1 - np.clip((bt - 200) / 110, 0, 1)
    rgb = np.repeat(g[..., None], 3, axis=2)
    cores = {1: (0.84, 0.15, 0.16), 2: (1.0, 0.62, 0.11), 3: (0.18, 0.77, 1.0), 4: (1.0, 0.90, 0.0)}
    c = cls[sl]
    for k, col in cores.items():
        m_ = c == k
        rgb[m_] = 0.55 * rgb[m_] + 0.45 * np.array(col)
    q = np.round(rgb * 31).astype(int)
    code = q[..., 0] * 1024 + q[..., 1] * 32 + q[..., 2]
    uniq, idx = np.unique(code, return_inverse=True)
    pal = np.stack([(uniq // 1024) / 31, ((uniq // 32) % 32) / 31, (uniq % 32) / 31], axis=1)
    ax.pcolormesh(fr.E[sl], fr.N[sl], idx.reshape(code.shape), cmap=ListedColormap(pal),
                  vmin=-0.5, vmax=len(uniq) - 0.5, shading="auto", linewidth=0, antialiased=False,
                  rasterized=True)
    t = np.linspace(0, 2 * np.pi, 200)
    for r in res["raios_km"]:
        ax.plot(r * np.cos(t), r * np.sin(t), color="white", lw=0.8, ls="--")
        ax.text(r * 0.71, r * 0.71, f"{r} km", color="white", fontsize=7)
    ax.plot(0, 0, marker="+", color="lime", ms=14, mew=2)
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    handles = [Patch(color=c, label=l) for c, l in
               zip(["#d62728", "#ff9f1c", "#2ec4ff", "#ffe600"],
                   ["opaca fria", "nuvem", "cirrus fino", "nevoeiro/estrato"])]

    def arrow(vE, vN, color, ls, lw):
        # seta do ponto de onde as nuvens partem até o observatório em 1,5 h (limitada à janela)
        x0, y0 = -vE * 1.5, -vN * 1.5
        k = min(1.0, 0.95 * ext / max(1e-6, math.hypot(x0, y0)))
        ax.annotate("", xy=(0, 0), xytext=(x0 * k, y0 * k),
                    arrowprops=dict(arrowstyle="-|>", color=color, lw=lw, ls=ls, mutation_scale=14))

    mv = res.get("movimento")
    if mv:
        if mv["valido"]:
            arrow(mv["vE_kmh"], mv["vN_kmh"], "lime", "-", 2.2)
            handles.append(Line2D([], [], color="lime", lw=2.2,
                                  label=f"nuvens (observado): {mv['vel_kmh']:.0f} km/h de {mv['dir_de']:.0f}°"))
        else:
            arrow(mv["vE_kmh"], mv["vN_kmh"], "#bbbbbb", "--", 1.5)
            handles.append(Line2D([], [], color="#bbbbbb", lw=1.5, ls="--",
                                  label=f"nuvens (pouco confiável): {mv['vel_kmh']:.0f} km/h de {mv['dir_de']:.0f}°"))
    for p, col in (("700", "#00e5ff"), ("250", "#ff4fd8")):
        w = ((snd or {}).get("ventos") or {}).get(p)
        if w and w["vel_ms"] >= 1:
            arrow(w["u"] * 3.6, w["v"] * 3.6, col, ":", 1.8)
            handles.append(Line2D([], [], color=col, lw=1.8, ls=":",
                                  label=f"vento sonda {p} hPa: {w['vel_ms'] * 3.6:.0f} km/h de {w['dir']:.0f}°"))
    ax.legend(handles=handles,
              loc="lower left", fontsize=7, framealpha=0.85)
    ax.set_xlim(-ext, ext)
    ax.set_ylim(-ext, ext)
    ax.set_aspect("equal")
    ax.set_xlabel("km (Leste)")
    ax.set_ylabel("km (Norte)")
    ax.set_title(f"GOES-19 canal 13 + classificação — {site} — {fr.time:%Y-%m-%d %H:%MZ}"
                 + (" (noite)" if info["noite"] else " (dia)"), fontsize=9)
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Nowcasting de nuvens com GOES-19 (SatDump/HRIT)")
    ap.add_argument("--root", type=Path, required=True,
                    help="pasta com as subpastas de horário (ex.: .../IMAGES/GOES-19/Full Disk)")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--site", default="Observatório")
    ap.add_argument("--frames", type=int, default=4, help="nº de horários mais recentes a usar")
    ap.add_argument("--ate", help="usar só produtos até este horário UTC (ISO), p/ reprocessar")
    ap.add_argument("--sounding-json", type=Path, help="JSON do astro_sounding.py (ventos e T de superfície)")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--json", type=Path, default=Path("goes_nowcast.json"))
    ap.add_argument("--png", type=Path, default=Path("goes_nowcast.png"))
    ap.add_argument("--l2-root", type=Path, help="pasta L2 do SatDump (ex.: .../L2/GOES-19/Full Disk)")
    ap.add_argument("--lrit-dir", type=Path, help="pasta com os .lrit (lê a calibração dos cabeçalhos)")
    ap.add_argument("--l2-max-age-h", type=float, default=1.5, help="idade máxima dos produtos L2")
    args = ap.parse_args()

    cfg = json.loads(json.dumps(DEFAULTS))
    if args.config:
        with open(args.config, "rb") as fh:
            deep_update(cfg, tomllib.load(fh).get("goes", {}))

    prods = list_products(args.root)
    if args.ate:
        lim = datetime.fromisoformat(args.ate.replace("Z", "+00:00"))
        prods = [p for p in prods if p[0] <= lim]
    if not prods:
        sys.exit(f"Nenhum produto com product.cbor em {args.root}")
    prods = prods[-args.frames:]

    snd = json.loads(args.sounding_json.read_text()) if args.sounding_json else {}
    tsfc_k = (snd["sfc_t_c"] + 273.15) if snd.get("sfc_t_c") is not None else None

    frames, results = [], []
    for t, d in prods:
        try:
            fr = Frame(d, args.lat, args.lon, cfg["analise"]["janela_km"], cfg)
        except Exception as e:  # noqa: BLE001
            print(f"aviso: ignorando {d.name}: {e}", file=sys.stderr)
            continue
        sun = solar_elevation(fr.time, args.lat, args.lon)
        cls, info = classify(fr, sun, tsfc_k, cfg)
        r = {"tempo": fr.time.isoformat(), "elev_sol": round(sun, 1), **{k: (round(v, 2) if isinstance(v, float) else v)
                                                                          for k, v in info.items()}}
        for R in cfg["analise"]["raios_km"]:
            r[f"r{R}"] = stats(cls, fr.dist <= R)
        site_m = fr.dist <= 4
        r["local"] = dict(bt13_k=float(fr.bt["13"][site_m].mean()),
                          btd13_15_k=float((fr.bt["13"] - fr.bt["15"])[site_m].mean()),
                          classe=NAMES.get(int(np.bincount(cls[site_m][cls[site_m] != 255]).argmax()), "?"))
        frames.append((fr, cls, info))
        results.append(r)
    if not frames:
        sys.exit("Nenhum quadro utilizável.")

    fr, cls, info = frames[-1]
    last = results[-1]
    res = dict(site=args.site, lat=args.lat, lon=args.lon, raios_km=cfg["analise"]["raios_km"],
               ultimo_quadro=last["tempo"], quadros=results,
               km_por_pixel=round(fr.km_per_px, 2), raiz=str(args.root))

    # Tendência da fração nublada em 50 km
    if len(results) >= 2:
        ts = np.array([(datetime.fromisoformat(r["tempo"]) - datetime.fromisoformat(results[0]["tempo"]))
                       .total_seconds() / 3600 for r in results])
        cf = np.array([1 - r["r50"]["claro"] for r in results])
        slope = float(np.polyfit(ts, cf, 1)[0]) if ts[-1] > 0 else 0.0
        res["tendencia"] = dict(nublado_50km=[round(x, 2) for x in cf], pontos_por_h=round(slope * 100, 1),
                                leitura="aumentando" if slope > 0.05 else ("diminuindo" if slope < -0.05 else "estável"))

    # Movimento observado (últimos dois quadros)
    mv = combined_motion(frames, cfg, snd)
    res["movimento"] = mv

    site_cloudy = last["r25"]["claro"] < 0.5
    obstr = [1, 2, 3, 4]
    previsoes = {}
    if mv and mv["valido"]:
        scan = upwind_scan(fr, cls, mv["vE_kmh"], mv["vN_kmh"], obstr, cfg)
        previsoes["observado"] = dict(scan=scan, **eta_from_scan(scan, site_cloudy))
    for nome, p, classes in (("camada baixa/média (700 hPa)", 700, [2, 4]), ("camada alta (250 hPa)", 250, [1, 3])):
        w = wind_from_sounding(snd, p)
        if w:
            scan = upwind_scan(fr, cls, w[0], w[1], classes, cfg)
            previsoes[nome] = dict(vento_kmh=round(math.hypot(*w), 1), scan=scan,
                                   **eta_from_scan(scan, site_cloudy))
    res["previsoes"] = previsoes

    # Nota de transparência pelo satélite
    c0 = last["r25"]["claro"]
    fut = None
    if "observado" in previsoes:
        vals = [1 - s["frac"] for s in previsoes["observado"]["scan"] if s["h"] <= 2 and not math.isnan(s["frac"])]
        fut = float(np.mean(vals)) if vals else None
    w0 = cfg["nota"]["peso_agora"]
    cf_eff = c0 if fut is None else w0 * c0 + (1 - w0) * fut
    score = 1 + 4 * cf_eff - cfg["nota"]["penal_cirrus"] * last["r25"]["cirrus fino"]
    res["transparencia_sat"] = dict(nota=int(max(1, min(5, math.floor(score + 0.5)))), bruto=round(score, 2),
                                    claro_agora_25km=round(c0, 2),
                                    claro_previsto_2h=None if fut is None else round(fut, 2))
    res["validade_h"] = 3
    if args.l2_root:
        res["l2"] = analyze_l2(args.l2_root, args.lrit_dir, args.lat, args.lon, fr.time, args.l2_max_age_h, snd)
    res["png"] = str(args.png.resolve())

    quicklook(fr, cls, info, res, args.png, args.site, snd)
    args.json.write_text(json.dumps(res, ensure_ascii=False, indent=2))

    print(f"{args.site} — último quadro {fr.time:%d/%m %H:%MZ} ({'noite' if info['noite'] else 'dia'}, "
          f"{len(frames)} quadro(s))")
    for R in cfg["analise"]["raios_km"]:
        s = last[f"r{R}"]
        print(f"  {R:>3} km: claro {s['claro']:.0%} | nuvem {s['nuvem'] + s['opaca fria']:.0%} | "
              f"cirrus {s['cirrus fino']:.0%} | nevoeiro {s['nevoeiro/estrato']:.0%}")
    print(f"  no local: BT13 {last['local']['bt13_k'] - 273.15:.1f} °C → {last['local']['classe']}")
    if mv:
        print(f"  movimento: de {mv['dir_de']:.0f}° a {mv['vel_kmh']:.0f} km/h "
              f"({'válido' if mv['valido'] else 'pouco confiável: ' + '; '.join(mv['motivos'])}, "
              f"pico {mv['pico']:.2f}, {mv['pares_bons']}/{mv['pares']} pares"
              + (f", coerente com vento em {mv['coerente_com']}" if mv.get('coerente_com') else "") + ")")
    for k, v in previsoes.items():
        print(f"  {k}: {v['evento']}" + (f" em ~{v['h']} h" if v["h"] else ""))
    for p, it in (res.get("l2") or {}).items():
        r = it["r25"] if p in ("ACHA", "ACHT", "LST") else it["r50"]
        med = "—" if r["mediana"] is None else f"{r['mediana']:.0f} {it['unidade']}"
        print(f"  L2 {p} ({it['tempo'][11:16]}Z): mediana {med}, válidos {r['validos']:.0%}")
    print(f"  Transparência (satélite): {res['transparencia_sat']['nota']}/5")
    print(f"  JSON: {args.json}  PNG: {args.png}")


if __name__ == "__main__":
    main()
