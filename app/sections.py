"""Découpage d'un document Markdown en sections (titres #…######), pour ne lire que l'utile.

Une section va de son titre jusqu'au titre suivant de niveau égal ou supérieur : elle
contient donc ses sous-sections. Les titres à l'intérieur des blocs de code sont ignorés.
Chaque section reçoit un numéro hiérarchique (« 2 », « 2.3 »…) relatif aux niveaux présents.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TITRE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
CLOTURE_RE = re.compile(r"^\s*(```|~~~)")
INTRO = "(début)"


def tokens(texte: str) -> int:
    """Estimation grossière (texte français + Markdown ≈ 3,5 caractères par token)."""
    return max(1, round(len(texte) / 3.5))


def taille(texte_ou_nb) -> str:
    n = texte_ou_nb if isinstance(texte_ou_nb, int) else tokens(texte_ou_nb)
    return f"~{n / 1000:.1f}k tokens".replace(".0k", "k") if n >= 1000 else f"~{n} tokens"


def _normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode().lower()
    texte = re.sub(r"[`*_\[\]()]", "", texte)
    return re.sub(r"\s+", " ", texte).strip()


@dataclass
class Section:
    numero: str        # « 2.3 » ; « 0 » pour le texte avant le premier titre
    niveau: int        # 1..6 ; 0 pour l'introduction
    titre: str
    debut: int         # index de la ligne du titre
    fin: int           # index exclu (sous-sections comprises)
    propre: int        # index exclu du texte propre (avant la 1re sous-section)


def decouper(contenu: str) -> list[Section]:
    lignes = contenu.splitlines()
    titres: list[tuple[int, int, str]] = []
    dans_code = False
    for i, ligne in enumerate(lignes):
        if CLOTURE_RE.match(ligne):
            dans_code = not dans_code
            continue
        if not dans_code:
            m = TITRE_RE.match(ligne)
            if m:
                titres.append((i, len(m.group(1)), m.group(2).strip()))

    sections: list[Section] = []
    premier = titres[0][0] if titres else len(lignes)
    if any(l.strip() for l in lignes[:premier]):
        sections.append(Section("0", 0, INTRO, 0, premier, premier))

    niveaux = sorted({n for _, n, _ in titres})
    rang = {n: r for r, n in enumerate(niveaux)}
    compteurs = [0] * len(niveaux)
    for k, (i, niveau, titre) in enumerate(titres):
        r = rang[niveau]
        compteurs[r] += 1
        compteurs[r + 1:] = [0] * (len(niveaux) - r - 1)
        numero = ".".join(str(c) for c in compteurs[: r + 1] if c) or "1"
        fin = next((j for j, n, _ in titres[k + 1:] if n <= niveau), len(lignes))
        propre = titres[k + 1][0] if k + 1 < len(titres) else len(lignes)
        sections.append(Section(numero, niveau, titre, i, fin, propre))
    return sections


def texte(contenu: str, s: Section) -> str:
    return "\n".join(contenu.splitlines()[s.debut:s.fin]).strip("\n")


def plan(contenu: str) -> str:
    """Table des matières : numéro, titre, taille (sous-sections comprises)."""
    lignes = contenu.splitlines()
    sections = decouper(contenu)
    base = min((s.niveau for s in sections if s.niveau), default=1)
    sortie = []
    for s in sections:
        retrait = "  " * max(0, s.niveau - base)
        n = tokens("\n".join(lignes[s.debut:s.fin]))
        sortie.append(f"{retrait}{s.numero}  {s.titre}  ({taille(n)})")
    return "\n".join(sortie) or "(document sans titres)"


def trouver(contenu: str, cle: str) -> Section:
    """Section désignée par son numéro (« 2.3 ») ou son titre (exact, puis partiel ; casse et accents ignorés)."""
    cle = (cle or "").strip()
    sections = decouper(contenu)
    par_numero = [s for s in sections if s.numero == cle.rstrip(".")]
    if par_numero:
        return par_numero[0]
    n = _normaliser(cle.lstrip("#"))
    exacts = [s for s in sections if _normaliser(s.titre) == n]
    candidats = exacts or [s for s in sections if n and n in _normaliser(s.titre)]
    if len(candidats) == 1:
        return candidats[0]
    if not candidats:
        raise LookupError(f"Section « {cle} » introuvable. Plan du document :\n{plan(contenu)}")
    liste = "\n".join(f"  {s.numero}  {s.titre}" for s in candidats[:15])
    raise LookupError(f"« {cle} » désigne plusieurs sections ; préciser le numéro :\n{liste}")


def section_de_ligne(sections: list[Section], index: int) -> Section | None:
    """Section la plus profonde contenant la ligne d'index donné."""
    contenant = [s for s in sections if s.debut <= index < s.fin]
    return max(contenant, key=lambda s: s.niveau) if contenant else None
