# BLDEMAT — Revue critique et feuille de route

Revue du code au 28/07/2026, à l'issue du chantier « améliorations ». Elle est
volontairement sévère : les points forts (RBAC fermé par défaut, transactions
auditées, IA non bloquante, configuration unique en `app.yaml`) ne sont pas
rappelés ici, seuls les **écarts** et les **manques** le sont.

Chaque point est classé par gravité et assorti du coût de correction :
🔴 corriger avant montée en charge · 🟠 dette à traiter · 🟡 confort.

---

# 1. Points faibles du code actuel

## 1.1 🔴 Aucun test d'intégration contre une vraie base

**Constat.** Les 85 tests couvrent la logique pure (agrégats, PDF, prompts,
contrôles) et le rendu Streamlit avec un `repository` **entièrement simulé**.
Le SQL réel n'est exécuté par aucun test. Les requêtes de
`repository.py` (~1 200 lignes) ne sont validées que par l'exécution en
production.

**Ce que ça laisse passer.** Une colonne renommée, un `ON CONFLICT` sur un
index inexistant, une régression de `_conditions_bl` : rien ne l'attrape. Le
bug `pg_user` / OAuth de la semaine dernière est de cette famille.

**Correction.** Une suite `pytest` sur PostgreSQL éphémère (conteneur
`postgres:16` ou `pytest-postgresql`), qui joue les migrations V001→V004 puis
un scénario CRUD complet par table. ~1 jour, et c'est le meilleur retour sur
investissement de cette liste.

## 1.2 🔴 Les migrations SQL ne sont pas versionnées en base

**Constat.** Les fichiers `V001` à `V004` sont exécutés **à la main** dans
l'éditeur SQL Lakebase. Rien n'enregistre ce qui a été appliqué. La seule
protection est l'idempotence des scripts.

**Ce que ça laisse passer.** Impossible de savoir dans quel état est une base ;
un `V004` oublié se manifeste par une erreur applicative (« migration V004
exécutée ? » est écrit en dur dans la vue Rapports — c'est l'aveu du
problème). En cas de rollback, aucun script de descente.

**Correction.** Table `schema_migrations(version, applique_le, checksum)` et
un script `tools/migrer.py` qui applique les versions manquantes dans l'ordre,
en transaction. ~0,5 jour. Alternative : Flyway/Alembic, plus lourd à opérer
sur Lakebase.

## 1.3 🟠 La copie de `bl_core` dans chaque app est fragile

**Constat.** `shared/bl_core` est la source de vérité, dupliquée par `cp` dans
`src/app_creation/bl_core` et `src/app_administration/bl_core`. Rien
n'empêche de modifier une copie, ni de déployer avec des copies désynchronisées.

**Ce que ça laisse passer.** Un correctif appliqué à une seule app. Le
`__init__.py` le signale en commentaire, mais un commentaire n'est pas un
garde-fou.

**Correction.** Au choix : (a) un test CI qui compare les trois arborescences
et échoue en cas d'écart — 1 heure, à faire tout de suite ; (b) packager
`bl_core` en wheel privé et le référencer dans les deux `requirements.txt` —
propre, mais impose une chaîne de publication.

## 1.4 🟠 `app.py` de l'app Administration : 2 100 lignes

**Constat.** Un seul module porte la navigation, 12 boîtes de dialogue, 10
vues, le tableau de bord, les filtres, la gestion d'écrans et le nouveau
wizard d'archivage. Les variables globales `vue`, `CTX_RBAC`, `utilisateur`,
`PLEIN_ECRAN` sont lues depuis l'intérieur des fonctions.

**Ce que ça laisse passer.** Toute modification exige de relire un fichier
énorme ; les fonctions ne sont pas testables isolément (d'où des AppTest qui
lancent l'app entière pour vérifier un bouton) ; le couplage par variable
globale rend l'ordre des définitions signifiant — j'ai dû déplacer
`_lot_fermer_prechargeur` en cours de chantier pour cette raison exacte.

**Correction.** Découper en `vues/` (un module par vue), avec un contexte
explicite passé en paramètre plutôt que lu en global. ~2 jours, à faire avant
la prochaine grosse fonctionnalité, pas après.

## 1.5 🟠 Le verrouillage optimiste n'est pas systématique

**Constat.** `suivi_bl.version` existe et `mettre_a_jour_bl` s'en sert. Mais le
passage EDI NOK → OK en lot, la restauration et la suppression multiple ne
vérifient pas la version avant d'écrire.

**Ce que ça laisse passer.** Deux gestionnaires traitant la même sélection en
même temps : le second écrase le premier sans avertissement. Peu probable
aujourd'hui (peu d'utilisateurs simultanés), certain à terme.

**Correction.** Étendre le `WHERE version = %(v)s` à toutes les mutations, et
remonter un message métier explicite sur 0 ligne affectée. ~0,5 jour.

## 1.6 🟠 Pas de pagination sur les grosses lectures

**Constat.** `bl_periode` ramène **toutes** les lignes de la période pour
agréger en pandas. Sur un rapport annuel à 5 000 BL c'est confortable ; à
100 000 BL sur 3 ans d'historique, c'est ~100 Mo dans le processus du job.
Même schéma pour `lire_bl_pour_dashboard`.

**Ce que ça laisse passer.** Un OOM silencieux du job de rapports le 1er
janvier, jour où le rapport annuel se déclenche — le pire moment.

**Correction.** Basculer les agrégats du rapport en SQL (`GROUP BY` côté
Postgres) et ne ramener que les tableaux de tête. ~1 jour. Le tableau de bord
peut rester en pandas, son périmètre étant borné par les filtres.

## 1.7 🟠 Le préchargeur d'archivage n'a pas de garde-fou mémoire

**Constat.** 200 pages rasterisées à 150 dpi vivent dans `st.session_state`,
soit ~100 à 200 Mo par session d'archivage. Rien ne limite le nombre de
sessions simultanées, et le `ThreadPoolExecutor` n'est fermé que sur les
chemins prévus (changement de vue, fin de lot) — pas sur une fermeture
d'onglet.

**Ce que ça laisse passer.** Trois opérateurs archivant en parallèle sur une
app dimensionnée petit : saturation mémoire et redémarrage du conteneur, avec
perte du lot en cours.

**Correction.** (a) Ne garder en session que les pages d'une **fenêtre
glissante** autour de l'index courant, en relisant le PDF à la demande ;
(b) borner `BL_LOT_MAX_PAGES` selon la taille du conteneur ; (c) purger le
préchargeur sur `on_session_end`. ~1 jour.

## 1.8 🟡 Le RBAC lit la base à chaque rendu

**Constat.** Correction apportée dans ce chantier : `roles_utilisateur` n'est
plus mis en cache, pour qu'un retrait de droit soit vu immédiatement par les
deux apps. Le prix est une requête par rerun Streamlit — donc par clic.

**Ce que ça laisse passer.** Rien de fonctionnel ; c'est un choix assumé
(correction > performance sur une décision d'autorisation). Mais à 50
utilisateurs actifs, cela fait un trafic constant sur `roles_utilisateurs`.

**Correction si le besoin se manifeste.** Cache court (5–10 s) **plus** un
compteur de version des rôles lu à chaque rendu (une seule requête
`SELECT max(attribue_le)`), invalidant le cache au changement. Ne pas le faire
avant d'avoir mesuré.

## 1.9 🟡 Pas d'observabilité

**Constat.** Les logs sont en JSON sur stdout, ce qui est bien, mais rien
n'agrège : aucun tableau de bord technique, aucune alerte sur le taux d'échec
des notifications Teams, sur la latence du modèle ou sur les erreurs
applicatives. `job_executions` trace les jobs, mais personne ne le regarde
sans y aller à la main.

**Correction.** Router les logs vers une table Delta ou un système de log
managé, et alerter sur trois signaux : job en échec, taux de notifications non
publiées > 10 %, latence p95 du modèle. ~1 jour.

## 1.10 🟡 Dette résiduelle identifiée mais non traitée

| Point | Détail |
|---|---|
| `st.altair_chart` | seul appel encore en `use_container_width` (le paramètre `width` n'existe pas en Streamlit 1.49.1) — à basculer à la montée de version |
| `pdf_lot.py` et `rapports.py` | copiés dans les deux apps alors qu'une seule les utilise (`pdf_lot`/`rapports` → Administration) ; sans effet à l'exécution (import paresseux), mais trompeur |
| `V001` crée `teams_id`, `V004` la supprime | correct mais inélégant pour une installation neuve |
| `images.py` | dépend d'OpenCV (~60 Mo) pour du redressement de contour ; à évaluer face à un équivalent Pillow |
| Aucun `pyproject.toml` | pas de lint (`ruff`), pas de format (`black`), pas de typage vérifié (`mypy`) en CI |

---

# 2. Fonctionnalités à ajouter

Classées par valeur métier décroissante, avec une estimation grossière.

## 2.1 Recherche et exploitation

| # | Fonctionnalité | Pourquoi | Effort |
|---|---|---|---|
| 1 | **Recherche plein texte sur le contenu des BL** (OCR indexé) | Aujourd'hui on cherche par numéro ou tiers. Retrouver « tous les BL mentionnant le lot XY » est impossible. Stocker le texte OCR dans une colonne `tsvector` le rend trivial. | 2 j |
| 2 | **Export Excel/CSV des vues filtrées** | Demandé par toute équipe Finance dans les 3 mois. Le filtre existe déjà ; il ne manque qu'un `st.download_button`. | 0,5 j |
| 3 | **Vue « Mes BL »** (saisis par moi, récents) | L'opérateur revient corriger sa saisie et doit la retrouver dans une grille globale. | 0,5 j |
| 4 | **Recherche par plage de dates sur `saisie_le`** (et non seulement `date_reception`) | Un archivage saisi hier pour un BL de 2024 est introuvable par les filtres actuels. | 0,5 j |

## 2.2 Qualité de la donnée

| # | Fonctionnalité | Pourquoi | Effort |
|---|---|---|---|
| 5 | **Détection de doublons par image** (hash perceptuel) | La même page scannée deux fois dans deux lots passe aujourd'hui si les numéros diffèrent d'un caractère OCR. Un pHash sur `pieces_jointes_bl` la signale. | 1 j |
| 6 | **Boucle de ré-entraînement du prompt** | `qualite_extraction` mesure la précision mais rien n'en fait rien. Un écran « champs les moins bien extraits + exemples » guiderait les corrections de prompt. | 1 j |
| 7 | **Score de confiance par champ** remonté par le modèle | Permettrait de surligner en orange les champs douteux dans l'archivage par lots, et d'accélérer la relecture. | 1 j |
| 8 | **Validation métier des numéros de BL** (format par fournisseur) | Le référentiel connaît les formats réels ; un numéro hors format est aujourd'hui accepté sans broncher. | 1 j |

## 2.3 Archivage par lots — suite logique

| # | Fonctionnalité | Pourquoi | Effort |
|---|---|---|---|
| 9 | **Reprise d'un lot interrompu** | Une session perdue à la page 150 sur 200 fait tout recommencer. Persister l'état du lot en base le résout. | 1 j |
| 10 | **BL multi-pages dans un lot** | L'hypothèse « une page = un BL » est fausse pour certains fournisseurs. Un bouton « rattacher cette page au BL précédent » suffirait. | 1 j |
| 11 | **Détection automatique des séparateurs** | Le modèle sait déjà dire « ce n'est pas un BL » ; on pourrait pré-cocher « ignorer » et ne demander qu'une confirmation. | 0,5 j |
| 12 | **Rotation / recadrage d'une page** dans l'écran de relecture | Une page scannée à l'envers oblige aujourd'hui à refaire le PDF. | 0,5 j |

## 2.4 Pilotage

| # | Fonctionnalité | Pourquoi | Effort |
|---|---|---|---|
| 13 | **Envoi des rapports par e-mail / Teams** | Un rapport que personne n'ouvre ne sert à rien. Publier le PDF journalier dans le canal Teams au petit matin change son usage. | 0,5 j |
| 14 | **Comparaison N vs N-1** dans les rapports annuels | La comparaison actuelle est « période précédente » ; pour l'annuel, c'est l'année N-1 qui parle. | 0,5 j |
| 15 | **Seuils d'alerte configurables** (taux NOK, rapprochement) | Aujourd'hui les couleurs du PDF sont en dur (`> 15 %` = rouge). | 0,5 j |
| 16 | **Suivi du délai de traitement des EDI NOK** | On sait combien il y en a, pas combien de temps ils restent NOK — c'est pourtant l'indicateur d'efficacité du gestionnaire. | 1 j |

## 2.5 Exploitation et conformité

| # | Fonctionnalité | Pourquoi | Effort |
|---|---|---|---|
| 17 | **Purge / archivage à froid des images** | Les BYTEA grossissent sans limite. Une politique « > 3 ans → volume UC ou suppression » est à définir **avant** que le problème se pose. | 1 j |
| 18 | **Écran d'audit consultable** | `audit_evenements` est alimentée mais aucune vue ne l'expose. | 0,5 j |
| 19 | **Export d'un dossier de litige** (BL + DESADV + audit en un PDF) | Cas d'usage réel du service litiges, aujourd'hui reconstitué à la main. | 1 j |
| 20 | **Restitution RGPD / traçabilité des accès** | Qui a consulté quel BL ? Aucune trace de lecture aujourd'hui. | 1 j |

---

# 3. Améliorations techniques

| # | Amélioration | Gain | Effort |
|---|---|---|---|
| A | **CI GitHub Actions** : `ruff` + `pytest` + contrôle de synchro `bl_core` | empêche les régressions dont trois ont été trouvées à la main ce mois-ci | 0,5 j |
| B | **Tests d'intégration sur Postgres éphémère** (cf. 1.1) | couvre les 1 200 lignes de SQL non testées | 1 j |
| C | **Table `schema_migrations`** (cf. 1.2) | rend l'état de la base connaissable | 0,5 j |
| D | **Agrégats de rapport en SQL** (cf. 1.6) | supprime le risque d'OOM sur l'annuel | 1 j |
| E | **Découpage de `app.py` Administration** (cf. 1.4) | condition d'une évolution sereine | 2 j |
| F | **`pyproject.toml` + `ruff` + `mypy`** | dette de typage rattrapable progressivement | 0,5 j |
| G | **Fenêtre glissante d'images dans l'archivage** (cf. 1.7) | supporte plusieurs opérateurs simultanés | 1 j |
| H | **Idempotence de bout en bout de l'archivage** | un lot rejoué ne doit rien dupliquer, même après un timeout réseau | 0,5 j |
| I | **Retry avec back-off sur l'appel modèle** | les 504 observés sur l'endpoint sont aujourd'hui des échecs secs | 0,5 j |
| J | **Health check applicatif** (`/healthz` : base, endpoint, Teams) | diagnostic en 5 secondes au lieu de 20 minutes | 0,5 j |

---

# 4. Ce que je ferais dans cet ordre

1. **CI + contrôle de synchronisation `bl_core`** (A) — 0,5 j. Sans filet, tout
   le reste est risqué.
2. **Tests d'intégration Postgres** (B) — 1 j. Couvre le plus gros angle mort.
3. **Table des migrations** (C) — 0,5 j. Supprime une classe entière
   d'incidents de déploiement.
4. **Agrégats de rapport en SQL** (D) — 1 j. À faire **avant** le 1er janvier.
5. **Export Excel** (#2) et **envoi du rapport dans Teams** (#13) — 1 j. Deux
   demandes certaines, très peu coûteuses.
6. **Découpage de `app.py`** (E) — 2 j, avant la prochaine fonctionnalité
   d'ampleur.

Soit environ **6 jours** pour passer d'un logiciel qui marche à un logiciel
qu'on peut faire évoluer sans crainte.
