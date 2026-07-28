"""Synchronisation historisée ERP -> Lakebase, sans collecte globale driver."""

from __future__ import annotations

import hashlib
import logging
import re
import types
import uuid
from datetime import datetime, timezone
from itertools import islice

from common import (
    configure_logging,
    job_dbutils,
    json_metrics,
    identite_connexion,
    lakebase_connection,
    lire_parametres,
    resoudre_endpoint,
)
from databricks.sdk import WorkspaceClient
from pyspark.sql import SparkSession, Window
from pyspark.sql import functions as F

logger = logging.getLogger("bl.jobs.sync")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def batches(iterator, size: int = 1000):
    iterator = iter(iterator)
    while batch := list(islice(iterator, size)):
        yield batch


def row_hash(*values) -> str:
    raw = "|".join("" if value is None else str(value) for value in values)
    return hashlib.sha256(raw.encode()).hexdigest()


# Job parameters (Key/Value dans l'onglet Parameters de la tâche). Les clés
# correspondent exactement aux entrées de l'interface ; la valeur par défaut
# s'applique si le paramètre n'est pas renseigné.
PARAMETRES = [
    ("catalogue_erp", "emotors_data_platform"),
    ("schema_erp", "bronze_erp"),
    ("catalogue_staging", "emotors_data_champions"),
    ("schema_staging", "bl_demat_staging"),
    ("pg_host", ""),
    ("pg_database", "databricks_postgres"),
    ("pg_schema", "bl_demat"),
    ("lakebase_endpoint", ""),
    ("pg_user", ""),
    ("sales_desadv_enabled", "false"),
]


def parametres():
    """Lit les job parameters via dbutils.widgets (plus d'argparse)."""
    valeurs = lire_parametres(job_dbutils(), PARAMETRES)
    for nom in ("catalogue_erp", "schema_erp", "catalogue_staging",
                "schema_staging", "pg_schema"):
        if not IDENTIFIER.fullmatch(valeurs[nom]):
            raise ValueError(f"Le paramètre {nom} contient un identifiant invalide "
                             f"({valeurs[nom]!r}).")
    if not valeurs["pg_host"]:
        raise ValueError("Le paramètre pg_host est obligatoire.")
    # pg_user est facultatif : par défaut, l'identité qui exécute le job
    # (celle pour laquelle le jeton OAuth est frappé). Ne le renseigner que
    # pour forcer un autre rôle Postgres.
    # lakebase_endpoint est facultatif : retrouvé depuis pg_host si absent.
    if valeurs["sales_desadv_enabled"] not in ("true", "false"):
        raise ValueError("Le paramètre sales_desadv_enabled doit valoir true ou false.")
    # Noms d'attributs conservés pour le reste du script.
    return types.SimpleNamespace(
        catalog_erp=valeurs["catalogue_erp"],
        schema_erp=valeurs["schema_erp"],
        catalog_staging=valeurs["catalogue_staging"],
        schema_staging=valeurs["schema_staging"],
        pg_host=valeurs["pg_host"],
        pg_database=valeurs["pg_database"],
        pg_schema=valeurs["pg_schema"],
        lakebase_endpoint=valeurs["lakebase_endpoint"],
        pg_user=valeurs["pg_user"],
        sales_desadv_enabled=valeurs["sales_desadv_enabled"],
    )


def main() -> None:
    configure_logging()
    args = parametres()
    spark = SparkSession.builder.getOrCreate()
    workspace = WorkspaceClient()
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    erp = f"{args.catalog_erp}.{args.schema_erp}"
    staging = f"{args.catalog_staging}.{args.schema_staging}"

    edi = spark.sql(
        f"""
        SELECT recid, issuedatetime, CAST(integrationdate AS DATE) AS integrationdate,
               documentreference1 AS numero_bl,
               CASE CAST(messagestate AS INT)
                 WHEN 2 THEN 'OK' WHEN 3 THEN 'EDI NOK' ELSE NULL
               END AS statut_edi
        FROM (
          SELECT *, ROW_NUMBER() OVER (
            PARTITION BY documentreference1
            ORDER BY issuedatetime DESC, recid DESC
          ) AS rn
          FROM {erp}.siledimessage
          WHERE messagetype = 'DespatchAdvice-Purchase'
            AND documentreference1 IS NOT NULL
            AND trim(documentreference1) <> ''
        ) x
        WHERE rn = 1
        """
    )
    purchase_orders = spark.sql(
        f"""
        SELECT purchid, orderaccount AS source_key,
               concat(orderaccount, ' : ', purchname) AS name
        FROM {erp}.purch_table
        WHERE purchid LIKE 'CO%'
          AND orderaccount IS NOT NULL AND purchname IS NOT NULL
        """
    )
    suppliers = purchase_orders.select("source_key", "name").dropDuplicates(["source_key"])
    sales_orders = spark.sql(
        f"""
        SELECT salesid, custaccount AS source_key,
               concat(custaccount, ' : ', salesname) AS name
        FROM {erp}.sales_table
        WHERE salesid IS NOT NULL
          AND custaccount IS NOT NULL AND salesname IS NOT NULL
        """
    )
    clients = (
        sales_orders.withColumn(
            "rn",
            F.row_number().over(
                Window.partitionBy("source_key").orderBy(F.col("salesid").desc())
            ),
        )
        .where(F.col("rn") == 1)
        .select("source_key", "name")
    )
    lines = spark.table(f"{erp}.siledi_item_line")
    desadv_purchase = (
        lines.join(edi, lines.documentref == edi.recid)
        .join(purchase_orders, lines.purchordernum == purchase_orders.purchid)
        .select(
            edi.numero_bl,
            purchase_orders.source_key.alias("tiers_source_key"),
            purchase_orders.name.alias("nom_fournisseur"),
            edi.issuedatetime,
            edi.integrationdate,
            edi.statut_edi,
        )
        .dropDuplicates(["numero_bl"])
    )
    desadv_sales = None
    if args.sales_desadv_enabled == "true":
        edi_sales = spark.sql(
            f"""
            SELECT recid, issuedatetime,
                   CAST(integrationdate AS DATE) AS integrationdate,
                   documentreference1 AS numero_bl,
                   CASE CAST(messagestate AS INT)
                     WHEN 2 THEN 'OK' WHEN 3 THEN 'EDI NOK' ELSE NULL
                   END AS statut_edi
            FROM (
              SELECT *, ROW_NUMBER() OVER (
                PARTITION BY documentreference1
                ORDER BY issuedatetime DESC, recid DESC
              ) AS rn
              FROM {erp}.siledimessage
              WHERE messagetype = 'DespatchAdvice-Sales'
                AND documentreference1 IS NOT NULL
                AND trim(documentreference1) <> ''
            ) x
            WHERE rn = 1
            """
        )
        desadv_sales = (
            lines.join(edi_sales, lines.documentref == edi_sales.recid)
            .join(sales_orders, lines.salesordernum == sales_orders.salesid)
            .select(
                edi_sales.numero_bl,
                sales_orders.source_key.alias("tiers_source_key"),
                sales_orders.name.alias("nom_fournisseur"),
                edi_sales.issuedatetime,
                edi_sales.integrationdate,
                edi_sales.statut_edi,
            )
            .dropDuplicates(["numero_bl"])
        )

    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {staging}")
    for name, dataframe in (
        ("fournisseurs_erp", suppliers),
        ("clients_erp", clients),
        ("desadv_achat_erp", desadv_purchase),
    ):
        (
            dataframe.withColumn("_run_id", F.lit(run_id))
            .withColumn("_ingested_at", F.current_timestamp())
            .write.mode("append")
            .saveAsTable(f"{staging}.{name}")
        )
    if desadv_sales is not None:
        (
            desadv_sales.withColumn("_run_id", F.lit(run_id))
            .withColumn("_ingested_at", F.current_timestamp())
            .write.mode("append")
            .saveAsTable(f"{staging}.desadv_vente_erp")
        )

    metrics = {
        "suppliers": suppliers.count(),
        "clients": clients.count(),
        "desadv_purchase": desadv_purchase.count(),
        "desadv_sales": desadv_sales.count() if desadv_sales is not None else 0,
        "reconciled_changed": 0,
    }
    endpoint = resoudre_endpoint(workspace, args.pg_host, args.lakebase_endpoint)
    with lakebase_connection(
        workspace,
        endpoint=endpoint,
        host=args.pg_host,
        database=args.pg_database,
        user=args.pg_user or identite_connexion(workspace),
    ) as connection:
        try:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {args.pg_schema}.job_executions "
                    "(job_name, run_id, statut, started_at) "
                    "VALUES ('sync_referentiels_erp', %s, 'STARTED', %s) RETURNING id",
                    (run_id, started_at),
                )
                execution_id = cursor.fetchone()[0]
                if metrics["suppliers"] == 0 or metrics["clients"] == 0:
                    raise RuntimeError(
                        "Garde-fou activé : le snapshot ERP fournisseurs ou clients "
                        "est vide. Aucune désactivation Lakebase n'a été exécutée."
                    )

                tier_sql = (
                    f"INSERT INTO {args.pg_schema}.base_tiers "
                    "(name, type_tiers, source_donnee, source_key, actif, last_seen_at) "
                    "VALUES (%s, %s, 'ERP', %s, true, %s) "
                    "ON CONFLICT (type_tiers, source_key) WHERE source_key IS NOT NULL "
                    "DO UPDATE SET name = EXCLUDED.name, actif = true, "
                    "last_seen_at = EXCLUDED.last_seen_at, modifie_le = now(), "
                    "version = base_tiers.version + 1"
                )
                for dataframe, tier_type in (
                    (suppliers, "FOURNISSEUR"),
                    (clients, "CLIENT"),
                ):
                    rows = (
                        (row.name, tier_type, row.source_key, started_at)
                        for row in dataframe.toLocalIterator()
                    )
                    # Reprise d'une base V4 : rattache d'abord les tiers portant
                    # déjà le même nom, avant l'upsert sur la clé ERP stable.
                    attach_sql = (
                        f"UPDATE {args.pg_schema}.base_tiers "
                        "SET source_donnee = 'ERP', source_key = %s, actif = true, "
                        "last_seen_at = %s, modifie_le = now() "
                        "WHERE name = %s AND type_tiers = %s AND source_key IS NULL"
                    )
                    for batch in batches(rows):
                        attach_batch = [
                            (source_key, seen_at, name, current_type)
                            for name, current_type, source_key, seen_at in batch
                        ]
                        cursor.executemany(attach_sql, attach_batch)
                        cursor.executemany(tier_sql, batch)

                desadv_sql = (
                    f"INSERT INTO {args.pg_schema}.base_desadv "
                    "(numero_bl, nom_fournisseur, sens, issuedatetime, integrationdate, "
                    "statut_edi, source_donnee, source_key, actif, last_seen_at, payload_hash) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'ERP', %s, true, %s, %s) "
                    "ON CONFLICT (numero_bl, sens) DO UPDATE SET "
                    "nom_fournisseur = EXCLUDED.nom_fournisseur, "
                    "issuedatetime = EXCLUDED.issuedatetime, "
                    "integrationdate = EXCLUDED.integrationdate, "
                    "statut_edi = EXCLUDED.statut_edi, source_donnee = 'ERP', "
                    "source_key = EXCLUDED.source_key, actif = true, "
                    "last_seen_at = EXCLUDED.last_seen_at, "
                    "payload_hash = EXCLUDED.payload_hash, "
                    "version = base_desadv.version + 1"
                )
                desadv_sources = [(desadv_purchase, "ACHAT")]
                if desadv_sales is not None:
                    desadv_sources.append((desadv_sales, "VENTE"))
                for dataframe, sens in desadv_sources:
                    rows = (
                        (
                            row.numero_bl,
                            row.nom_fournisseur,
                            sens,
                            row.issuedatetime,
                            row.integrationdate,
                            row.statut_edi,
                            row.numero_bl,
                            started_at,
                            row_hash(
                                sens,
                                row.numero_bl,
                                row.nom_fournisseur,
                                row.issuedatetime,
                                row.integrationdate,
                                row.statut_edi,
                            ),
                        )
                        for row in dataframe.toLocalIterator()
                    )
                    for batch in batches(rows):
                        cursor.executemany(desadv_sql, batch)

                cursor.execute(
                    f"UPDATE {args.pg_schema}.base_tiers SET actif = false, modifie_le = now() "
                    "WHERE source_donnee = 'ERP' AND last_seen_at < %s",
                    (started_at,),
                )
                if metrics["desadv_purchase"] > 0:
                    cursor.execute(
                        f"UPDATE {args.pg_schema}.base_desadv SET actif = false "
                        "WHERE source_donnee = 'ERP' AND sens = 'ACHAT' "
                        "AND last_seen_at < %s",
                        (started_at,),
                    )
                else:
                    logger.warning(
                        "Snapshot DESADV achat vide : les lignes actives sont conservées."
                    )
                if desadv_sales is not None:
                    if metrics["desadv_sales"] > 0:
                        cursor.execute(
                            f"UPDATE {args.pg_schema}.base_desadv SET actif = false "
                            "WHERE source_donnee = 'ERP' AND sens = 'VENTE' "
                            "AND last_seen_at < %s",
                            (started_at,),
                        )
                    else:
                        logger.warning(
                            "Snapshot DESADV vente vide : les lignes actives sont conservées."
                        )
                cursor.execute(
                    f"""
                    UPDATE {args.pg_schema}.suivi_bl b
                    SET desadv_rapproche = x.rapproche,
                        desadv_rapproche_le = CASE WHEN x.rapproche THEN now() ELSE NULL END,
                        version = b.version + 1
                    FROM (
                      SELECT b2.id_bl,
                        (
                          b2.document_statut = 'COMPLET'
                          AND b2.est_supprime = false
                          AND EXISTS (
                            SELECT 1 FROM {args.pg_schema}.base_desadv d
                            WHERE upper(d.numero_bl) = upper(b2.numero_bl)
                              AND d.sens = b2.sens AND d.actif = true
                          )
                        ) AS rapproche
                      FROM {args.pg_schema}.suivi_bl b2
                    ) x
                    WHERE b.id_bl = x.id_bl
                      AND b.desadv_rapproche IS DISTINCT FROM x.rapproche
                    """
                )
                metrics["reconciled_changed"] = cursor.rowcount
                cursor.execute(
                    f"UPDATE {args.pg_schema}.job_executions "
                    "SET statut = 'SUCCEEDED', finished_at = now(), metrics = %s::jsonb "
                    "WHERE id = %s",
                    (json_metrics(**metrics), execution_id),
                )
            logger.info("Synchronisation terminée : %s", json_metrics(**metrics))
        except Exception as exc:
            logger.exception("Synchronisation en échec")
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {args.pg_schema}.job_executions "
                    "(job_name, run_id, statut, started_at, finished_at, erreur) "
                    "VALUES ('sync_referentiels_erp', %s, 'FAILED', %s, now(), %s)",
                    (run_id, started_at, str(exc)[:4000]),
                )
            raise


if __name__ == "__main__":
    main()
