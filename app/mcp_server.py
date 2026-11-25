"""Serveur MCP (transport Streamable HTTP) exposant le contexte partagé.

Endpoint : http://<hôte>:8765/mcp — authentification :
- en-tête « Authorization: Bearer <MCP_TOKEN> » (Claude Code, VS Code, mcp-remote) ;
- OAuth 2.1 si MCP_PUBLIC_URL et OAUTH_PASSWORD sont définis (connecteurs claude.ai).
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from . import sections as sec
from . import usage
from .store import DOC_DIRECTIVES, PROJET_PROCEDURES, Document, ErreurContexte, Store, empreinte, plier

log = logging.getLogger("contexte.mcp")
store = Store()

INSTRUCTIONS = """\
Source de vérité partagée des projets de l'utilisateur.
Lecture (économise le contexte) :
- Commence par `vue_ensemble` (avec `projet` si le sujet est connu) : fiche résumé + index.
- Puis `rechercher` ou `plan_document`, et `lire_document` avec `section`.
  `complet=true` seulement si tout le document est nécessaire.
Écriture :
- Tiens le contexte à jour de toi-même dès qu'un fait est établi ou change (configuration,
  adresse, version, procédure, décision), puis signale-le en une ligne.
- Corrige l'information À SA PLACE (`remplacer_texte`) : pas de nouvelle section datée,
  pas de « 🆕 », pas de journal des changements ; l'historique Git garde l'ancien état.
  Supprime ce qui est périmé. Un incident résolu se résume en une ligne de cause/solution.
- Tiens à jour la fiche `<projet>/resume` (~500 tokens : machines, adresses, rôles, accès)
  si un fait essentiel change ; crée-la si elle manque.
Organisation :
- Un projet peut avoir des sous-projets (`projet/sous-projet`, un seul niveau, chacun avec sa
  fiche `projet/sous-projet/resume`). Passe `projet/sous-projet` à `vue_ensemble` et
  `rechercher` pour ne charger que ce périmètre.
- Si un sujet mérite un nouveau projet ou sous-projet, PROPOSE-le (identifiant, titre,
  documents à y déplacer) et n'appelle `creer_projet` qu'après accord explicite.
- Lis la section avant de la modifier. `ecrire_document` remplace tout : à éviter.
- Décision validée → `enregistrer_decision`. Renseigne `auteur` et un `message` clair.
- Uniquement des faits confirmés. Ne supprime un document que sur demande explicite.
Directives et procédures :
- Les directives globales de l'utilisateur (document `directives`) priment sur tes habitudes,
  dans toutes les conversations. Une nouvelle consigne générale validée → l'y ajouter.
- Avant une tâche couverte par une procédure (`procedures/…`), lis-la et suis-la.
"""

MAX_DIRECTIVES = 2000             # tokens estimés : au-delà, renvoi vers le document

JETON = os.environ.get("MCP_TOKEN", "")
URL_PUBLIQUE = os.environ.get("MCP_PUBLIC_URL", "").strip().rstrip("/")
MOT_DE_PASSE_OAUTH = os.environ.get("OAUTH_PASSWORD", "")
fournisseur = None
options_auth = {}
if URL_PUBLIQUE and MOT_DE_PASSE_OAUTH:
    from .oauth import PORTEE, FournisseurOAuth

    if len(MOT_DE_PASSE_OAUTH) < 12:
        raise SystemExit("OAUTH_PASSWORD trop court (12 caractères minimum).")
    fournisseur = FournisseurOAuth(URL_PUBLIQUE, MOT_DE_PASSE_OAUTH, JETON,
                                   Path(os.environ.get("DATA_DIR", "/data")) / ".oauth")
    options_auth = dict(
        auth_server_provider=fournisseur,
        auth=AuthSettings(
            issuer_url=URL_PUBLIQUE,
            resource_server_url=f"{URL_PUBLIQUE}/mcp",
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[PORTEE], default_scopes=[PORTEE]),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )

mcp = FastMCP(
    "mcp-memory",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    # Le contrôle de l'en-tête Host est fait par le jeton ; on accepte l'IP et les noms du LAN.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    **options_auth,
)


def instructions_dynamiques() -> str:
    """Instructions fixes + directives globales + liste des procédures, relues à chaque
    connexion : une modification dans le contexte s'applique à la session suivante."""
    morceaux = [INSTRUCTIONS]
    try:
        if store.existe(DOC_DIRECTIVES):
            doc = store.lire(DOC_DIRECTIVES)
            contenu = doc.contenu.strip()
            if contenu.startswith(f"# {doc.titre}\n"):
                contenu = contenu.split("\n", 1)[1].strip()
            if sec.tokens(contenu) > MAX_DIRECTIVES:
                contenu = (f"(document trop long pour être transmis ici : le lire avec "
                           f"`lire_document(\"{DOC_DIRECTIVES}\", complet=true)` dès le début)")
            morceaux.append(f"\nDIRECTIVES GLOBALES DE L'UTILISATEUR (à respecter en toutes circonstances) :\n"
                            f"{contenu}\n")
        procedures = [d for d in store.lister(PROJET_PROCEDURES) if not d.chemin.endswith("/resume")]
        if procedures:
            morceaux.append("Procédures disponibles (`lire_document` avant d'agir) :\n"
                            + "\n".join(f"- `{d.chemin}` — {d.titre}" for d in procedures) + "\n")
    except Exception:  # le contexte ne doit jamais empêcher la connexion
        log.exception("Lecture des directives impossible")
    return "".join(morceaux)


class _ServeurInstructionsDynamiques(type(mcp._mcp_server)):
    """Le SDK lit `instructions` à chaque initialisation : on les recalcule à la volée."""

    @property
    def instructions(self):
        return instructions_dynamiques()

    @instructions.setter
    def instructions(self, valeur):
        pass


mcp._mcp_server.__class__ = _ServeurInstructionsDynamiques


@mcp.custom_route("/sante", methods=["GET"])
async def sante(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


if fournisseur:
    @mcp.custom_route("/oauth/connexion", methods=["GET", "POST"])
    async def connexion_oauth(request: Request):
        return await fournisseur.page_connexion(request)


def _fiche(doc: Document) -> str:
    tags = ", ".join(doc.tags) or "—"
    return f"# {doc.titre}\n_chemin : {doc.chemin} · tags : {tags} · maj : {doc.maj}_\n\n{doc.contenu}"


SEUIL_COMPLET = 4000  # au-delà (en tokens estimés), lire_document renvoie le plan plutôt que tout le texte


def _ligne_doc(d: Document) -> str:
    """Une ligne d'index : chemin, titre, tags utiles (hors nom du projet), taille."""
    tags = [t for t in d.tags if t not in (d.categorie, d.conteneur.split("/")[-1], "archive")]
    tags = f" [{', '.join(tags)}]" if tags else ""
    archive = " — archive, à ne lire qu'en cas de besoin" if "archive" in d.tags else ""
    return f"- `{d.chemin}` — {d.titre}{tags} ({sec.taille(d.contenu)}{archive})"


def _ligne_sous_projet(p) -> str:
    desc = f" : {p.description}" if p.description else ""
    return f"- 📁 `{p.slug}` — {p.titre} ({p.nb} doc.){desc}"


def _index(projet: str | None = None) -> str:
    """Index des documents. Les sous-projets n'y figurent qu'en une ligne : leur contenu
    s'obtient avec `vue_ensemble(projet="projet/sous-projet")`."""
    conteneurs = {p.slug: p for p in store.lister_conteneurs()}
    filtre = projet.strip().strip("/").lower() if projet else None
    if filtre and filtre not in conteneurs and filtre != "general":
        connus = ", ".join(conteneurs) or "aucun"
        raise ErreurContexte(f"Projet « {filtre} » inconnu. Projets et sous-projets : {connus}.")
    par_conteneur: dict[str, list[Document]] = {}
    for d in store.lister(filtre):
        par_conteneur.setdefault(d.conteneur, []).append(d)
    lignes = []
    if filtre:
        affiches = [filtre]
    else:
        affiches = ["general"] * ("general" in par_conteneur) + [s for s in conteneurs if "/" not in s]
    for cle in affiches:
        p = conteneurs.get(cle)
        if p is None:
            lignes.append("\n## Sans projet")
        else:
            genre = "sous-projet" if p.parent else "projet"
            lignes.append(f"\n## {p.titre} ({genre} `{cle}`)")
            if p.description:
                lignes.append(f"_{p.description}_")
        docs = [d for d in par_conteneur.get(cle, []) if not (filtre and d.chemin == f"{filtre}/resume")]
        sous = p.sous_projets if p else []
        lignes += [_ligne_doc(d) for d in docs]
        lignes += [_ligne_sous_projet(s) for s in sous]
        if not docs and not sous:
            lignes.append("- (aucun document)")
    return "# Index du contexte\n" + "\n".join(lignes)


def _ok(doc: Document, sha: str | None) -> str:
    return f"OK : « {doc.chemin} » enregistré" + (f" (version {sha})." if sha else " (aucun changement).")


# ------------------------------------------------------------------ outils
# Descriptions volontairement courtes : elles sont chargées dans chaque conversation.
# Restauration, suppression et renommage de projet se font dans l'interface web.
def _resume(projet: str | None) -> str:
    chemin = f"{projet.strip().strip('/').lower()}/resume" if projet else "resume"
    if store.existe(chemin):
        doc = store.lire(chemin)
        contenu = doc.contenu.strip()
        if contenu.startswith(f"# {doc.titre}\n"):  # titre déjà affiché
            contenu = contenu.split("\n", 1)[1].strip()
        return f"# {doc.titre}\n_chemin : {doc.chemin} · maj : {doc.maj[:10]}_\n\n{contenu}"
    return (f"(pas de fiche `{chemin}` : la créer, ~500 tokens, avec l'essentiel du projet)"
            if projet else "(pas de résumé général)")


@mcp.tool(structured_output=False)
@usage.journaliser
def vue_ensemble(projet: str | None = None) -> str:
    """À appeler en premier. Fiche résumé + index des documents (avec taille).
    projet : projet (maison) ou sous-projet (maison/nas) pour se limiter à ce périmètre."""
    return f"{_resume(projet)}\n\n---\n\n{_index(projet)}"


@mcp.tool(structured_output=False)
@usage.journaliser
def lister_documents(categorie: str | None = None) -> str:
    """Documents (chemin, titre, taille). categorie : projet, projet/sous-projet, ou general."""
    docs = store.lister(categorie)
    return "\n".join(_ligne_doc(d) for d in docs) or "Aucun document."


@mcp.tool(structured_output=False)
@usage.journaliser
def plan_document(chemin: str) -> str:
    """Sommaire numéroté d'un document, avec la taille de chaque section."""
    doc = store.lire(chemin)
    return (f"# {doc.titre}\n_chemin : {doc.chemin} · {sec.taille(doc.contenu)} · maj : {doc.maj}_\n\n"
            f"{sec.plan(doc.contenu)}")


@mcp.tool(structured_output=False)
@usage.journaliser
def lire_document(chemin: str, section: str | None = None, complet: bool = False) -> str:
    """Lit un document (chemin : projet/nom ou projet/sous-projet/nom). section : numéro (2.3) ou titre, sous-sections
    comprises. Sans section, un document long renvoie son sommaire sauf si complet=true."""
    doc = store.lire(chemin)
    if section:
        try:
            s = sec.trouver(doc.contenu, section)
        except LookupError as e:
            raise ErreurContexte(str(e)) from None
        return (f"# {doc.titre} › {s.numero} {s.titre}\n_chemin : {doc.chemin} · maj : {doc.maj}_\n\n"
                f"{sec.texte(doc.contenu, s)}")
    if not complet and sec.tokens(doc.contenu) > SEUIL_COMPLET:
        return (f"Document long ({sec.taille(doc.contenu)}) : lire la section utile avec "
                f"`lire_document(chemin, section=…)`, ou tout le document avec `complet=true`.\n\n"
                + plan_document(chemin))
    return _fiche(doc)


def _score(doc: Document, termes: list[str]) -> int:
    titre, contenu = plier(doc.titre), plier(doc.contenu)
    return sum(10 * titre.count(t) + contenu.count(t) for t in termes)


@mcp.tool(structured_output=False)
@usage.journaliser
def rechercher(requete: str, projet: str | None = None, limite: int = 8, archives: bool = False) -> str:
    """Recherche plein texte (tous les mots), triée par pertinence ; donne la section de chaque passage.
    projet : projet ou projet/sous-projet (sous-projets compris).
    archives=true inclut les documents archivés (journaux, historiques)."""
    termes = [t for t in plier(requete).split() if t]
    res = [doc for doc, _ in store.rechercher(requete, 1000)
           if doc.dans(projet) and (archives or "archive" not in doc.tags)]
    if not res:
        return "Aucun résultat."
    res.sort(key=lambda d: -_score(d, termes))
    sortie = []
    for doc in res[:limite]:
        sortie.append(f"- `{doc.chemin}` — {doc.titre} ({sec.taille(doc.contenu)})")
        sections = sec.decouper(doc.contenu)
        vues: dict[str, list[str]] = {}
        for i, ligne in enumerate(doc.contenu.splitlines()):
            if any(t in plier(ligne) for t in termes) and not sec.TITRE_RE.match(ligne):
                s = sec.section_de_ligne(sections, i)
                cle = f"§ {s.numero} {s.titre}" if s else "§ —"
                if len(vues) < 4 or cle in vues:
                    vues.setdefault(cle, []).append(ligne.strip()[:160])
        for cle, extraits in vues.items():
            sortie.append(f"    {cle}")
            sortie += [f"      > {e}" for e in extraits[:2]]
            if len(extraits) > 2:
                sortie.append(f"      (+{len(extraits) - 2} autre(s) passage(s))")
    if len(res) > limite:
        sortie.append(f"({len(res) - limite} autre(s) document(s) : augmenter `limite` ou préciser la requête)")
    return "\n".join(sortie)


@mcp.tool(structured_output=False)
@usage.journaliser
def ecrire_document(
    chemin: str,
    contenu: str,
    titre: str | None = None,
    tags: list[str] | None = None,
    message: str | None = None,
    auteur: str = "assistant",
) -> str:
    """Crée un document ou remplace TOUT son contenu (Markdown sans en-tête YAML)."""
    return _ok(*store.ecrire(chemin, contenu, titre, tags, message, auteur))


@mcp.tool(structured_output=False)
@usage.journaliser
def ajouter_au_document(
    chemin: str, texte: str, section: str | None = None, message: str | None = None, auteur: str = "assistant"
) -> str:
    """Ajoute du texte à la fin du document, ou de la section indiquée (numéro ou titre)."""
    if not section:
        return _ok(*store.ajouter(chemin, texte, message, auteur))
    doc = store.lire(chemin)
    try:
        s = sec.trouver(doc.contenu, section)
    except LookupError as e:
        raise ErreurContexte(str(e)) from None
    lignes = doc.contenu.splitlines()
    fin = s.fin
    while fin > s.debut + 1 and not lignes[fin - 1].strip():
        fin -= 1
    nouveau = lignes[:fin] + ["", texte.strip("\n"), ""] + lignes[fin:]
    contenu = "\n".join(nouveau).rstrip("\n") + "\n"
    return _ok(*store.ecrire(chemin, contenu, None, None,
                             message or f"Ajout dans « {s.titre} »", auteur, version_attendue=empreinte(doc)))


@mcp.tool(structured_output=False)
@usage.journaliser
def remplacer_texte(
    chemin: str, ancien: str, nouveau: str, message: str | None = None, auteur: str = "assistant"
) -> str:
    """Remplace un passage exact et unique par un nouveau texte (nouveau vide = supprimer le passage)."""
    return _ok(*store.remplacer(chemin, ancien, nouveau, message, auteur))


@mcp.tool(structured_output=False)
@usage.journaliser
def enregistrer_decision(
    titre: str, decision: str, justification: str | None = None, auteur: str = "assistant"
) -> str:
    """Ajoute une décision datée au journal « decisions »."""
    from .store import maintenant

    bloc = f"## {maintenant()[:10]} — {titre}\n\n**Décision :** {decision}\n"
    if justification:
        bloc += f"\n**Pourquoi :** {justification}\n"
    bloc += f"\n_par {auteur}_\n"
    if not store.existe("decisions"):
        store.ecrire("decisions", "# Journal des décisions\n", "Journal des décisions", ["decisions"], auteur=auteur)
    return _ok(*store.ajouter("decisions", bloc, f"Décision : {titre}", auteur))


@mcp.tool(structured_output=False)
@usage.journaliser
def deplacer_document(chemin: str, nouveau_chemin: str, auteur: str = "assistant") -> str:
    """Déplace ou renomme un document (l'historique suit), y compris vers un sous-projet."""
    doc, sha = store.deplacer(chemin, nouveau_chemin, auteur)
    return f"OK : « {chemin} » déplacé vers « {doc.chemin} » (version {sha})."


@mcp.tool(structured_output=False)
@usage.journaliser
def lister_projets() -> str:
    """Projets et sous-projets : identifiant (= préfixe des chemins), titre, nombre de documents."""
    lignes = []
    for p in store.lister_projets():
        lignes.append(f"- `{p.slug}` — {p.titre} ({p.nb} doc.)" + (f" : {p.description}" if p.description else ""))
        lignes += ["  " + _ligne_sous_projet(s) for s in p.sous_projets]
    return "\n".join(lignes) or "Aucun projet."


@mcp.tool(structured_output=False)
@usage.journaliser
def creer_projet(
    identifiant: str, titre: str, description: str = "", parent: str | None = None, auteur: str = "assistant"
) -> str:
    """Crée un projet, ou un sous-projet si parent est donné (un seul niveau).
    UNIQUEMENT après accord explicite de l'utilisateur sur la proposition (identifiant, titre).
    identifiant : minuscules, chiffres, tirets (ex. nas). Ensuite : fiche <id>/resume, déplacements."""
    projet, sha = store.creer_projet(identifiant, titre, description, auteur, parent=parent)
    genre = "Sous-projet" if projet.parent else "Projet"
    return (f"OK : {genre.lower()} « {projet.slug} » créé" + (f" (version {sha})." if sha else ".")
            + f" Documents : « {projet.slug}/<nom> » ; fiche résumé : « {projet.slug}/resume ».")


@mcp.tool(structured_output=False)
@usage.journaliser
def historique(chemin: str | None = None, limite: int = 20) -> str:
    """Dernières modifications (de tout le contexte ou d'un document)."""
    revs = store.historique(chemin, limite)
    return "\n".join(f"{r.sha}  {r.date}  {r.auteur} — {r.message}" for r in revs) or "Aucun historique."


def _alleger_schemas() -> None:
    """Retire les « title » générés automatiquement des schémas d'outils (≈ 25 % de leur taille)."""
    def nettoyer(o):
        if isinstance(o, dict):
            o.pop("title", None)
            for v in o.values():
                nettoyer(v)
        elif isinstance(o, list):
            for v in o:
                nettoyer(v)
    for outil in mcp._tool_manager.list_tools():
        nettoyer(outil.parameters)


_alleger_schemas()


# --------------------------------------------------------------- ressources
@mcp.resource("contexte://index", mime_type="text/markdown")
def ressource_index() -> str:
    """Index de tous les documents du contexte."""
    return _index()


@mcp.resource("contexte://doc/{nom}", mime_type="text/markdown")
def ressource_doc(nom: str) -> str:
    """Document sans catégorie (ex. contexte://doc/resume)."""
    return _fiche(store.lire(nom))


@mcp.resource("contexte://doc/{categorie}/{nom}", mime_type="text/markdown")
def ressource_doc_categorie(categorie: str, nom: str) -> str:
    """Document rangé dans une catégorie (ex. contexte://doc/regles/combat)."""
    return _fiche(store.lire(f"{categorie}/{nom}"))


@mcp.resource("contexte://doc/{categorie}/{sous_projet}/{nom}", mime_type="text/markdown")
def ressource_doc_sous_projet(categorie: str, sous_projet: str, nom: str) -> str:
    """Document d'un sous-projet (ex. contexte://doc/maison/nas/procedures)."""
    return _fiche(store.lire(f"{categorie}/{sous_projet}/{nom}"))


@mcp.prompt()
def charger_contexte() -> str:
    """Charge le contexte partagé dans la conversation."""
    return f"Voici le contexte partagé actuel :\n\n{vue_ensemble()}"


# ------------------------------------------------------------ application
class AuthBearer:
    """Middleware ASGI (mode sans OAuth) : exige « Authorization: Bearer <jeton> » sauf sur /sante."""

    def __init__(self, app, jeton: str):
        self.app = app
        self.jeton = jeton.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/sante":
            entetes = dict(scope.get("headers") or [])
            valeur = entetes.get(b"authorization", b"")
            fourni = valeur[7:].strip() if valeur[:7].lower() == b"bearer " else b""
            if not hmac.compare_digest(fourni, self.jeton):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"erreur":"jeton invalide"}'})
                return
        await self.app(scope, receive, send)


def creer_app():
    if len(JETON) < 24:
        raise SystemExit("MCP_TOKEN absent ou trop court (24 caractères minimum).")
    if fournisseur:  # le SDK gère jeton fixe + OAuth via le fournisseur
        log.info("OAuth activé pour %s", URL_PUBLIQUE)
        return mcp.streamable_http_app()
    return AuthBearer(mcp.streamable_http_app(), JETON)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(creer_app(), host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8765")),
                proxy_headers=True, forwarded_allow_ips="*")
