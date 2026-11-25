"""Mise à jour de l'application : dialogue avec le service hôte (scripts/maj-hote.sh).

L'interface web n'exécute rien elle-même : elle dépose « verifier » ou « appliquer » dans
<MAJ_DIR>/demande ; le service systemd de l'hôte fait git pull + docker compose, puis publie
son état dans <MAJ_DIR>/etat.json et son journal dans <MAJ_DIR>/derniere.log.

Le dépôt de référence (adresse + branche) est écrit dans <MAJ_DIR>/depot ; sans ce fichier,
l'hôte utilise DEPOT_DEFAUT / BRANCHE_DEFAUT (mêmes valeurs dans scripts/maj-hote.sh).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

DOSSIER = Path(os.environ.get("MAJ_DIR", "/maj"))
VERSION = os.environ.get("APP_VERSION", "").strip()
ACTIONS = {"verifier", "appliquer"}
DELAI_PRISE_EN_CHARGE = 20  # secondes : au-delà, le service hôte est considéré inactif
DEPOT_DEFAUT = "https://github.com/Ynats-Dev/mcp-memory.git"
BRANCHE_DEFAUT = "master"
# Mêmes règles que url_valide / branche_valide dans scripts/maj-hote.sh
URL_RE = re.compile(r"^(https?|ssh)://[A-Za-z0-9._~%:/+@-]+$|^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._~/+-]+$")
IDENTIFIANTS_RE = re.compile(r"^https?://[^/@]*:[^/@]*@")
BRANCHE_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


def config() -> dict:
    """Dépôt de référence configuré (ou par défaut)."""
    url, branche = DEPOT_DEFAUT, BRANCHE_DEFAUT
    try:
        for ligne in (DOSSIER / "depot").read_text(encoding="utf-8").splitlines():
            cle, _, valeur = ligne.partition("=")
            if cle == "url" and valeur.strip():
                url = valeur.strip()
            elif cle == "branche" and valeur.strip():
                branche = valeur.strip()
    except OSError:
        pass
    return {"url": url, "branche": branche, "defaut": url == DEPOT_DEFAUT and branche == BRANCHE_DEFAUT,
            "url_defaut": DEPOT_DEFAUT, "branche_defaut": BRANCHE_DEFAUT}


def enregistrer_config(url: str, branche: str) -> None:
    """Valide et enregistre le dépôt de référence ; les valeurs par défaut suppriment le fichier."""
    url, branche = (url or "").strip(), (branche or "").strip() or BRANCHE_DEFAUT
    if not URL_RE.match(url):
        raise ValueError("Adresse de dépôt invalide : https://…, ssh://… ou git@hôte:chemin.")
    if IDENTIFIANTS_RE.match(url):
        raise ValueError("Pas d'identifiants dans l'adresse : configurer l'accès au dépôt sur l'hôte "
                         "(clé SSH ou gestionnaire d'identifiants Git).")
    if not BRANCHE_RE.match(branche) or branche.startswith("-") or ".." in branche:
        raise ValueError("Nom de branche invalide.")
    if url == DEPOT_DEFAUT and branche == BRANCHE_DEFAUT:
        return reinitialiser_config()
    _ecrire("depot", f"url={url}\nbranche={branche}\n")


def reinitialiser_config() -> None:
    try:
        (DOSSIER / "depot").unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError(f"Impossible de modifier la configuration ({exc.strerror}).") from None


def _ecrire(nom: str, texte: str) -> None:
    if not DOSSIER.is_dir():
        raise RuntimeError("Service de mise à jour non installé sur l'hôte (dossier .maj absent).")
    tmp = DOSSIER / f".{nom}.tmp"
    try:
        tmp.write_text(texte, encoding="utf-8")
        os.replace(tmp, DOSSIER / nom)
    except OSError as exc:
        raise RuntimeError(f"Impossible d'écrire la configuration ({exc.strerror}) : "
                           "relancer scripts/installer-maj.sh sur l'hôte.") from None


def etat() -> dict:
    """État publié par l'hôte, complété de ce que sait le conteneur."""
    try:
        e = json.loads((DOSSIER / "etat.json").read_text(encoding="utf-8"))
        installe = True
    except (OSError, ValueError):
        e, installe = {}, False
    demande = DOSSIER / "demande"
    attente = None
    try:
        attente = time.time() - demande.stat().st_mtime
    except OSError:
        pass
    e.update(
        installe=installe,
        version=VERSION or e.get("local", ""),
        # Image construite à partir d'un autre commit que celui du dépôt (déploiement manuel non reconstruit…)
        decalage=bool(VERSION and e.get("local") and VERSION != e["local"]),
        en_attente=attente is not None,
        hote_inactif=attente is not None and attente > DELAI_PRISE_EN_CHARGE,
        disponible=int(e.get("retard") or 0) > 0,
        config=config(),
    )
    # Configuration modifiée depuis la dernière vérification de l'hôte
    e["config_a_verifier"] = bool(e.get("depot")) and (
        e.get("depot") != e["config"]["url"] or e.get("depot_branche") != e["config"]["branche"])
    return e


def disponible() -> bool:
    try:
        e = json.loads((DOSSIER / "etat.json").read_text(encoding="utf-8"))
        return int(e.get("retard") or 0) > 0
    except (OSError, ValueError, TypeError):
        return False


def demander(action: str) -> None:
    if action not in ACTIONS:
        raise ValueError("Action inconnue.")
    if not (DOSSIER / "etat.json").exists():
        raise RuntimeError("Service de mise à jour non installé sur l'hôte.")
    e = etat()
    if e["en_attente"] and not e["hote_inactif"]:
        raise RuntimeError("Une demande est déjà en attente de traitement.")
    if e.get("statut") in {"verification", "mise_a_jour"} and not _perime(e):
        raise RuntimeError("Une opération est déjà en cours.")
    tmp = DOSSIER / ".demande.tmp"
    try:
        tmp.write_text(action, encoding="utf-8")
        os.replace(tmp, DOSSIER / "demande")
    except OSError as exc:
        raise RuntimeError(f"Impossible de déposer la demande ({exc.strerror}) : "
                           "relancer scripts/installer-maj.sh sur l'hôte.") from None


def _perime(e: dict) -> bool:
    """Opération « en cours » depuis plus de 40 min : le service a été interrompu."""
    try:
        return time.time() - (DOSSIER / "etat.json").stat().st_mtime > 2400
    except OSError:
        return True


def journal(lignes: int = 80) -> str:
    try:
        texte = (DOSSIER / "derniere.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(texte.splitlines()[-lignes:])
