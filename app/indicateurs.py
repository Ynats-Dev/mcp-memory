"""Indicateurs de la mémoire : volumes en tokens, coût de démarrage, santé des documents."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import sections as sec
from .store import CHEMIN_RE, Document, Store

SEUIL_LONG_SANS_STRUCTURE = 6000   # tokens
SEUIL_SECTION = 3000               # tokens de texte propre (hors sous-sections)
SEUIL_RESUME = 1200
DATE_TITRE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b20\d\d-\d\d-\d\d\b")
RENVOI = re.compile(r"`([a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*){1,2})`")


@dataclass
class Alerte:
    niveau: str        # "important" ou "conseil"
    chemin: str
    message: str


@dataclass
class InfoDoc:
    doc: Document
    tokens: int
    archive: bool
    sections: int
    plus_grosse: tuple[str, int] | None   # (titre, tokens de texte propre)


@dataclass
class InfoProjet:
    slug: str
    titre: str
    docs: list[InfoDoc] = field(default_factory=list)
    alertes: list[Alerte] = field(default_factory=list)
    sous_projets: int = 0  # lignes « 📁 » dans l'index du projet

    @property
    def directs(self) -> list[InfoDoc]:
        """Documents rangés directement dans ce projet (hors sous-projets)."""
        return [d for d in self.docs if d.doc.conteneur == self.slug]

    @property
    def actifs(self):
        return [d for d in self.docs if not d.archive]

    @property
    def tokens_actifs(self) -> int:
        return sum(d.tokens for d in self.actifs)

    @property
    def tokens_archives(self) -> int:
        return sum(d.tokens for d in self.docs if d.archive)

    @property
    def resume(self) -> InfoDoc | None:
        return next((d for d in self.docs if d.doc.chemin == f"{self.slug}/resume"), None)

    @property
    def demarrage(self) -> int:
        """Tokens renvoyés par vue_ensemble(projet) : fiche résumé + une ligne d'index par document
        direct + une ligne par sous-projet (leurs documents ne sont pas listés)."""
        lignes = sum(sec.tokens(f"- `{d.doc.chemin}` — {d.doc.titre} (~1.2k tokens)") for d in self.directs
                     if d is not self.resume)
        return (self.resume.tokens if self.resume else 25) + lignes + 20 * self.sous_projets + 30

    @property
    def maj(self) -> str:
        return max((d.doc.maj for d in self.docs), default="")


def _info(doc: Document) -> InfoDoc:
    decoupe = sec.decouper(doc.contenu)
    lignes = doc.contenu.splitlines()
    propres = [(s.titre, sec.tokens("\n".join(lignes[s.debut:s.propre]))) for s in decoupe]
    return InfoDoc(doc, sec.tokens(doc.contenu), "archive" in doc.tags,
                   sum(1 for s in decoupe if s.niveau >= 2), max(propres, key=lambda x: x[1], default=None))


def _alertes(info: InfoDoc, existants: set[str]) -> list[Alerte]:
    if info.archive:
        return []
    d, a = info.doc, []
    if info.tokens > SEUIL_LONG_SANS_STRUCTURE and info.sections < 3:
        a.append(Alerte("important", d.chemin, f"{sec.taille(info.tokens)} sans titres ## : illisible par section"))
    if info.plus_grosse and info.plus_grosse[1] > SEUIL_SECTION:
        a.append(Alerte("conseil", d.chemin,
                        f"section « {info.plus_grosse[0][:60]} » de {sec.taille(info.plus_grosse[1])} à découper"))
    nouveautes = d.contenu.count("🆕")
    titres_dates = sum(1 for l in d.contenu.splitlines() if l.startswith("#") and DATE_TITRE.search(l))
    cochees = len(re.findall(r"^\s*[-*] \[x\]", d.contenu, re.M | re.I))
    signes = [f"{nouveautes} « 🆕 »" if nouveautes >= 3 else "",
              f"{titres_dates} titres datés" if titres_dates >= 3 else "",
              f"{cochees} tâches cochées" if cochees >= 5 else ""]
    if any(signes):
        a.append(Alerte("conseil", d.chemin, "tourne au journal : " + ", ".join(s for s in signes if s)
                        + " (corriger à sa place, retirer le fait)"))
    casses = sorted({r for r in RENVOI.findall(d.contenu)
                     if CHEMIN_RE.match(r) and r.split("/")[0] in {c.split("/")[0] for c in existants}
                     and r not in existants})
    if casses:
        a.append(Alerte("important", d.chemin, "renvoi vers un document absent : " + ", ".join(casses[:5])))
    if d.chemin.endswith("/resume") and info.tokens > SEUIL_RESUME:
        a.append(Alerte("conseil", d.chemin, f"fiche résumé de {sec.taille(info.tokens)} : viser moins de ~1k"))
    return a


def analyser(store: Store) -> list[InfoProjet]:
    docs = store.lister()
    # un renvoi vers un sous-projet (`maison/nas`) n'est pas un document absent
    existants = {d.chemin for d in docs} | {p.slug for p in store.lister_conteneurs()}
    liste = store.lister_projets()
    projets = {p.slug: InfoProjet(p.slug, p.titre, sous_projets=len(p.sous_projets)) for p in liste}
    for d in docs:
        p = projets.setdefault(d.categorie, InfoProjet(d.categorie, "Sans projet" if d.categorie == "general"
                                                      else d.categorie))
        info = _info(d)
        p.docs.append(info)
        p.alertes += _alertes(info, existants)
    for p in projets.values():
        if p.slug != "general" and p.docs and not p.resume:
            p.alertes.insert(0, Alerte("important", f"{p.slug}/resume",
                                       "pas de fiche résumé : vue_ensemble n'a rien à afficher en tête"))
        p.docs.sort(key=lambda i: -i.tokens)
    for projet in liste:
        for s in projet.sous_projets:
            if s.nb and f"{s.slug}/resume" not in existants:
                projets[projet.slug].alertes.append(Alerte("conseil", f"{s.slug}/resume",
                                                           "sous-projet sans fiche résumé"))
    return sorted(projets.values(), key=lambda p: (p.slug == "general", p.titre.lower()))


def analyser_projet(store: Store, slug: str) -> InfoProjet | None:
    """Projet (« maison ») ou sous-projet (« maison/nas ») : documents, alertes, coût de démarrage."""
    parent = next((p for p in analyser(store) if p.slug == slug.split("/")[0]), None)
    if parent is None or "/" not in slug:
        return parent
    sous = store.lire_projet(slug)
    docs = [d for d in parent.docs if d.doc.dans(slug)]
    alertes = [a for a in parent.alertes if a.chemin.startswith(slug + "/")]
    return InfoProjet(slug, sous.titre, docs, alertes)
