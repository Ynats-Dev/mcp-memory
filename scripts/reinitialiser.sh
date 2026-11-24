#!/usr/bin/env bash
# Supprime tous les documents et tout leur historique Git.
# La configuration est conservée (.env, accès OAuth du volume).
# Le contexte repart vide (un seul commit « Initialisation du contexte »).
# Une sauvegarde complète est faite avant (restaurable avec restauration.sh).
# Usage : ./scripts/reinitialiser.sh   (sur le serveur, depuis le dossier du projet)
set -euo pipefail
cd "$(dirname "$0")/.."
VOLUME="mcp-memory_contexte-data"

echo "ATTENTION : tous les documents et leur historique vont être effacés (la configuration est conservée)."
read -r -p "Taper EFFACER pour confirmer : " R
[ "$R" = "EFFACER" ] || { echo "Abandon."; exit 1; }

./scripts/sauvegarde.sh            # filet de sécurité
docker compose stop mcp web
docker run --rm -v "$VOLUME":/data alpine rm -rf /data/contexte /data/.git
docker compose start mcp web       # le serveur recrée un dépôt Git vide au démarrage

for i in $(seq 1 15); do
  curl -fs http://127.0.0.1:8766/sante >/dev/null && break
  sleep 2
done
docker compose exec -T mcp git -C /data log --oneline
echo "Contexte réinitialisé. Sauvegarde de l'état précédent dans ./sauvegardes/."
