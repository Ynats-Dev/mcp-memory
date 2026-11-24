#!/usr/bin/env bash
# Installe le service hôte de mise à jour (bouton « Mise à jour » de l'interface web).
#
#   sudo /opt/mcp-memory/scripts/installer-maj.sh            installer / réinstaller
#   sudo /opt/mcp-memory/scripts/installer-maj.sh --retirer  désinstaller
#
# Crée trois unités systemd :
#   mcp-memory-maj.path     surveille .maj/demande (déposé par l'interface web)
#   mcp-memory-maj.service  exécute scripts/maj-hote.sh auto
#   mcp-memory-maj.timer    vérifie les nouveautés toutes les 6 h (mcp-memory-maj-verif.service)
# Le service tourne sous le propriétaire du dépôt, qui doit pouvoir utiliser Docker.

set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }
DIR="$(cd "$(dirname "$0")/.." && pwd)"
U=/etc/systemd/system

if [ "${1:-}" = "--retirer" ]; then
  systemctl disable --now mcp-memory-maj.path mcp-memory-maj.timer 2>/dev/null || true
  rm -f $U/mcp-memory-maj.{path,service,timer} $U/mcp-memory-maj-verif.service
  systemctl daemon-reload
  echo "Service de mise à jour retiré."
  exit 0
fi

# Anciennes unités (avant le renommage en mcp-memory)
if [ -e $U/mcp-serveur-maj.path ]; then
  systemctl disable --now mcp-serveur-maj.path mcp-serveur-maj.timer 2>/dev/null || true
  rm -f $U/mcp-serveur-maj.{path,service,timer} $U/mcp-serveur-maj-verif.service
  echo "Anciennes unités mcp-serveur-maj.* retirées."
fi

PROPRIO="$(stat -c %U "$DIR")"
GROUPE="$(stat -c %G "$DIR")"
if [ "$PROPRIO" = root ] || ! sudo -u "$PROPRIO" docker info >/dev/null 2>&1; then
  # Docker inaccessible au propriétaire : exécution en root, Git autorisé explicitement sur ce dépôt.
  UTIL=root
  git config --system --get-all safe.directory 2>/dev/null | grep -qx "$DIR" \
    || git config --system --add safe.directory "$DIR"
  echo "! $PROPRIO ne peut pas utiliser Docker : le service tournera en root."
  echo "  (Préférable : sudo usermod -aG docker $PROPRIO, puis relancer ce script.)"
else
  UTIL="$PROPRIO"
fi

chmod +x "$DIR/scripts/maj-hote.sh"
# .maj est monté dans le conteneur web (utilisateur 1000) : il y dépose « demande » et lit l'état.
mkdir -p "$DIR/.maj"
if [ "$(id -u "$UTIL")" = 1000 ]; then
  chown "$UTIL:$GROUPE" "$DIR/.maj"; chmod 755 "$DIR/.maj"
else
  chown "$UTIL" "$DIR/.maj"; chmod 777 "$DIR/.maj"
fi
chown "$UTIL" "$DIR/.maj"/* "$DIR/.maj"/.verrou 2>/dev/null || true

cat > $U/mcp-memory-maj.service <<EOF
[Unit]
Description=MCP Memory : demande de l'interface web (vérifier / mettre à jour)
After=docker.service network-online.target
[Service]
Type=oneshot
User=$UTIL
WorkingDirectory=$DIR
ExecStart=$DIR/scripts/maj-hote.sh auto
TimeoutStartSec=1800
EOF

cat > $U/mcp-memory-maj.path <<EOF
[Unit]
Description=MCP Memory : surveille les demandes de mise à jour
[Path]
PathExists=$DIR/.maj/demande
Unit=mcp-memory-maj.service
[Install]
WantedBy=multi-user.target
EOF

cat > $U/mcp-memory-maj-verif.service <<EOF
[Unit]
Description=MCP Memory : recherche de mises à jour
After=network-online.target
[Service]
Type=oneshot
User=$UTIL
WorkingDirectory=$DIR
ExecStart=$DIR/scripts/maj-hote.sh verifier
TimeoutStartSec=300
EOF

cat > $U/mcp-memory-maj.timer <<EOF
[Unit]
Description=MCP Memory : recherche de mises à jour toutes les 6 h
[Timer]
OnBootSec=5min
OnUnitActiveSec=6h
Unit=mcp-memory-maj-verif.service
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now mcp-memory-maj.path mcp-memory-maj.timer
echo "→ Première vérification…"
systemctl start mcp-memory-maj-verif.service || true
cat "$DIR/.maj/derniere.log" 2>/dev/null || true
echo
echo "Installé (service exécuté par : $UTIL)."
echo "Si le conteneur web a été créé avant ce script : cd $DIR && docker compose up -d"
