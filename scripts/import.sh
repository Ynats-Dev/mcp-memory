#!/usr/bin/env bash
# Importe les fichiers .md du dossier ./import dans une catégorie du contexte.
# Usage : ./scripts/import.sh home [--essai]   (sur le serveur, depuis le dossier du projet)
set -euo pipefail
cd "$(dirname "$0")/.."
CATEGORIE="${1:?Usage : $0 <categorie> [--essai]}"; shift
[ -d import ] || { echo "Dossier ./import absent : y copier les fichiers .md."; exit 1; }
docker compose run --rm --no-deps -T -v "$PWD/import":/import:ro mcp \
  python -m app.importer --categorie "$CATEGORIE" /import "$@"
