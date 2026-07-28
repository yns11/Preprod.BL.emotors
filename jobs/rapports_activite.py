"""Génération quotidienne des rapports d'activité (PDF + analyse IA).

Une seule exécution par nuit produit **tous les rapports échus** :

* le rapport journalier de la veille — toujours ;
* le rapport hebdomadaire, chaque lundi (semaine close la veille) ;
* le rapport mensuel, le 1er de chaque mois ;
* le rapport trimestriel, le 1er janvier/avril/juillet/octobre ;
* le rapport annuel, le 1er janvier.

La logique métier n'est PAS dupliquée ici : ce script réutilise
`bl_core/rapports.py` et `bl_core/repository.py`, exactement comme l'app
Administration. Il se contente de préparer la connexion Lakebase (variables
PG* attendues par `bl_core/database.py`) puis d'appeler `generer_rapport`.

Un rapport déjà présent pour une période est **remplacé** : relancer le job
est sans risque, et permet de rattraper une nuit manquée avec le paramètre
`date_reference`.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import types

from common import (
    configure_logging,
    identite_connexion,
    job_dbutils,
    json_metrics,
    lire_parametres,
    resoudre_endpoint,
)
from databricks.sdk import WorkspaceClient

logger = logging.getLogger("bl.jobs.rapports")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PARAMETRES = [
    ("pg_host", ""),
    ("pg_database", "databricks_postgres"),
    ("pg_schema", "bl_demat"),
    ("lakebase_endpoint", ""),
    ("pg_user", ""),
    ("llm_endpoint", "databricks-claude-opus-4-8"),   # vide = sans analyse IA
    ("date_reference", ""),      # AAAA-MM-JJ ; défaut : aujourd'hui (rattrapage)
    ("periodicites", ""),        # ex. « QUOTIDIEN,MENSUEL » ; vide = les échues
]


def parametres():
    valeurs = lire_parametres(job_dbutils(), PARAMETRES)
    if not IDENTIFIER.fullmatch(valeurs["pg_schema"]):
        raise ValueError(f"pg_schema invalide ({valeurs['pg_schema']!r}).")
    if not valeurs["pg_host"]:
        raise ValueError("Le paramètre pg_host est obligatoire.")
    reference = valeurs["date_reference"].strip()
    try:
        reference = datetime.date.fromisoformat(reference) if reference \
            else datetime.date.today()
    except ValueError as exc:
        raise ValueError("date_reference doit être au format AAAA-MM-JJ.") from exc
    demandees = [p.strip().upper() for p in valeurs["periodicites"].split(",")
                 if p.strip()]
    return types.SimpleNamespace(
        pg_host=valeurs["pg_host"],
        pg_database=valeurs["pg_database"],
        pg_schema=valeurs["pg_schema"],
        lakebase_endpoint=valeurs["lakebase_endpoint"],
        pg_user=valeurs["pg_user"],
        llm_endpoint=valeurs["llm_endpoint"].strip(),
        reference=reference,
        periodicites=demandees,
    )


def preparer_environnement(args, workspace: WorkspaceClient) -> None:
    """Renseigne les variables lues par `bl_core.config` / `bl_core.database`.

    `database._access_token()` utilise LAKEBASE_ENDPOINT pour frapper un jeton
    OAuth au nom de l'identité du job — le même mécanisme que les autres
    tâches, sans mot de passe stocké."""
    endpoint = resoudre_endpoint(workspace, args.pg_host, args.lakebase_endpoint)
    os.environ.update({
        "LAKEBASE_ENDPOINT": endpoint,
        "PGHOST": args.pg_host,
        "PGPORT": "5432",
        "PGDATABASE": args.pg_database,
        "PGUSER": args.pg_user or identite_connexion(workspace),
        "PGSSLMODE": "require",
        "PGAPPNAME": "bldemat-rapports",
        "BL_PG_SCHEMA": args.pg_schema,
        # Le job n'a pas d'utilisateur SSO : le RBAC ne s'applique pas ici,
        # l'accès est déjà borné par les droits Postgres de l'identité du job.
        "BL_ENVIRONMENT": "prod",
        "BL_RBAC_MODE": "strict",
        "BL_LLM_ENDPOINT": args.llm_endpoint,
    })


def main() -> None:
    configure_logging()
    args = parametres()
    workspace = WorkspaceClient()
    preparer_environnement(args, workspace)

    # Import APRÈS la préparation des variables : bl_core lit sa configuration
    # au premier accès et la met en cache.
    from bl_core import rapports

    if args.periodicites:
        inconnues = set(args.periodicites) - set(rapports.PERIODICITES)
        if inconnues:
            raise ValueError(f"Périodicités inconnues : {sorted(inconnues)}. "
                             f"Valeurs admises : {rapports.PERIODICITES}.")
        veille = args.reference - datetime.timedelta(days=1)
        a_produire = [(p, *rapports.bornes(p, veille)) for p in args.periodicites]
    else:
        a_produire = rapports.periodes_echues(args.reference)

    metriques = {"generes": 0, "echecs": 0, "avec_analyse_ia": 0}
    erreurs = []
    for periodicite, debut, _fin, libelle in a_produire:
        try:
            resultat = rapports.generer_rapport(
                periodicite, debut, genere_par="job:rapports_activite")
            metriques["generes"] += 1
            metriques["avec_analyse_ia"] += int(resultat["analyse_ia"])
            logger.info("Rapport %s « %s » : %d BL, %d octets",
                        periodicite, libelle, resultat["total_bl"],
                        resultat["taille_octets"])
        except Exception as exc:
            # Un rapport en échec ne doit pas empêcher les autres : on
            # journalise, on continue, et la tâche échoue à la fin.
            metriques["echecs"] += 1
            erreurs.append(f"{periodicite} ({libelle}) : {type(exc).__name__} : {exc}")
            logger.exception("Rapport %s « %s » en échec", periodicite, libelle)

    logger.info("Rapports d'activité : %s", json_metrics(**metriques))
    if erreurs:
        raise RuntimeError("Rapports en échec — " + " | ".join(erreurs))


if __name__ == "__main__":
    main()
