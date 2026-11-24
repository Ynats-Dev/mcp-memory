#!/usr/bin/env bash
# Migration unique : renommage du projet Docker « contexte-jeu » en « mcp-serveur ».
# Copie le volume contexte-jeu_contexte-data vers mcp-serveur_contexte-data
# (fichiers + historique Git), puis redémarre les services sous le nouveau nom.
# L'ancien volume est conservé : le supprimer à la main une fois la migration vérifiée.
# Usage : ./scripts/migration-volume.sh   (sur le serveur, depuis le dossier du projet)
set -euo pipefail
cd "$(dirname "$0")/.."
ANCIEN="contexte-jeu_contexte-data"
NOUVEAU="mcp-serveur_contexte-data"

docker volume inspect "$ANCIEN" >/dev/null 2>&1 || { echo "Volume $ANCIEN introuvable : rien à migrer."; exit 1; }
if docker volume inspect "$NOUVEAU" >/dev/null 2>&1; then
  echo "Le volume $NOUVEAU existe déjà : migration déjà faite ? Abandon."; exit 1
fi

# 1) Arrêt de l'ancienne pile (le volume n'est pas supprimé : pas de -v)
docker compose -p contexte-jeu down

# 2) Nouveau volume, étiqueté comme s'il avait été créé par Compose
docker volume create \
  --label com.docker.compose.project=mcp-serveur \
  --label com.docker.compose.volume=contexte-data \
  "$NOUVEAU" >/dev/null

# 3) Copie à l'identique (propriétaires, droits, dépôt .git)
docker run --rm -v "$ANCIEN":/from:ro -v "$NOUVEAU":/to alpine sh -c "cp -a /from/. /to/"

# 4) Démarrage sous le nouveau nom
docker compose up -d --build
docker compose ps
echo
echo "Migration terminée. Vérifier l'interface web et l'historique, puis supprimer l'ancien volume :"
echo "  docker volume rm $ANCIEN && docker image rm contexte-jeu:latest"
