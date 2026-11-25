"""Interface web de consultation et d'édition du contexte (port 8766)."""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
import re
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup, escape

import json

from . import fichier_projet, indicateurs, maj, regles, sauvegarde, usage
from .sections import taille as taille_tokens
from .compte import COOKIE, Comptes, ErreurCompte
from .importer import nom_document
from .store import (DOC_DIRECTIVES, PROJET_PROCEDURES, Document, ErreurContexte, ErreurSecret, Store, empreinte,
                    plier, verifier_chemin, verifier_conteneur)

ICI = Path(__file__).parent
store = Store()
md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": True}).enable("table").enable("strikethrough")

comptes = Comptes()
URL_MCP = (os.environ.get("MCP_PUBLIC_URL", "").strip().rstrip("/") + "/mcp") if os.environ.get("MCP_PUBLIC_URL", "").strip() else ""
# Adresse du serveur MCP vue depuis le conteneur web (vérification du jeton)
MCP_INTERNE = os.environ.get("MCP_INTERNE", "http://mcp:8765").rstrip("/")

@asynccontextmanager
async def _cycle_de_vie(_app: FastAPI):
    """Au démarrage : planificateur de la sauvegarde nocturne, purge du journal d'usage."""
    sauvegarde.demarrer_planificateur(store)
    usage.purger()
    yield


app = FastAPI(title="MCP Memory", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_cycle_de_vie)
app.mount("/static", StaticFiles(directory=ICI / "static"), name="static")
templates = Jinja2Templates(directory=ICI / "templates")
templates.env.filters["markdown"] = md.render

MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def date_fr(iso: str, heure: bool = True) -> str:
    """« 2026-11-23T14:05:00+01:00 » → « 23 nov. 2026 · 14:05 »."""
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    texte = f"{d.day} {MOIS[d.month - 1]} {d.year}"
    return f"{texte} · {d:%H:%M}" if heure else texte


def navigation() -> dict:
    """Projets et nombre de documents sans projet, pour la barre latérale."""
    return {"projets": store.lister_projets(),
            "sans_projet": sum(1 for d in store.lister() if d.categorie == "general")}


def sans_titre(contenu: str, titre: str) -> str:
    """Retire le premier « # Titre » du contenu s'il répète le titre affiché dans l'en-tête."""
    debut = contenu.lstrip("\n")
    premiere, _, reste = debut.partition("\n")
    if premiere.startswith("# ") and premiere[2:].strip() == titre.strip():
        return reste
    return contenu


templates.env.filters["date_fr"] = date_fr
templates.env.filters["sans_titre"] = sans_titre
templates.env.globals["navigation"] = navigation
templates.env.globals["maj_disponible"] = maj.disponible
templates.env.filters["tokens"] = taille_tokens

RENVOI_HTML = re.compile(r"<code>([a-z0-9][a-z0-9-]{0,63}(?:/[a-z0-9][a-z0-9-]{0,63}){1,2})</code>")


def liens_documents(html: str) -> str:
    """`projet/document` cité dans un texte → lien vers le document (ou le sous-projet) s'il existe,
    marqué s'il est absent."""
    def remplacer(m):
        chemin = m.group(1)
        if store.existe(chemin):
            return f'<a class="renvoi" href="/doc/{chemin}"><code>{chemin}</code></a>'
        if chemin.count("/") == 1 and store.projet_existe(chemin):
            return f'<a class="renvoi" href="/projet/{chemin}"><code>{chemin}</code></a>'
        projet = chemin.split("/")[0]
        if store.projet_existe(projet):
            return f'<code class="renvoi-casse" title="Document absent">{chemin}</code>'
        return m.group(0)
    return RENVOI_HTML.sub(remplacer, html)


templates.env.filters["liens_documents"] = liens_documents

TITRE_HTML = re.compile(r"<h([1-6])>")


def ancres_titres(html: str) -> str:
    """Numérote les titres (id="s-1", "s-2"…) dans l'ordre, pour le sommaire de la page document."""
    compteur = iter(range(1, 10_000))
    return TITRE_HTML.sub(lambda m: f'<h{m.group(1)} id="s-{next(compteur)}">', html)


templates.env.filters["ancres_titres"] = ancres_titres

CASE_HTML = re.compile(r"<li>(<p>)?\[([ xX])\]\s*")


def cases(html: str) -> str:
    """Listes de tâches Markdown « - [ ] » / « - [x] » affichées comme des cases (lecture seule)."""
    return CASE_HTML.sub(lambda m: f'<li class="tache{" faite" if m.group(2) in "xX" else ""}">{m.group(1) or ""}'
                         f'<span class="case-tache" aria-hidden="true"></span>', html)


templates.env.filters["cases"] = cases


def apercu(contenu: str, longueur: int = 150) -> str:
    """Premier paragraphe de texte d'un Markdown (sans titres, tableaux ni balises), tronqué."""
    for ligne in (contenu or "").splitlines():
        l = ligne.strip()
        if not l or l.startswith(("#", "|", "```", ">", "---", "_chemin")):
            continue
        l = re.sub(r"[*_`]|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "", l).lstrip("-*0123456789. ")
        return l if len(l) <= longueur else l[:longueur].rsplit(" ", 1)[0] + "…"
    return ""


templates.env.filters["apercu"] = apercu
templates.env.globals["utilisateur_courant"] = lambda request: getattr(request.state, "user", "")

# Pages accessibles sans session (le reste redirige vers /connexion)
PUBLIQUES = {"/sante", "/connexion", "/installation"}


@app.middleware("http")
async def authentification(request: Request, call_next):
    chemin = request.url.path
    if chemin.startswith("/static/") or chemin == "/sante":
        return await call_next(request)
    if not comptes.existe():
        if chemin == "/installation":
            return await call_next(request)
        return RedirectResponse("/installation", status_code=303)
    if chemin in PUBLIQUES:
        return await call_next(request)
    user = comptes.session(request.cookies.get(COOKIE))
    if not user:
        if request.method == "GET":
            suite = chemin + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse("/connexion?" + urlencode({"suite": suite}), status_code=303)
        return JSONResponse({"detail": "Session expirée : rechargez la page et reconnectez-vous."}, status_code=401)
    request.state.user = user
    return await call_next(request)


def utilisateur(request: Request) -> str:
    return getattr(request.state, "user", "") or "anonyme"


def meme_origine(request: Request) -> None:
    """Protection CSRF simple : un POST doit venir de cette même interface."""
    origine = request.headers.get("origin") or request.headers.get("referer") or ""
    hote = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    if not origine or urlparse(origine).netloc != hote:
        raise HTTPException(403, "Requête refusée (origine différente).")


def rendu(request: Request, nom: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, nom, ctx)


def vers_doc(chemin: str, **params) -> RedirectResponse:
    url = f"/doc/{quote(chemin)}"
    if params:
        url += "?" + "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return RedirectResponse(url, status_code=303)


@app.exception_handler(ErreurContexte)
async def erreur_contexte(request: Request, exc: ErreurContexte):
    return rendu(request, "erreur.html", message=str(exc))


def _suite_sure(suite: str) -> str:
    """Évite les redirections vers un autre site : chemin local uniquement."""
    return suite if suite.startswith("/") and not suite.startswith("//") and "\\" not in suite else "/"


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _avec_session(request: Request, reponse: RedirectResponse, longue: bool) -> RedirectResponse:
    valeur, duree = comptes.ouvrir_session(longue)
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    reponse.set_cookie(COOKIE, valeur, max_age=duree if longue else None, httponly=True,
                       samesite="lax", secure=https, path="/")
    return reponse


@app.get("/installation", response_class=HTMLResponse)
def installation_form(request: Request):
    if comptes.existe():
        return RedirectResponse("/connexion", status_code=303)
    return rendu(request, "auth.html", mode="installation", erreur="", identifiant="")


@app.post("/installation", dependencies=[Depends(meme_origine)])
def installation(request: Request, identifiant: str = Form(...), mdp: str = Form(...), confirmation: str = Form(...)):
    try:
        comptes.creer(identifiant, mdp, confirmation)
    except ErreurCompte as e:
        if comptes.existe():
            return RedirectResponse("/connexion", status_code=303)
        return rendu(request, "auth.html", mode="installation", erreur=str(e), identifiant=identifiant)
    return _avec_session(request, RedirectResponse("/", status_code=303), longue=True)


@app.get("/connexion", response_class=HTMLResponse)
def connexion_form(request: Request, suite: str = "/"):
    if comptes.session(request.cookies.get(COOKIE)):
        return RedirectResponse(_suite_sure(suite), status_code=303)
    return rendu(request, "auth.html", mode="connexion", erreur="", identifiant="", suite=_suite_sure(suite))


@app.post("/connexion", dependencies=[Depends(meme_origine)])
def connexion(request: Request, identifiant: str = Form(""), mdp: str = Form(""), souvenir: str = Form(""),
              suite: str = Form("/")):
    ip, suite = _ip(request), _suite_sure(suite)
    reste = comptes.bloque(ip)
    if reste:
        erreur = f"Trop d'essais : réessayez dans {reste // 60 + 1} min."
    elif comptes.verifier(identifiant, mdp):
        comptes.noter_succes(ip)
        return _avec_session(request, RedirectResponse(suite, status_code=303), longue=bool(souvenir))
    else:
        comptes.noter_echec(ip)
        erreur = "Identifiant ou mot de passe incorrect."
    return rendu(request, "auth.html", mode="connexion", erreur=erreur, identifiant=identifiant, suite=suite)


@app.post("/deconnexion", dependencies=[Depends(meme_origine)])
def deconnexion():
    reponse = RedirectResponse("/connexion", status_code=303)
    reponse.delete_cookie(COOKIE, path="/")
    return reponse


@app.get("/compte", response_class=HTMLResponse)
def compte_page(request: Request, msg: str = "", user: str = Depends(utilisateur)):
    return rendu(request, "compte.html", msg=msg, erreur="")


@app.post("/compte/mot-de-passe", dependencies=[Depends(meme_origine)])
def changer_mdp(request: Request, ancien: str = Form(...), mdp: str = Form(...), confirmation: str = Form(...),
                user: str = Depends(utilisateur)):
    try:
        comptes.changer_mdp(ancien, mdp, confirmation)
    except ErreurCompte as e:
        return rendu(request, "compte.html", msg="", erreur=str(e))
    # le secret a changé : nouvelle session pour ce navigateur, les autres sont déconnectés
    return _avec_session(request, RedirectResponse(f"/compte?msg={quote('Mot de passe modifié')}", status_code=303),
                         longue=True)


@app.get("/sante", include_in_schema=False)
def sante():
    return PlainTextResponse("ok")


# ------------------------------------------------------------ mémoire : indicateurs et sauvegarde

@app.get("/memoire", response_class=HTMLResponse)
def memoire(request: Request, msg: str = "", jours: int = 30, user: str = Depends(utilisateur)):
    jours = jours if jours in (1, 7, 30, 90, 365) else 30
    projets = indicateurs.analyser(store)
    tous = [d for p in projets for d in p.docs]
    return rendu(request, "memoire.html", projets=projets, msg=msg,
                 total_docs=len(tous), total_actifs=sum(p.tokens_actifs for p in projets),
                 total_archives=sum(p.tokens_archives for p in projets),
                 plus_gros=sorted((d for d in tous if not d.archive), key=lambda d: -d.tokens)[:8],
                 sauvegarde=sauvegarde.etat(store), usage=usage.synthese(jours))


def _zip(dossiers: list[Path], nom: str) -> Response:
    """Archive .zip des fichiers Markdown (avec leur en-tête titre / tags / maj), en mémoire."""
    import io
    import zipfile

    tampon = io.BytesIO()
    with store.verrou(), zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        for d in dossiers:
            for f in sorted(d.rglob("*")):
                if f.is_file() and (f.suffix == ".md" or f.name == ".projet.yml"):
                    z.write(f, f.relative_to(store.docs).as_posix())
    jour = datetime.now().strftime("%Y%m%d")
    return Response(tampon.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{nom}-{jour}.zip"'})


@app.get("/export")
def exporter_tout(user: str = Depends(utilisateur)):
    return _zip([store.docs], "mcp-memory")


@app.get("/projet/{slug:path}/export")
def exporter_projet(slug: str, user: str = Depends(utilisateur)):
    projet = store.lire_projet(slug)
    return _zip([store.docs / projet.slug], f"mcp-memory-{projet.slug}")


@app.post("/memoire/sauvegarde", dependencies=[Depends(meme_origine)])
def sauvegarder_maintenant(user: str = Depends(utilisateur)):
    r = sauvegarde.sauvegarder(store)
    return RedirectResponse(f"/memoire?msg={quote(r['message'][:300])}", status_code=303)


# ------------------------------------------------------------ mise à jour de l'application
@app.get("/mise-a-jour", response_class=HTMLResponse)
def mise_a_jour_page(request: Request, msg: str = "", erreur: str = "", user: str = Depends(utilisateur)):
    return rendu(request, "maj.html", etat=maj.etat(), journal=maj.journal(), msg=msg, erreur=erreur)


@app.get("/mise-a-jour/etat")
def mise_a_jour_etat(user: str = Depends(utilisateur)):
    return JSONResponse({**maj.etat(), "journal": maj.journal()}, headers={"Cache-Control": "no-store"})


def _verifier_apres_config() -> str:
    """Lance une vérification sur le nouveau dépôt de référence, si le service hôte est libre."""
    try:
        maj.demander("verifier")
        return " Vérification lancée."
    except (RuntimeError, ValueError):
        return " Cliquer sur « Vérifier maintenant » pour comparer avec ce dépôt."


@app.post("/mise-a-jour/depot", dependencies=[Depends(meme_origine)])
def mise_a_jour_depot(url: str = Form(""), branche: str = Form(""), user: str = Depends(utilisateur)):
    try:
        maj.enregistrer_config(url, branche)
    except (ValueError, RuntimeError) as e:
        return RedirectResponse(f"/mise-a-jour?{urlencode({'erreur': str(e)})}#depot", status_code=303)
    msg = "Dépôt de référence enregistré." + _verifier_apres_config()
    return RedirectResponse(f"/mise-a-jour?{urlencode({'msg': msg})}#depot", status_code=303)


@app.post("/mise-a-jour/depot/defaut", dependencies=[Depends(meme_origine)])
def mise_a_jour_depot_defaut(user: str = Depends(utilisateur)):
    try:
        maj.reinitialiser_config()
    except RuntimeError as e:
        return RedirectResponse(f"/mise-a-jour?{urlencode({'erreur': str(e)})}#depot", status_code=303)
    msg = "Dépôt par défaut rétabli." + _verifier_apres_config()
    return RedirectResponse(f"/mise-a-jour?{urlencode({'msg': msg})}#depot", status_code=303)


@app.post("/mise-a-jour/{action}", dependencies=[Depends(meme_origine)])
def mise_a_jour_demande(action: str, user: str = Depends(utilisateur)):
    try:
        maj.demander(action)
    except ValueError:
        raise HTTPException(404)
    except RuntimeError as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=409)
    return JSONResponse({"ok": True})


def _extrait(ligne: str, termes: list[str], largeur: int = 220) -> Markup:
    """Passage court autour du premier terme trouvé, sans Markdown, termes surlignés (HTML échappé)."""
    texte = re.sub(r"\*\*|__|`|^\s*[-*]\s+(\[[ xX]\]\s*)?|^#+\s*|^\s*\||\|\s*$", "", ligne.strip())
    texte = re.sub(r"\s*\|\s*", " · ", texte)
    plie = plier(texte)
    pos = min((plie.find(t) for t in termes if t in plie), default=0)
    debut = max(0, pos - largeur // 3)
    morceau = ("…" if debut else "") + texte[debut:debut + largeur] + ("…" if debut + largeur < len(texte) else "")
    plie_m = plier(morceau)
    if len(plie_m) != len(morceau):  # « œ », « æ » : pas de correspondance des positions, pas de surlignage
        return Markup(escape(morceau))
    zones = sorted((m.start(), m.end()) for t in termes for m in re.finditer(re.escape(t), plie_m))
    html, curseur = [], 0
    for d, f in zones:
        if d < curseur:
            continue
        html += [str(escape(morceau[curseur:d])), "<mark>", str(escape(morceau[d:f])), "</mark>"]
        curseur = f
    html.append(str(escape(morceau[curseur:])))
    html = "".join(html)
    return Markup(html)


@app.get("/", response_class=HTMLResponse)
def accueil(request: Request, q: str = "", archives: bool = False, msg: str = "", user: str = Depends(utilisateur)):
    projets = store.lister_projets()
    titres = {p.slug: p.titre for p in projets}
    if q:
        termes = [t for t in plier(q).split() if t]
        tous = store.rechercher(q, 200)
        nb_archives = sum(1 for d, _ in tous if "archive" in d.tags)
        resultats = [(d, [_extrait(e, termes) for e in extraits]) for d, extraits in tous
                     if archives or "archive" not in d.tags][:50]
        return rendu(request, "recherche.html", q=q, resultats=resultats, titres=titres, archives=archives,
                     nb_archives=nb_archives,
                     tailles={d.chemin: indicateurs.sec.tokens(d.contenu) for d, _ in resultats})
    infos = {i.slug: i for i in indicateurs.analyser(store)}
    docs = store.lister()
    recents = sorted((d for d in docs if "archive" not in d.tags), key=lambda d: d.maj, reverse=True)[:8]
    directives = next((d for d in docs if d.chemin == DOC_DIRECTIVES), None)
    return rendu(request, "index.html", projets=projets, infos=infos, total=len(docs), msg=msg,
                 recents=recents, titres=titres, activite=store.historique(limite=6), directives=directives,
                 sans_projet=[d for d in docs if d.categorie == "general" and d.chemin != DOC_DIRECTIVES])


@app.get("/doc/{chemin:path}", response_class=HTMLResponse)
def voir(request: Request, chemin: str, version: str = "", msg: str = "", user: str = Depends(utilisateur)):
    doc = store.lire_version(chemin, version) if version else store.lire(chemin)
    projets = store.lister_projets()
    projet = next((p for p in projets if p.slug == doc.categorie), None)
    sous_projet = next((s for s in projet.sous_projets if s.slug == doc.conteneur), None) if projet else None
    texte = sans_titre(doc.contenu, doc.titre)
    lignes = texte.splitlines()
    sommaire = [(n, s.niveau, re.sub(r"[`*_]", "", s.titre), indicateurs.sec.tokens("\n".join(lignes[s.debut:s.fin])))
                for n, s in enumerate((s for s in indicateurs.sec.decouper(texte) if s.niveau), 1)]
    return rendu(request, "document.html", doc=doc, version=version, msg=msg, projets=projets,
                 projet=projet, sous_projet=sous_projet, revisions=store.historique(doc.chemin, 50),
                 sommaire=sommaire, taille=indicateurs.sec.tokens(doc.contenu),
                 directives=doc.chemin == DOC_DIRECTIVES, procedure=doc.conteneur == PROJET_PROCEDURES)


@app.get("/nouveau", response_class=HTMLResponse)
def nouveau(request: Request, projet: str = "", nom: str = "", user: str = Depends(utilisateur)):
    return rendu(request, "editer.html", doc=None, base="", projets=store.lister_projets(), projet=projet, nom_doc=nom)


@app.get("/editer/{chemin:path}", response_class=HTMLResponse)
def editer(request: Request, chemin: str, user: str = Depends(utilisateur)):
    doc = store.lire(chemin)
    return rendu(request, "editer.html", doc=doc, base=empreinte(doc))


@app.post("/enregistrer", dependencies=[Depends(meme_origine)])
def enregistrer(
    request: Request,
    chemin: str = Form(""),
    projet: str = Form(""),
    nom: str = Form(""),
    titre: str = Form(""),
    tags: str = Form(""),
    contenu: str = Form(""),
    message: str = Form(""),
    base: str = Form(""),
    autoriser_secrets: str = Form(""),
    user: str = Depends(utilisateur),
):
    if not chemin:  # création : projet + nom
        chemin = f"{verifier_conteneur(projet)}/{nom}" if projet else nom
    chemin = verifier_chemin(chemin)
    if not base and store.existe(chemin):
        raise ErreurContexte(f"Le document « {chemin} » existe déjà : ouvrez-le pour le modifier.")
    contenu = contenu.replace("\r\n", "\n")
    try:
        _, sha = store.ecrire(chemin, contenu, titre or None, tags, message or None,
                              auteur=f"web:{user}", version_attendue=base or None,
                              autoriser_secrets=bool(autoriser_secrets))
    except ErreurSecret as e:  # réafficher l'éditeur sans perdre la saisie
        brouillon = Document(chemin=chemin, titre=titre or chemin.split("/")[-1], contenu=contenu,
                             tags=[t.strip() for t in tags.split(",") if t.strip()])
        return rendu(request, "editer.html", doc=brouillon, base=base, erreur_secret=str(e),
                     message_saisi=message, nouveau=not base)
    return vers_doc(chemin, msg=f"Enregistré ({sha})" if sha else "Aucun changement")


@app.post("/supprimer/{chemin:path}", dependencies=[Depends(meme_origine)])
def supprimer(chemin: str, user: str = Depends(utilisateur)):
    store.supprimer(chemin, auteur=f"web:{user}")
    return RedirectResponse("/", status_code=303)


@app.post("/restaurer/{chemin:path}", dependencies=[Depends(meme_origine)])
def restaurer(chemin: str, version: str = Form(...), user: str = Depends(utilisateur)):
    _, sha = store.restaurer(chemin, version, auteur=f"web:{user}")
    return vers_doc(chemin, msg=f"Version {version} restaurée ({sha})")


@app.post("/deplacer/{chemin:path}", dependencies=[Depends(meme_origine)])
def deplacer(chemin: str, projet: str = Form(""), nom: str = Form(...), user: str = Depends(utilisateur)):
    nouveau = f"{verifier_conteneur(projet)}/{nom}" if projet else nom
    doc, sha = store.deplacer(chemin, nouveau, auteur=f"web:{user}")
    return vers_doc(doc.chemin, msg=f"Déplacé depuis « {chemin} » ({sha})")


# ------------------------------------------------------------------ projets
@app.get("/projets/nouveau", response_class=HTMLResponse)
def nouveau_projet(request: Request, parent: str = "", user: str = Depends(utilisateur)):
    parent_p = store.lire_projet(parent) if parent else None
    if parent_p and parent_p.parent:
        raise ErreurContexte("Un sous-projet ne peut pas contenir de sous-projet.")
    return rendu(request, "projet_form.html", projet=None, parent=parent_p)


@app.post("/projets/creer", dependencies=[Depends(meme_origine)])
def creer_projet(slug: str = Form(...), titre: str = Form(""), description: str = Form(""),
                 parent: str = Form(""), user: str = Depends(utilisateur)):
    p, _ = store.creer_projet(slug, titre, description, auteur=f"web:{user}", parent=parent or None)
    msg = "Sous-projet créé" if p.parent else "Projet créé"
    return RedirectResponse(f"/projet/{p.slug}?msg={quote(msg)}", status_code=303)


@app.post("/projets/ordre", dependencies=[Depends(meme_origine)])
async def ordonner_projets(request: Request, user: str = Depends(utilisateur)):
    """Nouvel ordre des projets (glisser-déposer dans la barre latérale) : JSON {"ordre": [slugs]}."""
    try:
        ordre = (await request.json()).get("ordre")
    except (ValueError, AttributeError):
        ordre = None
    if not isinstance(ordre, list):
        raise HTTPException(400, "Liste « ordre » attendue.")
    store.ordonner_projets(ordre, auteur=f"web:{user}")
    return {"ok": True}


MAX_FICHIERS, MAX_OCTETS = 100, 2 * 1024 * 1024


@app.post("/projet/{slug:path}/importer", dependencies=[Depends(meme_origine)])
async def importer_fichiers(slug: str, fichiers: list[UploadFile] = File(...), remplacer: str = Form(""),
                            user: str = Depends(utilisateur)):
    """Dépôt de fichiers .md (glisser-déposer) dans un projet. Réponse JSON, un résultat par fichier."""
    projet = store.lire_projet(slug)
    if len(fichiers) > MAX_FICHIERS:
        raise HTTPException(400, f"{MAX_FICHIERS} fichiers maximum par envoi.")
    resultats, vus = [], set()
    for f in fichiers:
        nom_fichier = Path(f.filename or "").name
        res = {"fichier": nom_fichier}
        resultats.append(res)
        nom = nom_document(Path(nom_fichier)) if nom_fichier.lower().endswith((".md", ".markdown")) else ""
        if not nom:
            res.update(etat="erreur", detail="pas un fichier .md")
            continue
        chemin = f"{projet.slug}/{nom}"
        res["chemin"] = chemin
        brut = await f.read(MAX_OCTETS + 1)
        if len(brut) > MAX_OCTETS:
            res.update(etat="erreur", detail="plus de 2 Mo")
            continue
        try:
            contenu = brut.decode("utf-8-sig").replace("\r\n", "\n")
        except UnicodeDecodeError:
            res.update(etat="erreur", detail="encodage non UTF-8")
            continue
        if chemin in vus:
            res.update(etat="erreur", detail="même nom qu'un autre fichier de l'envoi")
            continue
        vus.add(chemin)
        existe = store.existe(chemin)
        if existe and not remplacer:
            res.update(etat="ignoré", detail="existe déjà")
            continue
        try:
            store.ecrire(chemin, contenu, tags=projet.slug.split("/"), auteur=f"web:{user}",
                         message=f"Import de « {nom_fichier} »")
        except (ErreurContexte, ValueError) as e:
            res.update(etat="erreur", detail=str(e))
            continue
        res["etat"] = "mis à jour" if existe else "créé"
    return {"resultats": resultats}


@app.get("/projet/{slug:path}/modifier", response_class=HTMLResponse)
def modifier_projet_form(request: Request, slug: str, user: str = Depends(utilisateur)):
    projet = store.lire_projet(slug)
    parent = store.lire_projet(projet.parent) if projet.parent else None
    return rendu(request, "projet_form.html", projet=projet, parent=parent)


@app.post("/projet/{slug:path}/modifier", dependencies=[Depends(meme_origine)])
def modifier_projet(slug: str, nouveau_slug: str = Form(...), titre: str = Form(""),
                    description: str = Form(""), user: str = Depends(utilisateur)):
    p, _ = store.modifier_projet(slug, titre, description, nouveau_slug, auteur=f"web:{user}")
    return RedirectResponse(f"/projet/{p.slug}?msg={quote('Enregistré')}", status_code=303)


@app.post("/projet/{slug:path}/supprimer", dependencies=[Depends(meme_origine)])
def supprimer_projet(slug: str, user: str = Depends(utilisateur)):
    projet = store.lire_projet(slug)
    store.supprimer_projet(slug, auteur=f"web:{user}")
    if projet.parent:
        return RedirectResponse(f"/projet/{projet.parent}?msg={quote('Sous-projet supprimé')}", status_code=303)
    return RedirectResponse(f"/?msg={quote('Projet supprimé')}", status_code=303)


# Déclarée après les routes /projet/<slug>/… : « {slug:path} » accepte aussi « maison/nas ».
@app.get("/projet/{slug:path}", response_class=HTMLResponse)
def voir_projet(request: Request, slug: str, msg: str = "", user: str = Depends(utilisateur)):
    projet = store.lire_projet(slug)
    parent = store.lire_projet(projet.parent) if projet.parent else None
    info = indicateurs.analyser_projet(store, projet.slug)
    lignes = sorted(info.directs if info else [], key=lambda i: i.doc.titre.lower())
    resume = info.resume if info else None
    docs = [d for d in store.lister(projet.slug) if d.conteneur == projet.slug]
    sous = {s.slug: indicateurs.analyser_projet(store, s.slug) for s in projet.sous_projets}
    return rendu(request, "projet.html", projet=projet, parent=parent, docs=docs, msg=msg, info=info,
                 resume=resume, actifs=[i for i in lignes if not i.archive and i is not resume],
                 archives=[i for i in lignes if i.archive], infos_sous=sous,
                 procedures=projet.slug == PROJET_PROCEDURES)


# ------------------------------------------------------------ règles (hooks Claude Code)
def _page_regles(request: Request, msg: str = "", erreur: str = "", test: dict | None = None,
                 mode: str = "simple", saisie: dict | None = None):
    liste = regles.lire(store)
    config = json.dumps(regles.configuration_hook(), ensure_ascii=False, indent=2)
    if mode != "avance":
        par_id = {r.id: r for r in liste}
        ids_modeles = {m["id"] for m in regles.MODELES}
        return rendu(request, "regles_simple.html", msg=msg, erreur=erreur, config=config,
                     modeles=[(m, par_id.get(m["id"])) for m in regles.MODELES],
                     mes_regles=[r for r in liste if r.id not in ids_modeles], phrase=regles.phrase,
                     actions=regles.ACTIONS, decisions=regles.DECISIONS_SIMPLES, saisie=saisie or {},
                     installe=bool(liste))
    return rendu(request, "regles.html", regles=liste, msg=msg, erreur=erreur, test=test or {},
                 config=config, decisions=regles.DECISIONS, champs=regles.CHAMPS)


@app.get("/regles", response_class=HTMLResponse)
def page_regles(request: Request, msg: str = "", erreur: str = "", mode: str = "simple", modifier: str = "",
                user: str = Depends(utilisateur)):
    saisie = None
    if modifier:  # mode simple : constructeur prérempli
        r = next((r for r in regles.lire(store) if r.id == modifier), None)
        if r and r.action:
            saisie = {"ancien_id": r.id, "action": r.action, "cible": r.cible, "decision": r.decision,
                      "message": r.message if r.message != f"Règle « {r.nom} »." else ""}
    return _page_regles(request, msg, erreur, mode=mode, saisie=saisie)


@app.post("/regles/simple", response_class=HTMLResponse, dependencies=[Depends(meme_origine)])
def regle_simple(request: Request, action: str = Form(""), cible: str = Form(""), decision: str = Form("ask"),
                 message: str = Form(""), ancien_id: str = Form(""), user: str = Depends(utilisateur)):
    """Constructeur du mode simple : « Quand Claude veut … alors … »."""
    try:
        if ancien_id:
            ancienne = next((r for r in regles.lire(store) if r.id == ancien_id), None)
            if not ancienne:
                raise ErreurContexte("Cette règle n'existe plus.")
            brouillon = regles.Regle(id=ancien_id, nom="", action=action, cible=cible, decision=decision)
            nom = regles.phrase(brouillon)[:120] if action in regles.ACTIONS else ""
            regle, _ = regles.enregistrer(store, dict(id=ancien_id, nom=nom, action=action, cible=cible,
                                                       decision=decision, message=message, actif=ancienne.actif),
                                          ancien_id, auteur=f"web:{user}")
        else:
            regle = regles.creer_simple(store, action, cible, decision, message, auteur=f"web:{user}")
    except ErreurContexte as e:
        saisie = dict(action=action, cible=cible, decision=decision, message=message, ancien_id=ancien_id)
        return _page_regles(request, erreur=str(e), saisie=saisie)
    return RedirectResponse(f"/regles?{urlencode({'msg': f'Règle enregistrée : {regles.phrase(regle)}.'})}"
                            f"#r-{regle.id}", status_code=303)


@app.post("/regles/modele/{id_}", dependencies=[Depends(meme_origine)])
def regle_modele(id_: str, user: str = Depends(utilisateur)):
    r = regles.basculer_modele(store, id_, auteur=f"web:{user}")
    msg = f"« {r.nom} » {'activée' if r.actif else 'désactivée'}."
    return RedirectResponse(f"/regles?{urlencode({'msg': msg})}#modeles", status_code=303)


@app.get("/regles/nouvelle", response_class=HTMLResponse)
def nouvelle_regle(request: Request, user: str = Depends(utilisateur)):
    return rendu(request, "regle_form.html", regle=None, decisions=regles.DECISIONS, champs=regles.CHAMPS)


@app.get("/regles/{id_}/modifier", response_class=HTMLResponse)
def modifier_regle(request: Request, id_: str, user: str = Depends(utilisateur)):
    regle = next((r for r in regles.lire(store) if r.id == id_), None)
    if not regle:
        raise ErreurContexte(f"La règle « {id_} » n'existe pas.")
    return rendu(request, "regle_form.html", regle=regle, decisions=regles.DECISIONS, champs=regles.CHAMPS)


@app.post("/regles/enregistrer", dependencies=[Depends(meme_origine)])
def enregistrer_regle(request: Request, ancien_id: str = Form(""), id_: str = Form("", alias="id"),
                      nom: str = Form(""), outil: str = Form(""), champ: str = Form("commande"),
                      motif: str = Form(""), decision: str = Form("ask"), message: str = Form(""),
                      actif: str = Form(""), user: str = Depends(utilisateur)):
    donnees = dict(id=id_, nom=nom, outil=outil, champ=champ, motif=motif, decision=decision,
                   message=message, actif=bool(actif))
    try:
        regle, _ = regles.enregistrer(store, donnees, ancien_id or None, auteur=f"web:{user}")
    except ErreurContexte as e:  # réafficher le formulaire sans perdre la saisie
        return rendu(request, "regle_form.html", regle=regles.Regle(**donnees), ancien_id=ancien_id,
                     erreur=str(e), decisions=regles.DECISIONS, champs=regles.CHAMPS)
    return RedirectResponse(f"/regles?{urlencode({'msg': f'Règle « {regle.nom} » enregistrée.', 'mode': 'avance'})}"
                            f"#r-{regle.id}", status_code=303)


@app.post("/regles/{id_}/basculer", dependencies=[Depends(meme_origine)])
def basculer_regle(id_: str, mode: str = Form("simple"), user: str = Depends(utilisateur)):
    r = regles.basculer(store, id_, auteur=f"web:{user}")
    msg = f"Règle « {r.nom} » {'activée' if r.actif else 'désactivée'}."
    return RedirectResponse(f"/regles?{urlencode({'msg': msg, 'mode': mode})}#r-{r.id}", status_code=303)


@app.post("/regles/{id_}/supprimer", dependencies=[Depends(meme_origine)])
def supprimer_regle(id_: str, mode: str = Form("simple"), user: str = Depends(utilisateur)):
    regles.supprimer(store, id_, auteur=f"web:{user}")
    return RedirectResponse(f"/regles?{urlencode({'msg': 'Règle supprimée.', 'mode': mode})}", status_code=303)


@app.post("/regles/ordre", dependencies=[Depends(meme_origine)])
async def ordonner_regles(request: Request, user: str = Depends(utilisateur)):
    try:
        ordre = (await request.json()).get("ordre")
    except (ValueError, AttributeError):
        ordre = None
    if not isinstance(ordre, list):
        raise HTTPException(400, "Liste « ordre » attendue.")
    regles.ordonner(store, [str(i) for i in ordre], auteur=f"web:{user}")
    return {"ok": True}


@app.post("/regles/git", dependencies=[Depends(meme_origine)])
def regles_git(user: str = Depends(utilisateur)):
    n = regles.ajouter_regles_git(store, auteur=f"web:{user}")
    msg = f"{n} règle(s) Git ajoutée(s)." if n else "Les règles Git recommandées sont déjà présentes."
    return RedirectResponse(f"/regles?{urlencode({'msg': msg, 'mode': 'avance'})}", status_code=303)


@app.post("/regles/tester", response_class=HTMLResponse, dependencies=[Depends(meme_origine)])
def tester_regles(request: Request, outil: str = Form("Bash"), champ: str = Form("commande"),
                  texte: str = Form(""), user: str = Depends(utilisateur)):
    """Simule une action : ce que le hook répondrait à Claude Code."""
    outil = outil.strip() or "Bash"
    champ = champ if champ in ("commande", "fichier", "contenu") else "commande"
    sortie, declenchees = regles.evaluer(regles.lire(store), outil, **{champ: texte})
    test = {"outil": outil, "champ": champ, "texte": texte, "sortie": sortie.get("hookSpecificOutput", {}),
            "declenchees": [r.id for r in declenchees]}
    return _page_regles(request, test=test, mode="avance")


@app.get("/historique", response_class=HTMLResponse)
def historique(request: Request, user: str = Depends(utilisateur)):
    return rendu(request, "historique.html", revisions=store.historique(limite=200))


@app.get("/commit/{sha}", response_class=HTMLResponse)
def commit(request: Request, sha: str, user: str = Depends(utilisateur)):
    return rendu(request, "commit.html", sha=sha, diff=store.diff(sha))


def _fichier_projet(projet: str, categorie: str, autonome: bool) -> tuple[str, str]:
    categorie = fichier_projet.categorie_valide(categorie)
    if not projet.strip():
        p = next((p for p in store.lister_projets() if p.slug == categorie), None)
        projet = p.titre if p else categorie
    docs = [d for d in store.lister() if d.categorie == categorie]
    url = URL_MCP or "(MCP_PUBLIC_URL non défini)"
    return categorie, fichier_projet.generer(projet, categorie, url, docs, autonome)


@app.get("/claude", response_class=HTMLResponse)
def page_claude(request: Request, projet: str = "", categorie: str = "", autonome: str = "",
                user: str = Depends(utilisateur)):
    ctx = dict(url_mcp=URL_MCP, projet=projet, categorie=categorie, projets=store.lister_projets(),
               autonome=bool(autonome) or not categorie, contenu="", erreur="")
    if categorie:
        try:
            cat, ctx["contenu"] = _fichier_projet(projet, categorie, bool(autonome))
            ctx["nom_fichier"] = f"mcp-memory-{cat}.md"
            ctx["lien_fichier"] = "/claude/fichier?" + urlencode(
                {"projet": projet, "categorie": cat, "autonome": "1" if autonome else ""})
        except ValueError as e:
            ctx["erreur"] = str(e)
    return rendu(request, "claude.html", **ctx)


@app.get("/claude/fichier")
def fichier_claude(projet: str = "", categorie: str = "", autonome: str = "", user: str = Depends(utilisateur)):
    try:
        cat, contenu = _fichier_projet(projet, categorie, bool(autonome))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return PlainTextResponse(contenu, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="mcp-memory-{cat}.md"'})


@app.post("/claude/verifier-jeton")
async def verifier_jeton(request: Request, user: str = Depends(utilisateur)):
    """Teste un jeton auprès du serveur MCP ; le jeton n'est ni conservé ni journalisé."""
    meme_origine(request)
    try:
        jeton = str((await request.json()).get("jeton", "")).strip()
    except ValueError:
        jeton = ""
    if not jeton or len(jeton) > 512 or not jeton.isprintable():
        return {"valide": False, "message": "Jeton vide ou invalide."}
    req = urllib.request.Request(f"{MCP_INTERNE}/mcp", method="GET",
                                 headers={"Authorization": f"Bearer {jeton}", "Accept": "text/event-stream"})
    try:
        urllib.request.urlopen(req, timeout=5).close()
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    except (urllib.error.URLError, OSError):
        return {"valide": False, "message": "Serveur MCP injoignable depuis l'interface web."}
    if code == 401:
        return {"valide": False, "message": "Jeton refusé par le serveur MCP."}
    return {"valide": True, "message": "Jeton accepté par le serveur MCP."}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("WEB_PORT", "8766")),
                proxy_headers=True, forwarded_allow_ips="*")
