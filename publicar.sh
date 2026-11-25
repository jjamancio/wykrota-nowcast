#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# publicar.sh — Atualiza a GitHub Page com os relatórios dos últimos dias.
#
# A branch gh-pages é recriada a cada publicação com UM único commit (sem histórico),
# para que o repositório não cresça indefinidamente com PDFs antigos.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$DIR/config.local.sh"
PY="${PY:-python3}"
: "${GIT_REMOTE:?Defina GIT_REMOTE em config.local.sh}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

"$PY" "$DIR/gerar_site.py" --entrada "$OUT" --saida "$TMP/site" --site "$SITE" \
    --dias "${DIAS_PAGINA:-7}" --url-repo "${URL_REPO:-}"

cd "$TMP/site"
git init -q
git checkout -q -b gh-pages
git add -A
git -c user.name="${GIT_NOME:-wykrota-nowcast}" -c user.email="${GIT_EMAIL:-wykrota-nowcast@users.noreply.github.com}" \
    commit -q -m "Relatórios atualizados em $(date -u +%Y-%m-%dT%H:%MZ)"
git -c push.negotiate=false push -q -f "$GIT_REMOTE" gh-pages
echo "Página publicada."
