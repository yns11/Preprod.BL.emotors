# BLDEMAT — Guide de déploiement et de fonctionnement

Solution de dématérialisation des bordereaux de livraison (BL) : deux
applications Databricks (Streamlit), une base Lakebase (PostgreSQL managé),
deux jobs, et des notifications Microsoft Teams envoyées en temps réel.

**Principe de configuration** : *tout* est dans les deux fichiers `app.yaml`.
Pas de bundle, pas de secret scope, pas de valeur en dur dans le code.

---

# 1. Vue d'ensemble

```
   Smartphone/PC                                  Teams (canal « Récep BL »)
        │                                                    ▲
        ▼                                                    │ carte
┌──────────────────────┐   pages + métadonnées   ┌────────────┴─────────┐
│ App CRÉATION DE BL   │────────────────────────▶│                      │
│ (opérateurs au quai) │                         │      LAKEBASE        │
└──────────────────────┘                         │  (PostgreSQL managé) │
        ▲ IA vision                              │   schéma bl_demat    │
        │                                        │                      │
┌───────┴──────────────┐                         │                      │
│ Endpoint model       │   ┌─────────────────────┤                      │
│ serving (Claude)     │   │  App ADMINISTRATION │                      │
└──────────────────────┘   │  (appros, ADV,      │                      │
                           │   finance, admins)  └──────────┬───────────┘
                           └──────────┬─────────────────────┘
                                      │                     ▲
                                      ▼ carte Teams         │ jobs quotidiens
                              Teams (EDI NOK → OK)   ┌──────┴──────────────┐
                                                     │ sync_referentiels   │
                                                     │ maintenance         │
                                                     └─────────────────────┘
```

| Composant | Rôle |
|---|---|
| **App Création** | Deux parcours : *saisie unitaire* (scan → champs pré-remplis par IA → validation) et *archivage en lot* (PDF multipage, une page = un BL). Notifie Teams à chaque **nouvelle réception**. |
| **App Administration** | Pilotage (tableau de bord, KPI), **rapports d'activité PDF**, vues BL/DESADV/Rapprochement, référentiels, rôles. Notifie Teams au **passage EDI NOK → OK**. |
| **Lakebase** | Métadonnées, images (BYTEA) **et** PDF des rapports, dans le schéma `bl_demat`. |
| **Model serving** | Modèle vision qui lit les BL scannés **et** rédige l'analyse des rapports (optionnel). |
| **Jobs** | Synchronisation ERP + maintenance + **rapports d'activité**, quotidiens. |

---

# 2. Déploiement pas à pas

## Étape 1 — Créer le projet Lakebase

Databricks ▸ **Compute / Database (Lakebase)** ▸ *Create project*, par exemple
`demat-bl`. Noter le **PGHOST** (onglet Connection details) : il servira aux jobs.

## Étape 2 — Créer le schéma et les tables

Ouvrir l'**éditeur SQL du projet Lakebase** (⚠️ pas l'éditeur SQL Spark) sur la
branche `production`, puis exécuter :

| Cas | Fichier(s) à exécuter, dans l'ordre |
|---|---|
| Installation neuve | `V001__baseline_professionnelle.sql` puis `V004__rapports_activite.sql` |
| Installation existante (déjà en V001) | `V002__notifications_directes.sql`, `V003__mention_teams_id.sql`, `V004__rapports_activite.sql` |

> `V004` ajoute la table `rapports_activite` et **supprime** la colonne
> `gestionnaires.teams_id` : l'AAD Object ID n'est plus utilisé, l'action
> Power Automate exigeant l'e-mail (UPN).

Les scripts sont **idempotents** (ré-exécutables sans risque) et utilisent
directement le schéma `bl_demat` : rien à remplacer. Pour un autre nom de
schéma, faire un rechercher/remplacer de `bl_demat` et aligner `BL_PG_SCHEMA`
dans les deux `app.yaml`.

## Étape 3 — Créer les deux applications

Compute ▸ **Apps** ▸ *Create app* (app personnalisée), deux fois :
`bl-creation` et `bl-administration`.

Sur **chaque** app, onglet **Edit ▸ Resources ▸ + Add resource** :

| Ressource | Paramètres | Sur quelle app |
|---|---|---|
| **Database** | projet Lakebase, branche `production`, base `databricks_postgres`, permission **Can connect and create**, clé **`postgres`** | les deux |
| **Serving endpoint** | le modèle vision (ex. `databricks-claude-opus-4-8`), permission **Can query** | Création uniquement |

> Les variables `PGHOST`, `PGDATABASE`, `PGUSER`… sont alors injectées
> automatiquement : **ne pas** les écrire dans `app.yaml`.

## Étape 4 — Déployer le code

Déployer le dossier `src/app_creation` sur l'app Création et
`src/app_administration` sur l'app Administration (chaque dossier est
autonome : il embarque sa copie de `bl_core`).

> Après toute modification de `shared/bl_core`, resynchroniser les copies :
> `cp shared/bl_core/*.py src/app_creation/bl_core/` (idem administration).

## Étape 5 — Accorder les droits SQL

Récupérer le **client ID du service principal** de chaque app (page de l'app ▸
onglet *Authorization*), puis dans l'éditeur SQL Lakebase :

```sql
-- App Création
GRANT USAGE ON SCHEMA bl_demat TO "<SP_APP_CREATION>";
GRANT SELECT, INSERT ON bl_demat.suivi_bl, bl_demat.pieces_jointes_bl,
  bl_demat.audit_bl, bl_demat.qualite_extraction, bl_demat.notifications
  TO "<SP_APP_CREATION>";
GRANT UPDATE ON bl_demat.suivi_bl, bl_demat.notifications TO "<SP_APP_CREATION>";
GRANT SELECT ON bl_demat.base_tiers, bl_demat.base_desadv, bl_demat.quais,
  bl_demat.pla, bl_demat.adresses, bl_demat.sites_logistiques,
  bl_demat.portefeuilles, bl_demat.gestionnaires, bl_demat.roles_utilisateurs
  TO "<SP_APP_CREATION>";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA bl_demat TO "<SP_APP_CREATION>";

-- App Administration (CRUD complet)
GRANT USAGE ON SCHEMA bl_demat TO "<SP_APP_ADMINISTRATION>";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA bl_demat
  TO "<SP_APP_ADMINISTRATION>";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA bl_demat
  TO "<SP_APP_ADMINISTRATION>";
```

### Droits de l'identité qui exécute les jobs

Les deux jobs (`sync_referentiels_erp`, `maintenance`) se connectent à Lakebase
avec l'**identité qui exécute le job** — son *Run as* (Job ▸ ⋯ ▸ *Edit
permissions* / *Run as*). C'est **pour cette identité** que le jeton OAuth est
frappé ; c'est donc **elle**, et non un service principal saisi à la main, qui
doit posséder un rôle Postgres et des droits.

1. **Autoriser la connexion** (côté Databricks) : Compute ▸ **Database
   instances** ▸ votre instance ▸ *Permissions* ▸ ajouter l'identité *Run as*
   avec **Can connect**. Sans cela, aucun rôle Postgres n'est provisionné et la
   connexion échoue avec *`OAuth: User is not authorized`*.
2. **Créer/nommer le rôle** : le rôle Postgres porte le nom de l'identité —
   l'**e-mail** pour un utilisateur, l'**ID d'application** pour un service
   principal. Le retrouver ainsi :

   ```sql
   SELECT rolname FROM pg_roles ORDER BY rolname;   -- après la 1re connexion
   ```

3. **Accorder les droits** au rôle `<RUN_AS>` (remplacer par le nom exact) :

   ```sql
   GRANT USAGE ON SCHEMA bl_demat TO "<RUN_AS>";
   GRANT SELECT, INSERT, UPDATE ON bl_demat.suivi_bl, bl_demat.base_tiers,
     bl_demat.base_desadv, bl_demat.job_executions, bl_demat.audit_bl
     TO "<RUN_AS>";
   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA bl_demat TO "<RUN_AS>";
   ```

> Le paramètre `pg_user` du job est désormais **facultatif** : laissé vide, il
> prend automatiquement l'identité *Run as* (le job appelle
> `current_user.me()`), ce qui garantit que le nom d'utilisateur et le jeton
> désignent la même personne. Ne le renseigner que pour forcer explicitement
> un autre rôle Postgres — et dans ce cas, ce rôle doit être une identité pour
> laquelle **ce même** *Run as* est autorisé à frapper un jeton.

## Étape 6 — Créer le flux Teams (notifications)

1. Dans Teams, ouvrir le **canal** cible (ex. « Récep BL ») ▸ **+** ▸
   application **Workflows** ▸ modèle **« Envoyez des alertes webhook à un
   canal »** (*Post to a channel when a webhook request is received*).
2. Valider ; Teams affiche une **URL de webhook** : la copier.
3. La coller dans les `app.yaml` :
   - `BL_TEAMS_WEBHOOK_RECEPTION` (app Création),
   - `BL_TEAMS_WEBHOOK_EDI` (app Administration).
   La **même URL** peut servir aux deux (même canal).
4. **Ajouter au canal tous les gestionnaires** susceptibles d'être mentionnés :
   une mention ne notifie que si la personne est membre.

### Rendre les mentions cliquables

**Point clé** : écrire soi-même `msteams.entities` dans une carte envoyée par
Power Automate **ne fonctionne pas** — le Flow bot rejette la carte avec
« One or more mention entity could not be found in card text », ou affiche le
nom sans le rendre cliquable. La seule méthode fiable est l'action Teams
**« Obtenir un jeton @mention pour un utilisateur »** : elle renvoie un jeton
que le Flow bot transforme lui-même en vraie mention.

Cette action **échoue si la personne n'est pas membre de l'équipe**, et son
échec fait échouer tout le flux — donc aussi la notification. Le flux
ci-dessous évite ce piège : il **liste les membres réels de l'équipe** et ne
demande un jeton que pour ceux qui figurent dans les e-mails envoyés par
l'application. Un gestionnaire absent du canal est simplement ignoré ; la
carte part quand même.

L'application est déjà prête (`BL_TEAMS_MENTION_MODE=flow`) : elle envoie, à
la racine de la charge utile, un tableau `mentions` contenant les e-mails des
gestionnaires **en minuscules**, et place le marqueur `{{MENTIONS}}` dans la
carte, à l'endroit exact où les mentions doivent apparaître :

```json
{
  "type": "message",
  "mentions": ["marie.durand@emotors.com", "paul.martin@emotors.com"],
  "attachments": [{ "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": { "...": "… Gestionnaire(s) : {{MENTIONS}} …" } }]
}
```

`mentions` est **toujours** présent — vide pour la MessageCard « EDI NOK →
OK » ou pour une réception sans gestionnaire.

#### Convention préalable : renommer les actions

Les expressions référencent les actions par leur nom, apostrophes doublées et
espaces remplacés par `_` : `body('Répertorier_les_membres_de_l''équipe')` est
illisible et source d'erreurs. **Renommez chaque action ajoutée** (⋯ ▸
*Renommer*) avec les noms ASCII utilisés ci-dessous. Toutes les expressions du
guide en dépendent.

#### Modification du flux (6 actions à ajouter)

Power Automate ▸ Mes flux ▸ *Envoyer des alertes webhook à …* ▸ **Modifier**.
Les actions 1 à 5 se placent **après le déclencheur et avant** l'action
« Publier une carte dans un chat ou un canal » (donc avant la boucle sur les
pièces jointes, si le modèle en comporte une).

**1. Initialiser une variable** — nom `JetonsMentions`

| Champ | Valeur |
|---|---|
| Nom | `JetonsMentions` |
| Type | **Chaîne** |
| Valeur | *(vide)* |

**2. Teams ▸ « Répertorier les membres de l'équipe »** — renommer `Membres`

| Champ | Valeur |
|---|---|
| Équipe | l'équipe qui contient le canal de notification |

> Sortie utile : `body('Membres')?['value']`, un tableau d'objets contenant
> `displayName`, `userPrincipalName`, `email`, `userId`. Faites un premier
> **Test** du flux et regardez la sortie brute de cette action pour confirmer
> les noms de champs de votre tenant — les expressions ci-dessous utilisent un
> `coalesce` qui accepte `userPrincipalName` **ou** `email`, ce qui couvre les
> deux cas.

**3. « Filtrer un tableau »** — renommer `Gestionnaires`

| Champ | Valeur |
|---|---|
| De | `body('Membres')?['value']` |

Condition : basculer en **mode avancé** (bouton *Modifier en mode avancé*) et
coller :

```
@contains(
  coalesce(triggerBody()?['mentions'], createArray()),
  toLower(coalesce(item()?['userPrincipalName'], item()?['email'], ''))
)
```

> Ne garde que les membres de l'équipe dont l'e-mail figure dans la charge
> utile. `toLower` est indispensable : `contains` est sensible à la casse et
> Teams renvoie souvent l'UPN avec des majuscules. C'est pour cette raison que
> l'application envoie les e-mails déjà en minuscules.
>
> Le `coalesce` sur `triggerBody()?['mentions']` évite l'erreur *« expression
> … is of type 'Null' »* sur les charges sans mentions.

**4. « Appliquer à chacun »** — renommer `PourChaqueGestionnaire`

| Champ | Valeur |
|---|---|
| Sélectionner une sortie | `body('Gestionnaires')` |

> La sortie d'un *Filtrer un tableau* **est** le tableau : pas de `?['value']`
> ici. Un tableau vide fait simplement zéro itération.

**5a. Dans la boucle — Teams ▸ « Obtenir un jeton @mention pour un
utilisateur »** — renommer `Jeton`

| Champ | Valeur |
|---|---|
| Utilisateur | `coalesce(item()?['userPrincipalName'], item()?['email'])` |

**5b. Toujours dans la boucle — « Ajouter à la variable chaîne »**

| Champ | Valeur |
|---|---|
| Nom | `JetonsMentions` |
| Valeur | `concat(outputs('Jeton')?['body/atMention'], ' ')` |

> L'espace final sépare les mentions successives.

**6. Dans « Publier une carte dans un chat ou un canal »**, remplacer le
contenu du champ *Corps du message* (carte adaptative) par :

```
json(replace(string(item()?['content']), '{{MENTIONS}}', trim(variables('JetonsMentions'))))
```

> `item()` désigne ici la pièce jointe courante, dans la boucle du modèle
> d'origine. Si votre flux passe l'objet complet, l'équivalent direct est :
> `json(replace(string(triggerOutputs()?['body/attachments'][0]?['content']), '{{MENTIONS}}', trim(variables('JetonsMentions'))))`
>
> Dans la branche « si les pièces jointes sont nulles » (MessageCard EDI) :
> `json(replace(string(variables('Body')), '{{MENTIONS}}', trim(variables('JetonsMentions'))))`
> — cette carte ne contient pas le marqueur, le `replace` est donc sans effet.

**7. Enregistrer**, puis créer une réception de test : le nom du gestionnaire
doit apparaître en mention cliquable et la personne reçoit une notification
Teams personnelle.

#### Ordre final du flux

```
Déclencheur : requête webhook Teams
├─ Initialiser JetonsMentions (chaîne, vide)
├─ Membres            → Répertorier les membres de l'équipe
├─ Gestionnaires      → Filtrer un tableau (membres ∩ mentions)
├─ PourChaqueGestionnaire (sur body('Gestionnaires'))
│   ├─ Jeton          → Obtenir un jeton @mention
│   └─ Ajouter à JetonsMentions : concat(jeton, ' ')
└─ Publier une carte  → {{MENTIONS}} remplacé par JetonsMentions
```

#### Vérifier / diagnostiquer

App Administration ▸ **Gestion ▸ Gestionnaires** ▸ **🧪 Tester les mentions
Teams** : la carte de test publie quatre lignes — la n° 1 utilise la méthode
« Flow bot » (celle ci-dessus), les n° 2 à 4 les identifiants écrits par
l'application. **Seule la ligne réellement cliquable compte.** En principe
c'est la n° 1 une fois le flux modifié.

Points d'attention :

- L'e-mail saisi dans Gestion ▸ Gestionnaires doit être celui du **compte
  Microsoft 365** (UPN), pas un alias.
- La personne doit être **membre de l'équipe** ; sinon elle est filtrée
  silencieusement (pas de mention, mais pas d'échec non plus).
- « Répertorier les membres de l'équipe » est **paginée** : sur une grande
  équipe, activer *Paramètres ▸ Pagination* de l'action et monter le seuil,
  sinon un gestionnaire au-delà de la première page ne serait jamais trouvé.
- Seuls les blocs **TextBlock** et **FactSet** affichent une mention dans une
  carte adaptative.
- Si le marqueur reste vide, la ligne affiche « Gestionnaire(s) : » sans nom.
  Pour un repli explicite, remplacer `trim(variables('JetonsMentions'))` par
  `if(empty(trim(variables('JetonsMentions'))), '—', trim(variables('JetonsMentions')))`.
- Si vous ne pouvez pas modifier le flux du tout : passer
  `BL_TEAMS_MENTION_MODE` à `texte` — les gestionnaires sont alors cités en
  clair, sans notification personnelle.

## Étape 7 — Renseigner les `app.yaml`

Un extrait des variables à ajuster (le reste a des valeurs par défaut saines) :

| Variable | App | Valeur |
|---|---|---|
| `BL_ENVIRONMENT` | les deux | `prod` |
| `BL_RBAC_MODE` | les deux | `strict` |
| `BL_BOOTSTRAP_ADMINS` | les deux | votre e-mail (secours) |
| `BL_PG_SCHEMA` | les deux | `bl_demat` |
| `BL_LLM_ENDPOINT` | Création | nom de l'endpoint, ou vide pour désactiver l'IA |
| `BL_TEAMS_WEBHOOK_RECEPTION` | Création | URL du flux Teams |
| `BL_TEAMS_WEBHOOK_EDI` | Administration | URL du flux Teams |

Puis **Deploy**.

## Étape 8 — Paramétrer les accès et le référentiel

1. Page de chaque app ▸ **Permissions** ▸ ajouter les utilisateurs/groupes
   (**Can use**). C'est le 1ᵉʳ niveau : qui peut *ouvrir* l'app.
2. Ouvrir l'app Administration (vous êtes admin via `BL_BOOTSTRAP_ADMINS`) ▸
   **Gestion ▸ Rôles** : attribuer les rôles (voir §4). **S'attribuer
   ADMIN_METIER en premier**, puis vider `BL_BOOTSTRAP_ADMINS` et redéployer.
3. **Gestion ▸ Gestionnaires** : renseigner pour chacun le **nom affiché** et
   l'**e-mail Microsoft 365** (indispensable aux mentions Teams).
4. **Gestion ▸ Portefeuilles** : associer chaque gestionnaire à ses
   fournisseurs — c'est ce lien qui détermine **qui est mentionné**.
5. **Gestion ▸ PLA** : quai par fournisseur (pré-remplissage automatique).

## Étape 9 — Créer les jobs (facultatif mais recommandé)

Lakeflow Jobs ▸ *Create job* ▸ tâche **Python script** sur **serverless** :

| Script | Planification | Rôle |
|---|---|---|
| `jobs/sync_referentiels_erp.py` | quotidien 05h30 | tiers et DESADV depuis l'ERP |
| `jobs/maintenance.py` | quotidien 04h00 | brouillons expirés, jobs orphelins |
| `jobs/rapports_activite.py` | quotidien 03h30 | rapports PDF échus (voir §5) |
| `jobs/simulation_donnees.py` | à la demande | jeu de test (voir §6) |

Détails, paramètres et dépendances : `jobs/README.md`.

---

# 3. Fonctionnement des notifications

## 3.1 Nouvelle réception → carte adaptative avec mentions

```
Opérateur valide un BL de type « Nouvelle réception »
        │
        ├─▶ 1. BL + pages enregistrés dans Lakebase       (transaction)
        ├─▶ 2. Ligne écrite dans « notifications »        (trace, fait foi)
        ├─▶ 3. Gestionnaires du portefeuille du fournisseur → e-mails
        └─▶ 4. POST de la carte adaptative au flux Teams  (best effort)
                 └─ succès → envoyee = true
                 └─ échec  → erreur_envoi renseignée, BL conservé
```

La carte affiche : numéro de BL, fournisseur, quai, date + plage horaire,
état, nombre de pages, auteur de la saisie, et **@mentionne** les
gestionnaires concernés.

**Règle importante** : une indisponibilité de Teams **n'annule jamais**
l'enregistrement du BL. L'opérateur voit un avertissement, et la trace reste
consultable dans **Gestion ▸ Notifications** (colonne « Erreur »).

Seules les **nouvelles réceptions** déclenchent cette carte (ni expéditions,
ni archivages).

## 3.2 Passage EDI NOK → OK → MessageCard + commentaire

Depuis l'app Administration, deux chemins :
- **fiche BL** (bouton ✏️ Modifier) en basculant l'état sur OK ;
- **action de masse** (bouton ✅ Passer à OK) sur une sélection.

Dans les deux cas, un champ **« Commentaire pour la notification Teams
(facultatif) »** apparaît dans la fenêtre de confirmation. Son contenu est
ajouté à la carte, sous la ligne « Par ». Le format de carte historique est
conservé.

## 3.3 Sans notification configurée

Si l'URL de webhook est vide, tout fonctionne normalement : les événements
sont journalisés en base, simplement pas publiés dans Teams.

---

# 4. Archivage en lot d'anciens BL

Pour reprendre un stock de BL papier déjà numérisés, l'app Création propose un
parcours dédié aux deux opérations d'**archivage** (réception pour les appros,
expédition pour l'ADV). Le choix se fait à l'étape 1 ; les étapes suivantes
s'adaptent automatiquement.

```
Étape 1  Type = « Archivage d'un ancien BL … »
Étape 2  Dépôt d'UN PDF (jusqu'à 200 pages) — chaque page = un BL
             │  rasterisation en JPEG, une image par page
             ▼
Étape 3  Vérification page par page
             ├── à gauche : l'image de la page
             ├── à droite : numéro, date, tiers — détectés par l'IA, corrigeables
             ├── « ✅ Valider et continuer » → le BL rejoint la liste d'attente
             └── « ⏭️ Ignorer cette page »   → page écartée (séparateur, verso…)
             ▲
             │  EN ARRIÈRE-PLAN : les paquets de pages suivants sont déjà
             │  envoyés au modèle pendant que l'opérateur vérifie
Étape 4  Récapitulatif du lot → enregistrement de tous les BL retenus
```

**Pourquoi c'est fluide.** Les pages ne sont pas analysées une par une à la
demande : elles partent au modèle **par paquets de 4** (réglable, 3 à 6), et le
préchargeur maintient **deux paquets d'avance**. Quand l'opérateur valide la
page 4, le paquet des pages 5 à 8 est déjà revenu — le clic n'attend rien. Un
seul appel au modèle couvre 4 pages, ce qui divise d'autant le coût.

| Réglage (`app.yaml` de l'app Création) | Défaut | Effet |
|---|---|---|
| `BL_LOT_MAX_PAGES` | `200` | pages acceptées dans un même PDF |
| `BL_LOT_TAILLE_IA` | `4` | pages par appel au modèle (3–6) |

**Garde-fous à l'étape 4.** Avant enregistrement, chaque BL retenu est contrôlé :
numéro manquant, numéro **en double dans le lot**, numéro **déjà présent en
base**, tiers non renseigné. Les lignes en anomalie sont affichées et **exclues**
de l'enregistrement — le reste du lot part quand même. Chaque BL est une
transaction indépendante : un échec isolé n'annule pas les autres, et l'écran
final liste précisément ce qui est passé et ce qui ne l'est pas.

Si l'IA est indisponible (endpoint non configuré ou en erreur), le parcours
reste utilisable : les champs arrivent vides et sont saisis à la main.

---

# 5. Rapports d'activité

## Ce que contient un rapport

Un PDF de 2 à 4 pages, structuré ainsi :

1. **Bandeau + 8 cartes d'indicateurs** — BL traités, réceptions, expéditions,
   archivages, taux d'EDI NOK, taux de rapprochement DESADV, délai moyen de
   saisie, anomalies ; chacun avec son évolution vs la période précédente.
2. **Analyse et commentaires** — rédigée par le modèle : synthèse, points
   saillants, conclusion et recommandation.
3. **Volumes et comparaison** — tableau période courante / précédente / écart.
4. **Activité sur la période** — histogramme du nombre de BL par jour.
5. **Principaux tiers** (top 10) et **tiers à surveiller** (taux de NOK).
6. **Répartitions** par plage horaire, par quai, par opérateur.
7. **Notifications Teams** et **précision de l'extraction IA**.

L'analyse ne reçoit que des **agrégats** — jamais un numéro de BL ni une donnée
nominative. Si l'endpoint est absent ou en échec, le rapport est produit
complet, avec une mention explicite à la place de l'analyse : un modèle
indisponible ne fait jamais perdre un rapport.

## Les cinq périodicités

| Périodicité | Période couverte | Produite le |
|---|---|---|
| Journalière | la veille | tous les jours |
| Hebdomadaire | semaine ISO (lundi → dimanche) | le lundi |
| Mensuelle | mois civil | le 1er du mois |
| Trimestrielle | trimestre civil | 1er janvier / avril / juillet / octobre |
| Annuelle | année civile | le 1er janvier |

La règle est la **clôture**, pas le calendrier : le job ne produit une période
que lorsqu'elle est terminée. Un 1er janvier tombant un vendredi ne déclenche
donc pas le rapport hebdomadaire — la semaine court encore.

## Consulter les rapports

App Administration ▸ **Général ▸ Rapports d'activité**, sous le tableau de
bord. La vue est filtrable par périodicité et par période couverte. Sur
sélection d'une ligne :

- **📖 Ouvrir** — indicateurs clés et analyse à l'écran, PDF téléchargeable ;
- **⬇️ Préparer le PDF** — téléchargement direct (le PDF n'est chargé depuis la
  base qu'à ce moment : la grille reste rapide même avec des centaines de
  rapports) ;
- **🔄 Regénérer** — recalcule le rapport sur les données actuelles (utile après
  correction d'un BL). Réservé au niveau MODIFICATION.

Le bouton **📝 Générer un rapport** permet de produire n'importe quelle période
à la demande — rattrapage d'une nuit manquée, ou rapport d'un mois passé.

## Génération automatique

Job `jobs/rapports_activite.py`, une exécution quotidienne (03h30 conseillé,
avant la synchronisation ERP de 05h30 pour rapporter sur des données stables).
Il produit **toutes les périodes échues** en une passe. Paramètres dans
`jobs/README.md` ; `date_reference` permet de rejouer une date passée.

Un rapport déjà présent pour une période est **remplacé** : le job est
réexécutable sans risque de doublon.

---

# 6. Jeu de données de simulation

Pour une démonstration ou une recette, `jobs/simulation_donnees.py` génère un
volume réaliste (5 000 BL par défaut) **et toutes les lignes liées** :
fournisseurs, clients, adresses, sites logistiques, gestionnaires,
portefeuilles, PLA, DESADV, pages scannées, audit, notifications et mesures de
qualité IA.

**Identification.** Aucune colonne technique n'a été ajoutée au modèle : toutes
les lignes générées portent le préfixe **`SIM-`** sur leur clé naturelle
(`id_bl`, `numero_bl`, `base_tiers.name`, `code_gestionnaire`, `adresse`…). La
suppression est donc un simple `LIKE`.

**Reproductibilité.** Tout est tiré d'un générateur pseudo-aléatoire ensemencé :
à graine, bornes de dates et volume identiques, le jeu produit est exactement le
même.

**Sécurité.** Le job refuse de générer si des lignes préfixées existent déjà
(sauf `remplacer=true`) : impossible d'empiler deux jeux. Les lignes portent
`source_donnee = 'MANUEL'`, ce qui les met hors de portée du job de
synchronisation ERP, qui ne touche que les lignes `'ERP'`.

| Paramètre | Défaut | Rôle |
|---|---|---|
| `action` | `generer` | `generer` ou `supprimer` |
| `nb_bl` | `5000` | nombre de BL |
| `date_min` / `date_max` | 18 mois → hier | bornes des dates d'opération |
| `graine` | `20260727` | reproductibilité |
| `prefixe` | `SIM-` | marqueur (majuscules, terminé par `-`) |
| `pages_par_bl` | `1` | `0` = sans images, génération plus rapide |
| `remplacer` | `false` | `true` = efface le jeu existant d'abord |

**Suppression.** Deux voies équivalentes :

- relancer le job avec `action=supprimer` ;
- exécuter `sql/simulation/supprimer_donnees_simulation.sql` dans l'éditeur SQL
  Lakebase (aucune dépendance Databricks ; l'ordre des `DELETE` respecte les
  clés étrangères, et une requête de contrôle finale doit renvoyer 0 partout).

> Après suppression, les rapports d'activité déjà générés continuent d'agréger
> ces BL : les regénérer, ou vider `rapports_activite`.

---

# 7. Rôles et droits (RBAC)

| Rôle | App Création | App Administration |
|---|---|---|
| **LOG** | Nouvelle réception, nouvelle expédition | — |
| **APPROS** | Archivage réception (unitaire **et en lot**) | BL réception (modification) ; DESADV achat, Rapprochement achat, Notifications, Rapports d'activité (lecture) |
| **ADV** | Archivage expédition (unitaire **et en lot**) | BL expédition (modification) ; DESADV vente, Rapprochement vente, Notifications, Rapports d'activité (lecture) |
| **FINANCE** | — | BL, rapprochements et rapports d'activité (lecture) |
| **ADMIN_METIER** | Toutes | Toutes, y compris le module Gestion |

- Les vues sans droit sont **masquées** ; en lecture seule, les actions
  d'écriture disparaissent.
- La matrice est dans `shared/bl_core/rbac.py` (versionnée avec le code) ; les
  **affectations** sont en base (Gestion ▸ Rôles).
- `BL_RBAC_MODE=strict` : aucun rôle = aucun accès. Le mode `disabled` est
  refusé en `prod`.

---

# 8. Exploitation courante

| Situation | Où regarder / que faire |
|---|---|
| Une notification n'est pas arrivée | Gestion ▸ **Notifications** : colonne « Envoyée » et « Erreur ». Une erreur réseau ou HTTP y est explicite. |
| Une mention n'est pas cliquable | Voir « Rendre les mentions cliquables » : le flux doit produire les jetons (action « Obtenir un jeton @mention »), l'e-mail doit être renseigné dans Gestion ▸ Gestionnaires, et la personne être membre du canal. |
| Le flux échoue : *'foreach' expression … is of type 'Null'* | Une expression boucle directement sur `triggerBody()?['mentions']`. L'envelopper dans `coalesce(…, createArray())` : certaines cartes (EDI NOK → OK, réception sans gestionnaire) n'ont pas de mentions. |
| Le flux échoue sur *Obtenir un jeton @mention* | La personne n'est pas membre de l'équipe. Le filtre `Gestionnaires` doit s'intercaler avant la boucle (voir « Rendre les mentions cliquables ») pour l'écarter au lieu de faire échouer le flux. |
| Un gestionnaire n'est jamais mentionné | Son e-mail dans Gestion ▸ Gestionnaires n'est pas son UPN Microsoft 365, ou il dépasse la première page de « Répertorier les membres de l'équipe » (activer la pagination de l'action). |
| Personne n'est mentionné | Le fournisseur n'a pas de gestionnaire dans Gestion ▸ **Portefeuilles**. |
| L'IA ne pré-remplit plus | `BL_LLM_ENDPOINT` vide, ou ressource *Serving endpoint* absente / sans « Can query ». Le détail de l'erreur est affiché à l'étape 3. |
| « Ressource Lakebase absente » | La ressource Database n'est pas attachée à l'app (clé `postgres`). |
| Erreur de droits SQL | Rejouer les GRANT de l'étape 5 avec les bons client ID. |
| Job : `OAuth: User is not authorized` | Le `user` de la connexion n'est pas l'identité qui frappe le jeton. Laisser `pg_user` **vide** (il prend le *Run as* du job), autoriser ce *Run as* en **Can connect** sur l'instance Lakebase, et lui accorder les GRANT « identité qui exécute les jobs » (étape 5). |
| Un utilisateur ne voit rien | Aucun rôle attribué (Gestion ▸ Rôles). |

**Sauvegarde** : tout est dans Lakebase (métadonnées + images). S'appuyer sur
les sauvegardes/branches du projet Lakebase.

---

# 9. Structure du dépôt

```
shared/bl_core/          code partagé (source de vérité)
  config.py              configuration validée (lue depuis app.yaml)
  database.py            pool PostgreSQL + transactions
  repository.py          accès aux données métier
  teams.py               cartes Teams (adaptative + MessageCard)
  notifications.py       trace en base puis envoi (best effort)
  rbac.py                matrice des droits
  llm.py                 appel mutualisé à l'endpoint de model serving
  extraction.py          extraction IA des BL (unitaire ET « une page = un BL »)
  pdf_lot.py             archivage en lot : PDF -> images + préchargeur IA
  rapports.py            rapports d'activité : agrégats, analyse IA, rendu PDF
  cache.py               mémoïsation (Streamlit dans les apps, lru_cache en job)
  images.py, pdf_bl.py, ui.py, validation.py, identity.py
src/app_creation/        app + copie de bl_core + app.yaml + requirements.txt
src/app_administration/  idem
sql/migrations/          V001 (neuve), V002 à V004 (mises à niveau)
sql/simulation/          suppression du jeu de données de simulation
jobs/                    sync ERP, maintenance, rapports, simulation + README
```
