"""Stockage du contexte partagé : fichiers Markdown versionnés par Git.

Arborescence du volume (DATA_DIR, /data par défaut) :

    /data/.git                  historique complet
    /data/.lock                 verrou partagé entre conteneurs
    /data/contexte/resume.md    documents « racine »
    /data/contexte/regles/combat.md   documents rangés par catégorie

Un document est identifié par son chemin : « resume » ou « regles/combat »
(deux niveaux maximum). Chaque fichier commence par un en-tête YAML :

    ---
    titre: Combat
    tags: [regles, des]
    maj: 2026-11-23T10:00:00+01:00
    ---
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

CHEMIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}(/[a-z0-9][a-z0-9-]{0,63})?$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Paris"))

class ErreurContexte(Exception):
    """Erreur métier, renvoyée telle quelle aux clients."""


class ErreurSecret(ErreurContexte):
    """Le texte à enregistrer semble contenir un secret."""


@dataclass
class Document:
    chemin: str
    titre: str
    contenu: str
    tags: list[str] = field(default_factory=list)
    maj: str = ""

    @property
    def categorie(self) -> str:
        return self.chemin.split("/")[0] if "/" in self.chemin else "general"


@dataclass
class Projet:
    slug: str
    titre: str
    description: str = ""
    nb: int = 0


@dataclass
class Revision:
    sha: str
    auteur: str
    date: str
    message: str


def maintenant() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FICHIER_PROJET = ".projet.yml"
FICHIER_ORDRE = ".ordre-projets.yml"  # ordre d'affichage des projets (liste de slugs)
RESERVES = {"general"}  # « general » désigne les documents sans projet


def verifier_projet(slug: str) -> str:
    slug = (slug or "").strip().strip("/").lower()
    if not SEGMENT_RE.match(slug):
        raise ErreurContexte(
            f"Identifiant de projet invalide : {slug!r}. Minuscules, chiffres et tirets (ex. « maison »)."
        )
    if slug in RESERVES:
        raise ErreurContexte(f"« {slug} » est réservé.")
    return slug


def verifier_chemin(chemin: str) -> str:
    chemin = (chemin or "").strip().strip("/").lower()
    if chemin.endswith(".md"):
        chemin = chemin[:-3]
    if not CHEMIN_RE.match(chemin):
        raise ErreurContexte(
            f"Chemin invalide : {chemin!r}. Format attendu : « nom » ou « categorie/nom », "
            "en minuscules, chiffres et tirets (ex. « regles/combat »)."
        )
    return chemin


def _parser(chemin: str, brut: str) -> Document:
    meta: dict = {}
    contenu = brut
    if brut.startswith("---\n"):
        fin = brut.find("\n---\n", 4)
        if fin != -1:
            try:
                meta = yaml.safe_load(brut[4:fin]) or {}
            except yaml.YAMLError:
                meta = {}
            contenu = brut[fin + 5 :]
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return Document(
        chemin=chemin,
        titre=str(meta.get("titre") or chemin.split("/")[-1]),
        contenu=contenu.lstrip("\n"),
        tags=[str(t) for t in tags],
        maj=str(meta.get("maj") or ""),
    )


def _serialiser(doc: Document) -> str:
    meta = {"titre": doc.titre, "tags": doc.tags, "maj": doc.maj}
    entete = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    contenu = doc.contenu.rstrip("\n") + "\n"
    return f"---\n{entete}\n---\n\n{contenu}"


def plier(texte: str) -> str:
    """Forme de comparaison pour la recherche : minuscules, sans accents ni apostrophes typographiques."""
    texte = unicodedata.normalize("NFKD", texte or "")
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return texte.replace("’", "'").replace("œ", "oe").replace("æ", "ae").lower()


def normaliser_tags(tags) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = tags.split(",")
    return sorted({str(t).strip().lower() for t in tags if str(t).strip()})


class Store:
    def __init__(self, racine: str | os.PathLike | None = None):
        self.racine = Path(racine or os.environ.get("DATA_DIR", "/data"))
        self.docs = self.racine / "contexte"
        self._verrou_local = threading.RLock()
        self._profondeur = 0
        self.racine.mkdir(parents=True, exist_ok=True)
        with self.verrou():
            self._initialiser()

    # ------------------------------------------------------------------ Git
    def _git(self, *args: str, check: bool = True) -> str:
        res = subprocess.run(
            ["git", "-C", str(self.racine), *args],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if check and res.returncode != 0:
            raise ErreurContexte(f"git {args[0]} a échoué : {res.stderr.strip()}")
        return res.stdout

    def _initialiser(self) -> None:
        if not (self.racine / ".git").exists():
            self._git("init", "-q", "-b", "main")
            self._git("config", "user.name", "contexte")
            self._git("config", "user.email", "contexte@localhost")
            (self.racine / ".gitignore").write_text(".lock\n.oauth/\n", encoding="utf-8")
            self.docs.mkdir(exist_ok=True)
            self._git("add", "-A")
            self._git("commit", "-q", "-m", "Initialisation du contexte")
        self.docs.mkdir(exist_ok=True)

    def _commit(self, message: str, auteur: str) -> str | None:
        self._git("add", "-A", "contexte")
        if not self._git("status", "--porcelain", "contexte").strip():
            return None
        auteur = re.sub(r"[<>\n]", "", auteur or "inconnu").strip()[:80] or "inconnu"
        email = re.sub(r"[^a-z0-9.-]", "-", auteur.lower()) + "@contexte.local"
        self._git("commit", "-q", "-m", message[:500], "--author", f"{auteur} <{email}>")
        return self._git("rev-parse", "--short", "HEAD").strip()

    @contextmanager
    def verrou(self):
        """Verrou exclusif réentrant : thread local + fichier (partagé entre conteneurs).
        Le verrou fichier n'est pris qu'au premier niveau : flock sur un second
        descripteur du même fichier bloquerait le processus lui-même."""
        with self._verrou_local:
            if self._profondeur:
                self._profondeur += 1
                try:
                    yield
                finally:
                    self._profondeur -= 1
                return
            with open(self.racine / ".lock", "w") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                self._profondeur = 1
                try:
                    yield
                finally:
                    self._profondeur = 0
                    fcntl.flock(f, fcntl.LOCK_UN)

    # ------------------------------------------------------------ fichiers
    def _fichier(self, chemin: str) -> Path:
        return self.docs / f"{chemin}.md"

    def _ecrire_fichier(self, doc: Document) -> None:
        f = self._fichier(doc.chemin)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".md.tmp")
        tmp.write_text(_serialiser(doc), encoding="utf-8")
        tmp.replace(f)

    # ------------------------------------------------------------- lecture
    def existe(self, chemin: str) -> bool:
        return self._fichier(verifier_chemin(chemin)).exists()

    def lire(self, chemin: str) -> Document:
        chemin = verifier_chemin(chemin)
        f = self._fichier(chemin)
        if not f.exists():
            raise ErreurContexte(f"Le document « {chemin} » n'existe pas.")
        return _parser(chemin, f.read_text(encoding="utf-8"))

    def lister(self, categorie: str | None = None) -> list[Document]:
        docs = []
        for f in sorted(self.docs.rglob("*.md")):
            chemin = f.relative_to(self.docs).with_suffix("").as_posix()
            if not CHEMIN_RE.match(chemin):
                continue
            doc = _parser(chemin, f.read_text(encoding="utf-8"))
            if categorie and doc.categorie != categorie.strip().lower():
                continue
            docs.append(doc)
        docs.sort(key=lambda d: (d.categorie != "general", d.categorie, d.chemin))
        return docs

    def rechercher(self, requete: str, limite: int = 20) -> list[tuple[Document, list[str]]]:
        termes = [t for t in plier(requete).split() if t]
        if not termes:
            return []
        resultats = []
        for doc in self.lister():
            texte = plier(f"{doc.titre}\n{' '.join(doc.tags)}\n{doc.contenu}")
            if not all(t in texte for t in termes):
                continue
            extraits = [
                ligne.strip()
                for ligne in doc.contenu.splitlines()
                if any(t in plier(ligne) for t in termes)
            ][:3]
            resultats.append((doc, extraits))
        return resultats[:limite]

    def historique(self, chemin: str | None = None, limite: int = 30) -> list[Revision]:
        args = ["log", f"-n{max(1, min(limite, 500))}", "--format=%h%x1f%an%x1f%aI%x1f%s"]
        if chemin:
            # -M100% : ne suivre que les vrais déplacements (contenu identique) ; sinon git
            # prend des documents courts aux en-têtes proches pour des copies l'un de l'autre.
            args += ["--follow", "-M100%", "--", f"contexte/{verifier_chemin(chemin)}.md"]
        lignes = self._git(*args, check=False).strip().splitlines()
        return [Revision(*l.split("\x1f", 3)) for l in lignes if l.count("\x1f") == 3]

    def lire_version(self, chemin: str, sha: str) -> Document:
        chemin = verifier_chemin(chemin)
        if not SHA_RE.match(sha or ""):
            raise ErreurContexte("Identifiant de version invalide.")
        for fichier in (f"contexte/{chemin}.md", self._chemin_a_version(chemin, sha)):
            if not fichier:
                continue
            res = subprocess.run(
                ["git", "-C", str(self.racine), "show", f"{sha}:{fichier}"],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                return _parser(chemin, res.stdout)
        raise ErreurContexte(f"« {chemin} » n'existe pas dans la version {sha}.")

    def _chemin_a_version(self, chemin: str, sha: str) -> str | None:
        """Chemin du fichier dans une version donnée (le document a pu être déplacé depuis)."""
        sortie = self._git("log", "--follow", "-M100%", "--name-only", "--format=%x1e%h",
                           "--", f"contexte/{chemin}.md", check=False)
        for bloc in sortie.split("\x1e"):
            lignes = [l.strip() for l in bloc.splitlines() if l.strip()]
            if len(lignes) >= 2 and (lignes[0].startswith(sha) or sha.startswith(lignes[0])):
                return lignes[1]
        return None

    def diff(self, sha: str) -> str:
        if not SHA_RE.match(sha or ""):
            raise ErreurContexte("Identifiant de version invalide.")
        return self._git("show", "--format=%h — %an — %aI%n%s%n", "--stat", "--patch", sha)

    # ------------------------------------------------------------ écriture
    def ecrire(
        self,
        chemin: str,
        contenu: str,
        titre: str | None = None,
        tags=None,
        message: str | None = None,
        auteur: str = "inconnu",
        version_attendue: str | None = None,
        autoriser_secrets: bool = False,
    ) -> tuple[Document, str | None]:
        """Crée ou remplace un document. `version_attendue` (empreinte du contenu
        lu avant édition) permet de refuser l'écriture si quelqu'un d'autre a
        modifié le document entre-temps."""
        chemin = verifier_chemin(chemin)
        with self.verrou():
            ancien = self.lire(chemin) if self.existe(chemin) else None
            if version_attendue is not None and ancien is not None:
                if empreinte(ancien) != version_attendue:
                    raise ErreurContexte(
                        "Le document a été modifié par quelqu'un d'autre pendant l'édition. "
                        "Rechargez-le avant d'enregistrer."
                    )
            if not autoriser_secrets:
                from .secrets_detect import nouveaux

                trouves = nouveaux(f"{titre or ''}\n{contenu}", ancien.contenu if ancien else "")
                if trouves:
                    raise ErreurSecret(
                        "Écriture refusée : le texte semble contenir un secret ("
                        + " ; ".join(str(s) for s in trouves)
                        + "). Ne jamais enregistrer de secret dans le contexte : remplacer la valeur par un "
                        "modèle (`<secret>`, `***`) et indiquer seulement où elle est stockée."
                    )
            if not titre and not ancien:
                h1 = re.match(r"\s*# (.+)", contenu)
                titre = h1.group(1) if h1 else None
            doc = Document(
                chemin=chemin,
                titre=(titre or (ancien.titre if ancien else chemin.split("/")[-1])).strip(),
                contenu=contenu,
                tags=normaliser_tags(tags) if tags is not None else (ancien.tags if ancien else []),
                maj=maintenant(),
            )
            self._ecrire_fichier(doc)
            action = "Mise à jour" if ancien else "Création"
            sha = self._commit(message or f"{action} de {chemin}", auteur)
            return doc, sha

    def ajouter(self, chemin: str, texte: str, message: str | None = None, auteur: str = "inconnu"):
        chemin = verifier_chemin(chemin)
        with self.verrou():
            doc = self.lire(chemin)
            contenu = doc.contenu.rstrip("\n") + "\n\n" + texte.strip("\n") + "\n"
            return self.ecrire(chemin, contenu, message=message or f"Ajout dans {chemin}", auteur=auteur)

    def remplacer(
        self, chemin: str, ancien: str, nouveau: str, message: str | None = None, auteur: str = "inconnu"
    ):
        chemin = verifier_chemin(chemin)
        with self.verrou():
            doc = self.lire(chemin)
            n = doc.contenu.count(ancien) if ancien else 0
            if n == 0:
                raise ErreurContexte("Texte à remplacer introuvable (la correspondance doit être exacte).")
            if n > 1:
                raise ErreurContexte(
                    f"Texte à remplacer trouvé {n} fois : ajoutez du contexte pour qu'il soit unique."
                )
            contenu = doc.contenu.replace(ancien, nouveau, 1)
            return self.ecrire(chemin, contenu, message=message or f"Modification de {chemin}", auteur=auteur)

    def supprimer(self, chemin: str, message: str | None = None, auteur: str = "inconnu") -> str | None:
        chemin = verifier_chemin(chemin)
        with self.verrou():
            f = self._fichier(chemin)
            if not f.exists():
                raise ErreurContexte(f"Le document « {chemin} » n'existe pas.")
            f.unlink()
            self._nettoyer_dossier(f.parent)
            return self._commit(message or f"Suppression de {chemin}", auteur)

    def deplacer(self, chemin: str, nouveau: str, auteur: str = "inconnu", message: str | None = None):
        """Déplace / renomme un document ; l'historique suit (git log --follow)."""
        chemin, nouveau = verifier_chemin(chemin), verifier_chemin(nouveau)
        if chemin == nouveau:
            raise ErreurContexte("Le nouveau chemin est identique à l'actuel.")
        if "/" in nouveau and nouveau.split("/")[0] in RESERVES:
            raise ErreurContexte("« general » est réservé : choisir « Sans projet » à la place.")
        with self.verrou():
            source, cible = self._fichier(chemin), self._fichier(nouveau)
            if not source.exists():
                raise ErreurContexte(f"Le document « {chemin} » n'existe pas.")
            if cible.exists():
                raise ErreurContexte(f"Un document « {nouveau} » existe déjà.")
            cible.parent.mkdir(parents=True, exist_ok=True)
            source.rename(cible)
            self._nettoyer_dossier(source.parent)
            sha = self._commit(message or f"Déplacement de {chemin} vers {nouveau}", auteur)
            return self.lire(nouveau), sha

    def _nettoyer_dossier(self, dossier: Path) -> None:
        """Supprime un dossier de catégorie devenu vide, sauf s'il porte un projet."""
        if dossier != self.docs and not (dossier / FICHIER_PROJET).exists():
            try:
                dossier.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------- projets
    def _meta_projet(self, slug: str) -> dict:
        f = self.docs / slug / FICHIER_PROJET
        try:
            return yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (FileNotFoundError, yaml.YAMLError):
            return {}

    def _ecrire_meta_projet(self, slug: str, titre: str, description: str) -> None:
        d = self.docs / slug
        d.mkdir(parents=True, exist_ok=True)
        meta = {"titre": titre.strip() or slug, "description": (description or "").strip()}
        (d / FICHIER_PROJET).write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def lister_projets(self) -> list[Projet]:
        """Projets déclarés (même vides) et catégories implicites des documents."""
        compte: dict[str, int] = {}
        for d in self.lister():
            if d.categorie != "general":
                compte[d.categorie] = compte.get(d.categorie, 0) + 1
        slugs = set(compte)
        if self.docs.exists():
            slugs |= {p.name for p in self.docs.iterdir() if p.is_dir() and SEGMENT_RE.match(p.name)
                      and (p / FICHIER_PROJET).exists()}
        projets = []
        for s in slugs:
            meta = self._meta_projet(s)
            projets.append(Projet(s, str(meta.get("titre") or s), str(meta.get("description") or ""), compte.get(s, 0)))
        ordre = {s: i for i, s in enumerate(self._ordre_projets())}
        return sorted(projets, key=lambda p: (ordre.get(p.slug, len(ordre)), p.titre.lower()))

    def _ordre_projets(self) -> list[str]:
        try:
            ordre = yaml.safe_load((self.docs / FICHIER_ORDRE).read_text(encoding="utf-8")) or []
        except (FileNotFoundError, yaml.YAMLError):
            return []
        return [s for s in ordre if isinstance(s, str)] if isinstance(ordre, list) else []

    def _ecrire_ordre(self, slugs: list[str]) -> None:
        (self.docs / FICHIER_ORDRE).write_text(yaml.safe_dump(slugs, allow_unicode=True), encoding="utf-8")

    def ordonner_projets(self, slugs: list[str], auteur: str = "inconnu") -> str | None:
        """Enregistre l'ordre d'affichage. Les projets absents de la liste gardent l'ordre alphabétique, à la suite."""
        with self.verrou():
            existants = {p.slug for p in self.lister_projets()}
            ordre = list(dict.fromkeys(s for s in slugs if isinstance(s, str) and s in existants))
            self._ecrire_ordre(ordre)
            return self._commit("Réorganisation des projets", auteur)

    def lire_projet(self, slug: str) -> Projet:
        slug = verifier_projet(slug)
        for p in self.lister_projets():
            if p.slug == slug:
                return p
        raise ErreurContexte(f"Le projet « {slug} » n'existe pas.")

    def projet_existe(self, slug: str) -> bool:
        return any(p.slug == slug for p in self.lister_projets())

    def creer_projet(self, slug: str, titre: str = "", description: str = "", auteur: str = "inconnu"):
        slug = verifier_projet(slug)
        with self.verrou():
            if self.projet_existe(slug):
                raise ErreurContexte(f"Le projet « {slug} » existe déjà.")
            self._ecrire_meta_projet(slug, titre or slug, description)
            sha = self._commit(f"Création du projet {slug}", auteur)
            return self.lire_projet(slug), sha

    def modifier_projet(self, slug: str, titre: str, description: str = "",
                        nouveau_slug: str | None = None, auteur: str = "inconnu"):
        """Modifie titre / description ; renomme l'identifiant (déplace tous ses documents)."""
        slug = verifier_projet(slug)
        nouveau_slug = verifier_projet(nouveau_slug) if nouveau_slug else slug
        with self.verrou():
            self.lire_projet(slug)
            if nouveau_slug != slug:
                if self.projet_existe(nouveau_slug) or (self.docs / nouveau_slug).exists():
                    raise ErreurContexte(f"Le projet « {nouveau_slug} » existe déjà.")
                (self.docs / slug).rename(self.docs / nouveau_slug)
                ordre = self._ordre_projets()
                if slug in ordre:
                    self._ecrire_ordre([nouveau_slug if s == slug else s for s in ordre])
            self._ecrire_meta_projet(nouveau_slug, titre, description)
            message = (f"Renommage du projet {slug} en {nouveau_slug}" if nouveau_slug != slug
                       else f"Modification du projet {slug}")
            sha = self._commit(message, auteur)
            return self.lire_projet(nouveau_slug), sha

    def supprimer_projet(self, slug: str, auteur: str = "inconnu") -> str | None:
        slug = verifier_projet(slug)
        with self.verrou():
            projet = self.lire_projet(slug)
            if projet.nb:
                raise ErreurContexte(
                    f"Le projet « {slug} » contient {projet.nb} document(s) : les déplacer ou les supprimer d'abord."
                )
            d = self.docs / slug
            (d / FICHIER_PROJET).unlink(missing_ok=True)
            ordre = self._ordre_projets()
            if slug in ordre:
                self._ecrire_ordre([s for s in ordre if s != slug])
            try:
                d.rmdir()
            except OSError:
                pass
            return self._commit(f"Suppression du projet {slug}", auteur)

    def restaurer(self, chemin: str, sha: str, auteur: str = "inconnu"):
        ancien = self.lire_version(chemin, sha)
        return self.ecrire(
            ancien.chemin,
            ancien.contenu,
            titre=ancien.titre,
            tags=ancien.tags,
            message=f"Restauration de {ancien.chemin} à la version {sha}",
            auteur=auteur,
        )


def empreinte(doc: Document) -> str:
    import hashlib

    return hashlib.sha256(_serialiser(Document(doc.chemin, doc.titre, doc.contenu, doc.tags, "")).encode()).hexdigest()[:16]
