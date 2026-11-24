"""Détection de secrets dans un texte avant écriture (clés privées, jetons, mots de passe, DSN).

Volontairement prudent : on préfère laisser passer un cas douteux que bloquer une documentation
légitime. Les valeurs « modèles » (<…>, ***, ${…}, COLLER_ICI…, null://null, chemins) sont ignorées.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MOTIFS_DIRECTS = [
    ("clé privée", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("jeton JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("clé AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jeton GitHub", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("jeton Slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("clé d'API (sk-…)", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}")),
    ("clé Google", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
]
DSN = re.compile(r"\b[a-z][a-z0-9+.-]*://([^\s:/@`'\"<>]+):([^\s@/`'\"<>]+)@")
AFFECTATION = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_KEY|PASSPHRASE|CRYPTAGE)[A-Z0-9_]*)"
    r"\s*[=:]\s*[\"']?([^\s\"'`<>|]{8,})"
)
BEARER = re.compile(r"\bBearer\s+([A-Za-z0-9._~+/=-]{20,})")

MODELES = re.compile(
    r"^(?:\$\{.*|%.*|\*+|x{3,}.*|null://.*|true|false|none|null|changeme.*|votre.*|your.*|"
    r"example.*|exemple.*|motdepasse|password\d*!?|secret|token|[/~.].*|.*….*|.*\.\.\..*)$",
    re.IGNORECASE,
)


@dataclass
class Secret:
    type: str
    extrait: str  # valeur masquée, jamais la valeur complète

    def __str__(self) -> str:
        return f"{self.type} ({self.extrait})"


def _masquer(valeur: str) -> str:
    return valeur[:4] + "…" if len(valeur) > 6 else "…"


def _modele(valeur: str) -> bool:
    v = valeur.strip().strip("\"'")
    if MODELES.match(v):
        return True
    if re.fullmatch(r"[A-Z0-9_]+", v) and not re.search(r"\d", v):  # VOTRE_JETON, COLLER_ICI_LE_MCP_TOKEN
        return True
    return False


def _plausible(valeur: str) -> bool:
    """Une vraie valeur secrète mêle en général chiffres et lettres, ou majuscules et minuscules."""
    a_chiffre = bool(re.search(r"\d", valeur))
    a_lettre = bool(re.search(r"[A-Za-z]", valeur))
    casse_mixte = bool(re.search(r"[a-z]", valeur)) and bool(re.search(r"[A-Z]", valeur))
    return (a_chiffre and a_lettre) or casse_mixte or len(valeur) >= 24


def detecter(texte: str) -> list[tuple[Secret, str]]:
    """Secrets trouvés : (description masquée, valeur brute pour comparaison interne seulement)."""
    trouves: list[tuple[Secret, str]] = []
    for nom, motif in MOTIFS_DIRECTS:
        for m in motif.finditer(texte):
            trouves.append((Secret(nom, _masquer(m.group(0))), m.group(0)))
    for m in DSN.finditer(texte):
        mdp = m.group(2)
        if not _modele(mdp):
            trouves.append((Secret("mot de passe dans une adresse de connexion", _masquer(mdp)), mdp))
    for m in AFFECTATION.finditer(texte):
        cle, valeur = m.group(1), m.group(2)
        if not _modele(valeur) and _plausible(valeur):
            trouves.append((Secret(f"valeur de {cle}", _masquer(valeur)), valeur))
    for m in BEARER.finditer(texte):
        if not _modele(m.group(1)) and _plausible(m.group(1)):
            trouves.append((Secret("jeton Bearer", _masquer(m.group(1))), m.group(1)))
    return trouves


def nouveaux(texte: str, ancien: str = "") -> list[Secret]:
    """Secrets du texte qui ne figuraient pas déjà dans l'ancienne version (pas de blocage rétroactif)."""
    vus, resultat = set(), []
    for secret, brut in detecter(texte):
        if brut in ancien or brut in vus:
            continue
        vus.add(brut)
        resultat.append(secret)
    return resultat
