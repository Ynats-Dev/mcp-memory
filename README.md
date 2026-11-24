# MCP Memory — serveur MCP + interface web

Source de contexte centrale, partagée entre Claude (Claude Desktop, Claude Code),
VS Code (Copilot) et tout client MCP du **réseau local**, avec une interface web.
Par défaut, rien n'est exposé sur Internet (claude.ai web et mobile n'y ont alors pas accès).
Les assistants lisent le contexte et le tiennent à jour eux-mêmes via les outils MCP.

| Service | Port | Accès |
|---|---|---|
| Serveur MCP (Streamable HTTP) | 8765 | `http://IP_DU_SERVEUR:8765/mcp` (ou `https://mcp.example.com/mcp` derrière un reverse proxy) : jeton `Authorization: Bearer <MCP_TOKEN>`, ou OAuth (connecteur claude.ai) |
| Interface web | 8766 | `http://IP_DU_SERVEUR:8766` (ou `https://mcp.example.com`), page de connexion (compte créé à la première ouverture) |

Dans la suite, `IP_DU_SERVEUR` désigne la machine du réseau local qui héberge les conteneurs,
et `mcp.example.com` le nom éventuel donné au service par votre reverse proxy. Remplacez-les
par vos propres valeurs.

## Fonctionnement

- Chaque document est un fichier Markdown (`resume`, `decisions`, `regles/combat`…)
  stocké dans le volume Docker `mcp-memory_contexte-data`.
- Ce volume est un dépôt **Git** : chaque modification (LLM ou web) crée un commit
  avec son auteur. L'historique, les différences et la restauration d'une version
  sont disponibles dans l'interface web et via les outils MCP.
- Les deux conteneurs partagent le volume ; un verrou fichier sérialise les écritures.
- Sauvegarde : chaque nuit (et à la demande, page **Mémoire**), le dépôt Git du contexte est poussé vers un dépôt distant **vide** (`GIT_REMOTE`, `GIT_REMOTE_USER`, `GIT_REMOTE_TOKEN`, `SAUVEGARDE_HEURE`). Le jeton passe par un en-tête HTTP, jamais par `.git/config`. Si le conteneur ne résout pas le nom du dépôt (DNS du réseau local) : `GIT_REMOTE_HOST` et `GIT_REMOTE_HOST_IP`.

### Projets

Un **projet** regroupe les documents d'un même sujet : son identifiant sert de préfixe
(`maison/serveur-nas`) et de périmètre au fichier de projet pour Claude. Il est décrit
par `contexte/<projet>/.projet.yml` (titre, description) et existe même vide.
Interface web : créer, modifier, renommer (déplace tous ses documents), supprimer (s'il est
vide) ; sur un document, **Déplacer** le range dans un autre projet. L'historique suit le
document lors des déplacements.

### Outils MCP
`vue_ensemble` (à appeler en premier), `lister_projets`, `lister_documents`, `plan_document`,
`lire_document`, `rechercher`, `ecrire_document`, `ajouter_au_document`, `remplacer_texte`,
`deplacer_document`, `enregistrer_decision`, `historique`.
Création de projet, suppression et restauration d'une version : interface web.

**Économie de tokens.** Les documents longs ne sont pas renvoyés en entier :
- l'index (`vue_ensemble`, filtrable par `projet`) affiche la taille estimée de chaque document ;
- `plan_document` donne la table des matières numérotée, avec la taille de chaque section ;
- `lire_document(chemin, section="2.3")` (ou un titre, même partiel) ne renvoie que cette
  section. Sans `section`, un document de plus d'environ 4 000 tokens renvoie son plan, sauf
  avec `complet=true` ;
- `rechercher` (filtrable par `projet`) trie par pertinence et indique la section de chaque passage ;
- `ajouter_au_document(…, section=…)` insère à la fin d'une section sans relire le document.
- chaque projet a une fiche `<projet>/resume` (~500 tokens, tenue à jour par Claude), affichée
  en tête de `vue_ensemble(projet=…)` ;
- les consignes demandent de corriger l'information à sa place plutôt que d'empiler des
  sections datées (l'historique Git conserve l'ancien état) ;
- un document portant le tag `archive` (journal d'une mise en place, état ancien) est signalé
  dans l'index et exclu de `rechercher`, sauf avec `archives=true` ;
- descriptions d'outils courtes, sans sortie structurée en double (≈ 1 200 tokens pour l'ensemble).

Ressources : `contexte://index`, `contexte://doc/{nom}`, `contexte://doc/{categorie}/{nom}`.
Prompt : `charger_contexte`.

## Installation

Prérequis : une machine Linux du réseau local avec Docker et Docker Compose.

```bash
sudo git clone https://github.com/Ynats-Dev/mcp-memory.git /opt/mcp-memory
sudo chown -R "$USER" /opt/mcp-memory
cd /opt/mcp-memory

# Ports libres ? (aucune ligne attendue)
sudo ss -ltnp | grep -E ':(8765|8766)\b'

# Secrets
cp .env.example .env
sed -i "s/^MCP_TOKEN=.*/MCP_TOKEN=$(openssl rand -hex 32)/" .env
chmod 600 .env
cat .env          # noter MCP_TOKEN (à ranger dans un gestionnaire de mots de passe)

# Construction et démarrage
docker compose up -d --build
docker compose ps                     # les deux services doivent être « healthy » après ~1 min
curl -s http://127.0.0.1:8765/sante   # → ok
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8765/mcp   # → 401 (jeton exigé)
```

Si le pare-feu UFW est actif, n'ouvrir les ports qu'au réseau local (adapter la plage) :

```bash
sudo ufw status
sudo ufw allow from 192.168.1.0/24 to any port 8765,8766 proto tcp
```

Mise à jour du code : `git pull && docker compose up -d --build` (les données du volume ne
sont pas touchées), ou le bouton **Mise à jour** de l'interface (voir plus bas).

## Accès à l'interface web

À la première ouverture, l'interface demande de **créer le compte** (identifiant + mot de passe,
10 caractères minimum) : l'ouvrir juste après le premier déploiement. Ensuite, page de connexion
(« Rester connecté » = 30 jours, sinon 12 h ; 5 échecs = 5 minutes de blocage). Le mot de passe
est stocké haché (PBKDF2) dans `/data/.web/compte.json`, hors du dépôt Git du contexte.
**Mon compte** (bas de la barre latérale) : changer le mot de passe (déconnecte les autres
navigateurs) ou se déconnecter.

Mot de passe oublié :
```bash
cd /opt/mcp-memory && docker compose exec web python -m app.compte reinitialiser
# puis ouvrir l'interface : elle redemande la création du compte (documents intacts)
```

## Page Mémoire (indicateurs)

- **Par projet** : coût de démarrage d'une conversation (fiche résumé + index), volume des documents actifs et des archives, fiche résumé, dernière modification.
- **Santé** : fiche résumé absente ou trop longue, document long sans titres, section de plus de ~3k tokens, dérive en journal (« 🆕 », titres datés, tâches cochées), renvois vers des documents absents. Dans les documents, un chemin `projet/document` cité devient un lien (barré s'il n'existe pas).
- **Usage par Claude** : chaque appel d'outil MCP est journalisé dans `<DATA_DIR>/.usage/AAAA-MM.jsonl` (outil, chemin, section, requête, taille de la réponse, durée ; jamais de contenu ; 12 mois). La page montre les tokens servis par jour et par projet, les documents les plus lus, ceux lus en entier, les recherches sans résultat et les écritures par auteur.
- **Sauvegarde** et **export** `.zip` (tout, ou un projet depuis sa page).

**Garde-fou contre les secrets** : toute écriture (MCP, interface, import) contenant une clé privée, un jeton (JWT, GitHub, AWS, `sk-…`, Bearer), un mot de passe dans une adresse de connexion ou une affectation `…PASSWORD=`, `…TOKEN=`, `…SECRET=` avec une vraie valeur est refusée. Les valeurs modèles (`<secret>`, `***`, `${…}`, chemins) passent ; un secret déjà présent ne bloque pas les modifications suivantes. Dans l'interface, une case permet d'enregistrer quand même (faux positif).

**Recherche** insensible aux accents, à la casse et aux apostrophes typographiques.

## Mise à jour de l'application (bouton)

Menu **Mise à jour** : affiche la version en service, les nouveaux commits du dépôt (un point
signale qu'il y en a) et met à jour en un clic (`git pull --ff-only`, puis
`docker compose up -d --build`, soit une à deux minutes d'indisponibilité ; les documents ne
sont pas touchés).

L'interface n'a accès ni à Docker ni au dépôt. Elle dépose une demande dans `.maj/`, monté
dans le conteneur web, et un service systemd **de l'hôte** l'exécute (`scripts/maj-hote.sh`).
Il recherche aussi les nouveautés toutes les 6 h. Installation, une fois :

```bash
cd /opt/mcp-memory && git pull && docker compose up -d
sudo scripts/installer-maj.sh            # --retirer pour désinstaller
```

Le service tourne sous le propriétaire du dépôt (qui doit être dans le groupe `docker`), sinon
en root. En cas de problème : `systemctl status mcp-memory-maj.path`, `.maj/derniere.log`.
Une mise à jour est refusée si le dossier contient des modifications locales.

## Connexion des clients

La page **Connexion Claude** de l'interface web permet d'enregistrer le jeton
(`MCP_TOKEN`) **dans le navigateur** (localStorage, jamais côté serveur), de le
vérifier auprès du serveur MCP, et de copier les configurations Claude Desktop /
Claude Code déjà remplies.

### Claude Code (accès global, une seule commande)

```bash
claude mcp add --transport http --scope user mcp-memory https://mcp.example.com/mcp \
  --header "Authorization: Bearer VOTRE_MCP_TOKEN"
claude mcp list        # mcp-memory doit apparaître « connected »
```

### VS Code / GitHub Copilot Chat

- Pour un projet : copier `clients/vscode-mcp.json` dans `.vscode/mcp.json` et y mettre votre adresse.
- Pour tous les projets : palette de commandes → **MCP: Open User Configuration**, puis coller le même contenu.

VS Code demande le jeton au premier démarrage et le stocke de façon chiffrée.
Dans Copilot Chat, passer en mode **Agent** pour que les outils soient proposés.

### Claude Desktop

Claude Desktop ne lance que des serveurs locaux (stdio) ; le pont `mcp-remote`
(Node.js requis) relaie vers le serveur HTTP. Fusionner
`clients/claude_desktop_config.json` dans le fichier de configuration de Claude Desktop
(macOS : `~/Library/Application Support/Claude/claude_desktop_config.json`), remplacer
l'adresse et `COLLER_ICI_LE_MCP_TOKEN`, puis quitter et relancer Claude Desktop.

### Navigateur

`http://IP_DU_SERVEUR:8766` (ou l'adresse de votre reverse proxy) depuis n'importe quel
poste du réseau local ou du VPN.

### Option : connecteur claude.ai (exige une exposition Internet)

Par défaut, `OAUTH_PASSWORD` est vide dans `.env` : OAuth est désactivé et le serveur reste
local. La procédure ci-dessous ne sert que si vous décidez d'exposer le serveur.

claude.ai joint le serveur **depuis Internet** (serveurs d'Anthropic) et s'authentifie en
OAuth : le serveur MCP embarque son propre serveur OAuth, protégé par `OAUTH_PASSWORD`.

1. **Serveur** : renseigner `MCP_PUBLIC_URL` et `OAUTH_PASSWORD` dans `.env`
   (`openssl rand -base64 18`), puis `docker compose up -d --build`.
2. **Reverse proxy HTTPS** : rendre publics uniquement le endpoint MCP et le parcours OAuth,
   l'interface web restant réservée au réseau local. Exemple Apache :
   [deploy/apache-mcp.conf](deploy/apache-mcp.conf). Le port 443 doit être redirigé vers le
   reverse proxy par votre routeur. Vérifier depuis l'extérieur (4G, par exemple) :
   `curl -s https://mcp.example.com/.well-known/oauth-authorization-server` → JSON,
   et `https://mcp.example.com/` → refusé.
3. **claude.ai** → Réglages → Connecteurs → *Ajouter un connecteur personnalisé* :
   nom `MCP Memory`, URL `https://mcp.example.com/mcp`, paramètres avancés vides →
   *Se connecter* → page « Autoriser l'accès » → mot de passe `OAUTH_PASSWORD`.
   Le connecteur est alors disponible sur le web, le mobile et Claude Desktop.

Sécurité : seules les adresses de retour de claude.ai / claude.com (et `localhost`) sont
acceptées à l'enregistrement ; 5 mauvais mots de passe bloquent la page 15 min ; les jetons
(1 h, renouvelés automatiquement, 90 jours sans usage au maximum) sont stockés hachés dans
`/data/.oauth/`, hors historique Git. Pour révoquer tous les accès claude.ai :
`docker compose exec mcp rm /data/.oauth/etat.json && docker compose restart mcp`.

### Projet Claude : remplacer les fichiers de contexte par le serveur

Prérequis : Claude Desktop branché sur le serveur (section *Claude Desktop* ci-dessus).
Sans connecteur claude.ai, le fichier généré ne fonctionne que dans Claude Desktop sur le
réseau local ; sur le web ou le mobile, Claude signalera que les outils sont indisponibles.

1. Importer les fichiers du projet dans le serveur (voir *Import* ci-dessous), par exemple
   dans le projet `maison`.
2. Interface web → **Claude** : nom du projet + catégorie → *Télécharger* le fichier
   `mcp-memory-<categorie>.md`. Il indique à Claude le connecteur à utiliser, les documents
   du projet, et lui demande de tenir le contexte à jour sans qu'on le lui demande (case à
   décocher pour qu'il propose les modifications au lieu de les appliquer).
3. Dans le projet claude.ai : **Contexte → Ajouter** ce fichier, puis retirer les anciens.
4. Dans une conversation du projet (Claude Desktop), vérifier que les outils `mcp-memory`
   sont proposés (icône des outils / menu « + »).

### Import de fichiers existants

**Depuis l'interface** : glisser plusieurs fichiers `.md` sur la page d'un projet (ou cliquer
sur la zone de dépôt). Mêmes règles de nommage que le script ; les documents existants sont
ignorés, sauf si « Remplacer les documents existants » est coché.

**En ligne de commande** : copier les fichiers téléchargés depuis claude.ai dans `import/`
(dossier ignoré par Git) sur le serveur, puis :

```bash
./scripts/import.sh maison --essai   # aperçu : nom de chaque document, doublons
./scripts/import.sh maison           # import (un commit par fichier)
rm -r import/                        # ne pas laisser traîner les copies
```

`serveur nas_0145.md` devient `maison/serveur-nas` (le suffixe horaire de claude.ai
est retiré). Si deux fichiers donnent le même document, l'import s'arrête : n'en garder qu'un.

## Consignes recommandées pour Claude

Le serveur envoie déjà ses propres consignes aux clients MCP (lire peu, mettre à jour à la
bonne place). Elles ne garantissent pas que Claude **pense** à consulter le serveur : face à
une question courte (« tel site ne répond plus »), il a tendance à répondre avec des
hypothèses génériques sans lire votre documentation. Pour l'éviter, ajoutez une consigne
explicite à vos instructions personnelles :

- **Claude Code** : `~/.claude/CLAUDE.md` (tous les projets) ou `CLAUDE.md` à la racine d'un dépôt ;
- **Projet claude.ai / Claude Desktop** : instructions du projet ;
- **VS Code / Copilot** : `.github/copilot-instructions.md`.

Nommez des **sujets déclencheurs** précis (vos machines, services, domaines) plutôt qu'une
formule vague. Modèle à adapter :

```markdown
## Consultation obligatoire du serveur MCP mcp-memory

Déclencheurs : toute question ou panne portant sur <mes sujets : réseau, DNS, NAS,
domotique, serveurs, domaines *.mondomaine.fr…>.

Procédure, AVANT tout diagnostic ou toute réponse :
1. Charger les outils du serveur s'ils sont différés (Claude Code : un seul appel
   ToolSearch `select:mcp__mcp-memory__vue_ensemble,mcp__mcp-memory__rechercher,mcp__mcp-memory__lire_document`).
2. Appeler `vue_ensemble` (avec `projet` si le sujet est connu) pour repérer les documents pertinents.
3. Lancer `rechercher` avec les mots clés du sujet (nom d'hôte, service, symptôme).
4. Lire avec `lire_document` les documents trouvés, en commençant par les fiches
   d'état (pas les archives, sauf besoin).
5. Seulement ensuite, diagnostiquer. Ne pas proposer d'hypothèse générique avant
   d'avoir lu la documentation.

Faits à ne pas redécouvrir :
- <un ou deux pièges récurrents propres à votre installation>

Après résolution d'un incident : proposer d'ajouter la cause et le correctif
(une ligne) au document concerné du serveur.
```

Le nom `mcp__mcp-memory__…` suppose que le serveur est enregistré sous le nom `mcp-memory` ;
adaptez-le si vous l'avez nommé autrement. Gardez la liste « Faits à ne pas redécouvrir »
courte : les détails ont leur place dans les documents du serveur, qui restent la source de vérité.

## Sauvegarde et restauration

Sur le serveur, dans `/opt/mcp-memory` :

```bash
./scripts/sauvegarde.sh
# → sauvegardes/contexte-AAAAMMJJ-HHMMSS.tgz     (volume complet)
#   sauvegardes/contexte-AAAAMMJJ-HHMMSS.bundle  (historique Git, clonable)

./scripts/restauration.sh sauvegardes/contexte-AAAAMMJJ-HHMMSS.tgz
# fait d'abord une sauvegarde de sécurité, arrête les services, restaure, redémarre
```

En plus de la sauvegarde Git nocturne intégrée, sauvegarde complète du volume chaque nuit
à 3 h (`crontab -e`) :

```cron
0 3 * * * cd /opt/mcp-memory && ./scripts/sauvegarde.sh >> sauvegardes/cron.log 2>&1
```

Copier ensuite `sauvegardes/` vers une autre machine (NAS, montage réseau, `rsync`) pour
l'avoir hors du serveur.

Repartir d'un contexte vide (supprime **tous les documents et leur historique**, garde la
configuration ; sauvegarde automatique avant) :

```bash
./scripts/reinitialiser.sh     # demande de taper EFFACER
```

Lire un bundle sans rien restaurer : `git clone contexte-....bundle contexte-lu`.

Annuler une seule modification : interface web → document → *Historique de ce document*
→ ouvrir la version → **Restaurer cette version**.

## Sécurité

- Le serveur MCP refuse toute requête sans le bon jeton (401) ; l'interface web exige
  une session (page de connexion, cookie signé HttpOnly) et refuse les envois provenant d'un autre site (protection CSRF).
- Les ports 8765/8766 circulent en HTTP clair : ne jamais les publier directement sur Internet.
  Accès hors domicile : VPN, ou reverse proxy HTTPS limité au endpoint MCP (option connecteur claude.ai).
- OAuth désactivé (`OAUTH_PASSWORD` vide) : seul `MCP_TOKEN` donne accès au serveur MCP.
- Le HTML saisi dans les documents est affiché comme du texte, jamais exécuté.
