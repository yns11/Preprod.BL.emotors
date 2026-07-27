"""Jeu de données de simulation BLDEMAT — génération et suppression.

Crée un volume réaliste de BL (5 000 par défaut) et TOUTES les lignes liées
(fournisseurs, clients, adresses, sites logistiques, gestionnaires,
portefeuilles, PLA, DESADV, pages scannées, audit, notifications, qualité IA)
afin d'alimenter le tableau de bord, les rapports et les écrans de
rapprochement.

Identification des données factices
-----------------------------------
**Aucune colonne technique n'est ajoutée au modèle.** Toutes les lignes créées
portent un PRÉFIXE reconnaissable (« SIM- » par défaut) sur leur clé naturelle
— ``suivi_bl.id_bl`` et ``numero_bl``, ``base_tiers.name``,
``gestionnaires.code_gestionnaire``, ``adresses.adresse``, etc. La suppression
se résume donc à des ``DELETE … WHERE clé LIKE 'SIM-%'`` dans l'ordre des
clés étrangères (action ``supprimer``, ou le script
``sql/simulation/supprimer_donnees_simulation.sql``).

Le job REFUSE de générer si des lignes portant déjà le préfixe existent, sauf
si ``remplacer=true`` : impossible d'empiler deux jeux de simulation, et le
préfixe reste un marqueur non ambigu.

Reproductibilité
----------------
Tout est tiré d'un ``random.Random(graine)`` : à graine, bornes de dates et
volume identiques, le jeu généré est rigoureusement le même.

Les lignes de simulation portent ``source_donnee = 'MANUEL'`` : le job de
synchronisation ERP, qui ne touche que les lignes ``'ERP'``, ne peut ni les
désactiver ni les écraser.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import logging
import random
import re
import types
import uuid

from common import (
    configure_logging,
    entier_parametre,
    identite_connexion,
    job_dbutils,
    json_metrics,
    lakebase_connection,
    lire_parametres,
    resoudre_endpoint,
)
from databricks.sdk import WorkspaceClient

logger = logging.getLogger("bl.jobs.simulation")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PREFIXE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-$")

# Namespace UUID figé : deux exécutions de même graine produisent les mêmes id.
NAMESPACE = uuid.UUID("6f1b0d64-6a5f-5b1e-9a1e-2c9d3f5a7b11")

PARAMETRES = [
    ("action", "generer"),            # generer | supprimer
    ("nb_bl", "5000"),
    ("date_min", ""),                 # AAAA-MM-JJ ; défaut : il y a 18 mois
    ("date_max", ""),                 # AAAA-MM-JJ ; défaut : hier
    ("graine", "20260727"),
    ("prefixe", "SIM-"),
    ("pages_par_bl", "1"),            # 0 = aucune image (génération plus rapide)
    ("remplacer", "false"),           # true = supprimer le jeu existant d'abord
    ("pg_host", ""),
    ("pg_database", "databricks_postgres"),
    ("pg_schema", "bl_demat"),
    ("lakebase_endpoint", ""),
    ("pg_user", ""),
]

# --- Matière première du tirage (variabilité des libellés) ------------------
_RAISONS = [
    "ACIERS DE L'OUEST", "ALPHA COMPOSANTS", "BRETON PLASTURGIE", "CABLERIE DU RHONE",
    "DELTA MECANIQUE", "ELECTRO SYSTEMES", "FONDERIE ARDENNAISE", "GARNIER OUTILLAGE",
    "HYDRO PNEUMATIQUE", "INDUSTRIE DU NORD", "JOINTS ET ETANCHEITE", "KLEIN PRECISION",
    "LOIRE USINAGE", "METALLURGIE PROVENCE", "NORMANDIE FORGE", "OPTIQUE INDUSTRIELLE",
    "PIECES AUTO EXPRESS", "QUALIPLAST", "ROULEMENTS DE FRANCE", "SIDERURGIE VOSGES",
    "TRANSMISSIONS SUD", "USINAGE GRAND EST", "VISSERIE CENTRALE", "WAGNER THERMIQUE",
    "XYLO EMBALLAGES", "YONNE CAOUTCHOUC", "ZINGUERIE MODERNE", "ATELIERS DU MAINE",
    "BOURGOGNE TRAITEMENT", "CENTRE PRESSE OUTILS",
]
_FORMES = ["SAS", "SARL", "SA", "SASU", "GROUPE", "INDUSTRIE"]
_VILLES = [
    ("Rue de l'Industrie", "44800", "SAINT-HERBLAIN"),
    ("Avenue des Forges", "42000", "SAINT-ETIENNE"),
    ("Zone de la Plaine", "59810", "LESQUIN"),
    ("Route de Lyon", "69800", "SAINT-PRIEST"),
    ("Boulevard Mercier", "76600", "LE HAVRE"),
    ("Chemin des Ateliers", "31200", "TOULOUSE"),
    ("Rue Gustave Eiffel", "67400", "ILLKIRCH"),
    ("Parc de la Sarre", "57200", "SARREGUEMINES"),
    ("Allée du Fret", "13400", "AUBAGNE"),
    ("Rue des Frenes", "25000", "BESANCON"),
]
_PRENOMS = ["Claire", "Julien", "Sofia", "Marc", "Awa", "Thomas", "Ines", "Paul",
            "Leila", "Antoine", "Camille", "Hugo"]
_NOMS = ["Bernard", "Moreau", "Dubois", "Lefevre", "Garnier", "Perrin",
         "Rousseau", "Fontaine", "Mercier", "Blanchard", "Gauthier", "Roy"]
_JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi",
          "Lundi, Mercredi", "Mardi, Jeudi", "Lundi, Mercredi, Vendredi"]
_FREQUENCES = ["Hebdomadaire", "Bi-hebdomadaire", "Quotidienne", "Mensuelle"]
_COMMENTAIRES = [
    "", "", "", "", "",
    "Palette filmee endommagee a l'arrivee",
    "Chauffeur en retard, dechargement decale",
    "Colis manquant sur la ligne 3",
    "Emballage humide, reserve emise",
    "Livraison partielle, reliquat annonce",
    "Controle qualite a prevoir",
    "Bon signe sans reserve",
]
# Plages horaires : mêmes valeurs que repository.PLAGES_HORAIRES, pondérées
# pour reproduire une journée logistique réaliste (pic 08h-12h et 14h-16h).
_PLAGES = ["00h-06h", "06h-08h", "08h-10h", "10h-12h", "12h-14h",
           "14h-16h", "16h-18h", "18h-20h", "20h-00h"]
_POIDS_PLAGES = [1, 6, 18, 20, 8, 17, 14, 6, 2]
_TYPES_OP = ["RECEPTION", "EXPEDITION", "ARCHIVAGE_RECEPTION", "ARCHIVAGE_EXPEDITION"]
_POIDS_TYPES = [46, 24, 18, 12]


def parametres():
    valeurs = lire_parametres(job_dbutils(), PARAMETRES)
    if not IDENTIFIER.fullmatch(valeurs["pg_schema"]):
        raise ValueError(f"pg_schema invalide ({valeurs['pg_schema']!r}).")
    if not valeurs["pg_host"]:
        raise ValueError("Le paramètre pg_host est obligatoire.")
    action = valeurs["action"].strip().lower()
    if action not in ("generer", "supprimer"):
        raise ValueError("Le paramètre action doit valoir « generer » ou « supprimer ».")
    prefixe = valeurs["prefixe"].strip().upper()
    if not PREFIXE_RE.fullmatch(prefixe):
        raise ValueError(
            "Le paramètre prefixe doit être en majuscules et se terminer par « - » "
            "(ex. « SIM- »), pour rester un marqueur non ambigu.")
    aujourdhui = datetime.date.today()
    date_min = _date(valeurs["date_min"], aujourdhui - datetime.timedelta(days=548))
    date_max = _date(valeurs["date_max"], aujourdhui - datetime.timedelta(days=1))
    if date_min > date_max:
        raise ValueError("date_min doit être antérieure ou égale à date_max.")
    return types.SimpleNamespace(
        action=action,
        nb_bl=entier_parametre(valeurs["nb_bl"], "nb_bl", 1, 200_000),
        date_min=date_min,
        date_max=date_max,
        graine=entier_parametre(valeurs["graine"], "graine", 0, 2**31 - 1),
        prefixe=prefixe,
        pages_par_bl=entier_parametre(valeurs["pages_par_bl"], "pages_par_bl", 0, 5),
        remplacer=valeurs["remplacer"].strip().lower() == "true",
        pg_host=valeurs["pg_host"],
        pg_database=valeurs["pg_database"],
        pg_schema=valeurs["pg_schema"],
        lakebase_endpoint=valeurs["lakebase_endpoint"],
        pg_user=valeurs["pg_user"],
    )


def _date(texte: str, defaut: datetime.date) -> datetime.date:
    texte = (texte or "").strip()
    if not texte:
        return defaut
    try:
        return datetime.date.fromisoformat(texte)
    except ValueError as exc:
        raise ValueError(f"Date invalide : {texte!r} (format attendu AAAA-MM-JJ).") from exc


def _id(prefixe: str, *parties) -> str:
    """Identifiant déterministe et préfixé (donc supprimable par LIKE)."""
    return prefixe + str(uuid.uuid5(NAMESPACE, "|".join(map(str, parties))))


# ---------------------------------------------------------------------------
# Images de page : un petit JPEG lisible, réutilisé par variantes
# ---------------------------------------------------------------------------
# JPEG 1x1 valide, utilisé si Pillow n'est pas disponible dans l'environnement.
_JPEG_MINIMAL = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffc2000b08000100"
    "0101011100ffc40014000100000000000000000000000000000009ffda0008010100"
    "00000010ffd9"
)


def _images_modeles(nb: int = 12) -> list[bytes]:
    """Quelques visuels « page de BL » distincts, réutilisés en rotation."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        logger.warning("Pillow indisponible : pages remplacées par un JPEG minimal.")
        return [_JPEG_MINIMAL]
    modeles = []
    for index in range(nb):
        image = Image.new("RGB", (827, 1169), "white")
        dessin = ImageDraw.Draw(image)
        dessin.rectangle((40, 40, 787, 150), outline=(15, 98, 166), width=3)
        dessin.text((60, 70), "BORDEREAU DE LIVRAISON (SIMULATION)", fill=(15, 98, 166))
        dessin.text((60, 105), f"Modele de page n° {index + 1}", fill=(91, 107, 124))
        for ligne in range(14):
            y = 200 + ligne * 60
            dessin.line((60, y, 767, y), fill=(200, 208, 216), width=2)
            dessin.text((70, y - 22), f"Reference article {index}-{ligne:02d}"
                                      f"    Qte {(index * 7 + ligne * 3) % 90 + 1}",
                        fill=(60, 70, 80))
        tampon = io.BytesIO()
        image.save(tampon, format="JPEG", quality=60, optimize=True)
        modeles.append(tampon.getvalue())
    return modeles


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------
# Ordre imposé par les clés étrangères : les enfants avant les parents.
# `pieces_jointes_bl.id_bl` est en ON DELETE RESTRICT -> pages avant BL ;
# `suivi_bl.nom_fournisseur` -> BL avant tiers.
SUPPRESSIONS = [
    ("pieces_jointes_bl", "id_bl LIKE %(p)s"),
    ("audit_bl", "id_bl LIKE %(p)s"),
    ("suivi_bl", "id_bl LIKE %(p)s"),
    ("base_desadv", "numero_bl LIKE %(p)s"),
    ("notifications", "event_key LIKE %(p)s"),
    ("qualite_extraction", "numero_bl LIKE %(p)s"),
    ("portefeuilles", "code_gestionnaire LIKE %(p)s OR nom_fournisseur LIKE %(p)s"),
    ("pla", "nom_fournisseur LIKE %(p)s"),
    ("sites_logistiques", "entite LIKE %(p)s OR adresse LIKE %(p)s"),
    ("adresses", "adresse LIKE %(p)s"),
    ("gestionnaires", "code_gestionnaire LIKE %(p)s"),
    ("base_tiers", "name LIKE %(p)s"),
]


def supprimer(cursor, schema: str, prefixe: str) -> dict:
    """Efface tout le jeu de simulation. Idempotent."""
    motif = prefixe + "%"
    supprimees = {}
    for table, condition in SUPPRESSIONS:
        cursor.execute(f"DELETE FROM {schema}.{table} WHERE {condition}", {"p": motif})
        if cursor.rowcount:
            supprimees[table] = cursor.rowcount
    return supprimees


def compter_existant(cursor, schema: str, prefixe: str) -> int:
    cursor.execute(
        f"SELECT count(*) FROM {schema}.suivi_bl WHERE id_bl LIKE %(p)s",
        {"p": prefixe + "%"})
    return int(cursor.fetchone()[0])


# ---------------------------------------------------------------------------
# Génération
# ---------------------------------------------------------------------------
def _lots(sequence, taille: int = 1000):
    for debut in range(0, len(sequence), taille):
        yield sequence[debut:debut + taille]


def generer(cursor, args) -> dict:
    alea = random.Random(args.graine)
    schema, prefixe = args.pg_schema, args.prefixe

    # --- Quais : on réutilise le référentiel réel (aucune ligne SIM créée). --
    cursor.execute(f"SELECT code_quai FROM {schema}.quais WHERE actif = true "
                   "ORDER BY code_quai")
    quais = [ligne[0] for ligne in cursor.fetchall()]
    if not quais:
        raise RuntimeError(
            "Aucun quai actif dans le référentiel : exécutez d'abord la "
            "migration V001 (qui insère B15, B06EST, …).")

    # --- Adresses ---------------------------------------------------------
    adresses = [f"{prefixe}{rue} — {cp} {ville}" for rue, cp, ville in _VILLES]
    cursor.executemany(
        f"INSERT INTO {schema}.adresses (adresse) VALUES (%s) ON CONFLICT DO NOTHING",
        [(adresse,) for adresse in adresses])

    # --- Tiers : fournisseurs (achat) et clients (vente) ------------------
    fournisseurs, clients = [], []
    for index, raison in enumerate(_RAISONS):
        forme = _FORMES[index % len(_FORMES)]
        fournisseurs.append(f"{prefixe}S-{900001 + index} : {raison} {forme}")
    for index, raison in enumerate(reversed(_RAISONS)):
        forme = _FORMES[(index + 3) % len(_FORMES)]
        clients.append(f"{prefixe}C-{900001 + index} : {raison} {forme}")
    cursor.executemany(
        f"INSERT INTO {schema}.base_tiers (name, type_tiers, source_donnee, actif) "
        "VALUES (%s, %s, 'MANUEL', true) ON CONFLICT (name) DO NOTHING",
        [(nom, "FOURNISSEUR") for nom in fournisseurs]
        + [(nom, "CLIENT") for nom in clients])

    # --- Sites logistiques : 1 à 2 adresses par tiers ---------------------
    sites = []
    for tiers in fournisseurs + clients:
        for adresse in alea.sample(adresses, alea.choice([1, 1, 2])):
            sites.append((tiers, adresse))
    cursor.executemany(
        f"INSERT INTO {schema}.sites_logistiques (entite, adresse) "
        "VALUES (%s, %s) ON CONFLICT DO NOTHING", sites)

    # --- Gestionnaires + portefeuilles ------------------------------------
    gestionnaires = []
    for index in range(8):
        prenom = _PRENOMS[index % len(_PRENOMS)]
        nom = _NOMS[index % len(_NOMS)]
        gestionnaires.append((
            f"{prefixe}G{index + 1:02d}",
            f"{prenom} {nom}",
            f"{prenom.lower()}.{nom.lower()}@simulation.invalid",
        ))
    cursor.executemany(
        f"INSERT INTO {schema}.gestionnaires "
        "(code_gestionnaire, nom_affichage, email, actif) "
        "VALUES (%s, %s, %s, true) ON CONFLICT (code_gestionnaire) DO NOTHING",
        gestionnaires)
    # Chaque fournisseur est couvert par 1 ou 2 gestionnaires (portefeuille).
    portefeuilles = []
    for tiers in fournisseurs:
        for code, _nom, _mail in alea.sample(gestionnaires, alea.choice([1, 1, 2])):
            portefeuilles.append((code, tiers))
    cursor.executemany(
        f"INSERT INTO {schema}.portefeuilles (code_gestionnaire, nom_fournisseur) "
        "VALUES (%s, %s) ON CONFLICT DO NOTHING", portefeuilles)

    # --- PLA : protocole logistique de ~70 % des fournisseurs -------------
    pla = [
        (tiers, alea.choice(quais), alea.choice(_JOURS), alea.choice(_FREQUENCES))
        for tiers in fournisseurs if alea.random() < 0.7
    ]
    cursor.executemany(
        f"INSERT INTO {schema}.pla "
        "(nom_fournisseur, code_quai, jours_livraison, frequence_livraison) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (nom_fournisseur) DO NOTHING", pla)
    quai_pla = {ligne[0]: ligne[1] for ligne in pla}

    # --- BL + lignes liées -------------------------------------------------
    modeles = _images_modeles() if args.pages_par_bl else []
    jours = (args.date_max - args.date_min).days
    utilisateurs = [f"{p.lower()}.{n.lower()}@simulation.invalid"
                    for p, n in zip(_PRENOMS, _NOMS)]

    bls, pages, desadv, audits, notifs, qualites = [], [], [], [], [], []
    compteurs = {"reception": 0, "expedition": 0, "archivage": 0,
                 "supprimes": 0, "brouillons": 0, "edi_nok": 0, "desadv": 0}

    for index in range(args.nb_bl):
        type_op = alea.choices(_TYPES_OP, weights=_POIDS_TYPES, k=1)[0]
        vente = type_op in ("EXPEDITION", "ARCHIVAGE_EXPEDITION")
        archivage = type_op.startswith("ARCHIVAGE")
        sens = "VENTE" if vente else "ACHAT"

        # Date : tirage uniforme, puis report des week-ends sur le vendredi
        # (une plateforme logistique ne reçoit quasiment pas le samedi).
        date_op = args.date_min + datetime.timedelta(days=alea.randint(0, jours))
        if date_op.weekday() >= 5 and alea.random() < 0.85:
            date_op -= datetime.timedelta(days=date_op.weekday() - 4)

        tiers = alea.choice(clients if vente else fournisseurs)
        numero = f"{prefixe}{'V' if vente else 'A'}-{index + 1:06d}"
        id_bl = _id(prefixe, "bl", index)

        plage = None if archivage else alea.choices(_PLAGES, weights=_POIDS_PLAGES, k=1)[0]
        quai = None if archivage else (quai_pla.get(tiers) or alea.choice(quais))
        # État EDI : uniquement porté par les nouvelles réceptions.
        statut = "1"
        if type_op == "RECEPTION" and alea.random() < 0.16:
            statut = "0"
            compteurs["edi_nok"] += 1
        commentaire = "" if archivage else alea.choice(_COMMENTAIRES)

        # Cycle de vie : très majoritairement COMPLET, quelques anomalies.
        tirage = alea.random()
        if tirage < 0.965:
            doc_statut = "COMPLET"
        elif tirage < 0.985:
            doc_statut = "BROUILLON"
            compteurs["brouillons"] += 1
        else:
            doc_statut = "ERREUR"

        saisie_par = alea.choice(utilisateurs)
        saisie_le = datetime.datetime.combine(
            date_op, datetime.time(alea.randint(6, 19), alea.randint(0, 59)))
        modifie_par = modifie_le = None
        if alea.random() < 0.22:
            modifie_par = alea.choice(utilisateurs)
            modifie_le = saisie_le + datetime.timedelta(hours=alea.randint(1, 72))
        # Suppression logique : les trois colonnes vont ensemble (contrainte CHECK).
        supprime = doc_statut == "COMPLET" and alea.random() < 0.018
        if supprime:
            compteurs["supprimes"] += 1

        bls.append((
            id_bl, numero, date_op, plage, tiers, quai, statut, commentaire,
            saisie_par, saisie_le, modifie_par, modifie_le, type_op, doc_statut,
            supprime,
            alea.choice(utilisateurs) if supprime else None,
            saisie_le + datetime.timedelta(days=alea.randint(1, 30)) if supprime else None,
        ))
        compteurs["archivage" if archivage
                  else ("expedition" if vente else "reception")] += 1

        # Pages scannées (BYTEA), sauf brouillons interrompus.
        if args.pages_par_bl and doc_statut != "BROUILLON":
            for page in range(args.pages_par_bl):
                contenu = modeles[(index + page) % len(modeles)]
                pages.append((
                    _id(prefixe, "photo", index, page), id_bl, contenu,
                    hashlib.sha256(contenu).hexdigest(), len(contenu), page,
                ))

        # DESADV : présent pour ~82 % des BL complets -> rapprochement réaliste.
        if doc_statut == "COMPLET" and alea.random() < 0.82:
            # 4 % de DESADV portant un tiers différent : alimente les écarts.
            tiers_desadv = (alea.choice(clients if vente else fournisseurs)
                            if alea.random() < 0.04 else tiers)
            emission = datetime.datetime.combine(
                date_op - datetime.timedelta(days=alea.randint(0, 2)),
                datetime.time(alea.randint(5, 22), alea.randint(0, 59)))
            desadv.append((
                numero, tiers_desadv, sens, emission, date_op,
                "EDI NOK" if alea.random() < 0.12 else "OK",
            ))
            compteurs["desadv"] += 1

        audits.append((id_bl, "CREATION", numero, saisie_par, saisie_le))
        if doc_statut == "COMPLET":
            audits.append((id_bl, "FINALISATION", str(args.pages_par_bl),
                           saisie_par, saisie_le))

        # Notification Teams : une par nouvelle réception (comme l'app).
        if type_op == "RECEPTION" and doc_statut == "COMPLET":
            envoyee = alea.random() < 0.94
            notifs.append((
                _id(prefixe, "notif", index), "NOUVELLE_RECEPTION", numero,
                f"Nouvelle réception — BL {numero} ({tiers})",
                alea.choice(gestionnaires)[1], envoyee,
                saisie_le if envoyee else None,
                None if envoyee else "HTTP 504 : Gateway Timeout",
                saisie_le, saisie_par,
            ))

        # Qualité de l'extraction IA : 2 mesures pour ~30 % des BL.
        if alea.random() < 0.30:
            for champ, precision in (("numero_bl", 0.93), ("tiers", 0.87)):
                identique = alea.random() < precision
                qualites.append((
                    saisie_le, saisie_par, numero, champ,
                    numero if identique else numero.replace("0", "O", 1),
                    numero, identique, "databricks-claude-opus-4-8", "2026-07-01",
                    round(alea.uniform(0.62, 0.99), 4),
                ))

    # --- Écritures par lots ------------------------------------------------
    for lot in _lots(bls):
        cursor.executemany(
            f"INSERT INTO {schema}.suivi_bl "
            "(id_bl, numero_bl, date_reception, plage_horaire, nom_fournisseur, "
            " quai_reception, statut_bl, comment_bl, saisie_par, saisie_le, "
            " modifie_par, modifie_le, type_operation, document_statut, "
            " est_supprime, supprime_par, supprime_le, source_donnee, version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "        %s, %s, %s, 'MANUEL', 1) ON CONFLICT (id_bl) DO NOTHING", lot)
    for lot in _lots(pages, 200):        # BYTEA : lots plus petits
        cursor.executemany(
            f"INSERT INTO {schema}.pieces_jointes_bl "
            "(id_photo, id_bl, contenu, sha256, taille_octets, content_type, index_page) "
            "VALUES (%s, %s, %s, %s, %s, 'image/jpeg', %s) "
            "ON CONFLICT (id_bl, index_page) DO NOTHING", lot)
    for lot in _lots(desadv):
        cursor.executemany(
            f"INSERT INTO {schema}.base_desadv "
            "(numero_bl, nom_fournisseur, sens, issuedatetime, integrationdate, "
            " statut_edi, source_donnee, actif, last_seen_at, version) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'MANUEL', true, now(), 1) "
            "ON CONFLICT (numero_bl, sens) DO NOTHING", lot)
    for lot in _lots(audits):
        cursor.executemany(
            f"INSERT INTO {schema}.audit_bl "
            "(id_bl, evenement, valeur_apres, modifie_par, modifie_le) "
            "VALUES (%s, %s, %s, %s, %s)", lot)
    for lot in _lots(notifs):
        cursor.executemany(
            f"INSERT INTO {schema}.notifications "
            "(event_key, type_notif, numero_bl, message, destinataires, envoyee, "
            " envoyee_le, erreur_envoi, cree_le, cree_par) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (event_key) DO NOTHING", lot)
    for lot in _lots(qualites):
        cursor.executemany(
            f"INSERT INTO {schema}.qualite_extraction "
            "(cree_le, utilisateur, numero_bl, champ, valeur_ia, valeur_validee, "
            " identique, modele_endpoint, prompt_version, score_confiance) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", lot)

    # --- Rapprochement BL <-> DESADV : même règle que le job de synchro ----
    cursor.execute(
        f"""
        UPDATE {schema}.suivi_bl b
        SET desadv_rapproche = true, desadv_rapproche_le = b.saisie_le
        WHERE b.id_bl LIKE %(p)s
          AND b.document_statut = 'COMPLET'
          AND b.est_supprime = false
          AND EXISTS (
            SELECT 1 FROM {schema}.base_desadv d
            WHERE upper(d.numero_bl) = upper(b.numero_bl)
              AND d.sens = b.sens AND d.actif = true
          )
        """,
        {"p": prefixe + "%"})
    compteurs["rapproches"] = cursor.rowcount
    compteurs.update({"bl": len(bls), "pages": len(pages), "tiers":
                      len(fournisseurs) + len(clients),
                      "gestionnaires": len(gestionnaires),
                      "notifications": len(notifs), "mesures_ia": len(qualites)})
    return compteurs


# ---------------------------------------------------------------------------
def main() -> None:
    configure_logging()
    args = parametres()
    workspace = WorkspaceClient()
    endpoint = resoudre_endpoint(workspace, args.pg_host, args.lakebase_endpoint)

    with lakebase_connection(
        workspace,
        endpoint=endpoint,
        host=args.pg_host,
        database=args.pg_database,
        user=args.pg_user or identite_connexion(workspace),
    ) as connection:
        with connection.transaction(), connection.cursor() as cursor:
            if args.action == "supprimer":
                supprimees = supprimer(cursor, args.pg_schema, args.prefixe)
                logger.info("Jeu de simulation supprimé : %s", json_metrics(**supprimees))
                return

            existant = compter_existant(cursor, args.pg_schema, args.prefixe)
            if existant and not args.remplacer:
                raise RuntimeError(
                    f"{existant} BL portant le préfixe « {args.prefixe} » sont déjà "
                    "en base. Relancez avec action=supprimer, ou remplacer=true "
                    "pour régénérer le jeu.")
            if existant:
                supprimer(cursor, args.pg_schema, args.prefixe)
                logger.info("Jeu de simulation précédent supprimé (%d BL).", existant)

            compteurs = generer(cursor, args)
            logger.info("Jeu de simulation généré (%s → %s) : %s",
                        args.date_min, args.date_max, json_metrics(**compteurs))


if __name__ == "__main__":
    main()
