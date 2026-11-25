#!/bin/bash
# Hook PreToolUse (Bash) : directives Git globales, appliquées localement.
# Filet de sécurité : fonctionne même quand le serveur MCP Memory (règles de la page Hooks) est injoignable.
# - Refuse tout commit / PR / tag signé au nom de Claude (Co-Authored-By, « Generated with
#   Claude Code », auteur ou committer Claude).
# - Exige une confirmation explicite avant tout « git push », en affichant son détail
#   (dépôt distant, branches, commits envoyés).
entree=$(cat)
cmd=$(jq -r '.tool_input.command // empty' <<<"$entree")
[ -z "$cmd" ] && exit 0
cwd=$(jq -r '.cwd // empty' <<<"$entree")

repondre() {  # $1 = allow|ask|deny, $2 = raison
  jq -n --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: $d, permissionDecisionReason: $r}}'
  exit 0
}

# Détail d'un push, d'après l'état local du dépôt (sans accès réseau).
detail_push() {
  local dir remote="" src="" dst="" args mot url branche base n log
  dir=$(printf '%s' "$cmd" | grep -oE 'git[[:space:]]+-C[[:space:]]+[^[:space:];&|]+' | head -1 | awk '{print $3}' | tr -d "\"'")
  dir="${dir:-$cwd}"; dir="${dir/#\~/$HOME}"
  [ -n "$dir" ] && git -C "$dir" rev-parse --git-dir >/dev/null 2>&1 || { echo "(détail indisponible : dépôt introuvable)"; return; }
  # Arguments de « push » jusqu'à la fin de la commande (; & |) : distant puis référence, options ignorées
  args=$(printf '%s' "$cmd" | sed -E 's/.*[[:space:]]push([[:space:]]|$)//' | sed -E 's/[;&|].*//')
  for mot in $args; do
    case "$mot" in -*) continue ;; esac
    if [ -z "$remote" ]; then remote="$mot"; elif [ -z "$src" ]; then src="$mot"; fi
  done
  branche=$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ -z "$remote" ]; then remote=$(git -C "$dir" rev-parse --abbrev-ref '@{u}' 2>/dev/null | cut -d/ -f1); fi
  remote="${remote:-origin}"
  case "$src" in *:*) dst="${src#*:}"; src="${src%%:*}" ;; esac
  src="${src:-HEAD}"; dst="${dst:-${src/#HEAD/$branche}}"; dst="${dst#refs/heads/}"; src="${src#+}"
  git -C "$dir" rev-parse -q --verify "$src" >/dev/null 2>&1 || src=HEAD   # référence absente : HEAD
  url=$(git -C "$dir" remote get-url "$remote" 2>/dev/null || echo "$remote")
  base="$remote/$dst"
  git -C "$dir" rev-parse -q --verify "$base" >/dev/null 2>&1 || base=""
  echo "Push vers $remote ($url)"
  echo "Dépôt local : $dir · $src → $dst"
  if [ -n "$base" ]; then
    n=$(git -C "$dir" rev-list --count "$base..$src" 2>/dev/null || echo "?")
    log=$(git -C "$dir" log --format='  %h %an — %<(90,trunc)%s' -n 15 "$base..$src" 2>/dev/null | sed 's/ *$//')
    echo "$n commit(s) envoyé(s) (d'après le dernier fetch) :"
    printf '%s\n' "${log:-  (aucun nouveau commit)}"
    [ "$n" != "?" ] && [ "$n" -gt 15 ] 2>/dev/null && echo "  … et $((n - 15)) autre(s)"
  else
    echo "Branche distante $dst inconnue localement (nouvelle branche ?). Derniers commits :"
    git -C "$dir" log --format='  %h %an — %<(90,trunc)%s' -n 5 "$src" 2>/dev/null | sed 's/ *$//'
  fi
}

# 1. Signature au nom de Claude dans une commande Git / GitHub
if printf '%s' "$cmd" | grep -Eqi '(git([[:space:]]+-[Cc][[:space:]]+[^[:space:]]+)*[[:space:]]+(commit|tag|notes|commit-tree|merge|rebase))|gh[[:space:]]+(pr|issue|release)'; then
  if printf '%s' "$cmd" | grep -Eqi 'co-authored-by:[^"'"'"']*(claude|anthropic)|generated with \[?claude|noreply@anthropic\.com|--author[= ]+["'"'"']?[^"'"'"']*claude|GIT_(AUTHOR|COMMITTER)_(NAME|EMAIL)=["'"'"']?[^[:space:]]*(claude|anthropic)'; then
    repondre deny "Directive globale : les commits, tags et PR se font sous l'identité Git de l'utilisateur, jamais au nom de Claude. Retirer toute ligne « Co-Authored-By: Claude », « Generated with Claude Code » ou tout auteur Claude, puis relancer."
  fi
fi

# 2. git push : toujours demander l'accord explicite, avec le détail du push
if printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])git([[:space:]]+-[Cc][[:space:]]+[^[:space:]]+)*[[:space:]]+push([[:space:]]|$)'; then
  repondre ask "Directive globale : aucun git push sans l'accord explicite de l'utilisateur.
$(detail_push)"
fi

exit 0
