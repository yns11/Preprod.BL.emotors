-- V004 — Rapports d'activité périodiques + nettoyage des mentions Teams.
-- À exécuter dans l'éditeur SQL Lakebase après V001 à V003. Idempotent.

-- ---------------------------------------------------------------------------
-- 1. Rapports d'activité (journalier, hebdo, mensuel, trimestriel, annuel)
-- ---------------------------------------------------------------------------
-- Le PDF est stocké en base (BYTEA), comme les pages de BL : un rapport reste
-- consultable même si les données sources évoluent ensuite. `metriques` garde
-- les agrégats sous forme JSON, ce qui permet de comparer deux périodes sans
-- rouvrir les PDF.
CREATE TABLE IF NOT EXISTS bl_demat.rapports_activite (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  periodicite         TEXT NOT NULL CHECK (periodicite IN (
                        'QUOTIDIEN', 'HEBDOMADAIRE', 'MENSUEL',
                        'TRIMESTRIEL', 'ANNUEL')),
  periode_debut       DATE NOT NULL,
  periode_fin         DATE NOT NULL,
  libelle             TEXT NOT NULL,
  contenu             BYTEA NOT NULL,
  taille_octets       BIGINT NOT NULL CHECK (taille_octets > 0),
  synthese            TEXT,              -- analyse rédigée par le modèle
  analyse_ia          BOOLEAN NOT NULL DEFAULT false,
  metriques           JSONB NOT NULL DEFAULT '{}'::jsonb,
  genere_le           TIMESTAMPTZ NOT NULL DEFAULT now(),
  genere_par          TEXT NOT NULL,
  CHECK (periode_fin >= periode_debut)
);

-- Un seul rapport par (périodicité, période) : la regénération remplace.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rapport_periode
  ON bl_demat.rapports_activite (periodicite, periode_debut);
CREATE INDEX IF NOT EXISTS idx_rapport_recent
  ON bl_demat.rapports_activite (periode_debut DESC, periodicite);

-- ---------------------------------------------------------------------------
-- 2. Mentions Teams : suppression de la colonne teams_id
-- ---------------------------------------------------------------------------
-- L'AAD Object ID n'est jamais utilisé : l'action Power Automate « Obtenir un
-- jeton @mention pour un utilisateur » exige l'e-mail (UPN), et le flux
-- retrouve la personne dans les membres de l'équipe. La colonne induisait en
-- erreur dans l'écran Gestionnaires.
ALTER TABLE bl_demat.gestionnaires DROP COLUMN IF EXISTS teams_id;
