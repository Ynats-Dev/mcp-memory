"""Règles de contrôle des actions de Claude Code (hooks administrés depuis l'interface web).

Claude Code appelle, avant chaque action couverte, l'outil MCP `controler_action` au moyen
d'un hook générique de type `mcp_tool` (posé une fois dans ~/.claude/settings.json). Le
serveur évalue les règles ci-dessous et renvoie la sortie JSON d'un hook PreToolUse.

Les règles sont stockées dans `contexte/.regles.yml` (versionné par Git avec le reste) :

    - id: git-push
      nom: Confirmer tout git push
      actif: true
      outil: Bash                   # expression régulière sur le nom de l'outil
      champ: commande               # commande | fichier | contenu | tout
      motif: '\\bgit\\b.*\\bpush\\b'    # expression régulière (insensible à la casse)
      decision: ask                 # deny | ask | allow | contexte
      message: Aucun git push sans mon accord explicite.

Plusieurs règles peuvent correspondre : la décision la plus stricte l'emporte
(deny > ask > allow) et leurs messages sont réunis ; « contexte » ajoute seulement un
texte transmis à Claude.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

import yaml

from .store import ErreurContexte, Store

FICHIER = ".regles.yml"
EVENEMENT = "PreToolUse"
CHAMPS = {
    "commande": "la commande (Bash)",
    "fichier": "le chemin du fichier (Write, Edit, Read…)",
    "contenu": "le texte écrit (Write, Edit)",
    "tout": "toute l'entrée de l'outil",
}
DECISIONS = {
    "deny": "Refuser",
    "ask": "Demander confirmation",
    "allow": "Autoriser sans demander",
    "contexte": "Ajouter une consigne pour Claude",
}
FORCE = {"allow": 1, "ask": 2, "deny": 3}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_REGLES, MAX_MOTIF, MAX_ENTREE = 200, 500, 20000
SERVEUR_MCP = "mcp-memory"   # nom sous lequel le serveur est enregistré dans Claude Code
OUTIL_MCP = "controler_action"

# --------------------------------------------------------------- mode simple
# Une « action » décrit en clair ce que Claude veut faire ; elle se traduit en outil + champ +
# motif. `cible` : None = pas de texte à saisir, sinon le libellé du champ texte (vide permis
# si `cible_facultative`).
PUSH_BASE = r"(^|[^\w-])git(\s+-[cC]\s+\S+)*\s+push"
PUSH = PUSH_BASE + r"(\s|$)"
SIGNATURE = (r"(git\b.*\b(commit|tag|notes|merge|rebase)|gh\s+(pr|issue|release)).*"
             r"(co-authored-by:[^\n]*(claude|anthropic)|generated with \[?claude|noreply@anthropic\.com"
             r"|--author[= ]+[\"']?[^\"']*claude)")
ECRITURE = "Write|Edit|MultiEdit|NotebookEdit"
ACTIONS = {
    "push": {"libelle": "pousser du code (git push)", "cible": "vers un dépôt contenant", "cible_facultative": True,
             "outil": "Bash", "champ": "commande", "motif": PUSH},
    "push-force": {"libelle": "forcer un push (git push --force)", "cible": None, "outil": "Bash",
                   "champ": "commande", "motif": PUSH_BASE + r"(\s.*)?\s(--force\S*|-f|\+\S+)(\s|$)"},
    "signature": {"libelle": "signer un commit ou une PR au nom de Claude", "cible": None, "outil": "Bash",
                  "champ": "commande", "motif": SIGNATURE},
    "commande": {"libelle": "lancer une commande contenant", "cible": "texte de la commande", "outil": "Bash",
                 "champ": "commande", "motif": ""},
    "suppression": {"libelle": "supprimer des fichiers (rm)", "cible": None, "outil": "Bash",
                    "champ": "commande", "motif": r"(^|[;&|(]\s*|\s)(sudo\s+)?rm\s"},
    "installation": {"libelle": "installer des paquets (npm, pip, brew…)", "cible": None, "outil": "Bash",
                     "champ": "commande",
                     "motif": r"\b(npm|pnpm|yarn|bun|pip3?|uv|brew|apt(-get)?|gem|cargo)\s+(install|add|i)\b"},
    "modifier": {"libelle": "modifier un fichier dont le chemin contient", "cible": "partie du chemin",
                 "outil": ECRITURE, "champ": "fichier", "motif": ""},
    "lire": {"libelle": "lire un fichier dont le chemin contient", "cible": "partie du chemin",
             "outil": "Read", "champ": "fichier", "motif": ""},
    "ecrire-texte": {"libelle": "écrire un texte contenant", "cible": "texte", "outil": ECRITURE,
                     "champ": "contenu", "motif": ""},
    "outil-mcp": {"libelle": "utiliser un outil MCP dont le nom contient", "cible": "nom du serveur ou de l'outil",
                  "outil": "", "champ": "tout", "motif": ""},
}
DECISIONS_SIMPLES = {"deny": "le bloquer", "ask": "me demander", "allow": "l'autoriser sans demander",
                     "contexte": "lui rappeler une consigne"}

# Règles prêtes à l'emploi (mode simple : un interrupteur chacune)
MODELES = [
    {"id": "git-signature-claude", "nom": "Jamais de commit ni de PR signé Claude", "action": "signature",
     "decision": "deny", "message": "Directive globale : commits, tags et PR sous l'identité Git de "
     "l'utilisateur, jamais au nom de Claude. Retirer « Co-Authored-By: Claude », « Generated with Claude "
     "Code » ou l'auteur Claude, puis relancer.",
     "aide": "Ce qui part sur GitHub ou ailleurs reste à votre nom."},
    {"id": "git-push", "nom": "Me demander avant chaque git push", "action": "push", "decision": "ask",
     "message": "Directive globale : aucun git push sans l'accord explicite de l'utilisateur.",
     "aide": "Rien n'est publié sans votre accord."},
    {"id": "git-push-force", "nom": "Bloquer les push forcés", "action": "push-force", "decision": "deny",
     "message": "Push forcé interdit : il réécrit l'historique distant.",
     "aide": "Évite d'écraser l'historique d'un dépôt partagé."},
    {"id": "suppression-fichiers", "nom": "Me demander avant de supprimer des fichiers", "action": "suppression",
     "decision": "ask", "message": "Suppression de fichiers : confirmation requise.",
     "aide": "Toute commande rm passe par vous."},
    {"id": "fichiers-env", "nom": "Me demander avant de modifier un fichier .env", "action": "modifier",
     "cible": ".env", "decision": "ask", "message": "Fichier de configuration sensible (.env) : confirmation requise.",
     "aide": "Les fichiers de secrets ne changent pas sans vous."},
    {"id": "installation-paquets", "nom": "Me demander avant d'installer des paquets", "action": "installation",
     "decision": "ask", "message": "Installation de paquets : confirmation requise.",
     "aide": "npm, pip, brew… ne s'exécutent qu'avec votre accord."},
]
MODELES_GIT = ("git-push", "git-signature-claude")


def depuis_action(action: str, cible: str = "") -> tuple[str, str, str]:
    """(outil, champ, motif) correspondant à une action du mode simple."""
    a = ACTIONS.get(action)
    if not a:
        raise ErreurContexte("Choisir ce que Claude veut faire.")
    cible = (cible or "").strip()
    if a["cible"] and not cible and not a.get("cible_facultative"):
        raise ErreurContexte(f"Préciser : {a['cible']}.")
    if not a["cible"]:
        cible = ""
    texte = re.escape(cible)
    if action == "push":
        return a["outil"], a["champ"], a["motif"] + (rf".*{texte}" if cible else "")
    if action == "outil-mcp":
        return rf"mcp__.*{texte}.*", "tout", ""
    return a["outil"], a["champ"], a["motif"] or texte


def phrase(r: "Regle") -> str:
    """La règle en clair : « Quand Claude veut … : me demander »."""
    if r.action in ACTIONS:
        a = ACTIONS[r.action]
        cible = f" « {r.cible} »" if r.cible else ""
        return f"Quand Claude veut {a['libelle']}{cible} : {DECISIONS_SIMPLES[r.decision]}"
    return r.nom


@dataclass
class Regle:
    id: str
    nom: str
    outil: str = "Bash"
    champ: str = "commande"
    motif: str = ""
    decision: str = "ask"
    message: str = ""
    actif: bool = True
    action: str = ""   # mode simple : clé de ACTIONS (vide = règle avancée)
    cible: str = ""    # mode simple : texte saisi


def valider(donnees: dict) -> Regle:
    """Contrôle une règle saisie ; lève ErreurContexte avec un message clair.
    Une règle du mode simple (`action`) a son outil, son champ et son motif recalculés."""
    r = Regle(
        id=str(donnees.get("id") or "").strip().lower(),
        nom=str(donnees.get("nom") or "").strip()[:120],
        outil=str(donnees.get("outil") or "").strip(),
        champ=str(donnees.get("champ") or "commande").strip(),
        motif=str(donnees.get("motif") or ""),
        decision=str(donnees.get("decision") or "ask").strip(),
        message=str(donnees.get("message") or "").strip()[:1000],
        actif=bool(donnees.get("actif", True)),
        action=str(donnees.get("action") or "").strip(),
        cible=str(donnees.get("cible") or "").strip()[:200],
    )
    if r.action:
        r.outil, r.champ, r.motif = depuis_action(r.action, r.cible)
        if not ACTIONS[r.action]["cible"]:
            r.cible = ""
        if r.decision not in DECISIONS:
            raise ErreurContexte(f"Décision inconnue : {r.decision}.")
        r.nom = r.nom or phrase(r)[:120]
        if not r.message and r.decision != "contexte":
            r.message = f"Règle « {r.nom} »."
    if not ID_RE.match(r.id):
        raise ErreurContexte("Identifiant de règle invalide : minuscules, chiffres et tirets (ex. « git-push »).")
    if not r.nom:
        raise ErreurContexte("Donner un nom à la règle.")
    if r.champ not in CHAMPS:
        raise ErreurContexte(f"Champ inconnu : {r.champ}.")
    if r.decision not in DECISIONS:
        raise ErreurContexte(f"Décision inconnue : {r.decision}.")
    if r.decision == "contexte" and not r.message:
        raise ErreurContexte("Écrire la consigne à rappeler à Claude.")
    if r.decision in ("deny", "ask") and not r.message:
        raise ErreurContexte("Indiquer un message : il explique la décision à Claude (et à vous).")
    for nom, motif in (("outil", r.outil or ".*"), ("motif", r.motif or ".*")):
        if len(motif) > MAX_MOTIF:
            raise ErreurContexte(f"Expression « {nom} » trop longue ({MAX_MOTIF} caractères maximum).")
        try:
            re.compile(motif)
        except re.error as e:
            raise ErreurContexte(f"Expression « {nom} » invalide : {e}.") from None
    return r


def identifiant_libre(regles: list["Regle"], base: str) -> str:
    """Identifiant dérivé d'un texte, rendu unique (« regle », « regle-2 »…)."""
    import unicodedata
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:50] or "regle"
    pris, ident, n = {r.id for r in regles}, base, 2
    while ident in pris:
        ident, n = f"{base}-{n}", n + 1
    return ident


# ------------------------------------------------------------------ stockage
def lire(store: Store) -> list[Regle]:
    f = store.docs / FICHIER
    try:
        brut = yaml.safe_load(f.read_text(encoding="utf-8")) or []
    except FileNotFoundError:
        return []
    except yaml.YAMLError as e:
        raise ErreurContexte(f"Fichier {FICHIER} illisible : {e}") from None
    regles = []
    for d in brut if isinstance(brut, list) else []:
        try:
            regles.append(valider(d))
        except (ErreurContexte, AttributeError):
            continue  # une règle abîmée (modifiée à la main) est ignorée, pas bloquante
    return regles


def _ecrire(store: Store, regles: list[Regle], message: str, auteur: str) -> str | None:
    if len(regles) > MAX_REGLES:
        raise ErreurContexte(f"{MAX_REGLES} règles maximum.")
    f = store.docs / FICHIER
    tmp = f.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump([asdict(r) for r in regles], allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(f)
    return store._commit(message, auteur)


def enregistrer(store: Store, donnees: dict, ancien_id: str | None = None, auteur: str = "inconnu"):
    """Crée ou modifie une règle (ancien_id : identifiant avant modification)."""
    regle = valider(donnees)
    with store.verrou():
        regles = lire(store)
        if ancien_id:
            if not any(r.id == ancien_id for r in regles):
                raise ErreurContexte(f"La règle « {ancien_id} » n'existe pas.")
            if regle.id != ancien_id and any(r.id == regle.id for r in regles):
                raise ErreurContexte(f"Une règle « {regle.id} » existe déjà.")
            regles = [regle if r.id == ancien_id else r for r in regles]
            message = f"Règle modifiée : {regle.id}"
        else:
            if any(r.id == regle.id for r in regles):
                raise ErreurContexte(f"Une règle « {regle.id} » existe déjà.")
            regles.append(regle)
            message = f"Règle ajoutée : {regle.id}"
        return regle, _ecrire(store, regles, message, auteur)


def supprimer(store: Store, id_: str, auteur: str = "inconnu") -> str | None:
    with store.verrou():
        regles = lire(store)
        if not any(r.id == id_ for r in regles):
            raise ErreurContexte(f"La règle « {id_} » n'existe pas.")
        return _ecrire(store, [r for r in regles if r.id != id_], f"Règle supprimée : {id_}", auteur)


def basculer(store: Store, id_: str, auteur: str = "inconnu") -> Regle:
    with store.verrou():
        regles = lire(store)
        cible = next((r for r in regles if r.id == id_), None)
        if not cible:
            raise ErreurContexte(f"La règle « {id_} » n'existe pas.")
        cible.actif = not cible.actif
        _ecrire(store, regles, f"Règle {'activée' if cible.actif else 'désactivée'} : {id_}", auteur)
        return cible


def ordonner(store: Store, ids: list[str], auteur: str = "inconnu") -> None:
    with store.verrou():
        regles = {r.id: r for r in lire(store)}
        ordre = [regles.pop(i) for i in dict.fromkeys(ids) if i in regles] + list(regles.values())
        _ecrire(store, ordre, "Réorganisation des règles", auteur)


def creer_simple(store: Store, action: str, cible: str, decision: str, message: str = "",
                 auteur: str = "inconnu") -> Regle:
    """Règle du mode simple : identifiant et nom déduits de la phrase."""
    with store.verrou():
        regles = lire(store)
        brouillon = Regle(id="x", nom="", action=action, cible=cible, decision=decision)
        if action not in ACTIONS:
            raise ErreurContexte("Choisir ce que Claude veut faire.")
        if decision not in DECISIONS:
            raise ErreurContexte("Choisir ce qui doit se passer.")
        nom = phrase(brouillon)[:120]
        donnees = dict(id=identifiant_libre(regles, f"{action} {cible}"), nom=nom, action=action, cible=cible,
                       decision=decision, message=message, actif=True)
        regle = valider(donnees)
        _ecrire(store, regles + [regle], f"Règle ajoutée : {regle.id}", auteur)
        return regle


def basculer_modele(store: Store, id_: str, auteur: str = "inconnu") -> Regle:
    """Interrupteur d'une règle prête à l'emploi : l'ajoute si absente, sinon l'active / désactive."""
    modele = next((m for m in MODELES if m["id"] == id_), None)
    if not modele:
        raise ErreurContexte(f"Modèle inconnu : {id_}.")
    with store.verrou():
        regles = lire(store)
        existante = next((r for r in regles if r.id == id_), None)
        if existante:
            existante.actif = not existante.actif
            _ecrire(store, regles, f"Règle {'activée' if existante.actif else 'désactivée'} : {id_}", auteur)
            return existante
        regle = valider({k: v for k, v in modele.items() if k != "aide"})
        _ecrire(store, regles + [regle], f"Règle ajoutée : {id_}", auteur)
        return regle


def ajouter_regles_git(store: Store, auteur: str = "inconnu") -> int:
    """Ajoute les règles Git recommandées absentes ; renvoie le nombre ajouté."""
    with store.verrou():
        regles = lire(store)
        existants = {r.id for r in regles}
        nouvelles = [valider({k: v for k, v in m.items() if k != "aide"}) for m in MODELES
                     if m["id"] in MODELES_GIT and m["id"] not in existants]
        if nouvelles:
            _ecrire(store, regles + nouvelles, "Règles Git recommandées ajoutées", auteur)
        return len(nouvelles)


# ---------------------------------------------------------------- évaluation
def _nettoyer(valeur) -> str:
    """Valeur reçue du hook : une substitution ${…} restée telle quelle (champ absent) vaut vide."""
    if valeur is None:
        return ""
    if not isinstance(valeur, str):
        valeur = json.dumps(valeur, ensure_ascii=False)
    return "" if re.fullmatch(r"\$\{[^}]*\}", valeur.strip()) else valeur[:MAX_ENTREE]


def evaluer(regles: list[Regle], outil: str, commande: str = "", fichier: str = "", contenu: str = "",
            nouveau: str = "", entree: str = "") -> tuple[dict, list[Regle]]:
    """Sortie de hook PreToolUse ({} si aucune règle ne s'applique) et règles déclenchées.
    contenu / nouveau : texte écrit par Write (content) ou Edit (new_string)."""
    outil = _nettoyer(outil)
    ecrit = "\n".join(v for v in (_nettoyer(contenu), _nettoyer(nouveau)) if v)
    valeurs = {"commande": _nettoyer(commande), "fichier": _nettoyer(fichier), "contenu": ecrit}
    valeurs["tout"] = _nettoyer(entree) or "\n".join(v for v in valeurs.values() if v)
    declenchees = []
    for r in regles:
        if not r.actif or not re.fullmatch(r.outil or ".*", outil):
            continue
        texte = valeurs[r.champ]
        if r.motif and not re.search(r.motif, texte, re.IGNORECASE | re.MULTILINE):
            continue
        if not r.motif and not texte and r.champ != "tout":
            continue
        declenchees.append(r)
    if not declenchees:
        return {}, []
    sortie: dict = {"hookEventName": EVENEMENT}
    decisives = [r for r in declenchees if r.decision in FORCE]
    if decisives:
        decision = max((r.decision for r in decisives), key=FORCE.get)
        sortie["permissionDecision"] = decision
        raisons = [r.message or r.nom for r in decisives if r.decision == decision]
        sortie["permissionDecisionReason"] = " ".join(dict.fromkeys(raisons))
    contextes = [r.message for r in declenchees if r.decision == "contexte"]
    if contextes:
        sortie["additionalContext"] = "\n".join(dict.fromkeys(contextes))
    return {"hookSpecificOutput": sortie}, declenchees


def configuration_hook(serveur: str = SERVEUR_MCP) -> dict:
    """Bloc à fusionner dans ~/.claude/settings.json. Il est fixe : « matcher » vaut « * », le
    serveur est consulté avant chaque action et toute règle ajoutée s'applique sans le recopier."""
    return {"hooks": {EVENEMENT: [{
        "matcher": "*",
        "hooks": [{
            "type": "mcp_tool",
            "server": serveur,
            "tool": OUTIL_MCP,
            "input": {
                "outil": "${tool_name}",
                "commande": "${tool_input.command}",
                "fichier": "${tool_input.file_path}",
                "contenu": "${tool_input.content}",
                "nouveau": "${tool_input.new_string}",
            },
            "timeout": 5,
            "onFailure": "continue",
        }],
    }]}}
