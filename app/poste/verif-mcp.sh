#!/bin/bash
# Hook PreToolUse (tous les outils) : prévient Claude quand le serveur MCP Memory est injoignable.
#
# Le hook « mcp_tool » des règles (page Hooks) échoue alors sans bloquer (onFailure: continue) et
# Claude ne le voit pas. Ce script teste la page de santé du serveur (résultat gardé 60 s) et, à
# chaque changement d'état dans une session, ajoute une consigne au contexte de Claude :
# serveur injoignable → le signaler ; serveur rétabli → le signaler aussi.
SANTE="${MCP_SANTE:-__URL_SANTE__}"
DOSSIER="$HOME/.claude/hooks/.etat-mcp"
CACHE="$DOSSIER/sante"          # « ok|ko <epoch> »
DELAI=60

mkdir -p "$DOSSIER" || exit 0
session="$(jq -r '.session_id // "inconnue"' 2>/dev/null | tr -cd 'A-Za-z0-9-')"
session="${session:-inconnue}"
maintenant=$(date +%s)

etat="" horodatage=0
[ -f "$CACHE" ] && read -r etat horodatage < "$CACHE"
if [ -z "$etat" ] || [ $((maintenant - ${horodatage:-0})) -ge $DELAI ]; then
  if [ "$(curl -s -m 2 "$SANTE" 2>/dev/null)" = "ok" ]; then etat=ok; else etat=ko; fi
  echo "$etat $maintenant" > "$CACHE.tmp" && mv -f "$CACHE.tmp" "$CACHE"
  find "$DOSSIER" -name 'session-*' -mtime +2 -delete 2>/dev/null   # ménage des anciennes sessions
fi

signale_f="$DOSSIER/session-$session"
signale="$(cat "$signale_f" 2>/dev/null)"
[ "$etat" = "${signale:-ok}" ] && exit 0      # rien de nouveau pour cette session
echo "$etat" > "$signale_f"

if [ "$etat" = ko ]; then
  msg="⚠️ Le serveur MCP Memory ($SANTE) est injoignable : les règles de la page Hooks ne sont PAS appliquées (seules les protections locales le sont) et le contexte partagé n'est pas disponible. Signale-le à l'utilisateur en une ligne dans ta prochaine réponse."
else
  msg="✓ Le serveur MCP Memory répond de nouveau : les règles de la page Hooks s'appliquent. Signale-le à l'utilisateur en une ligne."
fi
jq -n --arg m "$msg" '{hookSpecificOutput: {hookEventName: "PreToolUse", additionalContext: $m}}'
exit 0
