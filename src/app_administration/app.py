"""Application « Administration des BL » — V5.

Expérience « model-driven » modernisée :
  - navigation par sections, vues toujours visibles, filtrée par le RBAC ;
  - filtres en boutons (pills) avec icônes, périodes en boutons multi-
    sélection (ce mois / cette semaine / hier / aujourd'hui / personnalisé) ;
  - chips horizontales des filtres appliqués, retirables une à une ;
  - KPI sur les vues BL et DESADV ; tableau de bord enrichi (deltas) ;
  - toutes les grilles triables ; confirmation avant toute modification ou
    suppression ; visionneuse d'images plein format ;
  - RBAC strict et fermé par défaut : matrice bl_core/rbac.py + table
    roles_utilisateurs (Gestion ▸ Rôles).
"""

import datetime
import json
import logging

import altair as alt
import pandas as pd
import streamlit as st
from bl_core import notifications, pdf_bl, rapports, rbac, repository, ui
from bl_core.config import get_settings
from bl_core.identity import get_current_user

st.set_page_config(page_title="Administration BL", page_icon="🗂️", layout="wide")

ui.configurer_logs()
ui.injecter_style()
logger = logging.getLogger("bl.administration")

utilisateur = get_current_user()
CTX_RBAC = rbac.contexte_rbac(utilisateur)
SETTINGS = get_settings()
TAILLE_PAGE = 50
EFFACER = "__effacer__"          # sentinelle : « retirer la clé » (défaut du widget)

# NB : `use_container_width` est remplacé partout par `width="stretch"` /
# `width="content"` (déprécié après le 31/12/2025) — SAUF sur st.altair_chart,
# qui n'accepte pas encore `width` en Streamlit 1.49.1. À basculer lors de la
# montée de version qui l'ajoutera.
VUE_DASHBOARD = "Tableau de bord"
VUE_RAPPORTS = "Rapports d'activité"
NAVIGATION = [
    ("Général", [("📊", VUE_DASHBOARD), ("📄", VUE_RAPPORTS)]),
    ("Achat", [("📥", "BL réception"), ("📡", "DESADV achat"),
               ("⚖️", "Rapprochement achat")]),
    ("Vente", [("📤", "BL expédition"), ("📡", "DESADV vente"),
               ("⚖️", "Rapprochement vente")]),
    ("Gestion", [("🏭", "Fournisseurs"), ("🤝", "Clients"), ("👤", "Gestionnaires"),
                 ("💼", "Portefeuilles"), ("🚪", "Quais"), ("📍", "Adresses"),
                 ("🏢", "Sites logistiques"), ("📋", "PLA"), ("🔐", "Rôles"),
                 ("🤖", "Qualité IA"), ("🔔", "Notifications")]),
]
SECTION_DE_LA_VUE = {v: s for s, vues in NAVIGATION for _, v in vues}
# Libellé affiché quand il diffère du nom interne (clés nav/RBAC/routage).
LIBELLES_VUES = {"Rapprochement achat": "Rapprochement BL / DESADV",
                 "Rapprochement vente": "Rapprochement BL / DESADV"}

PERIODES = ["Aujourd'hui", "Hier", "Cette semaine", "Ce mois", "Personnalisé"]
ICONES_PERIODE = {"Aujourd'hui": "📅", "Hier": "🕑", "Cette semaine": "📆",
                  "Ce mois": "🗓️", "Personnalisé": "⚙️"}


def _vider_grille(cle: str) -> None:
    st.session_state.pop(cle, None)


def _notifier_passage_ok(numero_bl, fournisseur, quai, date_reception,
                         commentaire: str = "") -> tuple[bool, str]:
    """Journalise le passage EDI NOK -> OK et publie la carte dans Teams.
    Best effort : un échec d'envoi n'annule jamais le changement d'état."""
    return notifications.notifier_passage_ok(
        numero_bl=numero_bl, fournisseur=fournisseur, quai=quai,
        date_reception=date_reception, utilisateur=utilisateur,
        commentaire=commentaire)


# =====================================================================
# FILTRES : mises à jour différées, périodes en boutons, chips retirables
# =====================================================================
def _appliquer_maj_filtres() -> None:
    """Applique les modifications de filtres demandées par les chips AVANT
    l'instanciation des widgets (une valeur de widget ne peut pas être
    modifiée après coup dans le même run)."""
    for cle, valeur in st.session_state.pop("maj_filtres", {}).items():
        if isinstance(valeur, str) and valeur == EFFACER:
            st.session_state.pop(cle, None)
        else:
            st.session_state[cle] = valeur


def _demander_maj(cle: str, valeur) -> None:
    st.session_state.setdefault("maj_filtres", {})[cle] = valeur
    st.rerun()


def afficher_chips(chips: list[tuple[str, str, object]], cle_vue: str,
                   cles_effacer: list[str] | None = None) -> None:
    """Ligne horizontale des filtres appliqués : « libellé ✕ » ; un clic
    retire le filtre (valeur EFFACER = retour au défaut du widget).
    `cles_effacer` ajoute un bouton « Effacer les filtres » qui retire
    toutes les pills d'un coup (les clés reviennent à un état vide)."""
    if not chips:
        return
    with st.container(horizontal=True, key=f"chips_{cle_vue}", gap="small"):
        for i, (libelle, cle, valeur) in enumerate(chips):
            if st.button(f"{libelle}  ✕", key=f"chip_{cle_vue}_{i}"):
                _demander_maj(cle, valeur)
        if cles_effacer and st.button("🧹 Effacer les filtres", key=f"chip_raz_{cle_vue}"):
            maj = st.session_state.setdefault("maj_filtres", {})
            for cle in cles_effacer:
                # [] / "" / None selon le widget : une valeur « vide » explicite
                # (EFFACER ramènerait au DÉFAUT, pas à un état sans filtre).
                if cle.startswith("per_"):
                    maj[cle] = []
                elif cle.startswith(("f_num", "f_frs", "dnum", "dfrs", "dash_tiers",
                                     "pf_frs", "sl_adr", "sl_ent")):
                    maj[cle] = ""
                elif cle.startswith("f_sup"):
                    maj[cle] = False
                else:
                    maj[cle] = None
            st.rerun()


# =====================================================================
# ÉCRANS UTILISATEUR — capture/rappel des filtres, tri et colonnes d'une vue
# =====================================================================
def _serialiser_valeur(cle: str, valeur):
    if cle.startswith("perso_") and valeur:
        return [valeur[0].isoformat(), valeur[1].isoformat()]
    return valeur


def _deserialiser_valeur(cle: str, valeur):
    if cle.startswith("perso_") and valeur:
        return (datetime.date.fromisoformat(valeur[0]),
                datetime.date.fromisoformat(valeur[1]))
    return valeur


def gestion_ecrans(vue_id: str, cles: list[str]) -> None:
    """Contrôle discret « 🖥️ Écrans » : sauvegarde l'état courant (filtres,
    tri, colonnes) sous un nom, rappelle un écran d'un clic, définit l'écran
    par défaut de la vue (appliqué à chaque reconnexion)."""
    with st.popover("🖥️ Écrans", width="content"):
        try:
            ecrans = repository.lister_ecrans(utilisateur, vue_id)
        except Exception as e:
            st.caption(f"Écrans indisponibles : {e}")
            return
        if not ecrans.empty:
            st.caption("Mes écrans :")
            for _, ligne in ecrans.iterrows():
                c1, c2 = st.columns([5, 1])
                marque = " ⭐" if ligne["est_defaut"] else ""
                if c1.button(f"🖥️ {ligne['nom']}{marque}", key=f"ec_ap_{vue_id}_{ligne['nom']}",
                             width="stretch"):
                    try:
                        etat = json.loads(ligne["etat"])
                        maj = st.session_state.setdefault("maj_filtres", {})
                        for k in cles:                       # état complet : les clés
                            maj[k] = _deserialiser_valeur(k, etat.get(k))   # absentes -> None
                        st.rerun()
                    except Exception as e:
                        st.error(f"Écran illisible : {e}")
                if c2.button("🗑️", key=f"ec_sup_{vue_id}_{ligne['nom']}",
                             help="Supprimer cet écran"):
                    repository.supprimer_ecran(utilisateur, vue_id, ligne["nom"])
                    st.rerun()
            st.divider()
        nom = st.text_input("Nom de l'écran", key=f"ec_nom_{vue_id}",
                            placeholder="Ex. « EDI NOK de la semaine »").strip()
        defaut = st.checkbox("Écran par défaut de cette vue", key=f"ec_def_{vue_id}")
        if st.button("💾 Enregistrer l'écran actuel", key=f"ec_save_{vue_id}",
                     type="primary", width="stretch", disabled=not nom):
            etat = {k: _serialiser_valeur(k, st.session_state.get(k)) for k in cles}
            try:
                repository.sauver_ecran(utilisateur, vue_id, nom, json.dumps(etat), defaut)
                ui.set_flash("toast", f"Écran « {nom} » enregistré.")
                st.rerun()
            except Exception as e:
                st.error(f"Enregistrement impossible : {e}")


def appliquer_ecran_defaut(vue_id: str, cles: list[str]) -> None:
    """À CHAQUE arrivée sur la vue (changement de vue ou reconnexion),
    charge l'écran par défaut : celui défini par l'utilisateur s'il existe,
    sinon l'écran standard (retour aux défauts des filtres)."""
    premiere_fois = not st.session_state.get(f"ecran_defaut_fait_{vue_id}")
    changement = st.session_state.pop("charger_ecran_defaut", False)
    if not premiere_fois and not changement:
        return
    st.session_state[f"ecran_defaut_fait_{vue_id}"] = True
    try:
        ecrans = repository.lister_ecrans(utilisateur, vue_id)
        defauts = ecrans[ecrans["est_defaut"]] if not ecrans.empty else ecrans
    except Exception:
        return
    maj = st.session_state.setdefault("maj_filtres", {})
    if defauts is not None and not defauts.empty:
        try:
            etat = json.loads(defauts.iloc[0]["etat"])
        except Exception:
            return
        for k in cles:
            maj[k] = _deserialiser_valeur(k, etat.get(k))
        st.rerun()
    elif changement:
        # Pas d'écran par défaut : écran standard (défauts des widgets).
        for k in cles:
            maj[k] = EFFACER
        st.rerun()


@st.dialog("🗓️ Période personnalisée")
def dialog_periode_perso(cle: str):
    ajd = repository.maintenant_local().date()
    stocke = st.session_state.get(f"perso_{cle}") or (ajd - datetime.timedelta(days=7), ajd)
    deb = st.date_input("Du", value=stocke[0], key=f"perso_deb_{cle}")
    fin = st.date_input("Au", value=stocke[1], key=f"perso_fin_{cle}")
    col_ok, col_ko = st.columns(2)
    if col_ok.button("✅ Appliquer", type="primary", width="stretch"):
        if deb > fin:
            st.error("La date de début doit précéder la date de fin.")
            st.stop()
        st.session_state[f"perso_{cle}"] = (deb, fin)
        st.session_state.pop(f"perso_demande_{cle}", None)
        st.rerun()
    if col_ko.button("Annuler", width="stretch"):
        sel = [o for o in st.session_state.get(f"per_{cle}", []) if o != "Personnalisé"]
        st.session_state.pop(f"perso_demande_{cle}", None)
        _demander_maj(f"per_{cle}", sel)


def filtre_periode(cle: str, libelle: str = "Période",
                   defaut: tuple = ("Hier", "Aujourd'hui")):
    """Filtre de dates en boutons multi-sélection. Renvoie (dmin, dmax, sel) —
    l'enveloppe [min, max] des périodes cochées, None sans sélection."""
    sel = st.pills(libelle, PERIODES, selection_mode="multi", default=list(defaut),
                   key=f"per_{cle}",
                   format_func=lambda o: f"{ICONES_PERIODE[o]} {o}") or []
    ajd = repository.maintenant_local().date()
    hier = ajd - datetime.timedelta(days=1)
    bornes = []
    for o in sel:
        if o == "Aujourd'hui":
            bornes.append((ajd, ajd))
        elif o == "Hier":
            bornes.append((hier, hier))
        elif o == "Cette semaine":
            bornes.append((ajd - datetime.timedelta(days=ajd.weekday()), ajd))
        elif o == "Ce mois":
            bornes.append((ajd.replace(day=1), ajd))
        elif o == "Personnalisé":
            perso = st.session_state.get(f"perso_{cle}")
            if perso:
                bornes.append(tuple(perso))
                if st.button("✏️ Modifier la période personnalisée", key=f"editper_{cle}"):
                    dialog_periode_perso(cle)
            elif not st.session_state.get(f"perso_demande_{cle}"):
                st.session_state[f"perso_demande_{cle}"] = True
                dialog_periode_perso(cle)
            elif st.button("🗓️ Choisir les dates…", key=f"choisirper_{cle}"):
                dialog_periode_perso(cle)
    if not bornes:
        return None, None, sel
    return min(b[0] for b in bornes), max(b[1] for b in bornes), sel


def chips_periode(cle: str, sel: list) -> list[tuple[str, str, object]]:
    """Une chip par période sélectionnée (retirables une à une)."""
    chips = []
    for o in sel:
        libelle = f"{ICONES_PERIODE[o]} {o}"
        if o == "Personnalisé" and st.session_state.get(f"perso_{cle}"):
            deb, fin = st.session_state[f"perso_{cle}"]
            libelle = f"🗓️ Du {deb:%d/%m} au {fin:%d/%m}"
        chips.append((libelle, f"per_{cle}", [x for x in sel if x != o]))
    return chips


def _tri_grille(df: pd.DataFrame, cle: str,
                libelles: dict | None = None) -> tuple[pd.DataFrame, str]:
    """Tri des grilles éditables (le tri natif est désactivé quand l'ajout de
    lignes est possible) : colonne en pills + sens. Renvoie (df trié, suffixe
    de clé) — le suffixe fait recréer l'éditeur à chaque changement de tri,
    sinon les éditions en cours seraient réappliquées aux mauvaises lignes."""
    if df.empty or not len(df.columns):
        return df, ""
    libelles = libelles or {}
    with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
        col_tri = st.pills("Trier par", list(df.columns), key=f"tri_{cle}",
                           format_func=lambda c: libelles.get(c, c))
        sens = st.segmented_control("Sens", ["⬆️", "⬇️"], key=f"sens_tri_{cle}",
                                    default="⬆️", label_visibility="hidden")
    if not col_tri:
        return df, ""
    df = df.sort_values(col_tri, ascending=(sens != "⬇️"), kind="stable",
                        na_position="last").reset_index(drop=True)
    return df, f"_{col_tri}_{'d' if sens == '⬇️' else 'a'}"


# =====================================================================
# NAVIGATION LATÉRALE — sections texte + vues visibles, filtrée par le RBAC
# =====================================================================
_appliquer_maj_filtres()

NAV_VISIBLE = [(s, [(i, v) for i, v in vues
                    if rbac.niveau_vue(v, CTX_RBAC) != rbac.AUCUN])
               for s, vues in NAVIGATION]
NAV_VISIBLE = [(s, vues) for s, vues in NAV_VISIBLE if vues]
VUES_VISIBLES = [v for _, vues in NAV_VISIBLE for _, v in vues]

if not VUES_VISIBLES:
    ui.entete_app("Administration des BL")
    if CTX_RBAC.get("indisponible"):
        st.error(
            "Le contrôle des droits est indisponible. L'accès est fermé par sécurité."
        )
    else:
        st.error("Aucune vue ne vous est autorisée. Demandez un rôle à l'administrateur "
                 "métier (table roles_utilisateurs / vue Gestion ▸ Rôles).")
    st.stop()

st.session_state.setdefault("nav_vue", VUES_VISIBLES[0])
if st.session_state.nav_vue not in VUES_VISIBLES:
    st.session_state.nav_vue = VUES_VISIBLES[0]

with st.sidebar:
    ui.afficher_logo()
    st.divider()
    for section, vues in NAV_VISIBLE:
        ui.section_nav(section)
        for icone, v in vues:
            actif = st.session_state.nav_vue == v
            libelle = LIBELLES_VUES.get(v, v)
            if st.button(f"{icone}  {libelle}", key=f"nav_{v}", width="stretch",
                         type="primary" if actif else "secondary"):
                st.session_state.nav_vue = v
                # À chaque changement de vue, l'écran par défaut de la vue
                # cible (standard ou défini par l'utilisateur) est rechargé.
                st.session_state.charger_ecran_defaut = True
                st.rerun()
    st.divider()
    roles_txt = ", ".join(CTX_RBAC["roles"]) or "aucun rôle"
    st.caption(f"👤 {utilisateur}")
    st.caption(f"🔐 {roles_txt}")

vue = st.session_state.nav_vue
section = SECTION_DE_LA_VUE.get(vue, "Général")
NIVEAU = rbac.niveau_vue(vue, CTX_RBAC)
LECTURE_SEULE = NIVEAU == rbac.LECTURE

ui.entete_app("Administration des BL")
ui.show_flash()
ui.barre_contexte(utilisateur, SETTINGS.environment, CTX_RBAC["roles"])


# =====================================================================
# TABLEAU DE BORD — KPI avec deltas + graphiques
# =====================================================================
def render_dashboard() -> None:
    st.markdown("### 📊 Tableau de bord")

    dmin, dmax, sel_per = filtre_periode("dash", defaut=("Ce mois",))
    c1, c2, c3 = st.columns([2, 2, 3])
    portee_sel = c1.segmented_control(
        "Périmètre", ["🛒 Achat", "🚚 Vente", "🌐 Tous"],
        default="🛒 Achat", key="dash_portee",
    )
    gest_sel = c2.selectbox(
        "Gestionnaire achat",
        [""] + repository.lister_gestionnaires(),
        key="dash_gest",
        disabled=portee_sel == "🚚 Vente",
        help="Le portefeuille gestionnaire ne s'applique qu'aux fournisseurs.",
    )
    if portee_sel == "🚚 Vente":
        gest_sel = ""
    f_tiers = c3.text_input("Fournisseur / client contient", key="dash_tiers").strip()

    chips = chips_periode("dash", sel_per)
    if portee_sel:
        chips.append((portee_sel, "dash_portee", None))
    if gest_sel:
        chips.append((f"👤 {gest_sel}", "dash_gest", None))
    if f_tiers:
        chips.append((f"🔎 « {f_tiers} »", "dash_tiers", ""))
    afficher_chips(chips, "dash")

    ajd = repository.maintenant_local().date()
    dmin = dmin or ajd.replace(day=1)
    dmax = dmax or ajd

    def _filtrer(d: pd.DataFrame, avec_portee: bool = True) -> pd.DataFrame:
        if d is None or d.empty:
            return pd.DataFrame(columns=["type_operation", "statut_bl",
                                         "nom_fournisseur", "date_reception",
                                         "plage_horaire"])
        if avec_portee:
            if portee_sel == "🛒 Achat":
                d = d[d["type_operation"].isin(repository.TYPES_ACHAT)]
            elif portee_sel == "🚚 Vente":
                d = d[d["type_operation"].isin(repository.TYPES_VENTE)]
        if f_tiers:
            d = d[d["nom_fournisseur"].fillna("").str.lower().str.contains(f_tiers.lower())]
        if gest_sel:
            frs = set(repository.lire_portefeuilles(gestionnaire=gest_sel)["nom_fournisseur"])
            d = d[d["nom_fournisseur"].isin(frs)]
        return d

    try:
        df = _filtrer(repository.lire_bl_pour_dashboard(dmin, dmax))
        duree = (dmax - dmin).days + 1
        df_prec = _filtrer(repository.lire_bl_pour_dashboard(
            dmin - datetime.timedelta(days=duree), dmin - datetime.timedelta(days=1)))
        sens_desadv = (
            [repository.SENS_ACHAT] if portee_sel == "🛒 Achat"
            else [repository.SENS_VENTE] if portee_sel == "🚚 Vente"
            else [repository.SENS_ACHAT, repository.SENS_VENTE]
        )
        desadv_frames = [repository.lire_desadv(sens) for sens in sens_desadv]
        desadv = pd.concat(
            [frame for frame in desadv_frames if frame is not None and not frame.empty],
            ignore_index=True,
        ) if any(frame is not None and not frame.empty for frame in desadv_frames) else pd.DataFrame()
    except Exception as e:
        st.error(f"Erreur de lecture de la base : {e}")
        st.stop()

    # DESADV achat sur la période, mêmes filtres tiers/gestionnaire.
    if desadv is None or desadv.empty:
        desadv = pd.DataFrame(columns=["numero_bl", "nom_fournisseur",
                                       "integrationdate", "statut_edi"])
    desadv = desadv.dropna(subset=["integrationdate"]).copy()
    desadv["d"] = pd.to_datetime(desadv["integrationdate"]).dt.date
    desadv = desadv[(desadv["d"] >= dmin) & (desadv["d"] <= dmax)]
    if f_tiers:
        desadv = desadv[desadv["nom_fournisseur"].fillna("").str.lower()
                        .str.contains(f_tiers.lower())]
    if gest_sel:
        frs = set(repository.lire_portefeuilles(gestionnaire=gest_sel)["nom_fournisseur"])
        desadv = desadv[desadv["nom_fournisseur"].isin(frs)]

    def _kpis(d: pd.DataFrame) -> dict:
        est = d["type_operation"] if not d.empty else pd.Series(dtype=object)
        rec = int((est == repository.TYPE_RECEPTION).sum())
        nok = int(((est == repository.TYPE_RECEPTION) &
                   (d["statut_bl"] == repository.STATUT_EDI_NOK)).sum()) if not d.empty else 0
        return {"total": len(d), "rec": rec,
                "exp": int((est == repository.TYPE_EXPEDITION).sum()),
                "nok": nok}

    k, kp = _kpis(df), _kpis(df_prec)
    taux = f"{100 * k['nok'] / k['rec']:.0f} %" if k["rec"] else "—"
    desadv_nok = int((desadv["statut_edi"] == "EDI NOK").sum())

    cols = st.columns(6)
    cols[0].metric("BL (période)", k["total"], delta=k["total"] - kp["total"])
    cols[1].metric("Réceptions", k["rec"], delta=k["rec"] - kp["rec"])
    cols[2].metric("Expéditions", k["exp"], delta=k["exp"] - kp["exp"])
    cols[3].metric("RECEPTIONS NOK", k["nok"], delta=k["nok"] - kp["nok"],
                   delta_color="inverse",
                   help="Réceptions saisies avec l'état EDI NOK.")
    cols[4].metric("Taux réceptions NOK", taux)
    cols[5].metric("DESADV NOK", desadv_nok,
                   help="Avis d'expédition du périmètre dont le message EDI est "
                        "en erreur (messagestate = 3), sur la période filtrée.")
    st.caption(f"Période {dmin:%d/%m/%Y} → {dmax:%d/%m/%Y} · deltas vs période "
               "précédente de même durée.")

    # =================================================================
    # ACTIVITÉ — heatmap façon « contributions GitHub », deux visuels au
    # choix. Ces visuels IGNORENT les filtres de dates (année civile pour le
    # quotidien, trimestre en cours pour les plages horaires) mais suivent
    # les filtres périmètre / tiers / gestionnaire.
    # =================================================================
    st.markdown("#### Activité")
    mode_activite = st.segmented_control(
        "Visuel d'activité", ["📅 Par jour (année en cours)",
                              "🕑 Par plage horaire (trimestre)"],
        default="📅 Par jour (année en cours)", key="dash_activite",
        label_visibility="collapsed")

    VERTS = ["#EBEDF0", "#C6E48B", "#7BC96F", "#43B02A", "#196127"]
    jours_fr = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]

    if mode_activite == "🕑 Par plage horaire (trimestre)":
        # Trimestre en cours, réceptions et expéditions NOUVELLES uniquement.
        t_deb = datetime.date(ajd.year, 3 * ((ajd.month - 1) // 3) + 1, 1)
        t_fin = (datetime.date(ajd.year, t_deb.month + 3, 1) - datetime.timedelta(days=1)
                 if t_deb.month <= 9 else datetime.date(ajd.year, 12, 31))
        try:
            df_tri = _filtrer(repository.lire_bl_pour_dashboard(t_deb, t_fin))
        except Exception as e:
            st.error(f"Erreur de lecture : {e}")
            df_tri = pd.DataFrame()
        df_tri = df_tri[df_tri["type_operation"].isin(
            [repository.TYPE_RECEPTION, repository.TYPE_EXPEDITION])] if not df_tri.empty else df_tri
        base = (df_tri.dropna(subset=["date_reception", "plage_horaire"]).copy()
                if not df_tri.empty else pd.DataFrame())
        grille = pd.MultiIndex.from_product(
            [pd.date_range(t_deb, min(t_fin, ajd), freq="D"),
             repository.PLAGES_HORAIRES], names=["d", "Plage"]).to_frame(index=False)
        if base.empty:
            activite = grille.assign(BL=0.0)
        else:
            base["d"] = pd.to_datetime(base["date_reception"])
            compte = (base.groupby(["d", "plage_horaire"]).size().rename("BL")
                      .reset_index().rename(columns={"plage_horaire": "Plage"}))
            activite = grille.merge(compte, on=["d", "Plage"], how="left").fillna({"BL": 0})
        activite["Date"] = activite["d"].dt.strftime("%d/%m/%Y")
        heatmap = (
            alt.Chart(activite).mark_rect(cornerRadius=2, stroke="#FFFFFF",
                                          strokeWidth=1.5).encode(
                x=alt.X("d:O", title=None,
                        axis=alt.Axis(labelExpr="date(toDate(datum.value)) == 1 || "
                                                "date(toDate(datum.value)) == 15 ? "
                                                "timeFormat(toDate(datum.value), '%d %b') : ''",
                                      labelAngle=0, labelFontSize=11, tickSize=0)),
                y=alt.Y("Plage:N", sort=repository.PLAGES_HORAIRES, title=None,
                        axis=alt.Axis(labelFontSize=11)),
                color=alt.Color("BL:Q", title="BL", scale=alt.Scale(range=VERTS),
                                legend=alt.Legend(orient="right")),
                tooltip=["Date", "Plage", alt.Tooltip("BL:Q", format=".0f")],
            ).properties(height=230)
        )
        st.altair_chart(heatmap, use_container_width=True)
        st.caption(f"Réceptions et expéditions par plage horaire — trimestre en "
                   f"cours ({t_deb:%d/%m} → {t_fin:%d/%m}), hors archivages ; "
                   "indépendant des filtres de dates.")
    else:
        # Année civile en cours, BL NOUVEAUX uniquement (hors archivages).
        a_deb, a_fin = datetime.date(ajd.year, 1, 1), datetime.date(ajd.year, 12, 31)
        try:
            df_an = _filtrer(repository.lire_bl_pour_dashboard(a_deb, a_fin))
        except Exception as e:
            st.error(f"Erreur de lecture : {e}")
            df_an = pd.DataFrame()
        df_an = df_an[df_an["type_operation"].isin(
            [repository.TYPE_RECEPTION, repository.TYPE_EXPEDITION])] if not df_an.empty else df_an
        tmp = (df_an.dropna(subset=["date_reception"]).copy()
               if not df_an.empty else pd.DataFrame())
        calendrier = pd.DataFrame({"d": pd.date_range(a_deb, a_fin, freq="D")})
        if tmp.empty:
            par_jour = calendrier.assign(BL=0.0)
        else:
            tmp["d"] = pd.to_datetime(tmp["date_reception"])
            par_jour = (tmp.groupby("d").size().rename("BL").reset_index()
                        .merge(calendrier, on="d", how="right").fillna({"BL": 0}))
        par_jour["Semaine"] = (par_jour["d"]
                               - pd.to_timedelta(par_jour["d"].dt.weekday, unit="D"))
        par_jour["Jour"] = par_jour["d"].dt.weekday.map(dict(enumerate(jours_fr)))
        par_jour["Date"] = par_jour["d"].dt.strftime("%d/%m/%Y")
        heatmap = (
            alt.Chart(par_jour).mark_rect(cornerRadius=2, stroke="#FFFFFF",
                                          strokeWidth=1.5).encode(
                x=alt.X("Semaine:O", title=None,
                        axis=alt.Axis(labelExpr="date(toDate(datum.value)) <= 7 ? "
                                                "timeFormat(toDate(datum.value), '%b') : ''",
                                      labelAngle=0, labelFontSize=11, tickSize=0)),
                y=alt.Y("Jour:N", sort=jours_fr, title=None,
                        axis=alt.Axis(labelFontSize=11)),
                color=alt.Color("BL:Q", title="BL / jour", scale=alt.Scale(range=VERTS),
                                legend=alt.Legend(orient="right")),
                tooltip=["Date", alt.Tooltip("BL:Q", format=".0f")],
            ).properties(height=170)
        )
        st.altair_chart(heatmap, use_container_width=True)
        st.caption(f"BL nouveaux (réceptions + expéditions, hors archivages) — "
                   f"année {ajd.year} complète ; indépendant des filtres de dates.")

    # =================================================================
    # ÉVOLUTION DES NOK EN % (réceptions uniquement) — suit TOUS les filtres.
    # =================================================================
    col_evo, col_top = st.columns([1.4, 1])
    with col_evo:
        st.markdown("#### Évolution des NOK (%)")
        rec = df[df["type_operation"] == repository.TYPE_RECEPTION] \
            .dropna(subset=["date_reception"]).copy()
        series = []
        if not rec.empty:
            rec["d"] = pd.to_datetime(rec["date_reception"]).dt.date
            g = rec.groupby("d")
            evo_rec = (100 * g.apply(
                lambda x: (x["statut_bl"] == repository.STATUT_EDI_NOK).mean(),
                include_groups=False)).rename("Taux").reset_index()
            evo_rec["Série"] = "RECEPTIONS NOK"
            series.append(evo_rec)
        if not desadv.empty:
            gd = desadv.groupby("d")
            evo_dsd = (100 * gd.apply(
                lambda x: (x["statut_edi"] == "EDI NOK").mean(),
                include_groups=False)).rename("Taux").reset_index()
            evo_dsd["Série"] = "DESADV NOK"
            series.append(evo_dsd)
        if not series:
            st.caption("Aucune réception ni DESADV sur la période filtrée.")
        else:
            evolution = pd.concat(series, ignore_index=True)
            courbe = (
                alt.Chart(evolution).mark_line(point=True, strokeWidth=2.5).encode(
                    x=alt.X("d:T", title=None,
                            axis=alt.Axis(format="%d/%m", labelAngle=0)),
                    y=alt.Y("Taux:Q", title="% NOK",
                            scale=alt.Scale(domain=[0, 100])),
                    color=alt.Color("Série:N", title=None,
                                    scale=alt.Scale(domain=["RECEPTIONS NOK", "DESADV NOK"],
                                                    range=["#E4572E", "#0F62A6"]),
                                    legend=alt.Legend(orient="top")),
                    tooltip=[alt.Tooltip("d:T", title="Date", format="%d/%m/%Y"),
                             "Série", alt.Tooltip("Taux:Q", format=".0f", title="% NOK")],
                ).properties(height=290)
            )
            st.altair_chart(courbe, use_container_width=True)

    # =================================================================
    # TOP 10 DES PIRES FOURNISSEURS — % (réceptions NOK + DESADV NOK).
    # =================================================================
    with col_top:
        tiers_dashboard = "fournisseurs" if portee_sel == "🛒 Achat" else (
            "clients" if portee_sel == "🚚 Vente" else "tiers"
        )
        st.markdown(f"#### Top 10 {tiers_dashboard} (% NOK)")
        rec_frs = df[df["type_operation"] == repository.TYPE_RECEPTION]
        par_frs = []
        if not rec_frs.empty:
            g = rec_frs.groupby(rec_frs["nom_fournisseur"].fillna("—"))
            par_frs.append(pd.DataFrame({
                "total": g.size(),
                "nok": g.apply(lambda x: (x["statut_bl"] == repository.STATUT_EDI_NOK).sum(),
                               include_groups=False)}))
        if not desadv.empty:
            gd = desadv.groupby(desadv["nom_fournisseur"].fillna("—"))
            par_frs.append(pd.DataFrame({
                "total": gd.size(),
                "nok": gd.apply(lambda x: (x["statut_edi"] == "EDI NOK").sum(),
                                include_groups=False)}))
        if not par_frs:
            st.caption("Aucune donnée sur la période filtrée.")
        else:
            cumul = pd.concat(par_frs).groupby(level=0).sum()
            cumul = cumul[cumul["total"] > 0]
            cumul["Taux"] = (100 * cumul["nok"] / cumul["total"]).round(0)
            pires = (cumul[cumul["Taux"] > 0].sort_values(["Taux", "total"],
                                                          ascending=False)
                     .head(10).rename_axis("Fournisseur").reset_index())
            if pires.empty:
                st.success("Aucun NOK sur la période : rien à signaler. ✅")
            else:
                barres = (
                    alt.Chart(pires).mark_bar(color="#E4572E", cornerRadiusEnd=3).encode(
                        x=alt.X("Taux:Q", title="% NOK sur les flux contrôlés",
                                scale=alt.Scale(domain=[0, 100])),
                        y=alt.Y("Fournisseur:N", sort="-x", title=None,
                                axis=alt.Axis(labelFontSize=12, labelLimit=280)),
                        tooltip=["Fournisseur", alt.Tooltip("Taux:Q", format=".0f"),
                                 alt.Tooltip("nok:Q", title="NOK"),
                                 alt.Tooltip("total:Q", title="Total")],
                    ).properties(height=290)
                )
                st.altair_chart(barres, use_container_width=True)


# =====================================================================
# BOÎTES DE DIALOGUE
# =====================================================================
@st.dialog("✏️ Fiche du BL", width="medium")
def dialog_modifier_bl(bl: dict, ids_photos: list[str], cle_grille: str):
    rbac.exiger_vue(vue, CTX_RBAC, rbac.MODIFICATION)
    type_op = bl.get("type_operation") or repository.TYPE_RECEPTION
    avec_pq = repository.operation_avec_plage_et_quai(type_op)
    avec_st = repository.operation_avec_statut(type_op)
    tiers_lib = repository.libelle_tiers(type_op)
    type_tiers = (repository.TIERS_CLIENT if type_op in repository.TYPES_VENTE
                  else repository.TIERS_FOURNISSEUR)

    st.caption(f"{repository.LIBELLES_OPERATION.get(type_op, type_op)} · "
               f"saisi par {bl.get('saisie_par') or '?'} le {bl.get('saisie_le') or '?'}")

    with st.form("fiche_bl"):
        numero = st.text_input("Numéro de BL", value=bl["numero_bl"], max_chars=60)
        date_op = st.date_input("Date", value=bl.get("date_reception"))
        tiers_options = repository.lister_tiers(type_tiers)
        index_tiers = (tiers_options.index(bl["nom_fournisseur"])
                       if bl.get("nom_fournisseur") in tiers_options else None)
        nouveau_tiers = st.selectbox(tiers_lib, options=tiers_options, index=index_tiers,
                                     placeholder="Choisir…")
        plage = quai = None
        commentaire = ""
        if avec_pq:
            index_plage = (repository.PLAGES_HORAIRES.index(bl["plage_horaire"])
                           if bl.get("plage_horaire") in repository.PLAGES_HORAIRES else None)
            plage = st.selectbox("Plage horaire", options=repository.PLAGES_HORAIRES,
                                 index=index_plage, placeholder="Non renseignée")
            quais = repository.lister_quais()
            index_quai = quais.index(bl["quai_reception"]) if bl.get("quai_reception") in quais else None
            quai = st.selectbox("Quai", options=quais, index=index_quai, placeholder="Non renseigné")
            commentaire = st.text_area("Commentaire", value=bl.get("comment_bl") or "", max_chars=1000)
        statut_choix = None
        if avec_st:
            statut_choix = st.radio("État de réception", ["OK", "EDI NOK"], horizontal=True,
                                    index=0 if bl.get("statut_bl") == repository.STATUT_OK else 1)

        if st.form_submit_button("💾 Enregistrer", type="primary", width="stretch"):
            champs = {"numero_bl": numero.strip(), "date_reception": date_op,
                      "nom_fournisseur": nouveau_tiers}
            if avec_pq:
                champs["comment_bl"] = commentaire.strip()
                if plage:
                    champs["plage_horaire"] = plage
                if quai:
                    champs["quai_reception"] = quai
            if avec_st:
                champs["statut_bl"] = (repository.STATUT_OK if statut_choix == "OK"
                                       else repository.STATUT_EDI_NOK)
            st.session_state["fiche_a_confirmer"] = champs

    # Confirmation en deux temps, dans la même boîte de dialogue.
    champs = st.session_state.get("fiche_a_confirmer")
    if champs is not None:
        passe_a_ok = (avec_st and bl.get("statut_bl") == repository.STATUT_EDI_NOK
                      and champs.get("statut_bl") == repository.STATUT_OK)
        st.warning(f"Enregistrer les modifications du BL « {champs['numero_bl']} » ?")
        note = ""
        if passe_a_ok:
            note = st.text_area(
                "Commentaire pour la notification Teams (facultatif)",
                key="note_ok_fiche", max_chars=500,
                placeholder="Ex. « DESADV réintégré par le fournisseur ce matin »",
                help="Ce texte apparaît dans la carte publiée dans le canal Teams.")
        col_ok, col_ko = st.columns(2)
        if col_ok.button("✅ Confirmer", type="primary", width="stretch"):
            st.session_state.pop("fiche_a_confirmer", None)
            try:
                repository.mettre_a_jour_bl(
                    bl["id_bl"], champs, utilisateur, expected_version=bl.get("version")
                )
            except ValueError as e:            # numéro de BL déjà pris
                st.error(str(e))
                st.stop()
            if passe_a_ok:
                succes, detail = _notifier_passage_ok(
                    champs["numero_bl"], champs.get("nom_fournisseur"),
                    champs.get("quai_reception"), champs.get("date_reception"),
                    commentaire=note.strip())
                ui.set_flash(
                    "success" if succes else "warning",
                    f"BL {champs['numero_bl']} mis à jour — carte publiée dans Teams."
                    if succes else
                    f"BL {champs['numero_bl']} mis à jour, mais la notification "
                    f"Teams a échoué ({detail}). Voir Gestion ▸ Notifications.")
            else:
                ui.set_flash("success", f"BL {champs['numero_bl']} mis à jour.")
            _vider_grille(cle_grille)
            st.rerun()
        if col_ko.button("Annuler", width="stretch"):
            st.session_state.pop("fiche_a_confirmer", None)

    if ids_photos:
        with st.expander(f"📎 Pages ({len(ids_photos)})"):
            for i, id_photo in enumerate(ids_photos):
                try:
                    st.image(repository.telecharger_photo(id_photo), caption=f"Page {i + 1}",
                             width="stretch")
                except Exception as e:
                    st.caption(f"Page {i + 1} inaccessible : {e}")


@st.dialog("🖼️ Pages du BL", width="large")
def dialog_voir_images(numero_bl: str, ids_photos: list[str]):
    if not ids_photos:
        st.info("Aucune page attachée à ce BL.")
        return
    pages, erreurs = [], 0
    for id_photo in ids_photos:
        try:
            pages.append(repository.telecharger_photo(id_photo))
        except Exception:
            erreurs += 1
    if erreurs:
        st.warning(f"{erreurs} page(s) inaccessible(s).")
    if pages:
        ui.visionneuse_images(pages, f"BL {numero_bl}")


@st.dialog("📄 Export PDF du BL", width="medium")
def dialog_export_pdf(bl: dict, ids_photos: list[str]):
    """PDF d'archivage/litige : métadonnées + pages scannées."""
    numero = bl.get("numero_bl", "")
    type_op = bl.get("type_operation") or ""
    meta = [
        ("Numéro de BL", numero),
        ("Opération", repository.LIBELLES_OPERATION.get(type_op, type_op)),
        ("Date", bl.get("date_reception")),
        ("Plage horaire", bl.get("plage_horaire")),
        (repository.libelle_tiers(type_op or repository.TYPE_RECEPTION),
         bl.get("nom_fournisseur")),
        ("Quai", bl.get("quai_reception")),
        ("État de réception", ui.libelle_statut(bl.get("statut_bl"))
         if repository.operation_avec_statut(type_op) else "—"),
        ("Commentaire", bl.get("comment_bl")),
        ("Saisi par / le", f"{bl.get('saisie_par') or '—'} · {bl.get('saisie_le') or '—'}"),
        ("Modifié par / le", f"{bl.get('modifie_par') or '—'} · {bl.get('modifie_le') or '—'}"),
    ]
    pages = []
    for id_photo in ids_photos:
        try:
            pages.append(repository.telecharger_photo(id_photo))
        except Exception:
            pass
    with st.spinner("Génération du PDF…"):
        try:
            octets = pdf_bl.generer_pdf_bl(meta, pages, f"BL {numero}")
        except Exception as e:
            st.error(f"Génération du PDF impossible : {e}")
            return
    st.success(f"PDF prêt : page de garde + {len(pages)} page(s) scannée(s).")
    st.download_button("⬇️ Télécharger le PDF", data=octets,
                       file_name=f"BL_{numero}.pdf".replace(" ", "_"),
                       mime="application/pdf", type="primary",
                       width="stretch")


@st.dialog("🕘 Historique du BL", width="large")
def dialog_historique_bl(numero_bl: str, id_bl: str):
    """Audit : qui a changé quoi, quand — avec impression."""
    try:
        df = repository.lire_audit_bl(id_bl)
    except Exception as e:
        st.error(f"Historique indisponible : {e} (migrations V5 exécutées ?)")
        return
    st.caption(f"BL {numero_bl}")
    if df is None or df.empty:
        st.info("Aucun événement d'historique pour ce BL (l'audit trace les "
                "créations et modifications depuis la V5).")
        return
    df_aff = df.rename(columns={
        "modifie_le": "Quand", "evenement": "Événement", "champ": "Champ",
        "valeur_avant": "Avant", "valeur_apres": "Après", "modifie_par": "Par"})
    st.dataframe(df_aff, hide_index=True, width="stretch")
    ui.bouton_imprimer_tableau(df_aff, f"Historique du BL {numero_bl}")


@st.dialog("🗑️ Confirmation")
def dialog_supprimer_bls(df: pd.DataFrame, ids: list[str], cle_grille: str):
    rbac.exiger_vue(vue, CTX_RBAC, rbac.MODIFICATION)
    st.warning(f"Supprimer logiquement {len(ids)} BL ? Ils resteront restaurables "
               "(case « Inclure les BL supprimés »).")
    col_oui, col_non = st.columns(2)
    if col_oui.button("✅ Confirmer la suppression", type="primary", width="stretch"):
        for id_bl in ids:
            row = df[df["id_bl"] == id_bl].iloc[0]
            repository.supprimer_bl(
                id_bl, utilisateur, expected_version=int(row["version"])
            )
        ui.set_flash("success", f"{len(ids)} BL supprimé(s) logiquement.")
        _vider_grille(cle_grille)
        st.rerun()
    if col_non.button("Annuler", width="stretch"):
        st.rerun()


@st.dialog("✅ Confirmation")
def dialog_confirmer_grille(nom_ref: str, nom_vue: str, df_avant: pd.DataFrame,
                            df_apres: pd.DataFrame, valeurs_fixes: dict | None,
                            cle_grille: str):
    rbac.exiger_vue(vue, CTX_RBAC, rbac.MODIFICATION)
    st.warning(f"Appliquer les modifications de la grille « {nom_vue} » ? "
               "Les lignes supprimées le seront définitivement.")
    col_oui, col_non = st.columns(2)
    if col_oui.button("✅ Confirmer", type="primary", width="stretch"):
        try:
            ajouts, suppressions = repository.sauver_referentiel(
                nom_ref, df_avant, df_apres, valeurs_fixes, utilisateur)
            if ajouts or suppressions:
                ui.set_flash("success", f"{nom_vue} : {ajouts} ajout(s)/modification(s), "
                                        f"{suppressions} suppression(s).")
            else:
                ui.set_flash("info", "Aucune modification à enregistrer.")
        except ValueError as e:
            st.error(str(e))
            st.stop()
        except Exception as e:
            st.error(f"Échec de l'enregistrement : {e}")
            st.stop()
        _vider_grille(cle_grille)
        st.rerun()
    if col_non.button("Annuler", width="stretch"):
        st.rerun()


@st.dialog("✅ Confirmation")
def dialog_confirmer_ok(df: pd.DataFrame, ids: list[str], cle_grille: str):
    rbac.exiger_vue(vue, CTX_RBAC, rbac.MODIFICATION)
    nb_nok = int((df[df["id_bl"].isin(ids)]["statut_bl"]
                  == repository.STATUT_EDI_NOK).sum())
    st.warning(f"Passer à OK les BL EDI NOK sélectionnés ({nb_nok} concerné(s) "
               f"sur {len(ids)}) ? Une carte est publiée dans Teams pour chacun.")
    note = st.text_area(
        "Commentaire pour la notification Teams (facultatif)",
        key="note_ok_masse", max_chars=500,
        placeholder="Ex. « DESADV réintégré par le fournisseur ce matin »",
        help="Ce texte apparaît dans la ou les cartes publiées dans le canal Teams.")
    col_oui, col_non = st.columns(2)
    if col_oui.button("✅ Confirmer", type="primary", width="stretch"):
        bascules, echecs = 0, 0
        for id_bl in ids:
            ligne = df[df["id_bl"] == id_bl].iloc[0]
            if ligne["statut_bl"] != repository.STATUT_EDI_NOK:
                continue
            repository.mettre_a_jour_bl(
                id_bl,
                {"statut_bl": repository.STATUT_OK},
                utilisateur,
                expected_version=int(ligne["version"]),
            )
            succes, _detail = _notifier_passage_ok(
                ligne["numero_bl"], ligne["nom_fournisseur"],
                ligne["quai_reception"], ligne["date_reception"],
                commentaire=note.strip())
            bascules += 1
            echecs += 0 if succes else 1
        if bascules and echecs:
            message = (f"{bascules} BL passé(s) à OK, mais {echecs} notification(s) "
                       "Teams en échec (voir Gestion ▸ Notifications).")
        elif bascules:
            message = f"{bascules} BL passé(s) à OK — carte(s) publiée(s) dans Teams."
        else:
            message = "Aucun BL EDI NOK dans la sélection."
        ui.set_flash("warning" if echecs else ("success" if bascules else "info"),
                     message)
        _vider_grille(cle_grille)
        st.rerun()
    if col_non.button("Annuler", width="stretch"):
        st.rerun()


@st.dialog("♻️ Confirmation")
def dialog_confirmer_restauration(df: pd.DataFrame, ids: list[str], cle_grille: str):
    rbac.exiger_vue(vue, CTX_RBAC, rbac.MODIFICATION)
    st.warning(f"Restaurer {len(ids)} BL supprimé(s) ?")
    col_oui, col_non = st.columns(2)
    if col_oui.button("✅ Confirmer", type="primary", width="stretch"):
        for id_bl in ids:
            row = df[df["id_bl"] == id_bl].iloc[0]
            repository.restaurer_bl(
                id_bl, utilisateur, expected_version=int(row["version"])
            )
        ui.set_flash("success", f"{len(ids)} BL restauré(s).")
        _vider_grille(cle_grille)
        st.rerun()
    if col_non.button("Annuler", width="stretch"):
        st.rerun()


# =====================================================================
# VUES « BL » (réception / expédition) — KPI + grille + ruban d'actions
# =====================================================================
def vue_bl(nom_vue: str, types: list[str]) -> None:
    avec_statut = repository.TYPE_RECEPTION in types
    achat = types == repository.TYPES_ACHAT
    tiers_lib = "Fournisseur" if achat else "Client"
    lecture = LECTURE_SEULE

    # Clés capturées par les « écrans » (filtres + colonnes affichées).
    cles_ecran = [f"per_bl_{nom_vue}", f"perso_bl_{nom_vue}", f"f_num_{nom_vue}",
                  f"f_frs_{nom_vue}", f"f_sup_{nom_vue}", f"cols_{nom_vue}"]
    if avec_statut:
        cles_ecran.append(f"f_st_{nom_vue}")
    if achat:
        cles_ecran.append(f"f_gest_{nom_vue}")
    appliquer_ecran_defaut(f"bl:{nom_vue}", cles_ecran)

    # --- Filtres (boutons + saisies) ---
    with st.expander("🔍 Filtres", expanded=False):
        dmin, dmax, sel_per = filtre_periode(f"bl_{nom_vue}")
        c1, c2 = st.columns(2)
        f_numero = c1.text_input("Numéro contient", key=f"f_num_{nom_vue}").strip()
        f_tiers = c2.text_input(f"{tiers_lib} contient", key=f"f_frs_{nom_vue}").strip()
        if avec_statut:
            f_statut = st.pills("État", ["🟥 EDI NOK", "✅ OK"], key=f"f_st_{nom_vue}",
                                default="🟥 EDI NOK")
        else:
            f_statut = None
        if achat:
            f_gest = st.pills("Gestionnaire", repository.lister_gestionnaires(),
                              key=f"f_gest_{nom_vue}") or ""
        else:
            f_gest = ""
        f_suppr = st.checkbox("Inclure les BL supprimés", key=f"f_sup_{nom_vue}")
    statut = {"✅ OK": repository.STATUT_OK,
              "🟥 EDI NOK": repository.STATUT_EDI_NOK}.get(f_statut)

    # --- Écrans + colonnes affichées (contrôles discrets) ---
    colonnes_dispo = ["Numéro", "Date", "Plage", tiers_lib, "Quai"]
    if avec_statut:
        colonnes_dispo.append("État")
    colonnes_dispo += ["DESADV", "Opération", "Commentaire", "Pages",
                       "Saisi par", "Saisi le", "Supprimé"]
    cle_cols = f"cols_{nom_vue}"
    # Amorçage / assainissement AVANT le widget (valeurs venues d'un écran).
    actuel = st.session_state.get(cle_cols)
    valides = [c for c in (actuel or []) if c in colonnes_dispo]
    st.session_state[cle_cols] = valides or colonnes_dispo
    with st.container(horizontal=True, gap="small"):
        gestion_ecrans(f"bl:{nom_vue}", cles_ecran)
        with st.popover("🧩 Colonnes"):
            cols_choisies = st.multiselect("Colonnes affichées", colonnes_dispo,
                                           key=cle_cols)

    # --- Chips des filtres appliqués (retirables une à une ou toutes) ---
    chips = chips_periode(f"bl_{nom_vue}", sel_per)
    if f_numero:
        chips.append((f"N° « {f_numero} »", f"f_num_{nom_vue}", ""))
    if f_tiers:
        chips.append((f"{tiers_lib} « {f_tiers} »", f"f_frs_{nom_vue}", ""))
    if f_statut:
        chips.append((f_statut, f"f_st_{nom_vue}", None))
    if f_gest:
        chips.append((f"👤 {f_gest}", f"f_gest_{nom_vue}", None))
    if f_suppr:
        chips.append(("🗑️ Supprimés inclus", f"f_sup_{nom_vue}", False))
    afficher_chips(chips, f"blc_{nom_vue}",
                   cles_effacer=[c for c in cles_ecran if c != f"cols_{nom_vue}"])

    # Pagination et sélection réinitialisées quand les filtres changent.
    signature = (f_numero, f_tiers, str(dmin), str(dmax), f_statut, f_gest, f_suppr)
    cle_page, cle_grille = f"page_{nom_vue}", f"grille_{nom_vue}"
    if st.session_state.get(f"sig_{nom_vue}") != signature:
        st.session_state[f"sig_{nom_vue}"] = signature
        st.session_state[cle_page] = 1
        _vider_grille(cle_grille)
    page = st.session_state.setdefault(cle_page, 1)

    try:
        df, total = repository.rechercher_bl(
            numero=f_numero, fournisseur=f_tiers, types=types,
            date_min=dmin, date_max=dmax, statut=statut, gestionnaire=f_gest,
            inclure_supprimes=f_suppr, page=page, page_size=TAILLE_PAGE,
        )
        df = df.reset_index(drop=True)
        photos = repository.photos_pour_bls(df["id_bl"].tolist() if not df.empty else [])
        stats = repository.stats_bl(numero=f_numero, fournisseur=f_tiers, types=types,
                                    date_min=dmin, date_max=dmax, gestionnaire=f_gest,
                                    inclure_supprimes=f_suppr)
    except Exception as e:
        st.error(f"Erreur de lecture de la base : {e}")
        st.stop()

    # --- KPI du périmètre filtré (hors filtre d'état) ---
    if avec_statut:
        taux = f"{100 * stats['nok'] / stats['total']:.0f} %" if stats["total"] else "—"
        k = st.columns(5)
        k[0].metric("BL (périmètre)", stats["total"])
        k[1].metric("EDI NOK", stats["nok"])
        k[2].metric("OK", stats["ok"])
        k[3].metric("Taux EDI NOK", taux)
        k[4].metric("Pages jointes", stats["pages"])
    else:
        k = st.columns(2)
        k[0].metric("BL (périmètre)", stats["total"])
        k[1].metric("Pages jointes", stats["pages"])

    ruban = st.container()                     # rempli après la grille (sélection à jour)

    # --- Grille : un clic sur une ligne (cellule) la sélectionne ; cases à
    # cocher pour la sélection multiple. Tri natif par en-tête de colonne. ---
    ids_selection: list[str] = []
    if df.empty:
        st.info("Aucun BL ne correspond aux filtres.")
    else:
        colonnes = {
            "Numéro": df["numero_bl"],
            "Date": df["date_reception"],
            "Plage": df["plage_horaire"],
            tiers_lib: df["nom_fournisseur"],
            "Quai": df["quai_reception"],
        }
        if avec_statut:
            colonnes["État"] = df["statut_bl"].map(ui.libelle_statut)
        rappro = (df["desadv_rapproche"] if "desadv_rapproche" in df.columns
                  else pd.Series([None] * len(df)))
        colonnes.update({
            "DESADV": rappro.map(lambda x: "🔗 ✓" if (pd.notna(x) and bool(x)) else "—"),
            "Opération": df["type_operation"].map(
                lambda t: repository.LIBELLES_OPERATION.get(t, t)),
            "Commentaire": df["comment_bl"],
            "Pages": df["id_bl"].map(lambda i: len(photos.get(i, []))),
            "Saisi par": df["saisie_par"],
            "Saisi le": df["saisie_le"],
            "Supprimé": df["est_supprime"].fillna(False).map(lambda x: "🗑️" if x else ""),
        })
        df_aff = pd.DataFrame(colonnes)
        evenement = st.dataframe(
            df_aff, hide_index=True, width="stretch", key=cle_grille,
            on_select="rerun", selection_mode=["multi-row", "multi-cell"],
            column_order=[c for c in colonnes_dispo if c in cols_choisies] or None,
        )
        lignes = set(evenement.selection.rows)
        lignes.update(r for r, _ in evenement.selection.cells)
        lignes = {r for r in lignes if 0 <= r < len(df)}   # sélection périmée
        ids_selection = df.loc[sorted(lignes), "id_bl"].tolist()

    # --- Pagination (50 lignes par page) ---
    nb_pages = max((total + TAILLE_PAGE - 1) // TAILLE_PAGE, 1)
    if nb_pages > 1:
        col_prec, col_info, col_suiv = st.columns([1, 2, 1])
        if col_prec.button("⬅️", disabled=page <= 1, key=f"prec_{nom_vue}", width="stretch"):
            st.session_state[cle_page] -= 1
            _vider_grille(cle_grille)
            st.rerun()
        col_info.markdown(f"<div style='text-align:center'>page {page} / {nb_pages}</div>",
                          unsafe_allow_html=True)
        if col_suiv.button("➡️", disabled=page >= nb_pages, key=f"suiv_{nom_vue}",
                           width="stretch"):
            st.session_state[cle_page] += 1
            _vider_grille(cle_grille)
            st.rerun()

    # --- Ruban d'actions contextuel (réduit en lecture seule) ---
    with ruban:
        n = len(ids_selection)
        specs = [
            ("🔄 Actualiser", "act", False, 1.3),
            ("🖼️ Voir les images", "img", n != 1, 1.9),
            ("📄 PDF", "pdf", n != 1, 1.0),
            ("🕘 Historique", "his", n != 1, 1.4),
        ]
        if not lecture:
            specs.insert(1, ("✏️ Modifier", "mod", n != 1, 1.3))
            if avec_statut:
                specs.append(("✅ Passer à OK", "ok", n == 0, 1.5))
            specs += [("🗑️ Supprimer", "sup", n == 0, 1.4),
                      ("♻️ Restaurer", "res", n == 0, 1.4)]
        cols = st.columns([s[3] for s in specs] + [2.2])
        clics = {}
        for i, (label, code, disabled, _) in enumerate(specs):
            aide = ("Cliquez sur une ligne pour la sélectionner."
                    if code in ("mod", "img", "pdf", "his")
                    else "Passe les BL EDI NOK sélectionnés à OK." if code == "ok" else None)
            clics[code] = cols[i].button(label, key=f"{code}_{nom_vue}", disabled=disabled,
                                         width="stretch", help=aide)
        etat_droits = " · 🔒 lecture seule" if lecture else ""
        cols[-1].markdown(f"**{total}** BL · **{n}** sélectionné(s){etat_droits}")

        if clics["act"]:
            _vider_grille(cle_grille)
            st.rerun()
        if clics.get("mod"):
            ligne = df[df["id_bl"] == ids_selection[0]].iloc[0].to_dict()
            dialog_modifier_bl(ligne, photos.get(ids_selection[0], []), cle_grille)
        if clics["img"]:
            ligne = df[df["id_bl"] == ids_selection[0]].iloc[0]
            dialog_voir_images(ligne["numero_bl"], photos.get(ids_selection[0], []))
        if clics["pdf"]:
            ligne = df[df["id_bl"] == ids_selection[0]].iloc[0].to_dict()
            dialog_export_pdf(ligne, photos.get(ids_selection[0], []))
        if clics["his"]:
            ligne = df[df["id_bl"] == ids_selection[0]].iloc[0]
            dialog_historique_bl(ligne["numero_bl"], ligne["id_bl"])
        if clics.get("ok"):
            dialog_confirmer_ok(df, ids_selection, cle_grille)
        if clics.get("sup"):
            dialog_supprimer_bls(df, ids_selection, cle_grille)
        if clics.get("res"):
            dialog_confirmer_restauration(df, ids_selection, cle_grille)


# =====================================================================
# VUES « RÉFÉRENTIEL » — grille éditable triable (CRUD avec confirmation)
# =====================================================================
def vue_referentiel(nom_ref: str, nom_vue: str, valeurs_fixes: dict | None = None,
                    config_colonnes: dict | None = None,
                    df_charge: pd.DataFrame | None = None) -> None:
    if df_charge is None:
        try:
            df = repository.lire_referentiel(nom_ref, valeurs_fixes)
        except Exception as e:
            st.error(f"Erreur de lecture de la base : {e}")
            st.stop()
        visibles = [c for c in df.columns if c not in (valeurs_fixes or {})]
        df = df[visibles]
    else:
        df = df_charge
    df = df.reset_index(drop=True)
    cle = f"ref_{nom_vue}"
    libelles_tri = {c: (cfg.get("label") if isinstance(cfg, dict) else None) or c
                    for c, cfg in (config_colonnes or {}).items()}

    if LECTURE_SEULE:
        st.caption("🔒 Lecture seule (vos rôles ne permettent pas la modification).")
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config=config_colonnes or {})
        st.markdown(f"**{len(df)}** enregistrement(s)")
        return

    ruban = st.container()
    df, suffixe_tri = _tri_grille(df, cle, libelles_tri)
    st.caption("Ajoutez une ligne en bas de la grille, modifiez une cellule ou supprimez des "
               "lignes (sélection + touche Suppr), puis cliquez sur **💾 Enregistrer**.")
    edite = st.data_editor(df, num_rows="dynamic", width="stretch",
                           key=f"{cle}{suffixe_tri}", hide_index=True,
                           column_config=config_colonnes or {})

    with ruban:
        c1, c2, c3 = st.columns([1.6, 1.4, 5])
        if c1.button("💾 Enregistrer", type="primary", key=f"save_{nom_vue}",
                     width="stretch"):
            dialog_confirmer_grille(nom_ref, nom_vue, df, edite, valeurs_fixes, cle)
        if c2.button("🔄 Actualiser", key=f"refresh_{nom_vue}", width="stretch"):
            _vider_grille(cle)
            st.rerun()
        c3.markdown(f"**{len(df)}** enregistrement(s)")


# =====================================================================
# VUE « DESADV » — filtres boutons + chips + KPI EDI + grille triable
# =====================================================================
def vue_desadv(sens: str) -> None:
    achat = sens == repository.SENS_ACHAT
    tiers_lib = "Fournisseur" if achat else "Client"
    type_tiers = repository.TIERS_FOURNISSEUR if achat else repository.TIERS_CLIENT
    suffixe = sens.lower()
    lecture = LECTURE_SEULE

    # Fraîcheur du flux EDI, en haut à droite de la vue.
    try:
        fraicheur = repository.fraicheur_desadv(sens)
        integration = fraicheur.get("integration")
        creation = fraicheur.get("creation")
        texte_frais = (
            f"📡 Dernière intégration EDI : "
            f"{integration:%d/%m/%Y}" if integration is not None else
            "📡 Aucune intégration EDI connue")
        if creation is not None:
            texte_frais += f" · dernier message : {pd.Timestamp(creation):%d/%m/%Y %H:%M}"
    except Exception:
        texte_frais = ""
    if texte_frais:
        st.markdown(f"<div style='text-align:right;color:#5B6B7C;"
                    f"font-size:0.85rem;margin-top:-0.6rem'>{texte_frais}</div>",
                    unsafe_allow_html=True)

    cles_ecran = [f"per_dsd_{suffixe}", f"perso_dsd_{suffixe}", f"dnum_{suffixe}",
                  f"dfrs_{suffixe}", f"dsedi_{suffixe}",
                  f"tri_desadv_{suffixe}", f"sens_tri_desadv_{suffixe}"]
    if achat:
        cles_ecran.append(f"dgest_{suffixe}")
    appliquer_ecran_defaut(f"desadv:{suffixe}", cles_ecran)

    with st.expander("🔍 Filtres", expanded=False):
        dmin, dmax, sel_per = filtre_periode(f"dsd_{suffixe}", "Période d'intégration")
        c1, c2 = st.columns(2)
        f_num = c1.text_input("Numéro de BL contient", key=f"dnum_{suffixe}").strip()
        f_frs = c2.text_input(f"{tiers_lib} contient", key=f"dfrs_{suffixe}").strip()
        f_sedi = st.pills("État EDI", ["🟥 EDI NOK", "✅ OK"], key=f"dsedi_{suffixe}",
                          default="🟥 EDI NOK")
        if achat:
            f_gest = st.pills("Gestionnaire", repository.lister_gestionnaires(),
                              key=f"dgest_{suffixe}") or ""
        else:
            f_gest = ""
    statut_edi = {"✅ OK": "OK", "🟥 EDI NOK": "EDI NOK"}.get(f_sedi, "")

    with st.container(horizontal=True, gap="small"):
        gestion_ecrans(f"desadv:{suffixe}", cles_ecran)

    chips = chips_periode(f"dsd_{suffixe}", sel_per)
    if f_num:
        chips.append((f"N° « {f_num} »", f"dnum_{suffixe}", ""))
    if f_frs:
        chips.append((f"{tiers_lib} « {f_frs} »", f"dfrs_{suffixe}", ""))
    if f_sedi:
        chips.append((f_sedi, f"dsedi_{suffixe}", None))
    if f_gest:
        chips.append((f"👤 {f_gest}", f"dgest_{suffixe}", None))
    afficher_chips(chips, f"dsdc_{suffixe}",
                   cles_effacer=[c for c in cles_ecran if not c.startswith(("tri_", "sens_tri"))])

    try:
        df = repository.lire_desadv(sens, f_num, f_frs, f_gest, dmin, dmax,
                                    statut_edi=statut_edi).reset_index(drop=True)
    except Exception as e:
        st.error(f"Erreur de lecture de la base : {e}")
        st.stop()

    # KPI de l'état des messages EDI (sur le périmètre filtré).
    nb_ok = int((df["statut_edi"] == "OK").sum())
    nb_nok = int((df["statut_edi"] == "EDI NOK").sum())
    taux_nok = f"{100 * nb_nok / len(df):.0f} %" if len(df) else "—"
    k = st.columns(4)
    k[0].metric("Avis (filtrés)", len(df))
    k[1].metric("EDI OK", nb_ok)
    k[2].metric("EDI NOK", nb_nok)
    k[3].metric("Taux EDI NOK", taux_nok)

    cle = f"desadv_{suffixe}"
    config = {
        "numero_bl": st.column_config.TextColumn("Numéro de BL", required=True),
        "nom_fournisseur": st.column_config.SelectboxColumn(
            tiers_lib, options=repository.lister_tiers(type_tiers), required=True),
        "issuedatetime": st.column_config.DatetimeColumn("Créé le", disabled=True),
        "integrationdate": st.column_config.DateColumn("Date d'intégration", disabled=True),
        "statut_edi": st.column_config.TextColumn("État EDI", disabled=True),
    }

    if lecture:
        st.caption("🔒 Lecture seule (vos rôles ne permettent pas la modification).")
        st.dataframe(df, hide_index=True, width="stretch", column_config=config)
        st.markdown(f"**{len(df)}** avis d'expédition")
        return

    ruban = st.container()
    df, suffixe_tri = _tri_grille(df, cle, {"numero_bl": "Numéro de BL",
                                            "nom_fournisseur": tiers_lib,
                                            "issuedatetime": "Créé le",
                                            "integrationdate": "Date d'intégration",
                                            "statut_edi": "État EDI"})
    st.caption("Ajoutez / modifiez / supprimez des lignes (numéro de BL unique par sens), "
               "puis **💾 Enregistrer**. « Créé le », « Date d'intégration » et « État EDI » "
               "proviennent du flux EDI (lecture seule, rafraîchis par le job).")
    edite = st.data_editor(df, num_rows="dynamic", width="stretch",
                           key=f"{cle}{suffixe_tri}", hide_index=True, column_config=config)

    with ruban:
        c1, c2, c3 = st.columns([1.6, 1.4, 5])
        if c1.button("💾 Enregistrer", type="primary", key=f"save_{cle}", width="stretch"):
            dialog_confirmer_grille("desadv", f"DESADV {sens}", df, edite, {"sens": sens}, cle)
        if c2.button("🔄 Actualiser", key=f"refresh_{cle}", width="stretch"):
            _vider_grille(cle)
            st.rerun()
        c3.markdown(f"**{len(df)}** avis d'expédition")


# =====================================================================
# VUES filtrées du module Gestion (portefeuilles, sites, PLA, rôles)
# =====================================================================
def vue_portefeuilles() -> None:
    with st.expander("🔍 Filtres", expanded=False):
        f_gest = st.pills("Gestionnaire", repository.lister_gestionnaires(),
                          key="pf_gest") or ""
        f_frs = st.text_input("Fournisseur contient", key="pf_frs").strip()
    chips = []
    if f_gest:
        chips.append((f"👤 {f_gest}", "pf_gest", None))
    if f_frs:
        chips.append((f"🔎 « {f_frs} »", "pf_frs", ""))
    afficher_chips(chips, "pf")
    try:
        df = repository.lire_portefeuilles(f_gest, f_frs)
    except Exception as e:
        st.error(f"Erreur de lecture de la base : {e}")
        st.stop()
    vue_referentiel(
        "portefeuilles", "Portefeuilles", df_charge=df,
        config_colonnes={
            "code_gestionnaire": st.column_config.SelectboxColumn(
                "Gestionnaire", options=repository.lister_gestionnaires(), required=True),
            "nom_fournisseur": st.column_config.SelectboxColumn(
                "Fournisseur", options=repository.lister_tiers(repository.TIERS_FOURNISSEUR),
                required=True),
        })


def vue_sites_logistiques() -> None:
    with st.expander("🔍 Filtres", expanded=False):
        c1, c2 = st.columns(2)
        tiers_options = [""] + repository.lister_tous_tiers()
        f_ent = c1.selectbox("Entité (fournisseur ou client)", tiers_options,
                             format_func=lambda t: t or "Toutes", key="sl_ent")
        f_adr = c2.text_input("Adresse contient", key="sl_adr").strip()
    chips = []
    if f_ent:
        chips.append((f"🏢 {f_ent}", "sl_ent", ""))
    if f_adr:
        chips.append((f"🔎 « {f_adr} »", "sl_adr", ""))
    afficher_chips(chips, "sl")
    try:
        df = repository.lire_sites_logistiques(f_ent, f_adr)
    except Exception as e:
        st.error(f"Erreur de lecture de la base : {e}")
        st.stop()
    vue_referentiel(
        "sites_logistiques", "Sites logistiques", df_charge=df,
        config_colonnes={
            "entite": st.column_config.SelectboxColumn(
                "Entité", options=repository.lister_tous_tiers(), required=True),
            "adresse": st.column_config.SelectboxColumn(
                "Adresse", options=repository.lister_adresses(), required=True,
                help="Les adresses se gèrent dans la vue Adresses."),
        })


def vue_pla() -> None:
    st.caption("Protocole logistique d'achat : un protocole par tiers. Le quai du "
               "PLA pré-remplit automatiquement le champ Quai de l'app Création "
               f"(défaut « {repository.QUAI_DEFAUT} » pour un tiers sans PLA).")
    vue_referentiel(
        "pla", "PLA",
        config_colonnes={
            "nom_fournisseur": st.column_config.SelectboxColumn(
                "Tiers (fournisseur / client)", options=repository.lister_tous_tiers(),
                required=True),
            "code_quai": st.column_config.SelectboxColumn(
                "Quai", options=repository.lister_quais(), required=True),
            "jours_livraison": st.column_config.TextColumn(
                "Jours de livraison", help="Ex. « lundi, mercredi, vendredi »"),
            "frequence_livraison": st.column_config.TextColumn(
                "Fréquence de livraison", help="Ex. « quotidienne », « 2x/semaine »"),
        })


def vue_roles() -> None:
    st.caption("RBAC strict : rôles applicatifs par utilisateur (email Databricks). "
               "Un utilisateur sans rôle n'a aucun accès. La matrice des droits "
               "par vue est portée par le code (bl_core/rbac.py). Le dernier "
               "ADMIN_METIER ne peut pas être supprimé.")
    vue_referentiel(
        "roles", "Rôles",
        config_colonnes={
            "utilisateur": st.column_config.TextColumn(
                "Utilisateur (email)", required=True),
            "role": st.column_config.SelectboxColumn(
                "Rôle", options=rbac.ROLES, required=True),
        })


# =====================================================================
# VUE « ÉCARTS » — BL sans DESADV / DESADV sans BL (lecture seule)
# =====================================================================
def vue_ecarts(sens: str) -> None:
    achat = sens == repository.SENS_ACHAT
    tiers_lib = "Fournisseur" if achat else "Client"
    suffixe = f"ec_{sens.lower()}"
    st.caption("Rapprochement BL ⇄ DESADV (marqué par le job de synchronisation, "
               "colonne « DESADV » des vues BL). Ci-dessous, ce qui ne se "
               "rapproche pas — comparaison insensible à la casse du numéro.")

    with st.expander("🔍 Filtres", expanded=False):
        # Sans période sélectionnée : tout l'historique (les écarts s'accumulent).
        dmin, dmax, sel_per = filtre_periode(suffixe, defaut=())
        f_frs = st.text_input(f"{tiers_lib} contient", key=f"frs_{suffixe}").strip()
        f_gest = (st.pills("Gestionnaire", repository.lister_gestionnaires(),
                           key=f"gest_{suffixe}") or "") if achat else ""

    chips = chips_periode(suffixe, sel_per)
    if f_frs:
        chips.append((f"{tiers_lib} « {f_frs} »", f"frs_{suffixe}", ""))
    if f_gest:
        chips.append((f"👤 {f_gest}", f"gest_{suffixe}", None))
    afficher_chips(chips, suffixe,
                   cles_effacer=[f"per_{suffixe}", f"frs_{suffixe}", f"gest_{suffixe}"])

    try:
        bl_sans, desadv_sans = repository.lire_ecarts(sens, f_frs, f_gest, dmin, dmax)
    except Exception as e:
        st.error(f"Erreur de lecture de la base : {e}")
        st.stop()

    k1, k2 = st.columns(2)
    k1.metric("BL sans DESADV", len(bl_sans))
    k2.metric("DESADV sans BL", len(desadv_sans))

    col_g, col_d = st.columns(2)
    with col_g:
        st.markdown("#### 📥 BL sans avis d'expédition")
        if bl_sans.empty:
            st.success("Aucun écart : tous les BL ont un DESADV.")
        else:
            st.dataframe(bl_sans, hide_index=True, width="stretch",
                         column_config={
                             "numero_bl": st.column_config.TextColumn("Numéro de BL"),
                             "date_reception": st.column_config.DateColumn("Date"),
                             "nom_fournisseur": st.column_config.TextColumn(tiers_lib),
                             "saisie_par": st.column_config.TextColumn("Saisi par"),
                             "saisie_le": st.column_config.DatetimeColumn("Saisi le"),
                         })
    with col_d:
        st.markdown("#### 📡 Avis d'expédition sans BL")
        if desadv_sans.empty:
            st.success("Aucun écart : tous les DESADV ont un BL.")
        else:
            st.dataframe(desadv_sans, hide_index=True, width="stretch",
                         column_config={
                             "numero_bl": st.column_config.TextColumn("Numéro de BL"),
                             "nom_fournisseur": st.column_config.TextColumn(tiers_lib),
                             "integrationdate": st.column_config.DateColumn("Intégré le"),
                             "statut_edi": st.column_config.TextColumn("État EDI"),
                         })


# =====================================================================
# VUE « QUALITÉ IA » — précision de l'extraction, champ par champ
# =====================================================================
def vue_qualite_ia() -> None:
    st.caption("Comparaison « valeur IA vs valeur validée » journalisée par "
               "l'app Création au passage de l'étape 3 : taux de précision "
               "champ par champ, pour améliorer le prompt d'extraction.")
    try:
        stats = repository.stats_qualite_extraction()
    except Exception as e:
        st.error(f"Erreur de lecture : {e} (migrations V5 exécutées ?)")
        st.stop()
    if stats is None or stats.empty:
        st.info("Aucune mesure pour l'instant : les mesures s'accumulent à "
                "chaque BL validé avec l'extraction IA active.")
        return
    k = st.columns(min(len(stats), 6))
    for i, (_, ligne) in enumerate(stats.iterrows()):
        k[i % len(k)].metric(f"Précision « {ligne['champ']} »",
                             f"{ligne['precision_pct']} %",
                             help=f"{int(ligne['exactes'])}/{int(ligne['mesures'])} mesures exactes")
    st.dataframe(stats, hide_index=True, width="stretch",
                 column_config={
                     "champ": st.column_config.TextColumn("Champ"),
                     "mesures": st.column_config.NumberColumn("Mesures"),
                     "exactes": st.column_config.NumberColumn("Exactes"),
                     "precision_pct": st.column_config.NumberColumn("Précision (%)"),
                 })
    with st.expander("Journal détaillé (500 dernières mesures)"):
        try:
            st.dataframe(repository.lister_qualite_extraction(), hide_index=True,
                         width="stretch")
        except Exception as e:
            st.caption(f"Journal indisponible : {e}")


# =====================================================================
# VUE « NOTIFICATIONS » (lecture seule)
# =====================================================================
def vue_notifications() -> None:
    try:
        df = repository.lister_notifications()
    except Exception as e:
        st.error(f"Erreur de lecture de la base : {e}")
        st.stop()
    if st.button("🔄 Actualiser", key="notif_refresh"):
        st.rerun()
    if df is None or df.empty:
        st.info("Aucune notification pour l'instant.")
        return
    total = len(df)
    envoyees = int(df["envoyee"].fillna(False).astype(bool).sum())
    k = st.columns(3)
    k[0].metric("Notifications", total)
    k[1].metric("Envoyées dans Teams", envoyees)
    k[2].metric("En échec", total - envoyees,
                delta_color="inverse",
                help="Carte non publiée (Teams indisponible au moment de l'envoi). "
                     "Le détail figure dans la colonne « Erreur ».")
    st.dataframe(
        df, hide_index=True, width="stretch",
        column_config={
            "cree_le": st.column_config.DatetimeColumn("Date"),
            "type_notif": st.column_config.TextColumn("Type"),
            "numero_bl": st.column_config.TextColumn("N° BL"),
            "message": st.column_config.TextColumn("Message", width="large"),
            "commentaire": st.column_config.TextColumn("Commentaire"),
            "destinataires": st.column_config.TextColumn("Gestionnaire(s) mentionné(s)"),
            "cree_par": st.column_config.TextColumn("Par"),
            "envoyee": st.column_config.CheckboxColumn("Envoyée"),
            "envoyee_le": st.column_config.DatetimeColumn("Envoyée le"),
            "erreur_envoi": st.column_config.TextColumn("Erreur"),
        })
    st.caption("Journal en lecture seule. Les cartes sont publiées dans Teams "
               "directement par les applications, au moment de l'événement.")


# =====================================================================
# VUE « RAPPORTS D'ACTIVITÉ » — catalogue, lecture et génération
# =====================================================================
@st.dialog("📄 Rapport d'activité", width="large")
def dialog_rapport(rapport_id: int):
    """Ouvre un rapport : analyse rédigée, indicateurs clés, PDF téléchargeable."""
    rapport = repository.telecharger_rapport(rapport_id)
    if rapport is None:
        st.error("Ce rapport n'existe plus.")
        return
    st.markdown(f"#### {rapport['libelle']}")
    st.caption(
        f"{rapports.LIBELLES_PERIODICITE.get(rapport['periodicite'], rapport['periodicite'])}"
        f" · du {rapport['periode_debut']:%d/%m/%Y} au {rapport['periode_fin']:%d/%m/%Y}"
        f" · généré le {rapport['genere_le']:%d/%m/%Y à %H:%M} par {rapport['genere_par']}")

    metriques = rapport.get("metriques") or {}
    courant = metriques.get("courant") or {}
    if courant:
        k = st.columns(4)
        k[0].metric("BL traités", courant.get("total", 0))
        k[1].metric("Réceptions", courant.get("receptions", 0))
        k[2].metric("Taux EDI NOK", f"{courant.get('taux_nok', 0)} %")
        k[3].metric("Rapprochement", f"{courant.get('taux_rapproches', 0)} %")

    if rapport.get("synthese"):
        for titre, lignes in rapports.decouper_analyse(rapport["synthese"]):
            st.markdown(f"**{titre}**")
            st.markdown("\n".join(f"{ligne}" for ligne in lignes))
        if not rapport.get("analyse_ia"):
            st.caption("⚠️ Rapport produit sans analyse du modèle "
                       "(endpoint non configuré ou indisponible à ce moment-là).")

    st.download_button(
        "⬇️ Télécharger le PDF", data=rapport["contenu"],
        file_name=rapports.nom_fichier(rapport["periodicite"],
                                       rapport["periode_debut"]),
        mime="application/pdf", type="primary", width="stretch")


@st.dialog("🛠️ Générer un rapport")
def dialog_generer_rapport():
    """Génération à la demande — utile pour rattraper une période ou
    régénérer après correction de données."""
    st.caption("Le rapport de la période **contenant la date choisie** est "
               "produit puis enregistré. Un rapport existant pour cette même "
               "période est remplacé.")
    periodicite = st.selectbox(
        "Périodicité", options=rapports.PERIODICITES,
        format_func=lambda p: rapports.LIBELLES_PERIODICITE[p])
    reference = st.date_input(
        "Date de référence", value=datetime.date.today() - datetime.timedelta(days=1),
        max_value=datetime.date.today())
    debut, fin, libelle = rapports.bornes(periodicite, reference)
    st.info(f"Période visée : **{libelle}** "
            f"(du {debut:%d/%m/%Y} au {fin:%d/%m/%Y})")
    avec_ia = st.toggle("Inclure l'analyse rédigée par le modèle", value=True)
    if st.button("📝 Générer", type="primary", width="stretch"):
        rbac.exiger_vue(VUE_RAPPORTS, CTX_RBAC, rbac.MODIFICATION)
        with st.spinner("Calcul des indicateurs, analyse et mise en page…"):
            try:
                resultat = rapports.generer_rapport(
                    periodicite, reference, utilisateur, avec_ia=avec_ia)
            except Exception as exc:
                logger.exception("Génération du rapport %s en échec", periodicite)
                st.error(f"Génération impossible : {exc}")
                return
        ui.set_flash(
            "toast",
            f"Rapport « {resultat['libelle']} » généré "
            f"({resultat['total_bl']} BL, {resultat['taille_octets'] // 1024} Ko)")
        st.rerun()


def vue_rapports() -> None:
    st.caption("Rapports périodiques d'activité — volumes, qualité EDI, "
               "rapprochement DESADV, notifications et précision de l'IA, "
               "commentés automatiquement. Générés chaque nuit par le job "
               "« rapports_activite » ; consultables et téléchargeables ici.")

    barre = st.columns([3, 1])
    with barre[0]:
        types_sel = st.pills(
            "Périodicité", options=rapports.PERIODICITES, selection_mode="multi",
            format_func=lambda p: rapports.LIBELLES_PERIODICITE[p],
            default=[], key="rap_periodicites")
    with barre[1]:
        if not LECTURE_SEULE and st.button("📝 Générer un rapport",
                                           width="stretch", type="primary"):
            dialog_generer_rapport()
    dmin, dmax, _sel = filtre_periode("rap", libelle="Période couverte", defaut=())

    try:
        df = repository.lister_rapports(
            periodicites=list(types_sel) or None, date_min=dmin, date_max=dmax)
    except Exception as e:
        st.error(f"Erreur de lecture : {e} (migration V004 exécutée ?)")
        st.stop()

    if df is None or df.empty:
        st.info("Aucun rapport pour ces critères. Le job quotidien produit le "
                "rapport de la veille, et ceux des périodes qui viennent de se "
                "clore (semaine, mois, trimestre, année).")
        return

    k = st.columns(4)
    k[0].metric("Rapports disponibles", len(df))
    k[1].metric("Avec analyse IA",
                int(df["analyse_ia"].fillna(False).astype(bool).sum()))
    k[2].metric("Plus récent", f"{df['periode_debut'].max():%d/%m/%Y}")
    k[3].metric("Volume stocké", f"{int(df['taille_octets'].sum()) // 1024} Ko")

    affichage = df.assign(
        periodicite=df["periodicite"].map(rapports.LIBELLES_PERIODICITE),
        taille_ko=(df["taille_octets"] // 1024).astype(int),
    )
    cle_grille = "grille_rapports"
    selection = st.dataframe(
        affichage[["libelle", "periodicite", "periode_debut", "periode_fin",
                   "analyse_ia", "taille_ko", "genere_le", "genere_par"]],
        hide_index=True, width="stretch", height=430,
        on_select="rerun", selection_mode="single-row", key=cle_grille,
        column_config={
            "libelle": st.column_config.TextColumn("Rapport", width="large"),
            "periodicite": st.column_config.TextColumn("Périodicité"),
            "periode_debut": st.column_config.DateColumn("Début", format="DD/MM/YYYY"),
            "periode_fin": st.column_config.DateColumn("Fin", format="DD/MM/YYYY"),
            "analyse_ia": st.column_config.CheckboxColumn("Analyse IA"),
            "taille_ko": st.column_config.NumberColumn("Taille (Ko)"),
            "genere_le": st.column_config.DatetimeColumn("Généré le",
                                                         format="DD/MM/YYYY HH:mm"),
            "genere_par": st.column_config.TextColumn("Par"),
        })

    lignes = (selection.selection.rows if selection and selection.selection else [])
    if not lignes:
        st.caption("Sélectionnez une ligne pour ouvrir le rapport ou le télécharger.")
        return

    rapport = df.iloc[lignes[0]]
    actions = st.columns([2, 2, 2, 4])
    if actions[0].button("📖 Ouvrir", width="stretch", type="primary"):
        dialog_rapport(int(rapport["id"]))
    # Le PDF n'est chargé que si l'utilisateur demande explicitement le
    # téléchargement : la grille reste légère même avec des centaines de rapports.
    if actions[1].button("⬇️ Préparer le PDF", width="stretch"):
        st.session_state.rap_telecharger = int(rapport["id"])
    if not LECTURE_SEULE and actions[2].button("🔄 Regénérer", width="stretch",
                                               help="Recalcule ce rapport sur "
                                                    "les données actuelles."):
        rbac.exiger_vue(VUE_RAPPORTS, CTX_RBAC, rbac.MODIFICATION)
        with st.spinner("Regénération…"):
            rapports.generer_rapport(rapport["periodicite"],
                                     rapport["periode_debut"], utilisateur)
        ui.set_flash("toast", f"Rapport « {rapport['libelle']} » regénéré")
        st.rerun()

    if st.session_state.get("rap_telecharger") == int(rapport["id"]):
        contenu = repository.telecharger_rapport(int(rapport["id"]))
        if contenu:
            st.download_button(
                f"⬇️ {rapports.nom_fichier(rapport['periodicite'], rapport['periode_debut'])}",
                data=contenu["contenu"],
                file_name=rapports.nom_fichier(rapport["periodicite"],
                                               rapport["periode_debut"]),
                mime="application/pdf", width="stretch")


# =====================================================================
# ROUTAGE
# =====================================================================
def render_vue_courante() -> None:
    if vue != VUE_DASHBOARD:
        st.markdown(f"### {section} › {LIBELLES_VUES.get(vue, vue)}")

    if vue == VUE_DASHBOARD:
        render_dashboard()
    elif vue == VUE_RAPPORTS:
        vue_rapports()
    elif vue == "BL réception":
        vue_bl(vue, repository.TYPES_ACHAT)
    elif vue == "BL expédition":
        vue_bl(vue, repository.TYPES_VENTE)
    elif vue == "DESADV achat":
        vue_desadv(repository.SENS_ACHAT)
    elif vue == "DESADV vente":
        vue_desadv(repository.SENS_VENTE)
    elif vue == "Rapprochement achat":
        vue_ecarts(repository.SENS_ACHAT)
    elif vue == "Rapprochement vente":
        vue_ecarts(repository.SENS_VENTE)
    elif vue == "Fournisseurs":
        vue_referentiel(
            "tiers",
            vue,
            valeurs_fixes={"type_tiers": repository.TIERS_FOURNISSEUR},
            config_colonnes={
                "name": st.column_config.TextColumn("Fournisseur", required=True)
            },
        )
    elif vue == "Clients":
        vue_referentiel(
            "tiers",
            vue,
            valeurs_fixes={"type_tiers": repository.TIERS_CLIENT},
            config_colonnes={
                "name": st.column_config.TextColumn("Client", required=True)
            },
        )
    elif vue == "Gestionnaires":
        st.caption("Ces informations servent à @mentionner le gestionnaire dans "
                   "les cartes Teams des nouvelles réceptions de son "
                   "portefeuille. L'**e-mail** doit être l'adresse du compte "
                   "Microsoft 365 (UPN) et la personne doit être **membre de "
                   "l'équipe Teams** ; sans e-mail, le gestionnaire est cité "
                   "dans la carte mais n'est pas notifié.")
        vue_referentiel(
            "gestionnaires",
            vue,
            config_colonnes={
                "code_gestionnaire": st.column_config.TextColumn(
                    "Code gestionnaire", required=True
                ),
                "nom_affichage": st.column_config.TextColumn(
                    "Nom affiché (Teams)", help="Ex. « Younes El Hachi »"
                ),
                "email": st.column_config.TextColumn(
                    "E-mail Microsoft 365", help="Ex. prenom.nom@emotors.com"
                ),
            },
        )
    elif vue == "Portefeuilles":
        vue_portefeuilles()
    elif vue == "Quais":
        vue_referentiel(
            "quais",
            vue,
            config_colonnes={
                "code_quai": st.column_config.TextColumn("Code quai", required=True)
            },
        )
    elif vue == "Adresses":
        vue_referentiel(
            "adresses",
            vue,
            config_colonnes={
                "adresse": st.column_config.TextColumn("Adresse", required=True)
            },
        )
    elif vue == "Sites logistiques":
        vue_sites_logistiques()
    elif vue == "PLA":
        vue_pla()
    elif vue == "Rôles":
        vue_roles()
    elif vue == "Qualité IA":
        vue_qualite_ia()
    elif vue == "Notifications":
        vue_notifications()


try:
    render_vue_courante()
except PermissionError as exc:
    logger.warning("Action refusée : %s", exc)
    st.error("Vous n'avez pas les droits nécessaires pour cette action.")
except Exception as exc:
    logger.exception("Échec de rendu de la vue %s", vue)
    st.error(
        "Le service de données est momentanément indisponible. "
        "Aucune modification n'a été appliquée ; réessayez dans quelques instants."
    )
    if not SETTINGS.is_production:
        st.code(f"{type(exc).__name__}: {exc}")
