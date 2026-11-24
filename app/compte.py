"""Compte de l'interface web : identifiant + mot de passe haché, sessions par cookie signé.

Le compte est stocké dans <DATA_DIR>/.web/compte.json (hors du dépôt Git du contexte).
Mot de passe oublié :

    docker compose exec web python -m app.compte reinitialiser

puis ouvrir l'interface : elle redemande la création du compte.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

ITERATIONS = 600_000
COOKIE = "mcp_session"
DUREE_LONGUE = 30 * 24 * 3600   # « Rester connecté »
DUREE_COURTE = 12 * 3600
MDP_MIN = 10
ECHECS_MAX, BLOCAGE = 5, 300    # 5 échecs → 5 minutes de blocage (par adresse IP)
IDENTIFIANT_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


class ErreurCompte(ValueError):
    pass


def _hacher(mdp: str, sel: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", mdp.encode(), sel, ITERATIONS).hex()


def _b64(texte: str) -> str:
    return base64.urlsafe_b64encode(texte.encode()).decode().rstrip("=")


def _deb64(texte: str) -> str:
    return base64.urlsafe_b64decode(texte + "=" * (-len(texte) % 4)).decode()


class Comptes:
    def __init__(self, racine: str | os.PathLike | None = None):
        racine = Path(racine or os.environ.get("DATA_DIR", "/data"))
        self.fichier = racine / ".web" / "compte.json"
        self._verrou = threading.Lock()
        self._echecs: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------ stockage
    def lire(self) -> dict | None:
        try:
            return json.loads(self.fichier.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def existe(self) -> bool:
        return self.fichier.exists()

    def _ecrire(self, donnees: dict) -> None:
        self.fichier.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.fichier.parent, 0o700)
        tmp = self.fichier.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(donnees, f)
        os.replace(tmp, self.fichier)

    @staticmethod
    def _valider_mdp(mdp: str, confirmation: str) -> None:
        if len(mdp) < MDP_MIN:
            raise ErreurCompte(f"Mot de passe trop court ({MDP_MIN} caractères minimum).")
        if mdp != confirmation:
            raise ErreurCompte("Les deux mots de passe ne correspondent pas.")

    def creer(self, utilisateur: str, mdp: str, confirmation: str) -> None:
        utilisateur = utilisateur.strip()
        if not IDENTIFIANT_RE.match(utilisateur):
            raise ErreurCompte("Identifiant invalide (lettres, chiffres, . _ @ -).")
        self._valider_mdp(mdp, confirmation)
        with self._verrou:
            if self.existe():
                raise ErreurCompte("Le compte existe déjà.")
            sel = secrets.token_bytes(16)
            self._ecrire({"utilisateur": utilisateur, "sel": sel.hex(), "hash": _hacher(mdp, sel),
                          "secret": secrets.token_hex(32)})

    def changer_mdp(self, ancien: str, nouveau: str, confirmation: str) -> None:
        compte = self.lire()
        if not compte or not self.verifier(compte["utilisateur"], ancien):
            raise ErreurCompte("Mot de passe actuel incorrect.")
        self._valider_mdp(nouveau, confirmation)
        sel = secrets.token_bytes(16)
        # nouveau secret : toutes les autres sessions sont fermées
        compte.update(sel=sel.hex(), hash=_hacher(nouveau, sel), secret=secrets.token_hex(32))
        with self._verrou:
            self._ecrire(compte)

    def reinitialiser(self) -> bool:
        try:
            self.fichier.unlink()
            return True
        except FileNotFoundError:
            return False

    # ------------------------------------------------------------ vérification
    def verifier(self, utilisateur: str, mdp: str) -> bool:
        compte = self.lire()
        if not compte:
            return False
        calcule = _hacher(mdp, bytes.fromhex(compte["sel"]))
        ok_mdp = hmac.compare_digest(calcule, compte["hash"])
        ok_user = hmac.compare_digest(utilisateur.strip().lower().encode(), compte["utilisateur"].lower().encode())
        return ok_mdp and ok_user

    def bloque(self, ip: str) -> int:
        """Secondes restantes de blocage pour cette adresse (0 = autorisée)."""
        nb, fin = self._echecs.get(ip, (0, 0.0))
        reste = int(fin - time.time())
        return reste if nb >= ECHECS_MAX and reste > 0 else 0

    def noter_echec(self, ip: str) -> None:
        nb, fin = self._echecs.get(ip, (0, 0.0))
        if nb >= ECHECS_MAX and fin <= time.time():
            nb = 0
        nb += 1
        self._echecs[ip] = (nb, time.time() + BLOCAGE)

    def noter_succes(self, ip: str) -> None:
        self._echecs.pop(ip, None)

    # ------------------------------------------------------------ sessions
    def ouvrir_session(self, longue: bool) -> tuple[str, int]:
        compte = self.lire()
        if not compte:
            raise ErreurCompte("Aucun compte.")
        duree = DUREE_LONGUE if longue else DUREE_COURTE
        corps = f"{_b64(compte['utilisateur'])}.{int(time.time()) + duree}"
        signature = hmac.new(bytes.fromhex(compte["secret"]), corps.encode(), hashlib.sha256).hexdigest()
        return f"{corps}.{signature}", duree

    def session(self, valeur: str | None) -> str | None:
        """Utilisateur de la session si le cookie est valide et non expiré."""
        compte = self.lire()
        if not compte or not valeur or valeur.count(".") != 2:
            return None
        u, expiration, signature = valeur.split(".")
        attendue = hmac.new(bytes.fromhex(compte["secret"]), f"{u}.{expiration}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, attendue):
            return None
        try:
            if int(expiration) < time.time():
                return None
            return _deb64(u)
        except ValueError:
            return None


if __name__ == "__main__":
    if sys.argv[1:] == ["reinitialiser"]:
        ok = Comptes().reinitialiser()
        print("Compte supprimé : ouvrir l'interface pour en créer un nouveau." if ok else "Aucun compte à supprimer.")
        sys.exit(0)
    print(__doc__)
    sys.exit(1)
