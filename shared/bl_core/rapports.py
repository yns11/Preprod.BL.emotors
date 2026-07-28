"""Rapports d'activité périodiques : agrégats, analyse IA et rendu PDF.

Cinq périodicités — journalière, hebdomadaire, mensuelle, trimestrielle,
annuelle — partagent le même pipeline :

    bornes()  ->  collecter()  ->  commentaire_ia()  ->  generer_pdf()

`collecter` ne fait que lire et agréger (aucun effet de bord), `generer_pdf`
ne fait que mettre en forme : chacune est testable seule. L'appel au modèle est
**best effort** — un endpoint indisponible produit un rapport complet, sans la
section d'analyse.

Le PDF est produit avec fpdf2, sans dépendance graphique : les histogrammes
sont dessinés en primitives vectorielles (rectangles), ce qui garantit un
rendu net à l'impression et un fichier léger.
"""

from __future__ import annotations

import calendar
import contextlib
import datetime
import json
import logging

import pandas as pd
from fpdf import FPDF
from fpdf.enums import Align, XPos, YPos

from . import llm, repository

logger = logging.getLogger("bl.rapports")

QUOTIDIEN = "QUOTIDIEN"
HEBDOMADAIRE = "HEBDOMADAIRE"
MENSUEL = "MENSUEL"
TRIMESTRIEL = "TRIMESTRIEL"
ANNUEL = "ANNUEL"
PERIODICITES = [QUOTIDIEN, HEBDOMADAIRE, MENSUEL, TRIMESTRIEL, ANNUEL]
LIBELLES_PERIODICITE = {
    QUOTIDIEN: "Journalier",
    HEBDOMADAIRE: "Hebdomadaire",
    MENSUEL: "Mensuel",
    TRIMESTRIEL: "Trimestriel",
    ANNUEL: "Annuel",
}

_MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
         "août", "septembre", "octobre", "novembre", "décembre"]

# Palette : identique à celle de l'app (cohérence visuelle papier/écran).
_BLEU = (15, 98, 166)
_ENCRE = (27, 42, 58)
_GRIS = (91, 107, 124)
_GRIS_CLAIR = (228, 233, 238)
_VERT = (67, 176, 42)
_ROUGE = (198, 40, 40)
_AMBRE = (216, 143, 20)


# ---------------------------------------------------------------------------
# Bornes de période
# ---------------------------------------------------------------------------
def bornes(periodicite: str, reference: datetime.date) -> tuple:
    """(début, fin, libellé) de la période **contenant** `reference`.

    Les bornes sont inclusives. La semaine est ISO (lundi → dimanche), le
    trimestre civil, l'année civile."""
    if periodicite == QUOTIDIEN:
        return reference, reference, f"Journée du {reference:%d/%m/%Y}"
    if periodicite == HEBDOMADAIRE:
        debut = reference - datetime.timedelta(days=reference.weekday())
        fin = debut + datetime.timedelta(days=6)
        return debut, fin, (f"Semaine {debut.isocalendar().week} "
                            f"({debut:%d/%m} → {fin:%d/%m/%Y})")
    if periodicite == MENSUEL:
        debut = reference.replace(day=1)
        fin = debut.replace(day=calendar.monthrange(debut.year, debut.month)[1])
        return debut, fin, f"{_MOIS[debut.month - 1].capitalize()} {debut.year}"
    if periodicite == TRIMESTRIEL:
        trimestre = (reference.month - 1) // 3
        debut = reference.replace(month=trimestre * 3 + 1, day=1)
        dernier_mois = debut.replace(month=debut.month + 2)
        fin = dernier_mois.replace(
            day=calendar.monthrange(dernier_mois.year, dernier_mois.month)[1])
        return debut, fin, f"T{trimestre + 1} {debut.year}"
    if periodicite == ANNUEL:
        debut = reference.replace(month=1, day=1)
        return debut, reference.replace(month=12, day=31), f"Année {debut.year}"
    raise ValueError(f"Périodicité inconnue : {periodicite!r}")


def periode_precedente(periodicite: str, debut: datetime.date) -> tuple:
    """Bornes de la période immédiatement antérieure (base de comparaison)."""
    return bornes(periodicite, debut - datetime.timedelta(days=1))[:2]


def periodes_echues(aujourdhui: datetime.date) -> list[tuple]:
    """Périodes **closes** dont le rapport doit exister à cette date.

    Appelée quotidiennement par le job : la veille est toujours échue ; la
    semaine, le mois, le trimestre et l'année ne le sont que le premier jour
    qui suit leur clôture. Renvoie [(périodicité, début, fin, libellé)]."""
    hier = aujourdhui - datetime.timedelta(days=1)
    echues = [(QUOTIDIEN, *bornes(QUOTIDIEN, hier))]
    for periodicite in (HEBDOMADAIRE, MENSUEL, TRIMESTRIEL, ANNUEL):
        debut, fin, libelle = bornes(periodicite, hier)
        # La période d'hier est close si aujourd'hui n'en fait plus partie.
        if bornes(periodicite, aujourdhui)[0] != debut:
            echues.append((periodicite, debut, fin, libelle))
    return echues


# ---------------------------------------------------------------------------
# Collecte des indicateurs
# ---------------------------------------------------------------------------
def _pourcent(numerateur, denominateur) -> float:
    return round(100.0 * numerateur / denominateur, 1) if denominateur else 0.0


def _variation(courant, precedent) -> float | None:
    """Évolution en % ; None si la période précédente est vide (division
    impossible — un « +∞ % » n'a pas de sens dans un rapport)."""
    if not precedent:
        return None
    return round(100.0 * (courant - precedent) / precedent, 1)


def _agreger(bl: pd.DataFrame) -> dict:
    """Indicateurs d'un jeu de BL déjà filtré sur la période."""
    if bl.empty:
        return {"total": 0, "receptions": 0, "expeditions": 0, "archivages": 0,
                "edi_nok": 0, "taux_nok": 0.0, "rapproches": 0, "taux_rapproches": 0.0,
                "supprimes": 0, "brouillons": 0, "erreurs": 0, "delai_moyen_h": 0.0}
    vivants = bl[~bl["est_supprime"].fillna(False).astype(bool)]
    complets = vivants[vivants["document_statut"] == "COMPLET"]
    receptions = complets[complets["type_operation"] == repository.TYPE_RECEPTION]
    nok = int((receptions["statut_bl"] == repository.STATUT_EDI_NOK).sum())
    rapproches = int(complets["desadv_rapproche"].fillna(False).astype(bool).sum())
    # Délai entre la date d'opération et la saisie : mesure la fraîcheur.
    delais = (pd.to_datetime(complets["saisie_le"], utc=True, errors="coerce")
              .dt.tz_localize(None)
              - pd.to_datetime(complets["date_reception"], errors="coerce"))
    return {
        "total": int(len(complets)),
        "receptions": int(len(receptions)),
        "expeditions": int((complets["type_operation"]
                            == repository.TYPE_EXPEDITION).sum()),
        "archivages": int(complets["type_operation"]
                          .isin([repository.TYPE_ARCHIVAGE_RECEPTION,
                                 repository.TYPE_ARCHIVAGE_EXPEDITION]).sum()),
        "edi_nok": nok,
        "taux_nok": _pourcent(nok, len(receptions)),
        "rapproches": rapproches,
        "taux_rapproches": _pourcent(rapproches, len(complets)),
        "supprimes": int(bl["est_supprime"].fillna(False).astype(bool).sum()),
        "brouillons": int((vivants["document_statut"] == "BROUILLON").sum()),
        "erreurs": int((vivants["document_statut"] == "ERREUR").sum()),
        "delai_moyen_h": round(float(delais.dt.total_seconds().mean() / 3600), 1)
                         if len(delais.dropna()) else 0.0,
    }


def collecter(periodicite: str, debut: datetime.date, fin: datetime.date,
              libelle: str) -> dict:
    """Tous les indicateurs de la période, plus la comparaison à la précédente.

    Lecture seule ; renvoie une structure sérialisable en JSON (stockée telle
    quelle dans `rapports_activite.metriques`)."""
    bl = repository.bl_periode(debut, fin)
    prec_debut, prec_fin = periode_precedente(periodicite, debut)
    precedent = _agreger(repository.bl_periode(prec_debut, prec_fin))
    courant = _agreger(bl)

    vivants = bl[~bl["est_supprime"].fillna(False).astype(bool)] if not bl.empty \
        else bl
    complets = (vivants[vivants["document_statut"] == "COMPLET"]
                if not vivants.empty else vivants)

    metriques = {
        "periodicite": periodicite,
        "libelle": libelle,
        "debut": debut.isoformat(),
        "fin": fin.isoformat(),
        "jours": (fin - debut).days + 1,
        "courant": courant,
        "precedent": precedent,
        "precedent_libelle": bornes(periodicite, prec_debut)[2],
        "variations": {
            cle: _variation(courant[cle], precedent[cle])
            for cle in ("total", "receptions", "expeditions", "archivages", "edi_nok")
        },
    }

    # --- Répartitions -----------------------------------------------------
    if complets.empty:
        metriques.update({"par_jour": [], "top_tiers": [], "par_quai": [],
                          "par_plage": [], "par_utilisateur": []})
    else:
        par_jour = (complets.groupby(complets["date_reception"].astype(str))
                    .size().sort_index())
        metriques["par_jour"] = [{"date": d, "bl": int(n)}
                                 for d, n in par_jour.items()]

        # Top tiers : volume et qualité EDI, trié par volume décroissant.
        # `part_nok` = part du tiers dans le TOTAL des EDI NOK de la période,
        # et non son taux d'échec propre : c'est la mesure qui répond à
        # « où se concentre le problème ? », donc celle qui priorise l'action.
        # Un tiers à 100 % d'échec sur 2 réceptions pèse moins qu'un tiers à
        # 10 % sur 400.
        total_nok = courant["edi_nok"]
        groupes = complets.groupby("nom_fournisseur")
        top = []
        for nom, sous in groupes:
            receptions = sous[sous["type_operation"] == repository.TYPE_RECEPTION]
            nok = int((receptions["statut_bl"] == repository.STATUT_EDI_NOK).sum())
            top.append({
                "tiers": str(nom),
                "bl": int(len(sous)),
                "edi_nok": nok,
                "part_nok": _pourcent(nok, total_nok),
                "taux_rapproches": _pourcent(
                    int(sous["desadv_rapproche"].fillna(False).astype(bool).sum()),
                    len(sous)),
            })
        metriques["top_tiers"] = sorted(top, key=lambda x: -x["bl"])[:10]
        # Tiers à surveiller : ceux qui concentrent le plus d'EDI NOK.
        metriques["pires_tiers"] = sorted(
            [t for t in top if t["edi_nok"]],
            key=lambda x: (-x["edi_nok"], -x["bl"]))[:5]
        metriques["total_edi_nok"] = total_nok

        for cle, colonne in (("par_quai", "quai_reception"),
                             ("par_plage", "plage_horaire"),
                             ("par_utilisateur", "saisie_par")):
            serie = complets[colonne].fillna("—").replace("", "—").value_counts()
            metriques[cle] = [{"valeur": str(v), "bl": int(n)}
                              for v, n in serie.head(12).items()]
        metriques["par_plage"].sort(key=lambda x: x["valeur"])

    # --- Sources annexes : notifications et qualité IA ---------------------
    try:
        notifications = repository.notifications_periode(debut, fin)
        envoyees = int(notifications["envoyee"].fillna(False).astype(bool).sum()) \
            if not notifications.empty else 0
        metriques["notifications"] = {
            "total": int(len(notifications)),
            "envoyees": envoyees,
            "echecs": int(len(notifications)) - envoyees,
        }
    except Exception as exc:                       # table absente / non accordée
        logger.warning("Notifications indisponibles pour le rapport : %s", exc)
        metriques["notifications"] = {"total": 0, "envoyees": 0, "echecs": 0}

    try:
        qualite = repository.qualite_periode(debut, fin)
        metriques["qualite_ia"] = [
            {"champ": str(ligne["champ"]), "mesures": int(ligne["mesures"]),
             "precision": _pourcent(int(ligne["exactes"]), int(ligne["mesures"]))}
            for _, ligne in qualite.iterrows()
        ] if not qualite.empty else []
    except Exception as exc:
        logger.warning("Qualité IA indisponible pour le rapport : %s", exc)
        metriques["qualite_ia"] = []

    return metriques


# ---------------------------------------------------------------------------
# Analyse rédigée par le modèle
# ---------------------------------------------------------------------------
_CONSIGNE = """Tu es analyste des opérations logistiques d'un site industriel.
Rédige, en français, le commentaire d'analyse d'un rapport d'activité destiné
aux responsables Approvisionnements, ADV et Finance.

Structure ta réponse en EXACTEMENT trois sections, préfixées ainsi :
SYNTHESE: deux à trois phrases sur le volume et la tendance de la période.
POINTS: trois à cinq points saillants, un par ligne, commençant par « - ».
CONCLUSION: deux phrases — le risque principal et l'action recommandée.

Règles impératives :
- appuie CHAQUE affirmation sur un chiffre présent dans les données ci-dessous ;
- n'invente aucune donnée, aucun nom, aucune cause que les chiffres n'établissent
  pas ; si une variation manque de base de comparaison, dis-le ;
- pas de superlatifs ni de langue de bois, un ton factuel de note interne ;
- pas de titre, pas de markdown, pas de puces autres que « - ».

Repères métier : « EDI NOK » signale un bordereau dont l'avis d'expédition
électronique est en anomalie (à corriger par le gestionnaire) ; le « taux de
rapprochement » mesure la part des BL retrouvés dans les DESADV de l'ERP ; un
délai de saisie élevé signale un archivage tardif.

Attention au sens de `part_nok` dans les listes de tiers : c'est la part du
tiers dans le TOTAL des EDI NOK de la période, PAS son taux d'échec propre. Un
`part_nok` de 30 % signifie « ce tiers représente 30 % des anomalies », pas
« 30 % de ses BL sont en anomalie ». Formule tes commentaires en conséquence
(concentration, priorisation), sans jamais parler de taux d'échec du tiers.
Le seul taux d'échec disponible est global : `courant.taux_nok`.

DONNÉES DE LA PÉRIODE (JSON) :
"""


def commentaire_ia(metriques: dict) -> tuple[str, bool]:
    """(texte, analyse_effectuée). Best effort : si l'endpoint est absent ou
    en échec, renvoie un repli explicite plutôt que d'échouer le rapport."""
    if not llm.endpoint_configure():
        return ("Analyse automatique non configurée (BL_LLM_ENDPOINT vide) : "
                "le rapport présente les indicateurs sans commentaire rédigé.",
                False)
    # Le modèle ne reçoit que les agrégats, jamais de données nominatives de BL.
    charge = {cle: metriques[cle] for cle in (
        "libelle", "debut", "fin", "jours", "courant", "precedent",
        "precedent_libelle", "variations", "top_tiers", "pires_tiers",
        "par_quai", "par_plage", "notifications", "qualite_ia")
        if cle in metriques}
    try:
        texte = llm.completer(
            _CONSIGNE + json.dumps(charge, ensure_ascii=False, indent=1),
            max_tokens=1100)
        return texte.strip(), True
    except Exception as exc:
        logger.warning("Analyse IA du rapport indisponible : %s", exc, exc_info=True)
        return (f"Analyse automatique indisponible ({type(exc).__name__}). "
                "Les indicateurs ci-dessous restent complets et vérifiés.", False)


def decouper_analyse(texte: str) -> list[tuple[str, list[str]]]:
    """Découpe la réponse du modèle en sections (titre, lignes). Tolérant :
    un texte sans les préfixes attendus revient en une seule section."""
    titres = {"SYNTHESE": "Synthèse", "POINTS": "Points saillants",
              "CONCLUSION": "Conclusion et recommandation"}
    sections, courante, lignes = [], None, []
    for ligne in (texte or "").splitlines():
        nu = ligne.strip()
        if not nu:
            continue
        cle = next((k for k in titres if nu.upper().startswith(k)), None)
        if cle:
            if courante:
                sections.append((courante, lignes))
            courante, lignes = titres[cle], []
            reste = nu[len(cle):].lstrip(" :").strip()
            if reste:
                lignes.append(reste)
        else:
            lignes.append(nu)
    if courante:
        sections.append((courante, lignes))
    elif lignes:
        sections.append(("Analyse", lignes))
    return sections


# ---------------------------------------------------------------------------
# Rendu PDF
# ---------------------------------------------------------------------------
# Ponctuation typographique absente du latin-1 : traduite vers son équivalent
# plutôt que remplacée par « ? » (« — » et « ’ » sont fréquents en français).
_TRADUCTION = str.maketrans({
    "—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"',
    "…": "...", "€": "EUR", " ": " ", " ": " ",
})


def _latin(valeur) -> str:
    """Helvetica est encodé en latin-1 : on traduit d'abord la ponctuation
    courante, puis on remplace le résidu hors jeu par « ? » — jamais d'échec
    d'export sur une raison sociale exotique."""
    texte = "-" if valeur in (None, "") else str(valeur)
    return texte.translate(_TRADUCTION).encode("latin-1", "replace").decode("latin-1")


def _signe(variation) -> str:
    if variation is None:
        return "n.d."
    return f"{variation:+.1f} %".replace(".", ",")


class _Rapport(FPDF):
    """En-tête et pied de page communs à toutes les pages."""

    titre_courant = ""
    sous_titre = ""

    # Largeurs EXPLICITES : deux `cell(0, …)` à la suite laisseraient la
    # seconde avec une largeur nulle, ce qui fait boucler le retour à la ligne
    # de fpdf2 (et explose le nombre de pages quand l'en-tête est redessiné
    # au milieu d'un multi_cell).
    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_GRIS)
        self.cell(95, 6, _latin(self.titre_courant), align=Align.L)
        self.cell(95, 6, _latin(self.sous_titre), align=Align.R,
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*_GRIS_CLAIR)
        self.set_line_width(0.3)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_GRIS)
        self.cell(95, 6, _latin("BLDEMAT — document interne"), align=Align.L)
        self.cell(95, 6, f"{self.page_no()}/{{nb}}", align=Align.R)


def _reserver(pdf: _Rapport, hauteur: float) -> None:
    """Garantit `hauteur` mm sur la page courante, sinon passe à la suivante.

    Indispensable avant tout bloc dessiné en coordonnées ABSOLUES (cartes,
    histogrammes) : leurs `set_xy` court-circuitent le saut de page
    automatique, et une étiquette posée sous le bas de page provoquerait un
    saut par élément dessiné."""
    if pdf.get_y() + hauteur > pdf.page_break_trigger:
        pdf.add_page()


@contextlib.contextmanager
def _sans_saut_auto(pdf: _Rapport):
    """Suspend le saut de page pendant un dessin absolu (la place a déjà été
    réservée par `_reserver`)."""
    pdf.set_auto_page_break(False)
    try:
        yield
    finally:
        pdf.set_auto_page_break(auto=True, margin=16)


def _titre_section(pdf: _Rapport, texte: str, bloc: float = 24) -> None:
    """`bloc` = hauteur du titre ET du début de son contenu : un titre ne doit
    jamais rester orphelin en bas de page, séparé de son tableau ou de son
    graphique."""
    _reserver(pdf, bloc)
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*_BLEU)
    pdf.cell(0, 7, _latin(texte), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_draw_color(*_BLEU)
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), 60, pdf.get_y())
    pdf.ln(3)


def _cartes(pdf: _Rapport, cartes: list[tuple[str, str, str, tuple]]) -> None:
    """Rangée de cartes KPI : (libellé, valeur, mention, couleur de la valeur)."""
    if not cartes:
        return
    _reserver(pdf, 25)
    largeur = 190 / len(cartes)
    haut = pdf.get_y()
    with _sans_saut_auto(pdf):
        for index, (libelle, valeur, mention, couleur) in enumerate(cartes):
            x = 10 + index * largeur
            pdf.set_fill_color(248, 250, 252)
            pdf.set_draw_color(*_GRIS_CLAIR)
            pdf.rect(x + 1, haut, largeur - 2, 22, style="DF")
            pdf.set_xy(x + 4, haut + 2)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*_GRIS)
            pdf.cell(largeur - 8, 4, _latin(libelle.upper()))
            pdf.set_xy(x + 4, haut + 7)
            pdf.set_font("Helvetica", "B", 15)
            pdf.set_text_color(*couleur)
            pdf.cell(largeur - 8, 8, _latin(valeur))
            pdf.set_xy(x + 4, haut + 16)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*_GRIS)
            pdf.cell(largeur - 8, 4, _latin(mention))
    pdf.set_xy(10, haut + 25)


def _tableau(pdf: _Rapport, colonnes: list[tuple[str, float, str]],
             lignes: list[list], vide: str = "Aucune donnée") -> None:
    """Tableau simple : colonnes = (titre, largeur mm, alignement L/R/C)."""
    if not lignes:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*_GRIS)
        pdf.cell(0, 6, _latin(vide), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        return
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_fill_color(*_BLEU)
    pdf.set_text_color(255, 255, 255)
    for titre, largeur, align in colonnes:
        pdf.cell(largeur, 7, _latin(titre), align=align, fill=True)
    pdf.ln()
    pdf.set_font("Helvetica", "", 8.5)
    for index, ligne in enumerate(lignes):
        if pdf.get_y() > 258:                      # place pour l'en-tête suivant
            pdf.add_page()
            pdf.set_font("Helvetica", "B", 8.5)
            pdf.set_fill_color(*_BLEU)
            pdf.set_text_color(255, 255, 255)
            for titre, largeur, align in colonnes:
                pdf.cell(largeur, 7, _latin(titre), align=align, fill=True)
            pdf.ln()
            pdf.set_font("Helvetica", "", 8.5)
        pdf.set_fill_color(246, 248, 250)
        pdf.set_text_color(*_ENCRE)
        for (_titre, largeur, align), valeur in zip(colonnes, ligne):
            pdf.cell(largeur, 6, _latin(valeur), align=align, fill=index % 2 == 0)
        pdf.ln()
    pdf.ln(2)


def _histogramme(pdf: _Rapport, valeurs: list[tuple[str, float]],
                 hauteur: float = 34, couleur: tuple = _BLEU) -> None:
    """Histogramme vertical dessiné en primitives (aucune dépendance
    graphique) : lisible à l'impression et fichier léger."""
    if not valeurs:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*_GRIS)
        pdf.cell(0, 6, _latin("Aucune donnée sur la période."),
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        return
    _reserver(pdf, hauteur + 8)
    maxi = max(v for _, v in valeurs) or 1
    haut = pdf.get_y()
    # Séries courtes : on borne le pas et on centre, sinon deux barres
    # s'étaleraient sur toute la largeur de la page.
    pas = min(190 / len(valeurs), 26)
    marge = 10 + (190 - pas * len(valeurs)) / 2
    barre = min(pas * 0.68, 12)
    with _sans_saut_auto(pdf):
        pdf.set_draw_color(*_GRIS_CLAIR)
        pdf.set_line_width(0.3)
        pdf.line(marge, haut + hauteur, marge + pas * len(valeurs), haut + hauteur)
        pdf.set_fill_color(*couleur)
        for index, (_etiquette, valeur) in enumerate(valeurs):
            h = (valeur / maxi) * (hauteur - 5)
            x = marge + index * pas + (pas - barre) / 2
            if h > 0:
                pdf.rect(x, haut + hauteur - h, barre, h, style="F")
            # Valeur au-dessus de la barre, si la série n'est pas trop dense.
            if len(valeurs) <= 32:
                pdf.set_xy(x - pas * 0.15, haut + hauteur - h - 4)
                pdf.set_font("Helvetica", "", 6)
                pdf.set_text_color(*_GRIS)
                pdf.cell(barre + pas * 0.3, 3.5, _latin(f"{valeur:g}"), align=Align.C)
        # Étiquettes : allégées si la série est dense.
        saut = max(1, len(valeurs) // 16)
        pdf.set_font("Helvetica", "", 6)
        pdf.set_text_color(*_GRIS)
        for index, (etiquette, _valeur) in enumerate(valeurs):
            if index % saut:
                continue
            pdf.set_xy(marge + index * pas, haut + hauteur + 1)
            pdf.cell(pas, 4, _latin(etiquette), align=Align.C)
    pdf.set_xy(10, haut + hauteur + 7)


def generer_pdf(metriques: dict, analyse: str, genere_par: str) -> bytes:
    """Rapport complet : couverture + KPI + analyse IA + tableaux + graphiques."""
    courant = metriques["courant"]
    precedent = metriques["precedent"]
    variations = metriques["variations"]
    periodicite = LIBELLES_PERIODICITE.get(metriques["periodicite"],
                                           metriques["periodicite"])

    pdf = _Rapport(unit="mm", format="A4")
    pdf.titre_courant = f"Rapport d'activité {periodicite.lower()}"
    pdf.sous_titre = metriques["libelle"]
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(10, 12, 10)
    pdf.add_page()

    # --- Bandeau de couverture -------------------------------------------
    pdf.set_fill_color(*_BLEU)
    pdf.rect(0, 0, 210, 34, style="F")
    pdf.set_xy(10, 8)
    pdf.set_font("Helvetica", "B", 19)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 9, _latin(f"Rapport d'activité {periodicite.lower()}"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(10)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, _latin(metriques["libelle"]),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_xy(10, 38)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*_GRIS)
    pdf.multi_cell(190, 5, _latin(
        f"Bordereaux de livraison dématérialisés · période du "
        f"{_fr(metriques['debut'])} au {_fr(metriques['fin'])} "
        f"({metriques['jours']} jour(s)) · généré le "
        f"{datetime.datetime.now():%d/%m/%Y à %H:%M} par {genere_par}"),
        align=Align.L, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    # --- KPI --------------------------------------------------------------
    couleur_nok = _ROUGE if courant["taux_nok"] > 15 else (
        _AMBRE if courant["taux_nok"] > 8 else _VERT)
    couleur_rap = _VERT if courant["taux_rapproches"] >= 85 else (
        _AMBRE if courant["taux_rapproches"] >= 60 else _ROUGE)
    _cartes(pdf, [
        ("BL traités", str(courant["total"]),
         f"{_signe(variations['total'])} vs {metriques['precedent_libelle']}", _ENCRE),
        ("Réceptions", str(courant["receptions"]),
         _signe(variations["receptions"]), _ENCRE),
        ("Expéditions", str(courant["expeditions"]),
         _signe(variations["expeditions"]), _ENCRE),
        ("Archivages", str(courant["archivages"]),
         _signe(variations["archivages"]), _ENCRE),
    ])
    _cartes(pdf, [
        ("Taux EDI NOK", f"{courant['taux_nok']:.1f} %".replace(".", ","),
         f"{courant['edi_nok']} BL sur {courant['receptions']} réceptions", couleur_nok),
        ("Rapprochement DESADV",
         f"{courant['taux_rapproches']:.1f} %".replace(".", ","),
         f"{courant['rapproches']} BL rapprochés", couleur_rap),
        ("Délai moyen de saisie",
         f"{courant['delai_moyen_h']:.1f} h".replace(".", ","),
         "entre l'opération et l'enregistrement", _ENCRE),
        ("Anomalies de saisie",
         str(courant["brouillons"] + courant["erreurs"]),
         f"{courant['brouillons']} brouillon(s), {courant['erreurs']} erreur(s)",
         _AMBRE if courant["brouillons"] + courant["erreurs"] else _ENCRE),
    ])

    # --- Analyse rédigée par le modèle ------------------------------------
    _titre_section(pdf, "Analyse et commentaires")
    for titre, lignes in decouper_analyse(analyse):
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*_ENCRE)
        pdf.cell(0, 5.5, _latin(titre), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*_ENCRE)
        for ligne in lignes:
            pdf.multi_cell(0, 5, _latin(ligne), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1.5)

    # --- Volumes ----------------------------------------------------------
    _titre_section(pdf, "Volumes et comparaison", bloc=50)
    _tableau(pdf,
             [("Indicateur", 70, Align.L), (metriques["libelle"][:28], 45, Align.R),
              (metriques["precedent_libelle"][:28], 45, Align.R),
              ("Évolution", 30, Align.R)],
             [[libelle, str(courant[cle]), str(precedent[cle]),
               _signe(_variation(courant[cle], precedent[cle]))]
              for cle, libelle in (("total", "BL traités (complets)"),
                                   ("receptions", "Nouvelles réceptions"),
                                   ("expeditions", "Nouvelles expéditions"),
                                   ("archivages", "Archivages"),
                                   ("edi_nok", "Réceptions en EDI NOK"),
                                   ("rapproches", "BL rapprochés d'un DESADV"),
                                   ("supprimes", "BL supprimés (logique)"))])

    # --- Activité jour par jour ------------------------------------------
    if metriques.get("par_jour") and len(metriques["par_jour"]) > 1:
        _titre_section(pdf, "Activité sur la période", bloc=66)
        _histogramme(pdf, [(_jour_court(p["date"]), p["bl"])
                           for p in metriques["par_jour"]])
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(*_GRIS)
        pdf.cell(0, 5, _latin("Nombre de BL complets par date d'opération."),
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # --- Tiers ------------------------------------------------------------
    _titre_section(pdf, "Principaux tiers", bloc=50)
    _tableau(pdf,
             [("Fournisseur / client", 100, Align.L), ("BL", 22, Align.R),
              ("EDI NOK", 24, Align.R), ("Part NOK", 22, Align.R),
              ("Rapproché", 22, Align.R)],
             [[t["tiers"], str(t["bl"]), str(t["edi_nok"]),
               f"{t['part_nok']:.1f} %".replace(".", ","),
               f"{t['taux_rapproches']:.0f} %"]
              for t in metriques.get("top_tiers", [])],
             vide="Aucun BL complet sur la période.")

    if metriques.get("pires_tiers"):
        total_nok = metriques.get("total_edi_nok", 0)
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*_ENCRE)
        pdf.cell(0, 6, _latin("Tiers à surveiller (concentration des EDI NOK)"),
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        _tableau(pdf,
                 [("Tiers", 124, Align.L), ("BL", 22, Align.R),
                  ("EDI NOK", 22, Align.R), ("Part", 22, Align.R)],
                 [[t["tiers"], str(t["bl"]), str(t["edi_nok"]),
                   f"{t['part_nok']:.1f} %".replace(".", ",")]
                  for t in metriques["pires_tiers"]])
        cumul = sum(t["part_nok"] for t in metriques["pires_tiers"])
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(*_GRIS)
        pdf.multi_cell(0, 4.5, _latin(
            f"Ces {len(metriques['pires_tiers'])} tiers concentrent "
            f"{cumul:.0f} % des {total_nok} EDI NOK de la période. La part se "
            "lit sur le total des anomalies, non sur le volume propre du tiers : "
            "elle indique où porter l'effort en priorité."),
            align=Align.L, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # --- Répartitions -----------------------------------------------------
    if metriques.get("par_plage"):
        _titre_section(pdf, "Répartition par plage horaire", bloc=62)
        _histogramme(pdf, [(p["valeur"].replace("h", ""), p["bl"])
                           for p in metriques["par_plage"]], couleur=(66, 133, 190))

    _titre_section(pdf, "Répartition par quai", bloc=45)
    _tableau(pdf, [("Quai", 130, Align.L), ("BL", 60, Align.R)],
             [[p["valeur"], str(p["bl"])] for p in metriques.get("par_quai", [])[:8]],
             vide="Aucun quai renseigné (archivages uniquement).")

    _titre_section(pdf, "Activité par opérateur", bloc=45)
    _tableau(pdf, [("Opérateur", 130, Align.L), ("BL saisis", 60, Align.R)],
             [[p["valeur"], str(p["bl"])]
              for p in metriques.get("par_utilisateur", [])[:10]])

    # --- Notifications et qualité IA --------------------------------------
    _titre_section(pdf, "Notifications Teams et qualité de l'extraction IA", bloc=60)
    notifications = metriques.get("notifications", {})
    _cartes(pdf, [
        ("Notifications émises", str(notifications.get("total", 0)),
         "nouvelles réceptions", _ENCRE),
        ("Publiées dans Teams", str(notifications.get("envoyees", 0)),
         f"{_pourcent(notifications.get('envoyees', 0), notifications.get('total', 0)):.0f} % "
         "des envois", _VERT),
        ("En échec", str(notifications.get("echecs", 0)),
         "à rejouer si nécessaire",
         _ROUGE if notifications.get("echecs") else _ENCRE),
    ])
    _tableau(pdf,
             [("Champ extrait", 100, Align.L), ("Mesures", 45, Align.R),
              ("Précision", 45, Align.R)],
             [[q["champ"], str(q["mesures"]),
               f"{q['precision']:.1f} %".replace(".", ",")]
              for q in metriques.get("qualite_ia", [])],
             vide="Aucune mesure de qualité IA sur la période.")

    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 7.5)
    pdf.set_text_color(*_GRIS)
    pdf.multi_cell(0, 4, _latin(
        "Périmètre : BL au statut COMPLET, hors suppressions logiques, datés dans "
        "la période. Les variations comparent à la période équivalente "
        "précédente. La section « Analyse et commentaires » est rédigée "
        "automatiquement à partir des seuls agrégats de ce rapport."),
        new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.alias_nb_pages()
    return bytes(pdf.output())


def _fr(iso: str) -> str:
    try:
        return f"{datetime.date.fromisoformat(iso):%d/%m/%Y}"
    except Exception:
        return iso


def _jour_court(iso: str) -> str:
    try:
        return f"{datetime.date.fromisoformat(iso):%d/%m}"
    except Exception:
        return iso


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def generer_rapport(periodicite: str, reference: datetime.date, genere_par: str,
                    avec_ia: bool = True) -> dict:
    """Calcule, rédige et enregistre le rapport de la période contenant
    `reference`. Remplace un rapport déjà présent pour la même période.

    Renvoie {id, libelle, debut, fin, taille_octets, analyse_ia}."""
    if periodicite not in PERIODICITES:
        raise ValueError(f"Périodicité inconnue : {periodicite!r}")
    debut, fin, libelle = bornes(periodicite, reference)
    metriques = collecter(periodicite, debut, fin, libelle)
    analyse, faite = commentaire_ia(metriques) if avec_ia else (
        "Analyse automatique désactivée pour cette génération.", False)
    pdf = generer_pdf(metriques, analyse, genere_par)
    identifiant = repository.enregistrer_rapport(
        periodicite=periodicite, periode_debut=debut, periode_fin=fin,
        libelle=libelle, contenu=pdf, synthese=analyse, analyse_ia=faite,
        metriques=metriques, genere_par=genere_par)
    logger.info("Rapport %s « %s » généré (%d octets, analyse IA : %s)",
                periodicite, libelle, len(pdf), faite)
    return {"id": identifiant, "libelle": libelle, "debut": debut, "fin": fin,
            "taille_octets": len(pdf), "analyse_ia": faite,
            "total_bl": metriques["courant"]["total"]}


def nom_fichier(periodicite: str, debut: datetime.date) -> str:
    return f"BLDEMAT_{periodicite.lower()}_{debut:%Y%m%d}.pdf"
