"""Mise à jour de l'application : dialogue avec le service hôte (scripts/maj-hote.sh).

L'interface web n'exécute rien elle-même : elle dépose « verifier » ou « appliquer » dans
<MAJ_DIR>/demande ; le service systemd de l'hôte fait git pull + docker compose, puis publie
son état dans <MAJ_DIR>/etat.json et son journal dans <MAJ_DIR>/derniere.log.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DOSSIER = Path(os.environ.get("MAJ_DIR", "/maj"))
VERSION = os.environ.get("APP_VERSION", "").strip()
ACTIONS = {"verifier", "appliquer"}
DELAI_PRISE_EN_CHARGE = 20  # secondes : au-delà, le service hôte est considéré inactif


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
    )
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
