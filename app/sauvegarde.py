"""Sauvegarde du dépôt Git du contexte vers un dépôt distant (Gogs, Gitea…), chaque nuit et à la demande.

Configuration (.env) :
    GIT_REMOTE=https://git.example.com/moi/mcp-memory-data.git   (adresse SANS identifiants)
    GIT_REMOTE_USER=…            compte du dépôt
    GIT_REMOTE_TOKEN=…           jeton d'accès (jamais écrit dans .git/config ni dans les journaux)
    SAUVEGARDE_HEURE=03:15       heure locale de la sauvegarde nocturne

Le jeton est transmis par l'en-tête HTTP Authorization au seul moment du push.
L'état de la dernière tentative est dans <DATA_DIR>/.web/sauvegarde.json.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .store import TZ, Store, maintenant

log = logging.getLogger("contexte.sauvegarde")
DELAI_GIT = 180  # secondes


def configuration() -> dict:
    return {
        "remote": os.environ.get("GIT_REMOTE", "").strip(),
        "user": os.environ.get("GIT_REMOTE_USER", "").strip(),
        "token": os.environ.get("GIT_REMOTE_TOKEN", "").strip(),
        "heure": os.environ.get("SAUVEGARDE_HEURE", "03:15").strip() or "03:15",
    }


def _fichier_etat(store: Store) -> Path:
    return store.racine / ".web" / "sauvegarde.json"


def etat(store: Store) -> dict:
    conf = configuration()
    try:
        e = json.loads(_fichier_etat(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        e = {}
    e.update(configuree=bool(conf["remote"]), remote=conf["remote"], heure=conf["heure"],
             identifiants=bool(conf["user"] and conf["token"]))
    derniere_ok = e.get("derniere_reussite")
    e["en_retard"] = bool(conf["remote"]) and (
        not derniere_ok or datetime.fromisoformat(derniere_ok) < datetime.now(TZ) - timedelta(hours=26))
    return e


def _nettoyer(texte: str, conf: dict) -> str:
    """Retire tout identifiant d'un message d'erreur de git."""
    for secret in (conf["token"], conf["user"] and conf["token"] and base64.b64encode(
            f"{conf['user']}:{conf['token']}".encode()).decode()):
        if secret:
            texte = texte.replace(secret, "***")
    utiles = [l.strip() for l in texte.splitlines()
              if l.strip() and not l.startswith(("hint:", "To ", "Done")) and not l.startswith("\t")]
    return " ".join(utiles)[-400:]


def sauvegarder(store: Store) -> dict:
    """Pousse la branche main vers le dépôt distant ; enregistre et renvoie le résultat."""
    conf = configuration()
    if not conf["remote"]:
        return {"ok": False, "message": "Aucun dépôt de sauvegarde configuré (GIT_REMOTE)."}
    args = ["git", "-C", str(store.racine)]
    if conf["user"] and conf["token"]:
        jeton = base64.b64encode(f"{conf['user']}:{conf['token']}".encode()).decode()
        args += ["-c", f"http.extraHeader=Authorization: Basic {jeton}"]
    debut = time.monotonic()
    with store.verrou():
        sha = store._git("rev-parse", "--short", "HEAD").strip()
        nb_commits = store._git("rev-list", "--count", "HEAD").strip()
        try:
            res = subprocess.run(args + ["push", "--porcelain", conf["remote"], "main:main"],
                                 capture_output=True, text=True, timeout=DELAI_GIT,
                                 env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            ok, sortie = res.returncode == 0, res.stdout + res.stderr
        except subprocess.TimeoutExpired:
            ok, sortie = False, f"Délai dépassé ({DELAI_GIT} s) : dépôt injoignable ?"
    message = _nettoyer(sortie, conf)
    if not ok:
        if "non-fast-forward" in message or "rejected" in message:
            message = ("Refusé par le dépôt distant : il contient des commits absents d'ici "
                       "(dépôt créé avec un README ?). Utiliser un dépôt vide.")
        elif "Could not resolve host" in message:
            message = ("Nom du dépôt introuvable depuis le conteneur (DNS) : renseigner GIT_REMOTE_HOST "
                       "et GIT_REMOTE_HOST_IP dans le .env. — " + message)
        elif "401" in message or "Authentication" in message or "403" in message:
            message = "Identifiants refusés (GIT_REMOTE_USER / GIT_REMOTE_TOKEN). — " + message
    resultat = {"ok": ok, "date": maintenant(), "sha": sha, "commits": int(nb_commits or 0),
                "duree": round(time.monotonic() - debut, 1),
                "message": "Sauvegarde à jour." if ok and "[up to date]" in sortie else
                ("Sauvegarde envoyée." if ok else message)}
    precedent = etat(store)
    resultat["derniere_reussite"] = resultat["date"] if ok else precedent.get("derniere_reussite")
    f = _fichier_etat(store)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(resultat, ensure_ascii=False), encoding="utf-8")
    (log.info if ok else log.warning)("Sauvegarde %s : %s", "réussie" if ok else "en échec", resultat["message"])
    return resultat


def _prochaine(heure: str) -> datetime:
    h, m = (int(x) for x in heure.split(":"))
    maintenant_ = datetime.now(TZ)
    cible = maintenant_.replace(hour=h, minute=m, second=0, microsecond=0)
    return cible if cible > maintenant_ else cible + timedelta(days=1)


def demarrer_planificateur(store: Store) -> None:
    """Thread de fond : une sauvegarde chaque nuit, et une au démarrage si la dernière date de plus de 26 h."""
    conf = configuration()
    if not conf["remote"]:
        return

    def boucle():
        time.sleep(60)
        if etat(store)["en_retard"]:
            _essayer()
        while True:
            attente = (_prochaine(conf["heure"]) - datetime.now(TZ)).total_seconds()
            time.sleep(max(30, attente))
            _essayer()

    def _essayer():
        try:
            sauvegarder(store)
        except Exception:  # ne jamais arrêter le planificateur
            log.exception("Sauvegarde : erreur inattendue")

    threading.Thread(target=boucle, name="sauvegarde", daemon=True).start()
