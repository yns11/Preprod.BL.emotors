# BLDEMAT — BL dématérialisés (eMotors)

Solution Databricks pour numériser, enrichir, rapprocher et administrer les
bordereaux de livraison (BL) achat et vente.

👉 **Déploiement et mode de fonctionnement : [GUIDE.md](GUIDE.md).**

## Contenu

- `src/app_creation` : parcours terrain Streamlit en quatre étapes. Deux
  variantes selon le type d'opération : **saisie unitaire** (capture
  multi-page, champs pré-remplis par IA) et **archivage en lot** (PDF jusqu'à
  200 pages, une page = un BL, vérification page par page avec pré-analyse IA
  anticipée). Publie une carte Teams à chaque **nouvelle réception**, en
  mentionnant les gestionnaires du portefeuille du fournisseur.
- `src/app_administration` : tableau de bord, **rapports d'activité PDF**
  (journalier, hebdo, mensuel, trimestriel, annuel), vues BL, DESADV,
  rapprochement BL/DESADV, référentiels, audit, qualité IA, rôles et
  notifications. Publie une carte Teams au **passage EDI NOK → OK**, avec
  commentaire facultatif.
- `shared/bl_core` : cœur partagé — configuration, RBAC, transactions,
  repository, appel LLM mutualisé, extraction IA, archivage en lot, rapports
  d'activité, cartes Teams, PDF, design system.
- `jobs` : synchronisation ERP historisée, maintenance, rapports d'activité et
  générateur de données de simulation (`jobs/README.md`).
- `sql/migrations` : `V001` (installation neuve), `V002` à `V004` (mises à
  niveau) ; `sql/simulation` : purge du jeu de test.

## Principes d'architecture

- **Lakebase PostgreSQL** porte tout : métadonnées, images (colonne BYTEA),
  transactions et audits. Aucune dépendance à Unity Catalog.
- Les apps utilisent l'identité de leur **service principal** et des
  credentials OAuth Lakebase renouvelés automatiquement ; aucun mot de passe
  n'est stocké.
- **Toute la configuration est dans les deux `app.yaml`** : ni bundle, ni
  secret scope, ni valeur en dur. Une configuration incohérente fait échouer
  le démarrage avec un message explicite.
- **Notifications Teams en temps réel**, envoyées directement par les apps
  (aucun job) : la trace en base est écrite d'abord et fait foi ; l'envoi est
  *best effort* et n'interrompt jamais une opération métier.
- **RBAC strict et fermé par défaut** : un utilisateur sans rôle n'a aucun
  droit. La matrice est versionnée dans le code (`bl_core/rbac.py`), les
  affectations sont en base (Gestion ▸ Rôles).
- Les mutations critiques sont transactionnelles et auditées ; les
  modifications concurrentes sont protégées par un numéro de version.
- **L'IA n'est jamais bloquante** : endpoint absent ou en échec, la saisie
  reste manuelle, l'archivage en lot reste utilisable et les rapports sont
  produits sans la section d'analyse rédigée.
- `bl_core` **ne dépend pas de Streamlit** : les tâches Lakeflow réutilisent le
  même code métier (`cache.py` bascule sur `lru_cache` hors application).

## Développement

`shared/bl_core` est la source de vérité. Après modification, resynchroniser
les copies embarquées par chaque application :

```bash
cp shared/bl_core/*.py src/app_creation/bl_core/
cp shared/bl_core/*.py src/app_administration/bl_core/
```

Python 3.11 — Streamlit 1.49.1 — Lakebase (PostgreSQL managé).
