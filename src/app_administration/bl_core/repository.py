"""Couche d'accès aux données de BLDEMAT V5 Professional.

Métadonnées ET binaires documentaires (pages scannées, PDF des rapports) sont
stockés dans Lakebase PostgreSQL. Toutes les mutations critiques sont
atomiques et auditées.
"""

import datetime
import hashlib
import json
import logging
import os
import uuid
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg

from . import database
from .cache import cache_lecture
from .config import get_settings
from .validation import normalize_bl_number

logger = logging.getLogger("bl.repository")

STATUT_OK = "1"
STATUT_EDI_NOK = "0"

# --- Types d'opération -----------------------------------------------------
TYPE_RECEPTION = "RECEPTION"
TYPE_EXPEDITION = "EXPEDITION"
TYPE_ARCHIVAGE_RECEPTION = "ARCHIVAGE_RECEPTION"
TYPE_ARCHIVAGE_EXPEDITION = "ARCHIVAGE_EXPEDITION"
LIBELLES_OPERATION = {
    TYPE_RECEPTION: "Nouvelle réception",
    TYPE_EXPEDITION: "Nouvelle expédition",
    TYPE_ARCHIVAGE_RECEPTION: "Archivage d'un ancien BL réception",
    TYPE_ARCHIVAGE_EXPEDITION: "Archivage d'un ancien BL expédition",
}
TYPES_ACHAT = [TYPE_RECEPTION, TYPE_ARCHIVAGE_RECEPTION]      # tiers = fournisseur
TYPES_VENTE = [TYPE_EXPEDITION, TYPE_ARCHIVAGE_EXPEDITION]    # tiers = client

SENS_ACHAT = "ACHAT"
SENS_VENTE = "VENTE"
TIERS_FOURNISSEUR = "FOURNISSEUR"
TIERS_CLIENT = "CLIENT"


def sens_operation(type_operation: str) -> str:
    return SENS_VENTE if type_operation in TYPES_VENTE else SENS_ACHAT


def libelle_tiers(type_operation: str) -> str:
    """« Client » côté vente (expéditions), « Fournisseur » côté achat."""
    return "Client" if type_operation in TYPES_VENTE else "Fournisseur"


def operation_avec_plage_et_quai(type_operation: str) -> bool:
    """Plage horaire, quai et commentaire : nouvelles réceptions/expéditions
    uniquement (les archivages ne portent que numéro, date et tiers)."""
    return type_operation in (TYPE_RECEPTION, TYPE_EXPEDITION)


def operation_avec_statut(type_operation: str) -> bool:
    """L'état OK / EDI NOK n'existe que pour une nouvelle réception."""
    return type_operation == TYPE_RECEPTION


# --- Plages horaires -------------------------------------------------------
PLAGES_HORAIRES = ["00h-06h"] + [f"{h:02d}h-{h + 2:02d}h" for h in range(6, 20, 2)] + ["20h-00h"]


def maintenant_local() -> datetime.datetime:
    """Heure locale du site (le conteneur d'app tourne en UTC)."""
    try:
        fuseau = ZoneInfo(os.environ.get("BL_FUSEAU", "Europe/Paris"))
    except Exception:
        fuseau = None
    return datetime.datetime.now(fuseau)


def plage_horaire_courante() -> str:
    """Plage horaire contenant l'heure locale courante (préremplissage)."""
    h = maintenant_local().hour
    if h < 6:
        return PLAGES_HORAIRES[0]
    if h >= 20:
        return PLAGES_HORAIRES[-1]
    debut = 6 + ((h - 6) // 2) * 2
    return f"{debut:02d}h-{debut + 2:02d}h"


def _run(query: str, params: Optional[dict] = None, fetch: bool = False):
    """Compatibilité avec l'API historique ; délègue au moteur transactionnel."""
    return database.run(query, params, fetch=fetch)


# ---------------------------------------------------------------------------
# Référentiels — lectures en cache court (utilisées par l'app Création)
# ---------------------------------------------------------------------------
@cache_lecture(ttl=300)
def lister_tiers(type_tiers: str) -> list[str]:
    """Fournisseurs (TIERS_FOURNISSEUR) ou clients (TIERS_CLIENT)."""
    s = get_settings()
    df = _run(
        f"SELECT name FROM {s.pg_schema}.base_tiers "
        "WHERE type_tiers = %(t)s AND actif = true ORDER BY name",
        params={"t": type_tiers},
        fetch=True,
    )
    return df["name"].tolist() if df is not None else []


@cache_lecture(ttl=300)
def lister_quais() -> list[str]:
    s = get_settings()
    df = _run(
        f"SELECT code_quai FROM {s.pg_schema}.quais WHERE actif = true ORDER BY code_quai",
        fetch=True,
    )
    return df["code_quai"].tolist() if df is not None else []


@cache_lecture(ttl=300)
def lister_tous_tiers() -> list[str]:
    """Fournisseurs ET clients (référentiel des entités des sites logistiques)."""
    s = get_settings()
    df = _run(
        f"SELECT name FROM {s.pg_schema}.base_tiers WHERE actif = true ORDER BY name",
        fetch=True,
    )
    return df["name"].tolist() if df is not None else []


@cache_lecture(ttl=300)
def lister_adresses() -> list[str]:
    s = get_settings()
    df = _run(
        f"SELECT adresse FROM {s.pg_schema}.adresses WHERE actif = true ORDER BY adresse",
        fetch=True,
    )
    return df["adresse"].tolist() if df is not None else []


@cache_lecture(ttl=300)
def adresses_par_tiers() -> dict[str, str]:
    """{ tiers -> adresse de site } depuis sites_logistiques (première adresse
    par tiers). Contexte optionnel du rapprochement IA de l'app Création."""
    s = get_settings()
    df = _run(
        f"SELECT entite, min(adresse) AS adresse FROM {s.pg_schema}.sites_logistiques "
        "WHERE entite IN (SELECT name FROM "
        f"{s.pg_schema}.base_tiers WHERE actif = true) GROUP BY entite",
        fetch=True,
    )
    if df is None or df.empty:
        return {}
    return dict(zip(df["entite"], df["adresse"], strict=False))


@cache_lecture(ttl=300)
def lister_gestionnaires() -> list[str]:
    s = get_settings()
    df = _run(
        f"SELECT code_gestionnaire FROM {s.pg_schema}.gestionnaires "
        "WHERE actif = true ORDER BY code_gestionnaire",
        fetch=True,
    )
    return df["code_gestionnaire"].tolist() if df is not None else []


@cache_lecture(ttl=300)
def gestionnaires_pour_fournisseur(nom_fournisseur: str) -> list[dict]:
    """Gestionnaires actifs dont le portefeuille contient ce fournisseur.

    Renvoie [{"code", "email", "nom"}] : la notification Teams s'en sert pour
    @mentionner les personnes concernées. Un gestionnaire sans e-mail est
    affiché en texte simple (pas de mention)."""
    if not nom_fournisseur:
        return []
    s = get_settings()
    df = _run(
        f"SELECT g.code_gestionnaire, g.email, "
        "COALESCE(NULLIF(btrim(g.nom_affichage), ''), g.code_gestionnaire) AS nom "
        f"FROM {s.pg_schema}.portefeuilles p "
        f"JOIN {s.pg_schema}.gestionnaires g "
        "  ON g.code_gestionnaire = p.code_gestionnaire AND g.actif = true "
        "WHERE p.nom_fournisseur = %(frs)s ORDER BY g.code_gestionnaire",
        params={"frs": nom_fournisseur},
        fetch=True,
    )
    if df is None or df.empty:
        return []
    return [
        {"code": ligne["code_gestionnaire"],
         "email": (ligne["email"] or "").strip(),
         "nom": ligne["nom"]}
        for _, ligne in df.iterrows()
    ]


@cache_lecture(ttl=300)
def fournisseur_pour_bl(numero_bl: str, sens: str) -> Optional[str]:
    """Tiers annoncé par l'avis d'expédition (DESADV) du sens donné pour ce
    numéro de BL — None si absent (l'utilisateur choisira manuellement)."""
    s = get_settings()
    df = _run(
        f"SELECT nom_fournisseur FROM {s.pg_schema}.base_desadv "
        "WHERE upper(numero_bl) = upper(%(num)s) AND sens = %(sens)s "
        "AND actif = true LIMIT 1",
        params={"num": numero_bl, "sens": sens},
        fetch=True,
    )
    if df is None or df.empty:
        return None
    return df["nom_fournisseur"].iloc[0]


@cache_lecture(ttl=300)
def quai_pla(tiers: str) -> Optional[str]:
    """Quai défini dans le protocole logistique (PLA) du tiers, ou None.
    L'app Création l'utilise pour pré-remplir le champ Quai (défaut B15)."""
    if not tiers:
        return None
    s = get_settings()
    df = _run(f"SELECT code_quai FROM {s.pg_schema}.pla WHERE nom_fournisseur = %(t)s",
              params={"t": tiers}, fetch=True)
    if df is None or df.empty:
        return None
    return df["code_quai"].iloc[0]


@cache_lecture(ttl=120)
def roles_utilisateur(utilisateur: str) -> list[str]:
    """Rôles RBAC de l'utilisateur (email, insensible à la casse). Liste vide
    si aucun rôle. Les erreurs sont propagées afin que la couche RBAC ferme
    l'accès par sécurité."""
    s = get_settings()
    df = _run(
        f"SELECT role FROM {s.pg_schema}.roles_utilisateurs "
        "WHERE lower(utilisateur) = lower(%(u)s) "
        "AND (expire_le IS NULL OR expire_le > now())",
        params={"u": utilisateur or ""}, fetch=True)
    return df["role"].tolist() if df is not None else []


@cache_lecture(ttl=120)
def rbac_actif() -> bool:
    """Compatibilité : le mode strict ne dépend jamais du contenu de la table."""
    return get_settings().rbac_mode == "strict"


@cache_lecture(ttl=300)
def bls_desadv_pour_tiers(tiers: str, sens: str, limite: int = 200) -> list[str]:
    """Numéros de BL annoncés (DESADV) pour un tiers donné dans un sens
    (ACHAT/VENTE). Sert de contexte au rapprochement IA du numéro de BL
    (préfixe/suffixe). Correspondance exacte sur le nom du tiers."""
    if not tiers:
        return []
    s = get_settings()
    df = _run(
        f"SELECT numero_bl FROM {s.pg_schema}.base_desadv "
        "WHERE nom_fournisseur = %(t)s AND sens = %(sens)s AND actif = true "
        "ORDER BY integrationdate DESC NULLS LAST, numero_bl LIMIT %(lim)s",
        params={"t": tiers, "sens": sens, "lim": limite},
        fetch=True,
    )
    return df["numero_bl"].tolist() if df is not None and not df.empty else []


def vider_caches_referentiels() -> None:
    """À appeler après un CRUD sur un référentiel (app Administration)."""
    lister_tiers.clear()
    lister_tous_tiers.clear()
    lister_adresses.clear()
    adresses_par_tiers.clear()
    lister_quais.clear()
    lister_gestionnaires.clear()
    gestionnaires_pour_fournisseur.clear()
    fournisseur_pour_bl.clear()
    bls_desadv_pour_tiers.clear()
    quai_pla.clear()
    roles_utilisateur.clear()
    rbac_actif.clear()


# ---------------------------------------------------------------------------
# Référentiels — CRUD générique (app Administration)
# Les tables sont sur liste blanche ; le diff avant/après est calculé sur les
# lignes complètes (tous nos référentiels sont entièrement clés).
# ---------------------------------------------------------------------------
# « colonnes » = colonnes ÉCRITES par le CRUD ; « cles » = clé primaire
# (une ligne est valide si ses colonnes-clés sont renseignées ; les autres
# colonnes écrites peuvent être vides -> NULL). Les colonnes d'affichage seul
# (ex. horodatages DESADV venus de l'ERP) sont gérées par les lectures dédiées.
REFERENTIELS = {
    "tiers": {"table": "base_tiers", "colonnes": ["name", "type_tiers"], "cles": ["name"]},
    "desadv": {"table": "base_desadv", "colonnes": ["numero_bl", "nom_fournisseur", "sens"],
               "cles": ["numero_bl", "sens"]},
    "gestionnaires": {"table": "gestionnaires",
                      "colonnes": ["code_gestionnaire", "nom_affichage", "email"],
                      "cles": ["code_gestionnaire"]},
    "portefeuilles": {"table": "portefeuilles", "colonnes": ["code_gestionnaire", "nom_fournisseur"],
                      "cles": ["code_gestionnaire", "nom_fournisseur"]},
    "quais": {"table": "quais", "colonnes": ["code_quai"], "cles": ["code_quai"]},
    "adresses": {"table": "adresses", "colonnes": ["adresse"], "cles": ["adresse"]},
    "sites_logistiques": {"table": "sites_logistiques", "colonnes": ["entite", "adresse"],
                          "cles": ["entite", "adresse"]},
    "pla": {"table": "pla",
            "colonnes": ["nom_fournisseur", "code_quai", "jours_livraison",
                         "frequence_livraison"],
            "cles": ["nom_fournisseur"]},
    "roles": {"table": "roles_utilisateurs", "colonnes": ["utilisateur", "role"],
              "cles": ["utilisateur", "role"]},
}

QUAI_DEFAUT = "B15"


def _table_referentiel(nom: str) -> tuple[str, list[str], list[str]]:
    cfg = REFERENTIELS[nom]  # KeyError = bug d'appel, pas une entrée utilisateur
    return f"{get_settings().pg_schema}.{cfg['table']}", cfg["colonnes"], cfg["cles"]


def lire_referentiel(nom: str, filtres: Optional[dict] = None) -> pd.DataFrame:
    table, colonnes, _ = _table_referentiel(nom)
    filtres = {k: v for k, v in (filtres or {}).items() if k in colonnes}
    where = " AND ".join(f"{c} = %({c})s" for c in filtres) or "1=1"
    ordre = ", ".join(colonnes)
    return _run(f"SELECT {', '.join(colonnes)} FROM {table} WHERE {where} ORDER BY {ordre}",
                params=filtres, fetch=True)


def _norme(valeur) -> str:
    """Valeur de cellule -> chaîne comparable ('' pour vide/None/NaN)."""
    if valeur is None:
        return ""
    txt = str(valeur).strip()
    return "" if txt.lower() in ("", "nan", "none", "nat") else txt


def _indexer(df: pd.DataFrame, visibles: list[str], cles_visibles: list[str],
             detecter_doublons: bool = False) -> dict:
    """{ clé -> ligne complète } pour les lignes dont toutes les colonnes-clés
    sont renseignées (lignes incomplètes en cours de saisie ignorées).
    detecter_doublons=True (grille éditée) : refuse deux lignes de même clé."""
    index = {}
    for _, ligne in df.iterrows():
        cle = tuple(_norme(ligne.get(c)) for c in cles_visibles)
        if all(cle):
            if detecter_doublons and cle in index:
                raise ValueError(
                    "Opération refusée : doublon dans la grille pour "
                    f"« {' / '.join(cle)} » (chaque clé doit être unique).")
            index[cle] = tuple(_norme(ligne.get(c)) for c in visibles)
    return index


def sauver_referentiel(nom: str, df_avant: pd.DataFrame, df_apres: pd.DataFrame,
                       valeurs_fixes: Optional[dict] = None,
                       utilisateur: str = "systeme") -> tuple[int, int]:
    """Applique le diff avant/après d'un éditeur de données. Une ligne modifiée
    (même clé, contenu changé) = suppression puis réinsertion. `valeurs_fixes`
    porte les colonnes masquées à l'écran (ex. sens='ACHAT'). df_avant est le
    jeu chargé (éventuellement filtré) : les lignes non chargées ne sont jamais
    touchées. Retourne (nb_ajouts/modifications, nb_suppressions)."""
    table, colonnes, cles = _table_referentiel(nom)
    valeurs_fixes = valeurs_fixes or {}
    visibles = [c for c in colonnes if c not in valeurs_fixes]
    cles_visibles = [c for c in cles if c not in valeurs_fixes]

    avant = _indexer(df_avant, visibles, cles_visibles)
    apres = _indexer(df_apres, visibles, cles_visibles, detecter_doublons=True)

    if nom == "roles":
        admin_index = visibles.index("role")
        if not any(values[admin_index] == "ADMIN_METIER" for values in apres.values()):
            raise ValueError(
                "Opération refusée : au moins un ADMIN_METIER doit rester déclaré. "
                "Les comptes de bootstrap ne remplacent pas ce contrôle."
            )

    cles_a_supprimer = {k for k in avant if k not in apres or apres[k] != avant[k]}
    cles_a_inserer = {k for k in apres if k not in avant or apres[k] != avant[k]}

    try:
        with database.transaction() as tx:
            if nom in {"tiers", "desadv"}:
                for cle in cles_a_supprimer:
                    params = dict(zip(cles_visibles, cle, strict=False)) | valeurs_fixes
                    where = " AND ".join(f"{column} = %({column})s" for column in params)
                    source = tx.fetch_one(
                        f"SELECT source_donnee FROM {table} WHERE {where}",
                        params,
                    )
                    if source and source["source_donnee"] == "ERP":
                        raise ValueError(
                            "Une donnée issue de l'ERP ne peut pas être modifiée ou supprimée "
                            "dans BLDEMAT. Corrigez-la dans le système source."
                        )
            for cle in cles_a_supprimer:
                params = dict(zip(cles_visibles, cle, strict=False)) | valeurs_fixes
                where = " AND ".join(f"{c} = %({c})s" for c in params)
                tx.execute(f"DELETE FROM {table} WHERE {where}", params)
            for cle in cles_a_inserer:
                valeurs = dict(zip(visibles, apres[cle], strict=False))
                params = {c: (v if v != "" else None) for c, v in valeurs.items()} | valeurs_fixes
                cols = ", ".join(params)
                marqueurs = ", ".join(f"%({c})s" for c in params)
                tx.execute(f"INSERT INTO {table} ({cols}) VALUES ({marqueurs})", params)
            if cles_a_supprimer or cles_a_inserer:
                tx.execute(
                    f"INSERT INTO {get_settings().pg_schema}.audit_evenements "
                    "(categorie, action, cible, acteur, details) "
                    "VALUES ('REFERENTIEL', 'MISE_A_JOUR', %(cible)s, %(acteur)s, %(details)s::jsonb)",
                    {
                        "cible": nom,
                        "acteur": utilisateur,
                        "details": json.dumps(
                            {
                                "insertions": len(cles_a_inserer),
                                "suppressions": len(cles_a_supprimer),
                            }
                        ),
                    },
                )
    except psycopg.errors.ForeignKeyViolation:
        raise ValueError(
            "Opération refusée : une valeur est encore référencée ailleurs "
            "(portefeuille, BL...) ou référence une entrée inexistante."
        ) from None
    except psycopg.errors.UniqueViolation:
        raise ValueError(
            "Opération refusée : ce numéro de BL / cette entrée existe déjà "
            "(doublon interdit)."
        ) from None

    vider_caches_referentiels()
    return len(cles_a_inserer), len(cles_a_supprimer)


# ---------------------------------------------------------------------------
# Lectures filtrées des vues (app Administration)
# ---------------------------------------------------------------------------
def lire_desadv(sens: str, numero: str = "", fournisseur: str = "",
                gestionnaire: str = "", date_min: Optional[datetime.date] = None,
                date_max: Optional[datetime.date] = None,
                statut_edi: str = "") -> pd.DataFrame:
    """Avis d'expédition d'un sens (ACHAT/VENTE), avec filtres numéro de BL,
    tiers, gestionnaire (via portefeuille), plage de dates d'intégration et
    état du message EDI ('OK' / 'EDI NOK'). Renvoie numero_bl, nom_fournisseur,
    issuedatetime, integrationdate, statut_edi."""
    s = get_settings()
    conditions = ["sens = %(sens)s", "actif = true"]
    params: dict = {"sens": sens}
    if numero:
        conditions.append("lower(numero_bl) LIKE %(num)s")
        params["num"] = f"%{numero.lower()}%"
    if fournisseur:
        conditions.append("lower(nom_fournisseur) LIKE %(frs)s")
        params["frs"] = f"%{fournisseur.lower()}%"
    if gestionnaire:
        conditions.append(
            f"nom_fournisseur IN (SELECT nom_fournisseur FROM {s.pg_schema}.portefeuilles "
            "WHERE code_gestionnaire = %(gest)s)")
        params["gest"] = gestionnaire
    if date_min:
        conditions.append("integrationdate >= %(dmin)s")
        params["dmin"] = date_min
    if date_max:
        conditions.append("integrationdate <= %(dmax)s")
        params["dmax"] = date_max
    if statut_edi:
        conditions.append("statut_edi = %(sedi)s")
        params["sedi"] = statut_edi
    where = " AND ".join(conditions)
    return _run(
        f"SELECT numero_bl, nom_fournisseur, issuedatetime, integrationdate, statut_edi "
        f"FROM {s.pg_schema}.base_desadv WHERE {where} "
        "ORDER BY integrationdate DESC NULLS LAST, numero_bl",
        params=params, fetch=True)


def lire_sites_logistiques(entite: str = "", adresse: str = "") -> pd.DataFrame:
    """Sites logistiques avec filtres entité (tiers) et adresse (contient)."""
    s = get_settings()
    conditions = ["1=1"]
    params: dict = {}
    if entite:
        conditions.append("entite = %(ent)s")
        params["ent"] = entite
    if adresse:
        conditions.append("lower(adresse) LIKE %(adr)s")
        params["adr"] = f"%{adresse.lower()}%"
    where = " AND ".join(conditions)
    return _run(
        f"SELECT entite, adresse FROM {s.pg_schema}.sites_logistiques "
        f"WHERE {where} ORDER BY entite, adresse",
        params=params, fetch=True)


def lire_portefeuilles(gestionnaire: str = "", fournisseur: str = "") -> pd.DataFrame:
    """Portefeuilles avec filtres gestionnaire et fournisseur."""
    s = get_settings()
    conditions = ["1=1"]
    params: dict = {}
    if gestionnaire:
        conditions.append("code_gestionnaire = %(gest)s")
        params["gest"] = gestionnaire
    if fournisseur:
        conditions.append("lower(nom_fournisseur) LIKE %(frs)s")
        params["frs"] = f"%{fournisseur.lower()}%"
    where = " AND ".join(conditions)
    return _run(
        f"SELECT code_gestionnaire, nom_fournisseur FROM {s.pg_schema}.portefeuilles "
        f"WHERE {where} ORDER BY code_gestionnaire, nom_fournisseur",
        params=params, fetch=True)


# ---------------------------------------------------------------------------
# Notifications (journal EDI NOK -> OK ; affichées en lecture dans l'app Admin)
# ---------------------------------------------------------------------------
def enregistrer_notification(type_notif: str, numero_bl: str, message: str,
                             utilisateur: str, commentaire: str = "",
                             destinataires: str = "",
                             idempotency_key: str | None = None) -> int:
    """Journalise l'événement notifiable et renvoie son identifiant.

    L'envoi Teams est réalisé juste après par l'application (module `teams`) ;
    `marquer_notification_envoyee` enregistre son résultat. La clé
    d'idempotence évite qu'un double clic produise deux lignes.
    """
    s = get_settings()
    key = idempotency_key or hashlib.sha256(
        f"{type_notif}|{(numero_bl or '').strip().upper()}|{message}".encode("utf-8")
    ).hexdigest()
    with database.transaction() as tx:
        event = tx.fetch_one(
            f"INSERT INTO {s.pg_schema}.notifications "
            "(event_key, type_notif, numero_bl, message, commentaire, "
            " destinataires, cree_par) "
            "VALUES (%(key)s, %(t)s, %(num)s, %(msg)s, %(com)s, %(dest)s, %(par)s) "
            "ON CONFLICT (event_key) DO UPDATE SET event_key = EXCLUDED.event_key "
            "RETURNING id",
            {"key": key, "t": type_notif, "num": numero_bl, "msg": message,
             "com": commentaire or None, "dest": destinataires or None,
             "par": utilisateur},
        )
    return int(event["id"])


def marquer_notification_envoyee(notification_id: int, succes: bool,
                                 erreur: str = "") -> None:
    """Résultat de l'envoi Teams (best effort : n'interrompt jamais le métier)."""
    s = get_settings()
    try:
        _run(
            f"UPDATE {s.pg_schema}.notifications SET envoyee = %(ok)s, "
            "envoyee_le = CASE WHEN %(ok)s THEN now() ELSE envoyee_le END, "
            "erreur_envoi = %(err)s WHERE id = %(id)s",
            params={"ok": succes, "err": (erreur or None) if not succes else None,
                    "id": notification_id},
        )
    except Exception:
        logger.exception("Impossible de tracer l'envoi de la notification %s",
                         notification_id)


def lister_notifications(limite: int = 200) -> pd.DataFrame:
    s = get_settings()
    return _run(
        f"SELECT cree_le, type_notif, numero_bl, message, commentaire, "
        "destinataires, cree_par, envoyee, envoyee_le, erreur_envoi "
        f"FROM {s.pg_schema}.notifications "
        "ORDER BY cree_le DESC LIMIT %(lim)s",
        params={"lim": limite}, fetch=True)


# ---------------------------------------------------------------------------
# Rapports d'activité périodiques
# ---------------------------------------------------------------------------
def bl_periode(debut: datetime.date, fin: datetime.date) -> pd.DataFrame:
    """BL dont la date d'opération tombe dans [debut, fin], **y compris** les
    supprimés et les non-complets : le rapport doit pouvoir compter les
    anomalies de saisie, pas seulement l'activité nominale."""
    s = get_settings()
    return _run(
        f"SELECT id_bl, numero_bl, date_reception, plage_horaire, type_operation, "
        "sens, statut_bl, nom_fournisseur, quai_reception, document_statut, "
        "est_supprime, desadv_rapproche, saisie_par, saisie_le "
        f"FROM {s.pg_schema}.suivi_bl "
        "WHERE date_reception BETWEEN %(d)s AND %(f)s",
        params={"d": debut, "f": fin}, fetch=True)


def notifications_periode(debut: datetime.date, fin: datetime.date) -> pd.DataFrame:
    s = get_settings()
    return _run(
        f"SELECT type_notif, envoyee FROM {s.pg_schema}.notifications "
        "WHERE cree_le >= %(d)s AND cree_le < %(f)s",
        params={"d": debut, "f": fin + datetime.timedelta(days=1)}, fetch=True)


def qualite_periode(debut: datetime.date, fin: datetime.date) -> pd.DataFrame:
    """Précision de l'extraction IA sur la période, agrégée par champ."""
    s = get_settings()
    return _run(
        f"SELECT champ, count(*) AS mesures, "
        "count(*) FILTER (WHERE identique) AS exactes "
        f"FROM {s.pg_schema}.qualite_extraction "
        "WHERE cree_le >= %(d)s AND cree_le < %(f)s "
        "GROUP BY champ ORDER BY champ",
        params={"d": debut, "f": fin + datetime.timedelta(days=1)}, fetch=True)


def enregistrer_rapport(periodicite: str, periode_debut: datetime.date,
                        periode_fin: datetime.date, libelle: str, contenu: bytes,
                        synthese: str, analyse_ia: bool, metriques: dict,
                        genere_par: str) -> int:
    """Enregistre (ou REMPLACE) le rapport d'une période. Renvoie son id."""
    s = get_settings()
    with database.transaction() as tx:
        ligne = tx.fetch_one(
            f"INSERT INTO {s.pg_schema}.rapports_activite "
            "(periodicite, periode_debut, periode_fin, libelle, contenu, "
            " taille_octets, synthese, analyse_ia, metriques, genere_par) "
            "VALUES (%(p)s, %(d)s, %(f)s, %(lib)s, %(c)s, %(taille)s, %(syn)s, "
            "        %(ia)s, %(m)s::jsonb, %(par)s) "
            "ON CONFLICT (periodicite, periode_debut) DO UPDATE SET "
            "  periode_fin = EXCLUDED.periode_fin, libelle = EXCLUDED.libelle, "
            "  contenu = EXCLUDED.contenu, taille_octets = EXCLUDED.taille_octets, "
            "  synthese = EXCLUDED.synthese, analyse_ia = EXCLUDED.analyse_ia, "
            "  metriques = EXCLUDED.metriques, genere_le = now(), "
            "  genere_par = EXCLUDED.genere_par "
            "RETURNING id",
            {"p": periodicite, "d": periode_debut, "f": periode_fin,
             "lib": libelle, "c": contenu, "taille": len(contenu),
             "syn": synthese or None, "ia": analyse_ia,
             "m": json.dumps(metriques, ensure_ascii=False, default=str),
             "par": genere_par},
        )
    return int(ligne["id"])


def lister_rapports(periodicites: Optional[list[str]] = None,
                    date_min: Optional[datetime.date] = None,
                    date_max: Optional[datetime.date] = None,
                    limite: int = 300) -> pd.DataFrame:
    """Catalogue des rapports, **sans le PDF** (colonne lourde chargée à la
    demande par `telecharger_rapport`)."""
    s = get_settings()
    conditions, params = ["true"], {"lim": limite}
    if periodicites:
        conditions.append("periodicite = ANY(%(per)s)")
        params["per"] = list(periodicites)
    if date_min:
        conditions.append("periode_fin >= %(dmin)s")
        params["dmin"] = date_min
    if date_max:
        conditions.append("periode_debut <= %(dmax)s")
        params["dmax"] = date_max
    return _run(
        f"SELECT id, periodicite, libelle, periode_debut, periode_fin, "
        "taille_octets, analyse_ia, synthese, genere_le, genere_par "
        f"FROM {s.pg_schema}.rapports_activite WHERE {' AND '.join(conditions)} "
        "ORDER BY periode_debut DESC, periodicite LIMIT %(lim)s",
        params=params, fetch=True)


def telecharger_rapport(rapport_id: int) -> Optional[dict]:
    """PDF et métadonnées d'un rapport, ou None s'il n'existe plus."""
    s = get_settings()
    with database.transaction() as tx:
        ligne = tx.fetch_one(
            f"SELECT id, periodicite, libelle, periode_debut, periode_fin, "
            "contenu, synthese, analyse_ia, metriques, genere_le, genere_par "
            f"FROM {s.pg_schema}.rapports_activite WHERE id = %(id)s",
            {"id": rapport_id})
    if ligne is None:
        return None
    ligne["contenu"] = bytes(ligne["contenu"])
    return ligne


def supprimer_rapport(rapport_id: int) -> None:
    s = get_settings()
    _run(f"DELETE FROM {s.pg_schema}.rapports_activite WHERE id = %(id)s",
         params={"id": rapport_id})


# ---------------------------------------------------------------------------
# Tableau de bord (agrégats calculés côté app pour l'interactivité)
# ---------------------------------------------------------------------------
def lire_bl_pour_dashboard(date_min: Optional[datetime.date] = None,
                           date_max: Optional[datetime.date] = None) -> pd.DataFrame:
    """BL non supprimés (colonnes utiles au tableau de bord), filtrés sur la
    date d'opération. Les agrégats/KPI sont calculés dans l'app."""
    s = get_settings()
    conditions = [
        "(est_supprime IS NULL OR est_supprime = false)",
        "document_statut = 'COMPLET'",
    ]
    params: dict = {}
    if date_min:
        conditions.append("date_reception >= %(dmin)s")
        params["dmin"] = date_min
    if date_max:
        conditions.append("date_reception <= %(dmax)s")
        params["dmax"] = date_max
    where = " AND ".join(conditions)
    return _run(
        f"SELECT id_bl, numero_bl, date_reception, plage_horaire, type_operation, "
        f"statut_bl, nom_fournisseur, saisie_le "
        f"FROM {s.pg_schema}.suivi_bl WHERE {where}",
        params=params, fetch=True)


# ---------------------------------------------------------------------------
# Création d'un BL
# ---------------------------------------------------------------------------
def numero_bl_disponible(numero_bl: str, type_operation: str) -> bool:
    """Un numéro est unique par sens ACHAT/VENTE, suppression logique incluse."""
    s = get_settings()
    sens = sens_operation(type_operation)
    df = _run(
        f"SELECT 1 FROM {s.pg_schema}.suivi_bl "
        "WHERE upper(numero_bl) = upper(%(num)s) AND sens = %(sens)s LIMIT 1",
        params={"num": numero_bl, "sens": sens},
        fetch=True,
    )
    return df is None or df.empty


def inserer_bl(
    id_bl: str,
    numero_bl: str,
    nom_fournisseur: str,
    statut_bl: str,
    type_operation: str,
    utilisateur: str,
    date_reception: Optional[datetime.date] = None,
    quai_reception: Optional[str] = None,
    comment_bl: str = "",
    plage_horaire: Optional[str] = None,
) -> None:
    """Lève ValueError si le numéro de BL existe déjà (contrainte d'unicité :
    la vérification à la saisie ne suffit pas en cas de créations simultanées)."""
    s = get_settings()
    numero_bl = normalize_bl_number(numero_bl)
    try:
        with database.transaction() as tx:
            tx.execute(
                f"""
                INSERT INTO {s.pg_schema}.suivi_bl
                  (id_bl, numero_bl, date_reception, plage_horaire, nom_fournisseur,
                   quai_reception, statut_bl, comment_bl, saisie_par, saisie_le,
                   type_operation, est_supprime, source_donnee, document_statut, version)
                VALUES
                  (%(id)s, %(num)s, %(dr)s, %(plage)s, %(frs)s, %(quai)s,
                   %(st)s, %(com)s, %(par)s, now(), %(op)s, false, 'MANUEL', 'BROUILLON', 1)
                """,
                {
                    "id": id_bl,
                    "num": numero_bl,
                    "dr": date_reception,
                    "plage": plage_horaire,
                    "frs": nom_fournisseur,
                    "quai": quai_reception,
                    "st": statut_bl,
                    "com": comment_bl,
                    "par": utilisateur,
                    "op": type_operation,
                },
            )
            _auditer_tx(tx, id_bl, "CREATION", utilisateur, apres=numero_bl)
    except psycopg.errors.UniqueViolation:
        raise ValueError(f"Le numéro de BL « {numero_bl} » existe déjà.") from None


def enregistrer_page(id_bl: str, index_page: int, image_bytes: bytes) -> None:
    """Enregistre une page de manière idempotente (binaire en base, BYTEA).

    ON CONFLICT (id_bl, index_page) : rejouer l'enregistrement après un échec
    réseau ne crée jamais de doublon de page.
    """
    s = get_settings()
    id_photo = str(uuid.uuid4())
    if len(image_bytes) > s.max_image_bytes:
        raise ValueError(f"La page {index_page + 1} dépasse la taille autorisée.")

    with database.transaction() as tx:
        tx.execute(
            f"INSERT INTO {s.pg_schema}.pieces_jointes_bl "
            "(id_photo, id_bl, contenu, sha256, taille_octets, "
            "content_type, index_page) "
            "VALUES (%(idp)s, %(idb)s, %(contenu)s, %(sha)s, %(taille)s, "
            "'image/jpeg', %(idx)s) ON CONFLICT (id_bl, index_page) DO NOTHING",
            {
                "idp": id_photo,
                "idb": id_bl,
                "contenu": image_bytes,
                "sha": hashlib.sha256(image_bytes).hexdigest(),
                "taille": len(image_bytes),
                "idx": index_page,
            },
        )


def pages_enregistrees(id_bl: str) -> set[int]:
    """Index des pages déjà en base — reprise idempotente après un échec."""
    s = get_settings()
    df = _run(
        f"SELECT index_page FROM {s.pg_schema}.pieces_jointes_bl WHERE id_bl = %(id)s",
        params={"id": id_bl},
        fetch=True,
    )
    return set(df["index_page"].tolist()) if df is not None else set()


def finaliser_bl(id_bl: str, nombre_pages: int, utilisateur: str) -> None:
    """Passe un document à COMPLET uniquement si toutes ses pages sont présentes."""
    s = get_settings()
    with database.transaction() as tx:
        row = tx.fetch_one(
            f"SELECT document_statut FROM {s.pg_schema}.suivi_bl "
            "WHERE id_bl = %(id)s FOR UPDATE",
            {"id": id_bl},
        )
        if row is None:
            raise ValueError("Le BL à finaliser est introuvable.")
        page_row = tx.fetch_one(
            f"SELECT COUNT(*) AS pages FROM {s.pg_schema}.pieces_jointes_bl "
            "WHERE id_bl = %(id)s",
            {"id": id_bl},
        )
        pages = int(page_row["pages"] if page_row else 0)
        if pages != int(nombre_pages):
            raise RuntimeError(
                f"Document incomplet : {pages} page(s) enregistrée(s) "
                f"sur {nombre_pages}."
            )
        if row["document_statut"] != "COMPLET":
            tx.execute(
                f"UPDATE {s.pg_schema}.suivi_bl SET document_statut = 'COMPLET', "
                "modifie_par = %(par)s, modifie_le = now(), version = version + 1 "
                "WHERE id_bl = %(id)s",
                {"id": id_bl, "par": utilisateur},
            )
            _auditer_tx(tx, id_bl, "FINALISATION", utilisateur, apres=nombre_pages)


# ---------------------------------------------------------------------------
# Recherche / lecture (app Administration)
# ---------------------------------------------------------------------------
def _conditions_bl(numero, fournisseur, types, date_min, date_max, statut,
                   gestionnaire, inclure_supprimes, prefixe: str = "") -> tuple[str, dict]:
    """Clause WHERE commune à la recherche paginée et aux statistiques KPI.
    `prefixe` (ex. 'b.') qualifie les colonnes de suivi_bl en cas de jointure."""
    s = get_settings()
    p = prefixe
    conditions = [f"{p}document_statut = 'COMPLET'"]
    params: dict = {}
    if not inclure_supprimes:
        conditions.append(f"({p}est_supprime IS NULL OR {p}est_supprime = false)")
    if types:
        conditions.append(f"{p}type_operation = ANY(%(types)s)")
        params["types"] = list(types)
    if fournisseur:
        conditions.append(f"lower({p}nom_fournisseur) LIKE %(frs)s")
        params["frs"] = f"%{fournisseur.lower()}%"
    if gestionnaire:
        conditions.append(
            f"{p}nom_fournisseur IN (SELECT nom_fournisseur FROM {s.pg_schema}.portefeuilles "
            "WHERE code_gestionnaire = %(gest)s)")
        params["gest"] = gestionnaire
    if numero:
        conditions.append(f"lower({p}numero_bl) LIKE %(num)s")
        params["num"] = f"%{numero.lower()}%"
    if date_min:
        conditions.append(f"{p}date_reception >= %(dmin)s")
        params["dmin"] = date_min
    if date_max:
        conditions.append(f"{p}date_reception <= %(dmax)s")
        params["dmax"] = date_max
    if statut in (STATUT_OK, STATUT_EDI_NOK):
        conditions.append(f"{p}statut_bl = %(st)s")
        params["st"] = statut
    return " AND ".join(conditions), params


def stats_bl(numero="", fournisseur="", types=None, date_min=None, date_max=None,
             gestionnaire="", inclure_supprimes=False) -> dict:
    """KPI du périmètre filtré (hors filtre d'état) : total, OK, EDI NOK et
    nombre de pages jointes."""
    s = get_settings()
    where, params = _conditions_bl(numero, fournisseur, types, date_min, date_max,
                                   None, gestionnaire, inclure_supprimes)
    df = _run(
        f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE statut_bl = '{STATUT_OK}') AS ok,
               COUNT(*) FILTER (WHERE statut_bl = '{STATUT_EDI_NOK}') AS nok
        FROM {s.pg_schema}.suivi_bl WHERE {where}
        """,
        params=params, fetch=True)
    where_b, params_b = _conditions_bl(numero, fournisseur, types, date_min, date_max,
                                       None, gestionnaire, inclure_supprimes, prefixe="b.")
    df_pages = _run(
        f"SELECT COUNT(*) AS n FROM {s.pg_schema}.pieces_jointes_bl p "
        f"JOIN {s.pg_schema}.suivi_bl b ON b.id_bl = p.id_bl WHERE {where_b}",
        params=params_b, fetch=True)
    if df is None or df.empty:
        return {"total": 0, "ok": 0, "nok": 0, "pages": 0}
    ligne = df.iloc[0]
    pages = int(df_pages["n"].iloc[0]) if df_pages is not None and not df_pages.empty else 0
    return {"total": int(ligne["total"]), "ok": int(ligne["ok"]),
            "nok": int(ligne["nok"]), "pages": pages}


def rechercher_bl(
    fournisseur: str = "",
    numero: str = "",
    types: Optional[list[str]] = None,
    date_min: Optional[datetime.date] = None,
    date_max: Optional[datetime.date] = None,
    statut: Optional[str] = None,
    gestionnaire: str = "",
    inclure_supprimes: bool = False,
    page: int = 1,
    page_size: int = 50,
) -> tuple[pd.DataFrame, int]:
    """Recherche multicritère insensible à la casse, paginée (50 par défaut).
    `types` restreint aux types d'opération donnés (vue achat / vue vente) ;
    `gestionnaire` filtre les BL dont le fournisseur est dans son portefeuille."""
    s = get_settings()
    where, params = _conditions_bl(numero, fournisseur, types, date_min, date_max,
                                   statut, gestionnaire, inclure_supprimes)

    df_total = _run(
        f"SELECT COUNT(*) AS n FROM {s.pg_schema}.suivi_bl WHERE {where}", params=params, fetch=True
    )
    total = int(df_total["n"].iloc[0]) if df_total is not None else 0

    params_page = dict(params)
    params_page["lim"] = page_size
    params_page["off"] = max(page - 1, 0) * page_size
    df = _run(
        f"""
        SELECT id_bl, numero_bl, date_reception, plage_horaire, nom_fournisseur, quai_reception,
               statut_bl, comment_bl, saisie_par, saisie_le, modifie_par, modifie_le,
               type_operation, est_supprime, desadv_rapproche, version
        FROM {s.pg_schema}.suivi_bl
        WHERE {where}
        ORDER BY saisie_le DESC
        LIMIT %(lim)s OFFSET %(off)s
        """,
        params=params_page,
        fetch=True,
    )
    return (df if df is not None else pd.DataFrame()), total


def photos_pour_bls(ids_bl: list[str]) -> dict[str, list[str]]:
    """Identifiants des photos des BL affichés, en une seule requête."""
    if not ids_bl:
        return {}
    s = get_settings()
    params = {f"id_{i}": v for i, v in enumerate(ids_bl)}
    placeholders = ", ".join(f"%({k})s" for k in params)
    df = _run(
        f"SELECT id_bl, id_photo, index_page FROM {s.pg_schema}.pieces_jointes_bl "
        f"WHERE id_bl IN ({placeholders}) ORDER BY index_page",
        params=params,
        fetch=True,
    )
    if df is None or df.empty:
        return {}
    return df.groupby("id_bl")["id_photo"].apply(list).to_dict()


@cache_lecture(ttl=3600, max_entries=200)
def telecharger_photo(id_photo: str) -> bytes:
    """Télécharge une page et vérifie son empreinte SHA-256."""
    s = get_settings()
    df = _run(
        f"SELECT contenu, sha256 FROM {s.pg_schema}.pieces_jointes_bl "
        "WHERE id_photo = %(id)s",
        params={"id": id_photo},
        fetch=True,
    )
    if df is None or df.empty:
        raise ValueError(f"Photo introuvable : {id_photo}")
    row = df.iloc[0]
    if row.get("contenu") is None:
        raise ValueError(f"Contenu manquant pour la photo {id_photo}.")
    contents = bytes(row["contenu"])
    expected = row.get("sha256")
    if expected and hashlib.sha256(contents).hexdigest() != expected:
        raise RuntimeError(f"Contrôle d'intégrité en échec pour la photo {id_photo}.")
    return contents


# ---------------------------------------------------------------------------
# Mise à jour / suppression logique (app Administration)
# ---------------------------------------------------------------------------
CHAMPS_MODIFIABLES = {"numero_bl", "date_reception", "plage_horaire", "nom_fournisseur",
                      "quai_reception", "statut_bl", "comment_bl"}


def mettre_a_jour_bl(
    id_bl: str,
    champs: dict,
    utilisateur: str,
    expected_version: int | None = None,
) -> None:
    """UPDATE des seuls champs autorisés (liste blanche), avec traçabilité et
    audit champ par champ. Lève ValueError si le numéro de BL est déjà pris."""
    a_modifier = {k: v for k, v in champs.items() if k in CHAMPS_MODIFIABLES}
    if not a_modifier:
        return
    s = get_settings()
    if "numero_bl" in a_modifier:
        a_modifier["numero_bl"] = normalize_bl_number(str(a_modifier["numero_bl"]))
    set_clause = ", ".join(f"{k} = %({k})s" for k in a_modifier)
    params = dict(a_modifier)
    params["id"] = id_bl
    params["par"] = utilisateur
    params["version"] = expected_version
    try:
        with database.transaction() as tx:
            avant = tx.fetch_one(
                f"SELECT {', '.join(a_modifier)}, version FROM {s.pg_schema}.suivi_bl "
                "WHERE id_bl = %(id)s FOR UPDATE",
                {"id": id_bl},
            )
            if avant is None:
                raise ValueError("Le BL n'existe plus.")
            if expected_version is not None and int(avant["version"]) != int(expected_version):
                raise ValueError(
                    "Ce BL a été modifié par un autre utilisateur. "
                    "Actualisez la grille avant de recommencer."
                )
            updated = tx.execute(
                f"UPDATE {s.pg_schema}.suivi_bl SET {set_clause}, "
                "modifie_par = %(par)s, modifie_le = now(), version = version + 1 "
                "WHERE id_bl = %(id)s AND "
                "(%(version)s IS NULL OR version = %(version)s)",
                params,
            )
            if updated != 1:
                raise ValueError(
                    "Conflit de modification détecté. Actualisez la grille."
                )
            for champ, nouveau in a_modifier.items():
                ancien = avant.get(champ)
                if str(ancien if ancien is not None else "") != str(
                    nouveau if nouveau is not None else ""
                ):
                    _auditer_tx(
                        tx, id_bl, "MODIFICATION", utilisateur, champ, ancien, nouveau
                    )
    except psycopg.errors.UniqueViolation:
        raise ValueError(f"Le numéro de BL « {champs.get('numero_bl', '')} » existe déjà.") from None


def supprimer_bl(
    id_bl: str, utilisateur: str, expected_version: int | None = None
) -> None:
    """Suppression LOGIQUE : le BL et ses images restent en base."""
    s = get_settings()
    with database.transaction() as tx:
        count = tx.execute(
            f"UPDATE {s.pg_schema}.suivi_bl SET est_supprime = true, "
            "supprime_par = %(par)s, supprime_le = now(), version = version + 1 "
            "WHERE id_bl = %(id)s AND est_supprime = false "
            "AND (%(version)s IS NULL OR version = %(version)s)",
            {"id": id_bl, "par": utilisateur, "version": expected_version},
        )
        if count != 1:
            raise ValueError("BL déjà supprimé, introuvable ou modifié depuis la sélection.")
        _auditer_tx(tx, id_bl, "SUPPRESSION", utilisateur)


def restaurer_bl(
    id_bl: str, utilisateur: str, expected_version: int | None = None
) -> None:
    s = get_settings()
    with database.transaction() as tx:
        count = tx.execute(
            f"UPDATE {s.pg_schema}.suivi_bl SET est_supprime = false, "
            "supprime_par = NULL, supprime_le = NULL, modifie_par = %(par)s, "
            "modifie_le = now(), version = version + 1 "
            "WHERE id_bl = %(id)s AND est_supprime = true "
            "AND (%(version)s IS NULL OR version = %(version)s)",
            {"id": id_bl, "par": utilisateur, "version": expected_version},
        )
        if count != 1:
            raise ValueError("BL déjà actif, introuvable ou modifié depuis la sélection.")
        _auditer_tx(tx, id_bl, "RESTAURATION", utilisateur)


# ---------------------------------------------------------------------------
# Audit des BL (historique), écarts DESADV, qualité IA, écrans utilisateur
# ---------------------------------------------------------------------------
def _auditer_tx(
    tx: database.Transaction,
    id_bl: str,
    evenement: str,
    utilisateur: str,
    champ: str | None = None,
    avant=None,
    apres=None,
) -> None:
    s = get_settings()
    tx.execute(
        f"INSERT INTO {s.pg_schema}.audit_bl "
        "(id_bl, evenement, champ, valeur_avant, valeur_apres, modifie_par) "
        "VALUES (%(id)s, %(ev)s, %(ch)s, %(av)s, %(ap)s, %(par)s)",
        {"id": id_bl, "ev": evenement, "ch": champ,
         "av": None if avant is None else str(avant),
         "ap": None if apres is None else str(apres),
         "par": utilisateur},
    )


def _auditer(id_bl: str, evenement: str, utilisateur: str, champ: str | None = None,
             avant=None, apres=None) -> None:
    """API historique : audit obligatoire dans une transaction dédiée."""
    with database.transaction() as tx:
        _auditer_tx(tx, id_bl, evenement, utilisateur, champ, avant, apres)


def lire_audit_bl(id_bl: str) -> pd.DataFrame:
    s = get_settings()
    return _run(
        f"SELECT modifie_le, evenement, champ, valeur_avant, valeur_apres, modifie_par "
        f"FROM {s.pg_schema}.audit_bl WHERE id_bl = %(id)s ORDER BY modifie_le DESC",
        params={"id": id_bl}, fetch=True)


def lire_ecarts(sens: str, fournisseur: str = "", gestionnaire: str = "",
                date_min: Optional[datetime.date] = None,
                date_max: Optional[datetime.date] = None
                ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rapprochement BL ⇄ DESADV d'un sens : (BL sans DESADV, DESADV sans BL),
    avec filtres tiers (contient), gestionnaire (portefeuille) et dates
    (date d'opération côté BL, date d'intégration côté DESADV). Comparaison
    des numéros insensible à la casse."""
    s = get_settings()
    types = TYPES_ACHAT if sens == SENS_ACHAT else TYPES_VENTE
    conds_bl, conds_dsd = [], []
    params: dict = {"types": list(types), "sens": sens}
    if fournisseur:
        conds_bl.append("lower(b.nom_fournisseur) LIKE %(frs)s")
        conds_dsd.append("lower(d.nom_fournisseur) LIKE %(frs)s")
        params["frs"] = f"%{fournisseur.lower()}%"
    if gestionnaire:
        sous_req = (f"IN (SELECT nom_fournisseur FROM {s.pg_schema}.portefeuilles "
                    "WHERE code_gestionnaire = %(gest)s)")
        conds_bl.append(f"b.nom_fournisseur {sous_req}")
        conds_dsd.append(f"d.nom_fournisseur {sous_req}")
        params["gest"] = gestionnaire
    if date_min:
        conds_bl.append("b.date_reception >= %(dmin)s")
        conds_dsd.append("d.integrationdate >= %(dmin)s")
        params["dmin"] = date_min
    if date_max:
        conds_bl.append("b.date_reception <= %(dmax)s")
        conds_dsd.append("d.integrationdate <= %(dmax)s")
        params["dmax"] = date_max
    extra_bl = ("  AND " + "\n  AND ".join(conds_bl)) if conds_bl else ""
    extra_dsd = ("  AND " + "\n  AND ".join(conds_dsd)) if conds_dsd else ""
    bl_sans = _run(
        f"""
        SELECT numero_bl, date_reception, nom_fournisseur, saisie_par, saisie_le
        FROM {s.pg_schema}.suivi_bl b
        WHERE type_operation = ANY(%(types)s)
          AND (est_supprime IS NULL OR est_supprime = false)
          AND document_statut = 'COMPLET'
          AND NOT EXISTS (SELECT 1 FROM {s.pg_schema}.base_desadv d
                          WHERE upper(d.numero_bl) = upper(b.numero_bl)
                            AND d.sens = %(sens)s
                            AND d.actif = true)
        {extra_bl}
        ORDER BY date_reception DESC NULLS LAST, numero_bl
        """,
        params=params, fetch=True)
    desadv_sans = _run(
        f"""
        SELECT numero_bl, nom_fournisseur, integrationdate, statut_edi
        FROM {s.pg_schema}.base_desadv d
        WHERE sens = %(sens)s
          AND d.actif = true
          AND NOT EXISTS (SELECT 1 FROM {s.pg_schema}.suivi_bl b
                          WHERE upper(b.numero_bl) = upper(d.numero_bl)
                            AND b.type_operation = ANY(%(types)s)
                            AND (b.est_supprime IS NULL OR b.est_supprime = false)
                            AND b.document_statut = 'COMPLET')
        {extra_dsd}
        ORDER BY integrationdate DESC NULLS LAST, numero_bl
        """,
        params=params, fetch=True)
    vide = pd.DataFrame()
    return (bl_sans if bl_sans is not None else vide,
            desadv_sans if desadv_sans is not None else vide)


@cache_lecture(ttl=120)
def fraicheur_desadv(sens: str) -> dict:
    """Fraîcheur du flux EDI d'un sens : dernière intégration et dernière
    création de message connues."""
    s = get_settings()
    df = _run(
        f"SELECT max(integrationdate) AS integration, max(issuedatetime) AS creation "
        f"FROM {s.pg_schema}.base_desadv WHERE sens = %(sens)s AND actif = true",
        params={"sens": sens}, fetch=True)
    if df is None or df.empty:
        return {"integration": None, "creation": None}
    return {"integration": df["integration"].iloc[0], "creation": df["creation"].iloc[0]}


def enregistrer_qualite_extraction(lignes: list[dict]) -> None:
    """Journalise la comparaison « valeur IA vs valeur validée » champ par
    champ (mesure du taux de précision de l'extraction). Best effort."""
    s = get_settings()
    try:
        with database.transaction() as tx:
            tx.executemany(
                f"INSERT INTO {s.pg_schema}.qualite_extraction "
                "(utilisateur, numero_bl, champ, valeur_ia, valeur_validee, identique, "
                "modele_endpoint, prompt_version) "
                "VALUES (%(u)s, %(n)s, %(c)s, %(ia)s, %(v)s, %(i)s, %(m)s, %(p)s)",
                [
                    {
                        "u": ligne.get("utilisateur"),
                        "n": ligne.get("numero_bl"),
                        "c": ligne["champ"],
                        "ia": ligne.get("valeur_ia"),
                        "v": ligne.get("valeur_validee"),
                        "i": bool(ligne["identique"]),
                        "m": s.llm_endpoint,
                        "p": s.llm_prompt_version,
                    }
                    for ligne in lignes
                ],
            )
    except Exception as e:
        logger.warning("Journal qualité IA indisponible : %s", e)


def stats_qualite_extraction() -> pd.DataFrame:
    """Taux de précision de l'extraction IA par champ."""
    s = get_settings()
    return _run(
        f"""
        SELECT champ, COUNT(*) AS mesures,
               COUNT(*) FILTER (WHERE identique) AS exactes,
               ROUND(100.0 * COUNT(*) FILTER (WHERE identique) / COUNT(*), 1) AS precision_pct
        FROM {s.pg_schema}.qualite_extraction
        GROUP BY champ ORDER BY champ
        """, fetch=True)


def lister_qualite_extraction(limite: int = 500) -> pd.DataFrame:
    s = get_settings()
    return _run(
        f"SELECT cree_le, numero_bl, champ, valeur_ia, valeur_validee, identique, utilisateur "
        f"FROM {s.pg_schema}.qualite_extraction ORDER BY cree_le DESC LIMIT %(lim)s",
        params={"lim": limite}, fetch=True)


# --- Écrans utilisateur (filtres/tri/colonnes sauvegardés par vue) ----------
def lister_ecrans(utilisateur: str, vue: str) -> pd.DataFrame:
    s = get_settings()
    df = _run(
        f"SELECT nom, est_defaut, etat FROM {s.pg_schema}.ecrans_utilisateur "
        "WHERE lower(utilisateur) = lower(%(u)s) AND vue = %(v)s ORDER BY nom",
        params={"u": utilisateur, "v": vue}, fetch=True)
    return df if df is not None else pd.DataFrame(columns=["nom", "est_defaut", "etat"])


def sauver_ecran(utilisateur: str, vue: str, nom: str, etat: str,
                 est_defaut: bool = False) -> None:
    s = get_settings()
    with database.transaction() as tx:
        if est_defaut:                              # un seul défaut par vue
            tx.execute(
                f"UPDATE {s.pg_schema}.ecrans_utilisateur SET est_defaut = false "
                "WHERE lower(utilisateur) = lower(%(u)s) AND vue = %(v)s",
                {"u": utilisateur, "v": vue},
            )
        tx.execute(
            f"INSERT INTO {s.pg_schema}.ecrans_utilisateur "
            "(utilisateur, vue, nom, est_defaut, etat) "
            "VALUES (%(u)s, %(v)s, %(n)s, %(d)s, %(e)s) "
            "ON CONFLICT (utilisateur, vue, nom) "
            "DO UPDATE SET etat = EXCLUDED.etat, est_defaut = EXCLUDED.est_defaut",
            {"u": utilisateur, "v": vue, "n": nom, "d": est_defaut, "e": etat},
        )


def supprimer_ecran(utilisateur: str, vue: str, nom: str) -> None:
    s = get_settings()
    _run(f"DELETE FROM {s.pg_schema}.ecrans_utilisateur "
         "WHERE lower(utilisateur) = lower(%(u)s) AND vue = %(v)s AND nom = %(n)s",
         params={"u": utilisateur, "v": vue, "n": nom})
