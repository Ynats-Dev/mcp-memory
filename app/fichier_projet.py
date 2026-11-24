"""Génère le fichier Markdown à déposer dans le contexte d'un projet claude.ai.

Le fichier ne connecte pas Claude au serveur (c'est le rôle de la configuration
MCP de Claude Desktop ou de Claude Code, sur le réseau local) : il lui dit de
s'en servir comme unique source de contexte pour ce projet, et de le tenir à jour.
"""

from __future__ import annotations

import re

from .sections import taille

SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def categorie_valide(categorie: str) -> str:
    categorie = (categorie or "").strip().lower()
    if not SEGMENT_RE.match(categorie):
        raise ValueError("Catégorie invalide : minuscules, chiffres et tirets (ex. « home »).")
    return categorie


def generer(projet: str, categorie: str, url_mcp: str, documents: list, autonome: bool = True) -> str:
    projet = projet.strip() or categorie
    nb = len(documents)
    volume = sum(len(d.contenu) for d in documents)

    if autonome:
        maj = f"""## Mise à jour du contexte (automatique)

Tiens le contexte à jour **de toi-même** dès qu'un fait est établi ou change (configuration,
adresse IP, version, commande, procédure validée, panne résolue), puis signale-le en une ligne
(document + changement). Document absent → crée `{categorie}/<nom>` (minuscules, tirets)."""
    else:
        maj = f"""## Mise à jour du contexte (sur validation)

Quand une information mérite d'être conservée, propose la mise à jour (document `{categorie}/…`
+ texte) et ne l'applique qu'après mon accord."""

    return f"""# Projet « {projet} » — contexte sur le serveur MCP mcp-memory

Le contexte de ce projet est sur le serveur MCP local **mcp-memory** (`{url_mcp}`), seule
source de vérité, partagée avec Claude Code et VS Code ; projet `{categorie}`, {nb} document(s),
{taille(round(volume / 3.5))} au total : ne jamais tout charger.
Si les outils mcp-memory ne sont pas disponibles (hors Claude Desktop, hors réseau local,
serveur arrêté), dis-le dès ta première réponse et ne réponds pas de mémoire sur le projet.

## Lecture

1. Début de conversation : `vue_ensemble` avec `projet` = `{categorie}` (fiche résumé + index).
2. Question précise : `rechercher` avec `projet` = `{categorie}` (document **et section**).
3. Sinon `plan_document`, puis `lire_document` avec `section` (numéro ou titre).
4. `complet=true` uniquement si tout le document est nécessaire.

{maj}

## Règles d'écriture

- Corrige l'information **à sa place** (`remplacer_texte`) : pas de nouvelle section datée, pas
  de « 🆕 », pas de journal des changements (l'historique Git garde l'ancien état). Supprime le
  périmé ; un incident résolu tient en une ligne cause / solution.
- Tiens à jour la fiche `{categorie}/resume` (~500 tokens : l'essentiel du projet) ; crée-la si absente.
- Lis la section avant de la modifier ; évite `ecrire_document` (remplace tout le document).
- Décision validée → `enregistrer_decision`, titre préfixé par `[{projet}]`.
- `auteur` = « Claude (projet {projet}) », `message` décrivant le changement.
- Faits confirmés uniquement ; ne supprime rien sans demande explicite.
"""
