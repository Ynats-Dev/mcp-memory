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

## Organisation de l'interface

La barre latérale donne accès aux documents et à trois espaces, chacun découpé en onglets
(une adresse par onglet) :

| Espace | Onglets |
|---|---|
| **Contexte** | Accueil et recherche ; arbre des projets et sous-projets (page d'un projet : Documents · Sous-projets · Fiche résumé · Importer / Exporter · Paramètres) |
| **Claude** (`/claude`) | Directives · Procédures · Hooks (Règles prêtes à l'emploi · Mes règles · Avancé · Tester) |
| **Connexion** (`/clients`) | Claude Code · Claude Desktop · claude.ai · Projet Claude · Jeton |
| **Administration** (`/admin`) | Mémoire (Vue d'ensemble · Santé · Usage par Claude) · Historique · Sauvegarde · Mise à jour (Version · Dépôt de référence · Journal) · Compte |

Les anciennes adresses (`/memoire`, `/historique`, `/mise-a-jour`, `/compte`, `/regles`)
redirigent vers les onglets correspondants.

## Fonctionnement

- Chaque document est un fichier Markdown (`resume`, `maison/reseau`, `maison/nas/procedures`…)
  stocké dans le volume Docker `mcp-memory_contexte-data`.
- Ce volume est un dépôt **Git** : chaque modification (LLM ou web) crée un commit
  avec son auteur. L'historique, les différences et la restauration d'une version
  sont disponibles dans l'interface web et via les outils MCP.
- Les deux conteneurs partagent le volume ; un verrou fichier sérialise les écritures.
- Sauvegarde : chaque nuit (et à la demande, **Administration › Sauvegarde**), le dépôt Git du contexte est poussé vers un dépôt distant **vide** (`GIT_REMOTE`, `GIT_REMOTE_USER`, `GIT_REMOTE_TOKEN`, `SAUVEGARDE_HEURE`). Le jeton passe par un en-tête HTTP, jamais par `.git/config`. Si le conteneur ne résout pas le nom du dépôt (DNS du réseau local) : `GIT_REMOTE_HOST` et `GIT_REMOTE_HOST_IP`.

### Projets

Un **projet** regroupe les documents d'un même sujet : son identifiant sert de préfixe
(`maison/serveur-nas`) et de périmètre au fichier de projet pour Claude. Il est décrit
par `contexte/<projet>/.projet.yml` (titre, description) et existe même vide.
Interface web : créer, modifier, renommer (déplace tous ses documents), supprimer (s'il est
vide), réordonner par glisser-déposer dans la barre latérale ; sur un document, **Déplacer**
le range dans un autre projet ou sous-projet. L'historique suit le document lors des
déplacements.

### Sous-projets

Quand un projet grossit (un document par machine, avec son journal…), des **sous-projets**
le découpent, sur un seul niveau : `maison/nas`, décrit par `contexte/maison/nas/.projet.yml`.

- Leurs documents ont pour chemin `maison/nas/<nom>` ; chacun peut avoir sa fiche
  `maison/nas/resume`.
- Dans l'index d'un projet, un sous-projet n'occupe qu'**une ligne** (titre, description,
  nombre de documents) : `vue_ensemble(projet="maison")` reste court, et
  `vue_ensemble(projet="maison/nas")` ne charge que la fiche et les documents du NAS.
- `rechercher(projet="maison")` inclut les sous-projets ; `projet="maison/nas"` se limite au NAS.
- Un document de sous-projet exige un sous-projet déclaré (pas de création implicite), et un
  même nom ne peut pas désigner à la fois un document et un sous-projet.
- Interface web : onglet **Sous-projets** (ou **Paramètres**) de la page d'un projet. La barre latérale
  affiche les sous-projets sous leur projet ; la page d'un projet les présente en cartes
  (documents, coût de démarrage, fiche résumé manquante). Renommer, supprimer (s'il est
  vide), importer et exporter fonctionnent comme pour un projet.
- Noms réservés (segments d'adresse de l'interface) : `modifier`, `supprimer`, `importer`, `export`.

Claude peut aussi créer un projet ou un sous-projet avec l'outil `creer_projet` (voir
*Outils MCP*) : il le propose d'abord, puis attend votre accord.

### Directives globales

Le document `directives` (à la racine, sans projet) rassemble les consignes valables dans
**toutes** les conversations : langue de réponse, règles Git (« jamais de push sans accord »,
« commits sous mon nom »), réflexes à avoir… Le serveur l'ajoute à ses instructions MCP à
chaque connexion : tous les clients (Claude Code, Claude Desktop, claude.ai, VS Code) le
reçoivent sans appel d'outil, et une modification s'applique à la session suivante.

- Accès : **Claude › Directives** (l'accueil propose de le créer s'il manque).
- Restez bref : il est chargé dans chaque conversation. Au-delà d'environ 2 000 tokens, seul
  un renvoi vers le document est transmis.
- Ce sont des consignes, pas des verrous : pour **imposer** une règle dans Claude Code, voir
  *Consignes recommandées pour Claude* (hooks et réglages).

### Procédures

Le projet `procedures` contient des procédures réutilisables (« Diagnostic DNS maison »,
« Publier une release »…). Le **titre** de chaque document est listé dans les instructions du
serveur ; Claude lit la procédure (`lire_document`) avant d'effectuer la tâche qu'elle couvre.
Créez le projet `procedures` comme n'importe quel projet, et donnez des titres explicites :
c'est sur eux que Claude décide d'ouvrir une procédure.

### Hooks (règles appliquées par Claude Code)

L'onglet **Claude › Hooks** administre des règles évaluées **avant chaque action** de Claude Code :
refuser, demander confirmation, autoriser sans demander, ou ajouter une consigne pour Claude.
Exemples : confirmer tout `git push`, refuser un commit signé Claude, rappeler une consigne
avant toute écriture dans un `.env`.

- **Mode simple** (par défaut), sans aucune expression régulière :
  - des **règles prêtes à l'emploi**, activées d'un interrupteur : jamais de commit ni de PR
    signé Claude, messages de commit toujours dans ma langue (un message en anglais est refusé),
    me demander avant chaque `git push`, bloquer les push forcés, me demander
    avant de supprimer des fichiers, de modifier un `.env` ou d'installer des paquets ;
  - un **constructeur en phrase** : « Quand Claude veut [pousser du code / lancer une commande
    contenant… / modifier un fichier dont le chemin contient… / utiliser un outil MCP…]
    alors [le bloquer / me demander / l'autoriser / lui rappeler une consigne] ». Le texte
    saisi est pris tel quel ;
  - vos règles, affichées en phrases.
- **Mode avancé** : chaque règle vise un **outil** (expression régulière : `Bash`,
  `Write|Edit`, `mcp__.*`), un **champ** (commande, chemin du fichier, texte écrit, ou toute
  l'entrée) et un **motif** (expression régulière, insensible à la casse). **Tester une
  action** montre ce que le hook répondrait, sans rien exécuter. Une règle simple modifiée
  ici devient une règle avancée.
- Si plusieurs règles correspondent, la décision la plus stricte l'emporte (refuser >
  demander > autoriser).
- Un hook ne voit que la commande, pas le contenu des commits déjà faits. « Ne jamais pousser
  sur GitHub au nom de Claude » se traduit donc par « Jamais de commit ni de PR signé
  Claude » : ce qui est signé Claude ne peut plus être créé, donc plus être poussé.
- Les règles sont stockées dans `contexte/.regles.yml` et versionnées avec le reste
  (historique, sauvegarde).
- **Installation, une fois par poste** (**Connexion › Claude Code** : le plus simple est d'y copier la
  phrase à donner à Claude ; sinon, encadré *À la main : activer les hooks*) : un seul
  hook `PreToolUse` de type `mcp_tool`, qui appelle l'outil `controler_action` du serveur avant
  chaque action. Ensuite, toute modification dans l'interface s'applique immédiatement, sans
  toucher aux postes.
  1. **Nom du serveur** : `claude mcp list` ; le nom est ce qui précède les deux-points
     (`mcp-memory: https://…/mcp (HTTP) - ✔ Connected`). Le saisir dans la page : le bloc se
     met à jour (champ `server`).
  2. **Portée** : `claude mcp get <nom>` doit afficher `Scope: User config`. Sinon, le
     réenregistrer avec `--scope user` (voir *Claude Code (accès global, une seule commande)*) :
     dans les autres projets, le hook ne trouverait pas le serveur et laisserait tout passer.
  3. **Copier le bloc** et le fusionner dans `~/.claude/settings.json`, à côté des hooks
     existants.
  4. **Vérifier** : `/hooks` dans Claude Code liste le hook ; avec la règle « Me demander avant
     chaque git push » active, demander à Claude d'exécuter `echo git push` (sans effet) : une
     demande de confirmation doit apparaître.

  Le bloc est fixe (`"matcher": "*"`) : le serveur est consulté avant chaque action (un
  aller-retour réseau, quelques millisecondes sur un réseau local), et toute règle ajoutée
  s'applique sans rien recopier.
- `onFailure: "continue"` (par défaut) : si le serveur est injoignable, l'action suit le
  circuit normal des permissions. Pour les règles critiques, gardez aussi un réglage ou un
  hook local (voir *Imposer une règle dans Claude Code*).
- Les hooks n'existent que dans Claude Code ; pour claude.ai et Claude Desktop, utilisez les
  *Directives globales*.

### Outils MCP
`vue_ensemble` (à appeler en premier), `lister_projets`, `lister_documents`, `plan_document`,
`lire_document`, `rechercher`, `ecrire_document`, `ajouter_au_document`, `remplacer_texte`,
`deplacer_document`, `enregistrer_decision`, `creer_projet`, `historique`, et
`controler_action`, réservé au hook de **Claude › Hooks**.
`creer_projet` crée un projet, ou un sous-projet avec `parent`. Les consignes demandent à
Claude de le proposer (identifiant, titre, documents à y déplacer) et de n'appeler l'outil
qu'après votre accord. Dans Claude Code, ne l'ajoutez pas aux outils autorisés d'office : la
demande de permission sert alors de confirmation. Renommage et suppression de projet,
restauration d'une version : interface web.

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
- un sous-projet n'occupe qu'une ligne dans l'index de son projet : son contenu n'est chargé
  qu'avec `vue_ensemble(projet="projet/sous-projet")` ;
- les consignes demandent de corriger l'information à sa place plutôt que d'empiler des
  sections datées (l'historique Git conserve l'ancien état) ;
- un document portant le tag `archive` (journal d'une mise en place, état ancien) est signalé
  dans l'index et exclu de `rechercher`, sauf avec `archives=true` ;
- descriptions d'outils courtes, sans sortie structurée en double (≈ 1 200 tokens pour l'ensemble).

Ressources : `contexte://index`, `contexte://doc/{nom}`, `contexte://doc/{categorie}/{nom}`,
`contexte://doc/{categorie}/{sous_projet}/{nom}`.
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
sont pas touchées), ou **Administration › Mise à jour** dans l'interface (voir plus bas).

## Accès à l'interface web

À la première ouverture, l'interface demande de **créer le compte** (identifiant + mot de passe,
10 caractères minimum) : l'ouvrir juste après le premier déploiement. Ensuite, page de connexion
(« Rester connecté » = 30 jours, sinon 12 h ; 5 échecs = 5 minutes de blocage). Le mot de passe
est stocké haché (PBKDF2) dans `/data/.web/compte.json`, hors du dépôt Git du contexte.
**Administration › Compte** (ou l'avatar en bas de la barre latérale) : changer le mot de passe (déconnecte les autres
navigateurs) ou se déconnecter.

Mot de passe oublié :
```bash
cd /opt/mcp-memory && docker compose exec web python -m app.compte reinitialiser
# puis ouvrir l'interface : elle redemande la création du compte (documents intacts)
```

## Administration › Mémoire (indicateurs)

- **Par projet** : coût de démarrage d'une conversation (fiche résumé + index ; un sous-projet y compte pour une ligne), volume des documents actifs et des archives (sous-projets compris), fiche résumé, dernière modification.
- **Santé** : fiche résumé absente ou trop longue (projet ou sous-projet), document long sans titres, section de plus de ~3k tokens, dérive en journal (« 🆕 », titres datés, tâches cochées), renvois vers des documents absents. Dans les documents, un chemin `projet/document` ou `projet/sous-projet/document` cité devient un lien (barré s'il n'existe pas) ; un sous-projet cité (`projet/sous-projet`) mène à sa page.
- **Usage par Claude** : chaque appel d'outil MCP est journalisé dans `<DATA_DIR>/.usage/AAAA-MM.jsonl` (outil, chemin, section, requête, taille de la réponse, durée ; jamais de contenu ; 12 mois). La page montre les tokens servis par jour et par projet, les documents les plus lus, ceux lus en entier, les recherches sans résultat et les écritures par auteur.
- **Sauvegarde** et **export** `.zip` (tout, ou un projet ou sous-projet depuis sa page).

**Garde-fou contre les secrets** : toute écriture (MCP, interface, import) contenant une clé privée, un jeton (JWT, GitHub, AWS, `sk-…`, Bearer), un mot de passe dans une adresse de connexion ou une affectation `…PASSWORD=`, `…TOKEN=`, `…SECRET=` avec une vraie valeur est refusée. Les valeurs modèles (`<secret>`, `***`, `${…}`, chemins) passent ; un secret déjà présent ne bloque pas les modifications suivantes. Dans l'interface, une case permet d'enregistrer quand même (faux positif).

**Recherche** insensible aux accents, à la casse et aux apostrophes typographiques.

## Mise à jour de l'application (bouton)

**Administration › Mise à jour** : l'onglet Version affiche la version en service, les nouveaux commits du dépôt de
référence (un point signale qu'il y en a) et met à jour en un clic (récupération du dépôt
de référence, puis `docker compose up -d --build`, soit une à deux minutes d'indisponibilité ; les documents ne
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

**Dépôt de référence.** Par défaut, les mises à jour viennent de
`https://github.com/Ynats-Dev/mcp-memory.git` (branche `master`). L'onglet **Dépôt de
référence** de la page Mise à jour permet d'indiquer un autre dépôt (un fork, un miroir
interne) et sa branche, et **Revenir au dépôt par défaut** le rétablit en un clic.

- Le réglage est écrit dans `.maj/depot` (`url=…`, `branche=…`), relu et validé par le
  service hôte. L'hôte suit ce dépôt avec un remote dédié, `reference` : le remote `origin`
  du dossier n'est jamais modifié.
- Adresses acceptées : `https://…`, `ssh://…` ou `git@hôte:chemin`, sans identifiants. Pour un
  dépôt privé, configurer l'accès sur l'hôte (clé SSH ou gestionnaire d'identifiants Git de
  l'utilisateur du service).
- Enregistrer ne fait que lancer une vérification ; rien n'est installé avant « Mettre à jour ».
  Le code du dépôt choisi est construit et exécuté sur le serveur : n'indiquez qu'un dépôt
  de confiance.
- Si l'historique du dépôt choisi n'a pas de lien avec la version installée (dossier cloné
  depuis un autre dépôt), la mise à jour aligne le dossier sur la référence
  (`git reset --hard`) au lieu d'une simple avance. Les fichiers non suivis (`.env`,
  `.maj/`, `sauvegardes/`) sont conservés, et le commit précédent est noté dans le journal
  pour revenir en arrière si besoin (`git reset --hard <commit>`).

## Connexion des clients

L'espace **Connexion** de l'interface web permet (onglet **Jeton**) d'enregistrer le jeton
(`MCP_TOKEN`) **dans le navigateur** (localStorage, jamais côté serveur), de le
vérifier auprès du serveur MCP, et de copier les configurations Claude Desktop /
Claude Code déjà remplies.

### Claude Code : le plus simple

Ouvrir Claude Code sur le poste et lui demander :

```text
Installe MCP Memory sur ce poste en suivant https://mcp.example.com/claude-code/installation
```

Le serveur publie à cette adresse (sans authentification : elle ne contient aucun secret) une
procédure écrite pour Claude. Claude enregistre le serveur en vous demandant le jeton, installe
le hook global des règles (**Claude › Hooks**), ajoute deux filets locaux (`verif-mcp.sh` prévient Claude
quand le serveur est injoignable ; `garde-git.sh` applique les règles Git même serveur éteint
et affiche le détail de chaque push — dépôt, branches, commits envoyés — dans la demande de
confirmation),
règle l'attribution et la confirmation des `git push`, puis vérifie le tout. L'onglet **Connexion ›
Claude Code** affiche cette phrase avec la bonne adresse si `MCP_PUBLIC_URL` est renseigné.

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
2. Interface web → **Connexion › Projet Claude** (ou onglet **Importer / Exporter** d'un projet ; pas
   sur un sous-projet) : nom du projet + catégorie → *Télécharger* le fichier
   `mcp-memory-<categorie>.md`. Il indique à Claude le connecteur à utiliser, les documents
   du projet, et lui demande de tenir le contexte à jour sans qu'on le lui demande (case à
   décocher pour qu'il propose les modifications au lieu de les appliquer).
3. Dans le projet claude.ai : **Contexte → Ajouter** ce fichier, puis retirer les anciens.
4. Dans une conversation du projet (Claude Desktop), vérifier que les outils `mcp-memory`
   sont proposés (icône des outils / menu « + »).

### Import de fichiers existants

**Depuis l'interface** : glisser plusieurs fichiers `.md` sur la page d'un projet ou d'un
sous-projet (ou cliquer sur la zone de dépôt). Mêmes règles de nommage que le script ; les documents existants sont
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
bonne place), suivies de vos *Directives globales*. Les règles générales (langue, Git…) ont
leur place dans le document `directives` : elles valent alors pour tous les clients. Ces
consignes ne garantissent pas pour autant que Claude **pense** à consulter le serveur : face à
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

### Imposer une règle dans Claude Code (réglages et hooks)

Une directive reste une consigne : Claude peut l'oublier. Dans Claude Code, les règles
critiques s'imposent par les réglages (`~/.claude/settings.json`, à fusionner avec l'existant) :

```json
{
  "language": "french",
  "attribution": { "commit": "", "pr": "", "sessionUrl": false },
  "permissions": { "ask": ["Bash(git push *)", "Bash(git push)"] }
}
```

- `language` : langue des réponses ;
- `attribution` vide : Claude Code n'ajoute plus « Co-Authored-By: Claude » ni « Generated
  with Claude Code » aux commits et aux PR ;
- `permissions.ask` : tout `git push` demande confirmation, même s'il figure ailleurs dans
  la liste `allow` (retirez-l'en au passage).

Pour aller plus loin, l'onglet **Claude › Hooks** de l'interface administre des règles évaluées par le
serveur avant chaque action (voir *Hooks*). Un hook local (`PreToolUse` sur `Bash`, script sur
le poste) reste utile en filet pour les règles critiques : il s'applique même quand le serveur
est injoignable. Ces mécanismes ne concernent que Claude Code : pour les autres clients,
seules les directives s'appliquent.

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
