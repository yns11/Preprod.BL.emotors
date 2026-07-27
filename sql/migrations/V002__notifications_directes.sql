-- ============================================================================
-- V002 — Notifications Teams envoyées directement par les applications.
--
-- Ce qui change par rapport à V001 :
--   * gestionnaires : e-mail + nom d'affichage, pour pouvoir @mentionner la
--     personne dans la carte Teams ;
--   * notifications : commentaire facultatif, destinataires et résultat de
--     l'envoi (l'application envoie elle-même, il n'y a plus de job) ;
--   * suppression de la file d'attente par canal (notification_canaux /
--     notification_livraisons) devenue inutile — les URL de webhook sont
--     déclarées dans les app.yaml.
--
-- À exécuter dans l'éditeur SQL du projet Lakebase, APRÈS V001 (mise à
-- niveau d'une installation existante). Idempotent.
-- ============================================================================

-- 1. Gestionnaires : identité Teams ------------------------------------------
ALTER TABLE bl_demat.gestionnaires ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE bl_demat.gestionnaires ADD COLUMN IF NOT EXISTS nom_affichage TEXT;

COMMENT ON COLUMN bl_demat.gestionnaires.email IS
  'E-mail/UPN Microsoft 365 : sert à @mentionner le gestionnaire dans Teams.';
COMMENT ON COLUMN bl_demat.gestionnaires.nom_affichage IS
  'Nom affiché dans la mention Teams (par défaut : le code gestionnaire).';

-- 2. Notifications : commentaire, destinataires et résultat d'envoi ----------
ALTER TABLE bl_demat.notifications ADD COLUMN IF NOT EXISTS commentaire TEXT;
ALTER TABLE bl_demat.notifications ADD COLUMN IF NOT EXISTS destinataires TEXT;
ALTER TABLE bl_demat.notifications ADD COLUMN IF NOT EXISTS envoyee BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE bl_demat.notifications ADD COLUMN IF NOT EXISTS envoyee_le TIMESTAMPTZ;
ALTER TABLE bl_demat.notifications ADD COLUMN IF NOT EXISTS erreur_envoi TEXT;

CREATE INDEX IF NOT EXISTS idx_notifications_cree_le
  ON bl_demat.notifications (cree_le DESC);

-- 3. Suppression de la file d'attente par canal (plus de job d'envoi) --------
DROP TABLE IF EXISTS bl_demat.notification_livraisons;
DROP TABLE IF EXISTS bl_demat.notification_canaux;
