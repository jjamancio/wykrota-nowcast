#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# previsao.sh — previsão completa (sondagem + GOES-19) para um observatório.
# Uso: ./previsao.sh [horario_alvo_ISO]     ex.: ./previsao.sh 2026-11-25T00:00Z
# Configuração da máquina em config.local.sh (veja config.local.sh.example).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$DIR/astro_config.toml"
if [[ ! -f "$DIR/config.local.sh" ]]; then
    echo "Crie $DIR/config.local.sh a partir de config.local.sh.example" >&2
    exit 1
fi
# shellcheck source=/dev/null
source "$DIR/config.local.sh"
PY="${PY:-python3}"
LOG_QUIET_MIN="${LOG_QUIET_MIN:-15}"

TARGET="${1:-}"
mkdir -p "$OUT"

# Usa o log mais recente que NÃO foi modificado nos últimos $LOG_QUIET_MIN minutos
# (ou seja, cuja recepção já terminou). Sondagens em voo são ignoradas.
LOG="$(find "$SONDE_LOGS" -maxdepth 1 -name '*_sonde.log' -mmin +"$LOG_QUIET_MIN" -printf '%T@ %p\n' \
      | sort -n | tail -1 | cut -d' ' -f2-)"
if [[ -z "$LOG" ]]; then echo "Nenhum log de sonda concluído em $SONDE_LOGS" >&2; exit 1; fi
EMVOO="$(find "$SONDE_LOGS" -maxdepth 1 -name '*_sonde.log' -mmin -"$LOG_QUIET_MIN" | head -1)"
[[ -n "$EMVOO" ]] && echo "Aviso: $(basename "$EMVOO") ainda está sendo recebido; usando $(basename "$LOG")." >&2

STAMP="$(date -u +%Y%m%dT%H%MZ)"
BASE="$OUT/${SITE}_${STAMP}"
TGT=(); [[ -n "$TARGET" ]] && TGT=(--target "$TARGET")
COMMON=(--site "$SITE" --lat "$LAT" --lon "$LON" --obs-elev "$ELEV" --config "$CONF" "${TGT[@]}")

# 1) sondagem → JSON com ventos e condições no observatório
"$PY" "$DIR/astro_sounding.py" "$LOG" "${COMMON[@]}" \
    --json "$BASE.sonda.json" --pdf "$BASE.sonda.pdf" --png "$BASE.skewt.png" > /dev/null

# 2) satélite (usa os ventos da sondagem para olhar contra o vento)
"$PY" "$DIR/goes_nowcast.py" --root "$GOES_ROOT" --site "$SITE" --lat "$LAT" --lon "$LON" \
    --config "$CONF" --sounding-json "$BASE.sonda.json" \
    --l2-root "$GOES_L2" --lrit-dir "$GOES_LRIT" \
    --json "$BASE.goes.json" --png "$BASE.goes.png"

# 3) relatório final combinado
"$PY" "$DIR/astro_sounding.py" "$LOG" "${COMMON[@]}" --sat-json "$BASE.goes.json" \
    --json "$BASE.json" --pdf "$BASE.pdf" --png "$BASE.skewt.png"

rm -f "$BASE.sonda.pdf"
echo "Relatório: $BASE.pdf"

# 4) publicação opcional na GitHub Page
if [[ "${PUBLICAR:-0}" == "1" ]]; then
    "$DIR/publicar.sh" || echo "Aviso: falha ao publicar (o relatório local foi gerado)." >&2
fi
