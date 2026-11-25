#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# verificar.sh — Confere as previsões das últimas noites (rode de manhã, pelo cron).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/config.local.sh"
PY="${PY:-python3}"
"$PY" "$DIR/verificar.py" --entrada "$OUT" --goes-root "$GOES_ROOT" \
    --lat "$LAT" --lon "$LON" --site "$SITE" --config "$DIR/astro_config.toml" \
    --registro "${REGISTRO:-$DIR/registro_observacoes.csv}" --saida "${VERIF_DIR:-$OUT/verificacao}"
