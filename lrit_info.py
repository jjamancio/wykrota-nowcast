#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 João — projeto wykrota-nowcast
"""
lrit_info.py — Lista o cabeçalho de arquivos LRIT/HRIT (GOES-R HRIT, gravados pelo SatDump).

Para cada arquivo mostra: produto (anotação), dimensões, horário e se existe o
registro "Image Data Function" (tipo 3), que traz a tabela pixel → valor físico.

Uso:
    python3 lrit_info.py /caminho/para/goesdata/LRIT --min 120
    python3 lrit_info.py ARQUIVO.lrit --full      # mostra a tabela completa
"""
import argparse
import struct
import sys
import time
from pathlib import Path

NAMES = {0: "primário", 1: "estrutura da imagem", 2: "navegação", 3: "função dos dados (calibração)",
         4: "anotação", 5: "horário", 6: "texto auxiliar", 7: "chave", 128: "segmento",
         129: "NOAA", 130: "estrutura do cabeçalho", 131: "compressão Rice"}


def read_headers(path: Path):
    with open(path, "rb") as fh:
        head = fh.read(16)
        if len(head) < 16 or head[0] != 0:
            raise ValueError("não parece LRIT (cabeçalho primário ausente)")
        _, rlen, ftype, total = struct.unpack(">BHBI", head[:8])
        data_bits = struct.unpack(">Q", head[8:16])[0]
        rest = fh.read(total - 16)
    recs = {"_file_type": ftype, "_total_header": total, "_data_bits": data_bits}
    i = 0
    while i + 3 <= len(rest):
        htype, hlen = rest[i], struct.unpack(">H", rest[i + 1:i + 3])[0]
        if hlen < 3:
            break
        body = rest[i + 3:i + hlen]
        recs.setdefault(htype, []).append(body)
        i += hlen
    return recs


def txt(b):
    return b.decode("latin-1", "replace").strip("\x00").strip()


def summarize(path: Path, full=False):
    r = read_headers(path)
    out = {"arquivo": path.name, "tipo_arquivo": r["_file_type"]}
    if 4 in r:
        out["anotacao"] = txt(r[4][0])
    if 1 in r:
        b = r[1][0]
        bpp, cols, lines = b[0], struct.unpack(">H", b[1:3])[0], struct.unpack(">H", b[3:5])[0]
        out["imagem"] = f"{cols}x{lines} {bpp} bits"
    if 129 in r:
        b = r[129][0]
        sig = txt(b[:4])
        pid, sub, par = struct.unpack(">HHH", b[4:10])
        out["noaa"] = f"{sig} produto={pid} sub={sub} param={par}"
    if 5 in r:
        b = r[5][0]
        if len(b) >= 7:
            days, ms = struct.unpack(">HI", b[1:7])
            out["horario"] = time.strftime("%Y-%m-%d %H:%M:%SZ",
                                           time.gmtime((days - 4383) * 86400 + ms / 1000))
    if 6 in r:
        out["texto_auxiliar"] = txt(r[6][0])[:300]
    if 3 in r:
        f = txt(r[3][0])
        lines = [x for x in f.replace("\r", "").split("\n") if x]
        out["calibracao"] = f"SIM ({len(lines)} linhas)"
        out["calibracao_inicio"] = lines[:6] if not full else lines
    else:
        out["calibracao"] = "não"
    out["registros"] = ", ".join(f"{k}:{NAMES.get(k, '?')}" for k in r if isinstance(k, int))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--min", type=float, default=None, help="só arquivos modificados nos últimos N minutos")
    ap.add_argument("--filtro", default="", help="trecho do nome do arquivo")
    ap.add_argument("--full", action="store_true", help="mostra a tabela de calibração completa")
    args = ap.parse_args()

    files = [args.path] if args.path.is_file() else sorted(
        (p for p in args.path.rglob("*") if p.is_file() and args.filtro in p.name), key=lambda p: p.stat().st_mtime)
    if args.min is not None:
        lim = time.time() - args.min * 60
        files = [p for p in files if p.stat().st_mtime >= lim]
    for p in files:
        try:
            s = summarize(p, args.full)
        except Exception as e:  # noqa: BLE001
            print(f"{p.name}: erro: {e}", file=sys.stderr)
            continue
        print("-" * 70)
        for k, v in s.items():
            if isinstance(v, list):
                print(f"  {k}:")
                for x in v:
                    print(f"      {x}")
            else:
                print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
