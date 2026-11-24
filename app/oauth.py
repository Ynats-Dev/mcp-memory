"""Serveur d'autorisation OAuth 2.1 minimal pour les connecteurs claude.ai.

claude.ai (web, mobile, Desktop) n'envoie pas d'en-tête fixe : il découvre le
serveur OAuth, s'enregistre (DCR), puis ouvre une page d'autorisation dans le
navigateur. Ici, cette page demande le mot de passe OAUTH_PASSWORD.

Le jeton fixe MCP_TOKEN reste accepté (Claude Code, VS Code, mcp-remote).

État persistant : DATA_DIR/.oauth/etat.json (clients, jetons hachés en SHA-256),
hors du dépôt Git du contexte (seul le dossier « contexte » est commité).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

PORTEE = "contexte"
DUREE_ACCES = 3600                 # 1 h, renouvelé automatiquement par le client
DUREE_RAFRAICHISSEMENT = 90 * 86400  # 90 jours sans usage → nouvelle connexion
DUREE_DEMANDE = 600                # page de connexion valable 10 min
DUREE_CODE = 300
ECHECS_MAX, FENETRE_ECHECS = 5, 900  # 5 mauvais mots de passe / 15 min → blocage

REDIRECTIONS_AUTORISEES = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
]


def _h(jeton: str) -> str:
    return hashlib.sha256(jeton.encode()).hexdigest()


def redirection_autorisee(uri: str, extras: list[str]) -> bool:
    if uri in REDIRECTIONS_AUTORISEES or any(uri.startswith(p) for p in extras if p):
        return True
    u = urlparse(uri)  # clients locaux (Claude Code, MCP Inspector…)
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")


class FournisseurOAuth:
    def __init__(self, url_publique: str, mot_de_passe: str, jeton_fixe: str, dossier: Path):
        self.url_publique = url_publique.rstrip("/")
        self.mot_de_passe = mot_de_passe.encode()
        self.jeton_fixe = jeton_fixe.encode()
        self.extras = [p.strip() for p in os.environ.get("OAUTH_REDIRECTS", "").split(",")]
        dossier.mkdir(parents=True, exist_ok=True)
        os.chmod(dossier, 0o700)
        self.fichier = dossier / "etat.json"
        self._verrou = threading.Lock()
        self._etat = self._charger()
        self._demandes: dict[str, tuple[float, str, AuthorizationParams]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._echecs: list[float] = []

    # --------------------------------------------------------- persistance
    def _charger(self) -> dict:
        try:
            etat = json.loads(self.fichier.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            etat = {}
        for cle in ("clients", "acces", "rafraichissement"):
            etat.setdefault(cle, {})
        return etat

    def _sauver(self) -> None:
        maintenant = time.time()
        for cle in ("acces", "rafraichissement"):
            self._etat[cle] = {k: v for k, v in self._etat[cle].items() if v["expire"] > maintenant}
        tmp = self.fichier.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._etat, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.fichier)

    # -------------------------------------------------------------- clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        brut = self._etat["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(brut) if brut else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not redirection_autorisee(str(uri), self.extras):
                raise RegistrationError("invalid_redirect_uri", f"Adresse de retour non autorisée : {uri}")
        with self._verrou:
            self._etat["clients"][client_info.client_id] = client_info.model_dump(mode="json")
            self._sauver()

    # -------------------------------------------------------- autorisation
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        maintenant = time.time()
        self._demandes = {k: v for k, v in self._demandes.items() if v[0] > maintenant}
        demande = secrets.token_urlsafe(32)
        self._demandes[demande] = (maintenant + DUREE_DEMANDE, client.client_id, params)
        return f"{self.url_publique}/oauth/connexion?demande={demande}"

    async def page_connexion(self, request: Request) -> Response:
        """GET : formulaire ; POST : vérifie le mot de passe puis renvoie vers le client."""
        if request.method == "POST":
            form = await request.form()
            demande = str(form.get("demande", ""))
        else:
            demande = request.query_params.get("demande", "")
        entree = self._demandes.get(demande)
        if not entree or entree[0] < time.time():
            return _page("Demande expirée", "<p>Relancez la connexion depuis Claude.</p>", 400)
        _, client_id, params = entree
        client = await self.get_client(client_id)
        nom = html.escape((client.client_name if client else None) or client_id)
        hote = html.escape(urlparse(str(params.redirect_uri)).netloc)

        erreur = ""
        if request.method == "POST":
            self._echecs = [t for t in self._echecs if t > time.time() - FENETRE_ECHECS]
            if len(self._echecs) >= ECHECS_MAX:
                return _page("Trop d'essais", "<p>Réessayez dans 15 minutes.</p>", 429)
            fourni = str(form.get("mot_de_passe", "")).encode()
            if hmac.compare_digest(fourni, self.mot_de_passe):
                del self._demandes[demande]
                code = secrets.token_urlsafe(32)
                self._codes[code] = AuthorizationCode(
                    code=code,
                    scopes=params.scopes or [PORTEE],
                    expires_at=time.time() + DUREE_CODE,
                    client_id=client_id,
                    code_challenge=params.code_challenge,
                    redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=params.resource,
                    subject="proprietaire",
                )
                cible = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
                return RedirectResponse(cible, status_code=302)
            self._echecs.append(time.time())
            await asyncio.sleep(1)
            erreur = '<p class="err">Mot de passe incorrect.</p>'

        corps = f"""
<p><strong>{nom}</strong> demande l'accès en lecture et en écriture au contexte partagé.</p>
<p class="petit">Retour vers : {hote}</p>
{erreur}
<form method="post" action="/oauth/connexion">
  <input type="hidden" name="demande" value="{html.escape(demande)}">
  <input type="password" name="mot_de_passe" placeholder="Mot de passe" autofocus required>
  <button type="submit">Autoriser</button>
</form>"""
        return _page("Autoriser l'accès", corps)

    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code and code.client_id == client.client_id:
            return code
        return None

    # --------------------------------------------------------------- jetons
    def _emettre(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        acces, rafraichi = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        maintenant = time.time()
        commun = {"client_id": client_id, "scopes": scopes, "resource": resource}
        with self._verrou:
            self._etat["acces"][_h(acces)] = {
                **commun, "expire": int(maintenant + DUREE_ACCES), "lien": _h(rafraichi)}
            self._etat["rafraichissement"][_h(rafraichi)] = {
                **commun, "expire": int(maintenant + DUREE_RAFRAICHISSEMENT), "lien": _h(acces)}
            self._sauver()
        return OAuthToken(access_token=acces, token_type="Bearer", expires_in=DUREE_ACCES,
                          refresh_token=rafraichi, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        return self._emettre(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        brut = self._etat["rafraichissement"].get(_h(refresh_token))
        if not brut or brut["client_id"] != client.client_id or brut["expire"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=brut["client_id"], scopes=brut["scopes"],
                            expires_at=brut["expire"], resource=brut.get("resource"))

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        await self.revoke_token(refresh_token)  # rotation
        return self._emettre(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self.jeton_fixe):
            return AccessToken(token=token, client_id="jeton-fixe", scopes=[PORTEE])
        brut = self._etat["acces"].get(_h(token))
        if not brut or brut["expire"] < time.time():
            return None
        return AccessToken(token=token, client_id=brut["client_id"], scopes=brut["scopes"],
                           expires_at=brut["expire"], resource=brut.get("resource"))

    async def revoke_token(self, token) -> None:
        cle = _h(token.token)
        with self._verrou:
            for table, autre in (("acces", "rafraichissement"), ("rafraichissement", "acces")):
                brut = self._etat[table].pop(cle, None)
                if brut:
                    self._etat[autre].pop(brut.get("lien"), None)
            self._sauver()


def _page(titre: str, corps: str, statut: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>MCP Memory — {titre}</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f4f4f5;color:#18181b;display:grid;place-items:center;min-height:100vh;margin:0;padding:16px}}
main{{background:#fff;border-radius:12px;padding:28px;max-width:380px;width:100%;box-shadow:0 1px 4px #0002}}
h1{{font-size:1.2rem;margin:0 0 12px}} .petit{{color:#71717a;font-size:.85rem}} .err{{color:#b91c1c}}
input,button{{width:100%;box-sizing:border-box;padding:10px;margin-top:10px;font-size:1rem;border-radius:8px;border:1px solid #d4d4d8}}
button{{background:#18181b;color:#fff;border:0;cursor:pointer}}
@media (prefers-color-scheme:dark){{body{{background:#18181b;color:#f4f4f5}}main{{background:#27272a}}input{{background:#18181b;color:#f4f4f5;border-color:#52525b}}button{{background:#f4f4f5;color:#18181b}}}}
</style></head><body><main><h1>MCP Memory — {titre}</h1>{corps}</main></body></html>""",
        status_code=statut, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
