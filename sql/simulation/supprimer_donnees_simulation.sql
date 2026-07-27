-- Suppression du jeu de données de simulation BLDEMAT.
--
-- À coller tel quel dans l'éditeur SQL Lakebase. Équivaut à l'action
-- « supprimer » du job jobs/simulation_donnees.py, sans dépendre de Databricks.
--
-- Principe : toutes les lignes de simulation portent le préfixe « SIM- » sur
-- leur clé naturelle — aucune colonne technique n'a été ajoutée au modèle.
-- Un simple LIKE suffit donc à les isoler des données réelles.
--
-- Autre préfixe : remplacer 'SIM-%' partout (le job accepte le paramètre
-- `prefixe`). Idempotent : réexécutable sans effet si le jeu est déjà effacé.
--
-- L'ordre est imposé par les clés étrangères :
--   * pieces_jointes_bl.id_bl -> suivi_bl  (ON DELETE RESTRICT) : pages d'abord ;
--   * suivi_bl.nom_fournisseur -> base_tiers : BL avant les tiers ;
--   * portefeuilles / pla / sites_logistiques référencent tiers et adresses.
--
-- Si une suppression échoue sur une violation de clé étrangère, c'est qu'une
-- ligne RÉELLE référence une ligne de simulation (ex. un vrai BL saisi sur un
-- fournisseur « SIM- »). Le message nomme la table : traiter ce cas à la main
-- plutôt que de forcer.

BEGIN;

DELETE FROM bl_demat.pieces_jointes_bl WHERE id_bl LIKE 'SIM-%';
DELETE FROM bl_demat.audit_bl          WHERE id_bl LIKE 'SIM-%';
DELETE FROM bl_demat.suivi_bl          WHERE id_bl LIKE 'SIM-%';
DELETE FROM bl_demat.base_desadv       WHERE numero_bl LIKE 'SIM-%';
DELETE FROM bl_demat.notifications     WHERE event_key LIKE 'SIM-%';
DELETE FROM bl_demat.qualite_extraction WHERE numero_bl LIKE 'SIM-%';
DELETE FROM bl_demat.portefeuilles
  WHERE code_gestionnaire LIKE 'SIM-%' OR nom_fournisseur LIKE 'SIM-%';
DELETE FROM bl_demat.pla               WHERE nom_fournisseur LIKE 'SIM-%';
DELETE FROM bl_demat.sites_logistiques
  WHERE entite LIKE 'SIM-%' OR adresse LIKE 'SIM-%';
DELETE FROM bl_demat.adresses          WHERE adresse LIKE 'SIM-%';
DELETE FROM bl_demat.gestionnaires     WHERE code_gestionnaire LIKE 'SIM-%';
DELETE FROM bl_demat.base_tiers        WHERE name LIKE 'SIM-%';

-- Les rapports d'activité déjà générés agrègent ces BL : les régénérer
-- (vue Rapports d'activité ▸ Générer) après suppression, ou les effacer :
-- DELETE FROM bl_demat.rapports_activite;

COMMIT;

-- Contrôle : doit renvoyer 0 partout.
SELECT 'suivi_bl' AS table_cible, count(*) AS restant FROM bl_demat.suivi_bl WHERE id_bl LIKE 'SIM-%'
UNION ALL SELECT 'base_tiers', count(*) FROM bl_demat.base_tiers WHERE name LIKE 'SIM-%'
UNION ALL SELECT 'base_desadv', count(*) FROM bl_demat.base_desadv WHERE numero_bl LIKE 'SIM-%'
UNION ALL SELECT 'gestionnaires', count(*) FROM bl_demat.gestionnaires WHERE code_gestionnaire LIKE 'SIM-%'
UNION ALL SELECT 'adresses', count(*) FROM bl_demat.adresses WHERE adresse LIKE 'SIM-%';
