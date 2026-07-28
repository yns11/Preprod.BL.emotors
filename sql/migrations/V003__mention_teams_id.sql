-- ============================================================================
-- V003 — Identifiant Teams explicite pour les mentions.
--
-- Contexte : dans une carte adaptative, « msteams.entities[].mentioned.id »
-- doit désigner l'utilisateur. L'e-mail (UPN) fonctionne dans la plupart des
-- tenants, mais pas tous (comptes invités/externes, résolution par le Flow
-- bot). L'AAD **Object ID** (GUID) est la valeur la plus fiable.
--
-- teams_id est FACULTATIF : s'il est renseigné, il est utilisé pour la
-- mention ; sinon on retombe sur l'e-mail. À exécuter dans l'éditeur SQL du
-- projet Lakebase. Idempotent.
-- ============================================================================

ALTER TABLE bl_demat.gestionnaires ADD COLUMN IF NOT EXISTS teams_id TEXT;

COMMENT ON COLUMN bl_demat.gestionnaires.teams_id IS
  'AAD Object ID (GUID) utilisé pour la @mention Teams. Prioritaire sur '
  'email ; laisser vide pour utiliser l''e-mail.';
