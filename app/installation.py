"""Procédure d'installation d'un poste Claude Code, servie par le serveur MCP.

GET <serveur>/claude-code/installation renvoie un texte Markdown destiné à Claude Code :
il suffit de lui demander « Installe MCP Memory sur ce poste en suivant <adresse> ». La page
ne contient aucun secret (le jeton est demandé à l'utilisateur) ; elle est donc publique,
comme /sante.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import regles

POSTE = Path(__file__).parent / "poste"
CHEMIN = "/claude-code/installation"


def _script(nom: str, url_sante: str) -> str:
    return (POSTE / nom).read_text(encoding="utf-8").replace("__URL_SANTE__", url_sante)


def hooks_poste(serveur: str = regles.SERVEUR_MCP) -> list[dict]:
    """Les trois entrées PreToolUse à fusionner dans ~/.claude/settings.json."""
    global_ = regles.configuration_hook(serveur)["hooks"][regles.EVENEMENT][0]
    return [
        global_,
        {"matcher": "*", "hooks": [{"type": "command", "command": "~/.claude/hooks/verif-mcp.sh", "timeout": 5}]},
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/.claude/hooks/garde-git.sh", "timeout": 10}]},
    ]


def procedure(base: str) -> str:
    """Procédure complète ; `base` : adresse du serveur MCP sans « /mcp » (ex. https://mcp.example.com)."""
    base = base.rstrip("/")
    url_mcp, url_sante = f"{base}/mcp", f"{base}/sante"
    serveur = regles.SERVEUR_MCP
    bloc_hooks = json.dumps({"hooks": {"PreToolUse": hooks_poste(serveur)}}, ensure_ascii=False, indent=2)
    reglages = json.dumps({
        "attribution": {"commit": "", "pr": "", "sessionUrl": False},
        "permissions": {"ask": ["Bash(git push *)", "Bash(git push)"]},
    }, ensure_ascii=False, indent=2)
    return f"""# Installer MCP Memory sur ce poste (procédure pour Claude Code)

Serveur MCP : `{url_mcp}` · nom attendu dans Claude Code : `{serveur}`.

Règles pour toi, Claude :
- Annonce chaque étape en une ligne ; ne remplace jamais une configuration existante, **fusionne**.
- Sauvegarde `~/.claude/settings.json` avant de le modifier.
- N'affiche jamais le jeton en clair dans tes réponses.
- Si une étape échoue, arrête-toi et explique pourquoi.

## 1. Enregistrer le serveur MCP

1. `claude mcp list` : si un serveur pointe déjà vers `{url_mcp}`, note son nom et passe au point 3.
2. Sinon, demande à l'utilisateur son jeton (`MCP_TOKEN`, visible sur la page **Connexion Claude** de
   l'interface web), puis :
   ```bash
   claude mcp add --transport http --scope user {serveur} {url_mcp} --header "Authorization: Bearer <JETON>"
   ```
3. `claude mcp get <nom>` doit afficher `Scope: User config` et `Connected`. Si la portée est `Local` ou
   `Project`, réenregistre-le en portée utilisateur (`claude mcp remove <nom> -s <portée>`, puis le point 2).
4. Si le nom n'est pas `{serveur}`, remplace `"server": "{serveur}"` par ce nom dans le bloc du point 4.

## 2. Prérequis

`jq` et `curl` doivent être installés (`command -v jq curl`). S'il manque `jq`, propose son installation
(macOS : `brew install jq`) et attends l'accord de l'utilisateur.

## 3. Scripts locaux

Écris ces deux fichiers **avec ton outil d'écriture de fichiers** (pas avec une commande shell : ils
contiennent des motifs que les hooks bloquent dans une commande), puis `chmod +x ~/.claude/hooks/*.sh`.

`~/.claude/hooks/garde-git.sh` — filet local, actif même serveur éteint : refuse les commits, tags et PR
signés Claude, demande confirmation avant tout `git push` :

```bash
{_script("garde-git.sh", url_sante)}```

`~/.claude/hooks/verif-mcp.sh` — prévient Claude quand le serveur est injoignable (puis rétabli) :

```bash
{_script("verif-mcp.sh", url_sante)}```

## 4. Réglages `~/.claude/settings.json`

1. Sauvegarde : `cp ~/.claude/settings.json ~/.claude/settings.json.avant-mcp-memory` (crée `{{}}` s'il
   n'existe pas).
2. Fusionne dans `hooks.PreToolUse` les entrées ci-dessous **absentes** (compare `command`, ou `tool` pour
   l'entrée `mcp_tool`), sans toucher aux autres hooks :
   ```json
{bloc_hooks}
   ```
3. Fusionne ces réglages (ajoute aux listes, sans doublon) et **retire** `Bash(git push *)` de
   `permissions.allow` s'il y figure :
   ```json
{reglages}
   ```
4. Valide le fichier : `jq empty ~/.claude/settings.json`.

## 5. Vérifier

1. `true || git commit -m test -m "Co-Authored-By: Claude <noreply@anthropic.com>"` doit être **refusé**
   par le hook (ce refus est le résultat attendu ; `true ||` empêche toute exécution).
2. `echo '{{"session_id":"test"}}' | MCP_SANTE=https://injoignable.invalid/sante ~/.claude/hooks/verif-mcp.sh`
   doit afficher un avertissement « injoignable », puis `rm -rf ~/.claude/hooks/.etat-mcp`.
3. `curl -s {url_sante}` doit répondre `ok`.
4. Demande à l'utilisateur de taper `/hooks` : les trois hooks doivent y figurer.

## 6. Conclure

Résume ce qui a été installé et indique à l'utilisateur :
- les règles se gèrent sur la page **Hooks** de l'interface web (s'appliquent immédiatement) ;
- les outils du serveur apparaissent dans une **nouvelle session** (ou `/mcp` → Reconnect).
"""
