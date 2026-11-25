#!/usr/bin/env bash
# Mise à jour de MCP Memory, exécutée SUR L'HÔTE (service systemd, jamais dans un conteneur).
#
#   scripts/maj-hote.sh verifier    git fetch du dépôt de référence + état (nouveautés ?)
#   scripts/maj-hote.sh appliquer   avance sur le dépôt de référence + docker compose up -d --build
#   scripts/maj-hote.sh auto        traite la demande déposée par l'interface web (.maj/demande)
#
# Dépôt de référence : .maj/depot (lignes « url=… » et « branche=… », écrites par l'interface),
# à défaut DEPOT_DEFAUT / BRANCHE_DEFAUT ci-dessous. Il est suivi par un remote dédié
# « reference » : le remote « origin » du dossier n'est jamais modifié.
#
# L'interface web ne peut que déposer le mot « verifier » ou « appliquer » dans .maj/demande :
# elle n'a accès ni à Docker ni au dépôt. L'état est publié dans .maj/etat.json (lu par l'interface)
# et le journal de la dernière opération dans .maj/derniere.log.
# Installation : sudo scripts/installer-maj.sh

set -uo pipefail
export LC_ALL=C.UTF-8
DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAJ="$DIR/.maj"
ETAT="$MAJ/etat.json"
LOG="$MAJ/derniere.log"
DEMANDE="$MAJ/demande"
CONFIG="$MAJ/depot"
DEPOT_DEFAUT="https://github.com/Ynats-Dev/mcp-memory.git"
BRANCHE_DEFAUT="master"
cd "$DIR" || exit 1
mkdir -p "$MAJ"

exec 9>"$MAJ/.verrou"
flock -w 1800 9 || { echo "Verrou occupé" >&2; exit 1; }

action="${1:-auto}"
if [ "$action" = auto ]; then
  [ -f "$DEMANDE" ] || exit 0
  action="$(head -c 20 "$DEMANDE" | tr -cd 'a-z')"
  rm -f "$DEMANDE"
fi
case "$action" in verifier|appliquer) ;; *) echo "Action inconnue : $action" >&2; exit 2 ;; esac

maintenant() { date -Iseconds; }
json() {  # chaîne JSON échappée
  local s=${1//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\t'/ }; s=${s//$'\r'/}; s=${s//$'\n'/\\n}
  printf '"%s"' "$s"
}
precedent() {  # champ texte de l'état précédent (pour conserver maj_le / verifie_le)
  [ -f "$ETAT" ] && sed -n "s/.*\"$1\": *\"\\([^\"]*\\)\".*/\\1/p" "$ETAT" | head -1
}

# Dépôt de référence. Le fichier est écrit par l'interface web : il est relu et validé ici,
# jamais exécuté (pas de « source »).
url_valide() {
  [[ "$1" =~ ^(https?|ssh)://[A-Za-z0-9._~%:/+@-]+$ || "$1" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._~/+-]+$ ]] \
    && ! [[ "$1" =~ ^https?://[^/@]*:[^/@]*@ ]]   # pas d'identifiants dans l'adresse
}
branche_valide() { [[ "$1" =~ ^[A-Za-z0-9._/-]{1,100}$ && "$1" != -* && "$1" != *..* ]]; }
DEPOT="$DEPOT_DEFAUT" BRANCHE="$BRANCHE_DEFAUT" CONFIG_ERREUR=""
if [ -f "$CONFIG" ]; then
  u="$(sed -n 's/^url=//p' "$CONFIG" | head -1 | tr -d '\r')"
  b="$(sed -n 's/^branche=//p' "$CONFIG" | head -1 | tr -d '\r')"
  if [ -n "$u" ] && url_valide "$u"; then DEPOT="$u"; elif [ -n "$u" ]; then CONFIG_ERREUR="adresse invalide dans .maj/depot"; fi
  if [ -n "$b" ] && branche_valide "$b"; then BRANCHE="$b"; elif [ -n "$b" ]; then CONFIG_ERREUR="branche invalide dans .maj/depot"; fi
fi
REF="reference/$BRANCHE"

STATUT=pret MESSAGE="" VERIFIE_LE="$(precedent verifie_le)" MAJ_LE="$(precedent maj_le)"
ecrire_etat() {
  local branche local_sha local_date local_sujet distant retard modifs diverge commits="" sep=""
  branche="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  local_sha="$(git rev-parse --short HEAD 2>/dev/null)"
  local_date="$(git log -1 --format=%cI 2>/dev/null)"
  local_sujet="$(git log -1 --format=%s 2>/dev/null)"
  distant="$(git rev-parse --short "$REF" 2>/dev/null)"
  retard="$(git rev-list --count "HEAD..$REF" 2>/dev/null || echo 0)"
  # Historique sans lien avec la référence (autre dépôt) : la mise à jour réalignera le dossier.
  diverge=false
  if [ -n "$distant" ] && ! git merge-base --is-ancestor HEAD "$REF" 2>/dev/null; then
    diverge=true
    git diff --quiet HEAD "$REF" 2>/dev/null && retard=0   # même contenu : rien à faire
  fi
  modifs=false; [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ] && modifs=true
  while IFS=$'\t' read -r sha date sujet; do
    [ -n "$sha" ] || continue
    commits+="$sep{\"sha\": $(json "$sha"), \"date\": $(json "$date"), \"sujet\": $(json "$sujet")}"; sep=", "
  done < <(git log --format='%h%x09%cI%x09%s' -n 30 "HEAD..$REF" 2>/dev/null)
  cat > "$ETAT.tmp" <<EOF
{"statut": $(json "$STATUT"), "message": $(json "$MESSAGE"), "action": $(json "$action"),
 "branche": $(json "$branche"), "local": $(json "$local_sha"), "local_date": $(json "$local_date"),
 "local_sujet": $(json "$local_sujet"), "distant": $(json "$distant"), "retard": ${retard:-0},
 "depot": $(json "$DEPOT"), "depot_branche": $(json "$BRANCHE"), "depot_defaut": $([ "$DEPOT" = "$DEPOT_DEFAUT" ] && [ "$BRANCHE" = "$BRANCHE_DEFAUT" ] && echo true || echo false),
 "config_erreur": $(json "$CONFIG_ERREUR"), "diverge": $diverge,
 "modifs_locales": $modifs, "commits": [$commits],
 "verifie_le": $(json "$VERIFIE_LE"), "maj_le": $(json "$MAJ_LE"), "ecrit_le": $(json "$(maintenant)")}
EOF
  chmod 644 "$ETAT.tmp" && mv -f "$ETAT.tmp" "$ETAT"
}
echec() { STATUT=erreur MESSAGE="$1"; echo "✗ $1"; ecrire_etat; exit 1; }

: > "$LOG"; chmod 644 "$LOG"
exec > >(tee -a "$LOG") 2>&1
echo "■ $(maintenant) — $action ($DIR)"

STATUT=$([ "$action" = appliquer ] && echo mise_a_jour || echo verification)
MESSAGE="En cours…"; ecrire_etat

[ -z "$CONFIG_ERREUR" ] || echec "Configuration du dépôt de référence : $CONFIG_ERREUR."
if git remote get-url reference >/dev/null 2>&1; then
  [ "$(git remote get-url reference)" = "$DEPOT" ] || git remote set-url reference "$DEPOT"
else
  git remote add --no-tags reference "$DEPOT"
fi
echo "→ git fetch $DEPOT ($BRANCHE)"
git fetch --prune --no-tags reference "+refs/heads/$BRANCHE:refs/remotes/reference/$BRANCHE" \
  || echec "git fetch a échoué : dépôt $DEPOT, branche $BRANCHE (adresse, réseau ou accès au dépôt)."
VERIFIE_LE="$(maintenant)"

if [ "$action" = appliquer ]; then
  avant="$(git rev-parse --short HEAD)"
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git status --short --untracked-files=no
    echec "Modifications locales dans $DIR : mise à jour annulée (git stash ou git checkout -- . d'abord)."
  fi
  if git merge-base --is-ancestor HEAD "$REF"; then
    echo "→ git merge --ff-only $REF"
    git merge --ff-only "$REF" || echec "Avance impossible sur $REF."
  else
    # Autre dépôt de référence (historique sans lien) : le dossier est aligné sur la référence.
    # Fichiers non suivis (.env, .maj, sauvegardes) intacts ; retour possible : git reset --hard <avant>.
    echo "→ historique différent de $REF : alignement (version précédente : $(git rev-parse HEAD))"
    git reset --hard "$REF" || echec "Alignement sur $REF impossible."
  fi
  apres="$(git rev-parse --short HEAD)"
  echo "→ docker compose up -d --build (version $apres)"
  # VERSION est inscrite dans l'image : l'interface affiche la version réellement en service.
  VERSION="$apres" docker compose up -d --build --remove-orphans || echec "docker compose a échoué (voir le journal)."
  docker image prune -f >/dev/null 2>&1 || true
  MAJ_LE="$(maintenant)"
  if [ "$avant" = "$apres" ]; then MESSAGE="Déjà à jour ($apres) : conteneurs reconstruits et redémarrés."
  else MESSAGE="Mis à jour : $avant → $apres, conteneurs redémarrés."; fi
else
  n="$(git rev-list --count "HEAD..$REF" 2>/dev/null || echo 0)"
  if ! git merge-base --is-ancestor HEAD "$REF" 2>/dev/null; then
    if git diff --quiet HEAD "$REF"; then MESSAGE="À jour (même contenu que $REF, historique différent)."
    else MESSAGE="Version différente du dépôt de référence : « Mettre à jour » alignera le serveur sur $REF."; fi
  elif [ "$n" -gt 0 ]; then MESSAGE="$n mise(s) à jour disponible(s)."; else MESSAGE="À jour."; fi
fi
STATUT=pret
echo "✓ $MESSAGE"
ecrire_etat
