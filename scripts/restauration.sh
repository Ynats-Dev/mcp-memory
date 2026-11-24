#!/usr/bin/env bash
# Restaure le volume depuis une archive .tgz produite par sauvegarde.sh
# Usage : ./scripts/restauration.sh sauvegardes/contexte-AAAAMMJJ-HHMMSS.tgz
set -euo pipefail
cd "$(dirname "$0")/.."
ARCHIVE="${1:?Usage : $0 sauvegardes/contexte-....tgz}"
[ -f "$ARCHIVE" ] || { echo "Archive introuvable : $ARCHIVE"; exit 1; }
VOLUME="mcp-memory_contexte-data"

read -r -p "Le contenu actuel sera remplacé (une copie de sécurité est faite avant). Continuer ? [o/N] " R
[ "$R" = "o" ] || exit 1

./scripts/sauvegarde.sh            # filet de sécurité
docker compose stop mcp web
docker run --rm -v "$VOLUME":/data -v "$PWD/$(dirname "$ARCHIVE")":/in:ro alpine \
  sh -c "find /data -mindepth 1 -delete && tar xzf /in/$(basename "$ARCHIVE") -C /data && chown -R 1000:1000 /data"
docker compose start mcp web
echo "Restauration terminée depuis $ARCHIVE"
