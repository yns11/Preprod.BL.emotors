# Jobs Lakeflow — BLDEMAT

Quatre tâches, à créer dans l'interface Databricks (Lakeflow Jobs ▸ Create job,
tâche « Python script » sur compute **serverless**). Il n'y a **plus de bundle
ni de job d'envoi de notifications** : les cartes Teams sont publiées
directement par les applications au moment de l'événement.

| Script | Rôle | Planification conseillée |
|---|---|---|
| `sync_referentiels_erp.py` | Historise le staging Delta, synchronise tiers et DESADV achat par lots (avec `statut_edi` issu de `messagestate`), gère renommages/inactivations et recalcule le rapprochement BL ⇄ DESADV dans les deux sens. | quotidienne, 05h30 |
| `maintenance.py` | Marque en erreur les brouillons interrompus depuis plus de 24 h et clôt les exécutions de job restées `STARTED`. | quotidienne, 04h00 |
| `rapports_activite.py` | Produit les rapports d'activité PDF **échus** (journalier de la veille, plus hebdo/mensuel/trimestriel/annuel dès qu'ils se clôturent), analyse rédigée par le modèle comprise. | quotidienne, 03h30 |
| `simulation_donnees.py` | Génère ou supprime un jeu de données de simulation identifié par préfixe. **Ne pas planifier** : à lancer à la demande. | à la demande |

`common.py` est partagé par tous les scripts (connexion Lakebase, lecture des
job parameters, résolution de l'endpoint, journal d'exécution, métriques). Il
doit être déposé **dans le même dossier** que les scripts dans l'espace de
travail.

## Paramètres (job parameters)

Les scripts lisent les **job parameters** de la tâche (onglet *Parameters*,
Key/Value), via `dbutils.widgets` — plus d'`argparse`. Renseigner ces clés :

**`sync_referentiels_erp`**

| Key | Exemple / défaut |
|---|---|
| `catalogue_erp` | `emotors_data_platform` |
| `schema_erp` | `bronze_erp` |
| `catalogue_staging` | `emotors_data_champions` |
| `schema_staging` | `bl_demat_staging` |
| `pg_host` | *(PGHOST du projet Lakebase — obligatoire)* |
| `pg_database` | `databricks_postgres` |
| `pg_schema` | `bl_demat` |
| `lakebase_endpoint` | *(facultatif — déduit de `pg_host` si vide)* |
| `pg_user` | *(facultatif — par défaut l'identité **Run as** du job)* |
| `sales_desadv_enabled` | `false` (ou `true` pour le DESADV vente) |

**`maintenance`**

| Key | Exemple / défaut |
|---|---|
| `pg_host`, `pg_database`, `pg_schema`, `lakebase_endpoint`, `pg_user` | *(comme ci-dessus)* |
| `draft_hours` | `24` (1–168) |
| `stale_job_hours` | `6` (1–48) |

**`rapports_activite`**

| Key | Exemple / défaut |
|---|---|
| `pg_host`, `pg_database`, `pg_schema`, `lakebase_endpoint`, `pg_user` | *(comme ci-dessus)* |
| `llm_endpoint` | `databricks-claude-opus-4-8` — **vide = rapports sans analyse rédigée** |
| `date_reference` | *(vide = aujourd'hui)* ; `AAAA-MM-JJ` pour rattraper une nuit manquée |
| `periodicites` | *(vide = les périodes échues)* ; ex. `MENSUEL,ANNUEL` pour forcer |

Un rapport déjà présent pour une période est **remplacé** : la tâche est
réexécutable sans produire de doublon. Un rapport en échec n'interrompt pas les
autres ; la tâche échoue à la fin en listant les périodes concernées.

**`simulation_donnees`**

| Key | Exemple / défaut |
|---|---|
| `action` | `generer` ou `supprimer` |
| `nb_bl` | `5000` (1–200 000) |
| `date_min` / `date_max` | *(vide = 18 mois → hier)*, format `AAAA-MM-JJ` |
| `graine` | `20260727` — même graine = jeu identique |
| `prefixe` | `SIM-` — marqueur des lignes factices (majuscules, terminé par `-`) |
| `pages_par_bl` | `1` (0–5) ; `0` = aucune image, génération bien plus rapide |
| `remplacer` | `false` ; `true` = supprimer le jeu existant avant de régénérer |

Le job refuse de générer si des lignes préfixées existent déjà (sans
`remplacer=true`). La suppression est aussi disponible en SQL pur :
`sql/simulation/supprimer_donnees_simulation.sql`.

Chaque clé a une valeur par défaut ; la valeur saisie dans l'interface la
remplace. Une valeur manquante ou invalide fait échouer la tâche avec un
message explicite.

**`lakebase_endpoint` est facultatif** : laissé vide, il est retrouvé
automatiquement à partir de `pg_host` (parcours des projets/branches/endpoints
du workspace, comparaison du nom d'hôte). Le renseigner explicitement évite
ces appels de découverte. Le job s'authentifie auprès de Lakebase avec
`generate_database_credential` : aucun mot de passe n'est stocké.

**`pg_user` est facultatif** : laissé vide, le job prend l'identité qui
l'exécute — son **Run as** (`current_user.me()`). C'est **cette même identité**
qui frappe le jeton OAuth ; les deux coïncident donc toujours. Si `user` et
jeton diffèrent, Lakebase refuse la connexion avec
`OAuth: User is not authorized`. Prérequis : le *Run as* doit avoir **Can
connect** sur l'instance Lakebase et les GRANT correspondants (voir le guide,
« Droits de l'identité qui exécute les jobs »). Ne renseigner `pg_user` que
pour forcer un autre rôle, lui aussi frappable par ce *Run as*.

## Dépendances

Déclarer dans l'environnement de la tâche serverless :

```
psycopg[binary]==3.2.3
databricks-sdk>=0.81.0
```

Pour `rapports_activite` **uniquement**, ajouter — la tâche réutilise
`bl_core` (aucune logique métier n'est dupliquée dans les jobs) :

```
pandas==2.2.3
fpdf2==2.8.7
psycopg-pool==3.2.3
```

Déposer alors `shared/bl_core/` **à côté** du script dans l'espace de travail
(ou l'ajouter au `sys.path` de la tâche). `bl_core` ne dépend pas de
Streamlit : `cache.py` bascule automatiquement sur `functools.lru_cache` hors
application.

Pour `simulation_donnees`, `pillow` est utilisé si disponible pour produire des
pages lisibles ; à défaut, un JPEG minimal est employé (aucune erreur).
