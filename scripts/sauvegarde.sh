#!/usr/bin/env bash
# Sauvegarde du volume (fichiers + historique Git complet) dans ./sauvegardes/
# Usage : ./scripts/sauvegarde.sh        (à lancer depuis le dossier du projet)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p sauvegardes
NOM="contexte-$(date +%Y%m%d-%H%M%S)"
VOLUME="mcp-memory_contexte-data"

# 1) Archive complète du volume (lecture seule, conteneurs laissés en marche)
docker run --rm -v "$VOLUME":/data:ro -v "$PWD/sauvegardes":/out alpine \
  tar czf "/out/$NOM.tgz" -C /data .

# 2) Bundle Git : un seul fichier contenant tout l'historique, clonable
docker compose exec -T mcp git -C /data bundle create - --all > "sauvegardes/$NOM.bundle"

# Rotation : on garde les 30 dernières sauvegardes de chaque type
ls -1t sauvegardes/contexte-*.tgz    | tail -n +31 | xargs -r rm --
ls -1t sauvegardes/contexte-*.bundle | tail -n +31 | xargs -r rm --
echo "Sauvegarde : sauvegardes/$NOM.tgz et sauvegardes/$NOM.bundle"
