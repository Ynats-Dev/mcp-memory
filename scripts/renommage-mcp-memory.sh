#!/usr/bin/env bash
# Migration unique : renommage « mcp-serveur » → « mcp-memory » sur l'hôte.
#
#   cd /opt/mcp-serveur && git pull && sudo scripts/renommage-mcp-memory.sh
#
# 1. arrête la pile Docker « mcp-serveur » (volume conservé) ;
# 2. copie le volume mcp-serveur_contexte-data → mcp-memory_contexte-data (documents, historique Git,
#    compte de l'interface) ;
# 3. déplace le dossier /opt/mcp-serveur → /opt/mcp-memory ;
# 4. démarre la pile « mcp-memory » et réinstalle le service de mise à jour s'il existait.
# L'ancien volume et l'ancienne image sont conservés : commandes de nettoyage affichées à la fin.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }

ANCIEN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NOUVEAU_DIR="$(dirname "$ANCIEN_DIR")/mcp-memory"
ANCIEN_VOL="mcp-serveur_contexte-data"
NOUVEAU_VOL="mcp-memory_contexte-data"
PROPRIO="$(stat -c %U "$ANCIEN_DIR")"

grep -q '^name: mcp-memory' "$ANCIEN_DIR/docker-compose.yml" \
  || { echo "docker-compose.yml n'est pas encore à jour : faire « git pull » d'abord." >&2; exit 1; }
if [ "$ANCIEN_DIR" != "$NOUVEAU_DIR" ] && [ -e "$NOUVEAU_DIR" ]; then
  echo "$NOUVEAU_DIR existe déjà : abandon (rien n'a été modifié)." >&2; exit 1
fi
if docker volume inspect "$NOUVEAU_VOL" >/dev/null 2>&1; then
  echo "Le volume $NOUVEAU_VOL existe déjà : migration déjà faite ? Abandon." >&2; exit 1
fi
docker volume inspect "$ANCIEN_VOL" >/dev/null 2>&1 \
  || { echo "Volume $ANCIEN_VOL introuvable : rien à migrer." >&2; exit 1; }

MAJ_INSTALLE=false
if [ -e /etc/systemd/system/mcp-serveur-maj.path ]; then
  MAJ_INSTALLE=true
  echo "→ Arrêt du service de mise à jour (ancien nom)"
  systemctl disable --now mcp-serveur-maj.path mcp-serveur-maj.timer 2>/dev/null || true
  rm -f /etc/systemd/system/mcp-serveur-maj.{path,service,timer} /etc/systemd/system/mcp-serveur-maj-verif.service
  systemctl daemon-reload
fi

echo "→ Arrêt de la pile mcp-serveur (le volume est conservé)"
docker compose -p mcp-serveur -f "$ANCIEN_DIR/docker-compose.yml" down --remove-orphans

echo "→ Copie du volume $ANCIEN_VOL → $NOUVEAU_VOL"
docker volume create \
  --label com.docker.compose.project=mcp-memory \
  --label com.docker.compose.volume=contexte-data \
  "$NOUVEAU_VOL" >/dev/null
docker run --rm -v "$ANCIEN_VOL":/from:ro -v "$NOUVEAU_VOL":/to alpine sh -c "cp -a /from/. /to/"
echo "  $(docker run --rm -v "$NOUVEAU_VOL":/d alpine sh -c 'find /d/contexte -name "*.md" | wc -l') document(s) copiés"

if [ "$ANCIEN_DIR" != "$NOUVEAU_DIR" ]; then
  echo "→ Déplacement $ANCIEN_DIR → $NOUVEAU_DIR"
  mv "$ANCIEN_DIR" "$NOUVEAU_DIR"
fi
cd "$NOUVEAU_DIR"

echo "→ Démarrage de la pile mcp-memory"
VERSION="$(sudo -u "$PROPRIO" git -c safe.directory="$NOUVEAU_DIR" rev-parse --short HEAD 2>/dev/null || true)"
VERSION="$VERSION" docker compose up -d --build
for _ in $(seq 1 20); do
  curl -fs http://127.0.0.1:8765/sante >/dev/null && curl -fs http://127.0.0.1:8766/sante >/dev/null && break
  sleep 2
done
docker compose ps

if $MAJ_INSTALLE; then
  echo "→ Réinstallation du service de mise à jour (mcp-memory-maj.*)"
  rm -rf "$NOUVEAU_DIR/.maj"   # recréé avec les bons droits par l'installeur
  "$NOUVEAU_DIR/scripts/installer-maj.sh"
  docker compose up -d   # remonte .maj dans le conteneur web
fi

cat <<EOF

✓ Renommage terminé : $NOUVEAU_DIR, pile et volume « mcp-memory ».
  À faire :
  - mettre à jour le script de déploiement (cd $NOUVEAU_DIR) ;
  - côté clients : renommer l'entrée « mcp-serveur » en « mcp-memory » (Claude Desktop, Claude Code, VS Code).
  Une fois l'interface et l'historique vérifiés, nettoyage :
    docker volume rm $ANCIEN_VOL && docker image rm mcp-serveur:latest
EOF
