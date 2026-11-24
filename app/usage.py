"""Journal d'usage des outils MCP : ce que Claude lit et écrit, et combien de tokens cela coûte.

Une ligne JSON par appel dans <DATA_DIR>/.usage/AAAA-MM.jsonl (hors du dépôt Git du contexte).
Aucun contenu de document n'est journalisé : seulement l'outil, le chemin, la section,
la requête de recherche, la taille de la réponse et la durée. Conservation : 12 mois.
"""

from __future__ import annotations

import functools
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .sections import tokens
from .store import TZ

CONSERVATION_MOIS = 12


def dossier(racine: str | os.PathLike | None = None) -> Path:
    return Path(racine or os.environ.get("DATA_DIR", "/data")) / ".usage"


def _projet(args: dict) -> str:
    for cle in ("projet", "categorie"):
        if args.get(cle):
            return str(args[cle]).strip().lower()
    for cle in ("chemin", "nouveau_chemin"):
        c = str(args.get(cle) or "")
        if "/" in c:
            return c.split("/")[0].strip().lower()
        if c:
            return "general"
    return ""


def enregistrer(outil: str, args: dict, reponse: str | None, duree_ms: int, erreur: str | None = None,
                racine=None) -> None:
    ligne = {
        "t": datetime.now(TZ).isoformat(timespec="seconds"),
        "outil": outil,
        "projet": _projet(args),
        "chemin": args.get("chemin"),
        "section": args.get("section"),
        "complet": bool(args.get("complet")) or None,
        "requete": (args.get("requete") or "")[:200] or None,
        "auteur": (args.get("auteur") or "")[:80] or None,
        "tokens": tokens(reponse) if reponse else 0,
        "ms": duree_ms,
        "vide": bool(reponse and reponse.startswith(("Aucun résultat", "Aucun document"))) or None,
        "erreur": (erreur or "")[:200] or None,
    }
    d = dossier(racine)
    try:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{ligne['t'][:7]}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({k: v for k, v in ligne.items() if v is not None}, ensure_ascii=False) + "\n")
    except OSError:
        pass  # le journal ne doit jamais faire échouer un outil


def journaliser(fn):
    """Décorateur des outils MCP : mesure la réponse et la durée, sans modifier la signature."""
    @functools.wraps(fn)
    def enveloppe(*args, **kwargs):
        debut = time.monotonic()
        try:
            reponse = fn(*args, **kwargs)
        except Exception as e:
            enregistrer(fn.__name__, kwargs, None, int((time.monotonic() - debut) * 1000), str(e))
            raise
        enregistrer(fn.__name__, kwargs, reponse if isinstance(reponse, str) else str(reponse),
                    int((time.monotonic() - debut) * 1000))
        return reponse
    return enveloppe


def purger(racine=None) -> None:
    limite = (datetime.now(TZ) - timedelta(days=31 * CONSERVATION_MOIS)).strftime("%Y-%m")
    for f in dossier(racine).glob("*.jsonl"):
        if f.stem < limite:
            f.unlink(missing_ok=True)


def lire(jours: int = 30, racine=None) -> list[dict]:
    depuis = datetime.now(TZ) - timedelta(days=jours)
    lignes = []
    for f in sorted(dossier(racine).glob("*.jsonl")):
        if f.stem < depuis.strftime("%Y-%m"):
            continue
        for brut in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                l = json.loads(brut)
            except ValueError:
                continue
            if datetime.fromisoformat(l["t"]) >= depuis:
                lignes.append(l)
    return lignes


ECRITURES = {"ecrire_document", "ajouter_au_document", "remplacer_texte", "deplacer_document",
             "enregistrer_decision"}


def synthese(jours: int = 30, racine=None) -> dict:
    lignes = lire(jours, racine)
    par_jour: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    par_projet, par_outil, docs_lus, ecritures = Counter(), Counter(), Counter(), Counter()
    appels_projet, complets, vides, erreurs = Counter(), Counter(), Counter(), []
    for l in lignes:
        jour = l["t"][:10]
        par_jour[jour][0] += 1
        par_jour[jour][1] += l.get("tokens", 0)
        par_outil[l["outil"]] += l.get("tokens", 0)
        if l.get("projet"):
            par_projet[l["projet"]] += l.get("tokens", 0)
            appels_projet[l["projet"]] += 1
        if l["outil"] == "lire_document" and l.get("chemin"):
            docs_lus[l["chemin"]] += l.get("tokens", 0)
            if l.get("complet"):
                complets[l["chemin"]] += 1
        if l["outil"] == "rechercher" and l.get("vide") and l.get("requete"):
            vides[l["requete"]] += 1
        if l["outil"] in ECRITURES and not l.get("erreur"):
            ecritures[l.get("auteur") or "?"] += 1
        if l.get("erreur"):
            erreurs.append(l)
    jours_tries = sorted(par_jour.items())
    return {
        "jours": jours,
        "appels": len(lignes),
        "tokens": sum(l.get("tokens", 0) for l in lignes),
        "conversations_estimees": sum(1 for l in lignes if l["outil"] == "vue_ensemble"),
        "par_jour": jours_tries,
        "max_jour": max((v[1] for _, v in jours_tries), default=0),
        "par_projet": par_projet.most_common(),
        "appels_projet": appels_projet,
        "par_outil": par_outil.most_common(),
        "docs_lus": docs_lus.most_common(10),
        "complets": complets.most_common(10),
        "recherches_vides": vides.most_common(10),
        "ecritures": ecritures.most_common(),
        "erreurs": erreurs[-10:][::-1],
    }
