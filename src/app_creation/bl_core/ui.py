"""Aides d'interface communes aux deux applications.

Design system léger : les JETONS (couleurs, rayons, ombres, échelle
typographique en ratio 1,25) sont déclarés en variables CSS dans
injecter_style() et réutilisés partout — une retouche de charte = une ligne.
"""

import base64
import html as html_lib
import logging
import os
import sys

import streamlit as st
import streamlit.components.v1 as components

from . import repository


def configurer_logs() -> None:
    """Logs structurés vers stdout : repris par `databricks apps logs` et par la
    télémétrie OTEL de Databricks Apps si elle est activée sur l'app."""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            stream=sys.stdout,
            level=logging.INFO,
            format='{"ts":"%(asctime)s","niveau":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
        )


def injecter_style() -> None:
    """Habillage visuel commun aux deux applications, à appeler juste après
    st.set_page_config. Complète le thème déclaré dans .streamlit/config.toml
    (couleurs de base). Jetons en tête, règles ensuite."""
    st.markdown(
        """
        <style>
        /* ================= JETONS DU DESIGN SYSTEM =================
           Échelle typographique : ratio 1,25 (major third) sur 1rem.  */
        :root {
            --bl-primaire:      #0F62A6;   /* bleu eMotors (actions) */
            --bl-primaire-fonce:#0B4A7D;
            --bl-accent:        #43B02A;   /* vert eMotors (état actif, succès) */
            --bl-alerte:        #E4572E;
            --bl-encre:         #1B2A3A;   /* texte principal */
            --bl-encre-douce:   #5B6B7C;   /* texte secondaire */
            --bl-fond:          #F6F8FB;
            --bl-surface:       #FFFFFF;
            --bl-bordure:       #E3E9F2;
            --bl-nav-haut:      #10263C;   /* dégradé barre latérale */
            --bl-nav-bas:      #16334F;
            --bl-t-xs: 0.8rem;   --bl-t-s: 0.9rem;  --bl-t-m: 1rem;
            --bl-t-l: 1.25rem;   --bl-t-xl: 1.56rem; --bl-t-2xl: 1.95rem;
            --bl-t-3xl: 2.44rem;
            --bl-rayon-s: 8px;  --bl-rayon-m: 10px;  --bl-rayon-l: 12px;
            --bl-ombre-1: 0 1px 4px rgba(27, 42, 58, 0.06);
            --bl-ombre-2: 0 4px 14px rgba(15, 98, 166, 0.25);
            --bl-focus: 0 0 0 3px rgba(15, 98, 166, 0.28);
        }

        /* ===================== SURFACE PRINCIPALE ===================== */
        /* Espace au-dessus de l'entête réduit de 35 % (défaut ~3.5rem). */
        [data-testid="stAppViewContainer"] .block-container,
        [data-testid="stMainBlockContainer"] {
            padding-top: 2.3rem;
        }
        [data-testid="stAppViewContainer"] h1 {
            font-weight: 800;
            font-size: var(--bl-t-3xl);
            letter-spacing: -0.02em;
            padding-bottom: 0.4rem;
            background: linear-gradient(90deg, var(--bl-primaire), var(--bl-accent))
                        bottom left / 120px 5px no-repeat;
        }
        [data-testid="stAppViewContainer"] h3 { font-size: var(--bl-t-xl); font-weight: 750; }
        [data-testid="stAppViewContainer"] h4 { font-size: var(--bl-t-l); font-weight: 700; }

        /* Boutons : coins arrondis, relief léger au survol */
        .stButton > button, [data-testid="stFormSubmitButton"] > button,
        [data-testid="stDownloadButton"] > button {
            border-radius: var(--bl-rayon-m);
            font-weight: 600;
            min-height: 44px;
            transition: transform 0.08s ease, box-shadow 0.15s ease;
        }
        .stButton > button:focus-visible, input:focus-visible, textarea:focus-visible,
        [data-baseweb="select"] > div:focus-within {
            outline: none !important;
            box-shadow: var(--bl-focus) !important;
        }
        .stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover {
            transform: translateY(-1px);
            box-shadow: var(--bl-ombre-2);
        }
        /* Barre de progression du wizard en dégradé */
        .stProgress > div > div > div {
            background: linear-gradient(90deg, var(--bl-primaire), #4FA3E3);
        }
        /* Conteneurs bordés et expanders en "cartes" */
        [data-testid="stExpander"], div[data-testid="stVerticalBlockBorderWrapper"] {
            border-radius: var(--bl-rayon-l);
        }
        [data-testid="stExpander"] {
            border: 1px solid var(--bl-bordure);
            box-shadow: var(--bl-ombre-1);
            background: var(--bl-surface);
        }
        /* KPI (st.metric) en cartes */
        [data-testid="stMetric"] {
            background: var(--bl-surface);
            border: 1px solid var(--bl-bordure);
            border-radius: var(--bl-rayon-l);
            padding: 0.7rem 0.9rem;
            box-shadow: var(--bl-ombre-1);
        }
        [data-testid="stMetricLabel"] { color: var(--bl-encre-douce); }
        /* Champs de saisie adoucis */
        .stTextInput input, .stTextArea textarea, .stDateInput input,
        [data-baseweb="select"] > div {
            border-radius: var(--bl-rayon-s);
        }
        /* Tableau du récapitulatif : lignes aérées */
        [data-testid="stMarkdownContainer"] table { width: 100%; }
        [data-testid="stMarkdownContainer"] td { padding: 0.45rem 0.6rem; }
        /* Pied de page Streamlit masqué (application métier) */
        footer { visibility: hidden; }

        /* ===================== NAVIGATION LATÉRALE =====================
           Modèle « model-driven » : sections en petites capitales, vues en
           boutons pleine largeur toujours visibles, item actif surligné
           (barre verte). */
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, var(--bl-nav-haut) 0%, var(--bl-nav-bas) 100%);
        }
        [data-testid="stSidebar"] * { color: #E8EFF7 !important; }
        [data-testid="stSidebar"] hr { border-color: rgba(232, 239, 247, 0.22); margin: 0.5rem 0; }
        [data-testid="stSidebar"] .nav-section {
            font-size: var(--bl-t-xs);
            font-weight: 800;
            letter-spacing: 0.14em;
            text-transform: uppercase;
            color: rgba(232, 239, 247, 0.65) !important;
            margin: 0.9rem 0 0.15rem 0.55rem;
        }
        [data-testid="stSidebar"] .stButton > button {
            background: transparent;
            border: none;
            box-shadow: none !important;
            color: #E8EFF7;
            text-align: left;
            justify-content: flex-start;
            font-size: 1.06rem;
            font-weight: 600;
            padding: 0.42rem 0.65rem;
            margin: 0.03rem 0;
            border-radius: var(--bl-rayon-s);
            width: 100%;
            min-height: 0;
        }
        [data-testid="stSidebar"] .stButton > button:hover {
            background: rgba(255, 255, 255, 0.09);
            transform: none;
        }
        [data-testid="stSidebar"] .stButton > button[kind="primary"] {
            background: rgba(67, 176, 42, 0.20) !important;
            color: #FFFFFF !important;
            font-weight: 800;
            box-shadow: inset 3px 0 0 var(--bl-accent) !important;
        }

        /* Grilles de données : cadre net */
        [data-testid="stDataFrame"], [data-testid="stDataEditor"] {
            border: 1px solid var(--bl-bordure);
            border-radius: var(--bl-rayon-m);
        }

        /* Filtres en boutons (st.pills / st.segmented_control) : pastilles */
        [data-testid="stPills"] button,
        [data-testid="stSegmentedControl"] button {
            border-radius: 999px !important;
            font-weight: 600;
        }
        [data-testid="stPills"] button[kind="pillsActive"],
        [data-testid="stSegmentedControl"] button[kind="segmented_controlActive"] {
            border-color: var(--bl-primaire) !important;
            color: var(--bl-primaire) !important;
            background: #E8F1FA !important;
        }

        /* Chips des filtres appliqués : pastilles bleues avec ✕, en ligne */
        [class*="st-key-chips"] .stButton > button {
            border: 1.5px solid var(--bl-primaire);
            color: var(--bl-primaire);
            background: var(--bl-surface);
            border-radius: 999px;
            padding: 0.14rem 0.8rem;
            font-size: 0.86rem;
            font-weight: 700;
            min-height: 0;
        }
        [class*="st-key-chips"] .stButton > button:hover {
            background: #E8F1FA;
            transform: none;
            box-shadow: none;
        }

        /* Stepper de saisie, lisible sur mobile et au clavier. */
        .bl-stepper {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: .5rem;
            margin: .6rem 0 1.3rem;
        }
        .bl-step {
            border-top: 4px solid var(--bl-bordure);
            padding-top: .45rem;
            color: var(--bl-encre-douce);
            font-size: var(--bl-t-s);
            font-weight: 650;
        }
        .bl-step.done { border-color: var(--bl-accent); color: var(--bl-encre); }
        .bl-step.current { border-color: var(--bl-primaire); color: var(--bl-primaire); }
        .bl-step strong { display: block; font-size: var(--bl-t-xs); }

        .bl-context-bar {
            display:flex; flex-wrap:wrap; align-items:center; gap:.55rem;
            padding:.65rem .8rem; margin:.4rem 0 1rem;
            border:1px solid var(--bl-bordure); border-radius:var(--bl-rayon-m);
            background:var(--bl-surface); box-shadow:var(--bl-ombre-1);
        }
        /* En-tête d'application : titre et contexte sur la MÊME ligne. Le
           contexte est poussé à droite et ne prend plus de hauteur propre. */
        .bl-entete {
            display:flex; align-items:center; justify-content:space-between;
            flex-wrap:wrap; gap:1rem; margin-bottom:.6rem;
        }
        .bl-entete h1 { margin:0; }
        .bl-entete .bl-context-bar { margin:0; padding:.45rem .6rem; }
        .bl-pill {
            display:inline-flex; align-items:center; gap:.3rem;
            padding:.22rem .62rem; border-radius:999px;
            background:#E8F1FA; color:var(--bl-primaire-fonce);
            font-size:var(--bl-t-s); font-weight:700;
        }

        @media (max-width: 720px) {
            [data-testid="stAppViewContainer"] .block-container,
            [data-testid="stMainBlockContainer"] { padding: 1rem .9rem 5rem; }
            [data-testid="stAppViewContainer"] h1 { font-size: 1.9rem; }
            .bl-step span { display:none; }
            .bl-step { min-height: 1.6rem; }
            [data-testid="column"] { min-width: 0 !important; }
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after { scroll-behavior:auto !important; transition:none !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# Logo eMotors — recréation SVG fidèle du logo officiel : marque « E »
# anguleux (arms à droite coupés en biais vers l'avant, arm central plus court)
# + mot « EMOTORS », E vert. Version claire (blanc/vert) pour la barre latérale
# sombre. Remplaçable par le SVG officiel (garder l'appel afficher_logo()).
_NAVY = "#0B1B3A"
_VERT = "#43B02A"
LOGO_EMOTORS_SVG = """
<svg viewBox="0 0 214 200" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="eMotors"
     style="width:82%;max-width:190px;height:auto;display:block;margin:0.3rem auto 0.5rem;">
  <path transform="translate(52,4)" fill="#FFFFFF" d="
    M6 6 L104 6 L82 32 L30 32 L30 48 L92 48 L74 66 L30 66 L30 84 L100 84
    L78 110 L6 110 Z"/>
  <text x="107" y="188" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif"
        font-size="41" font-weight="800" letter-spacing="-1.5">
    <tspan fill="#43B02A">E</tspan><tspan fill="#FFFFFF">MOTORS</tspan>
  </text>
</svg>
"""


LOGO_PATH = "logo.png"


def afficher_logo() -> None:
    """Logo eMotors en haut de la barre latérale (haut à gauche de l'app).
    Utilise le fichier officiel logo.png s'il est présent dans le dossier de
    l'app, sinon la recréation SVG (repli sur fond sombre)."""
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, width="stretch")
    else:
        st.markdown(
            f'<div style="padding:0.2rem 0 0.5rem;">{LOGO_EMOTORS_SVG}</div>',
            unsafe_allow_html=True,
        )


def entete_app(titre: str, icone: str = "🗂️", utilisateur: str | None = None,
               environnement: str = "", roles: list[str] | None = None) -> None:
    """Grand titre de l'application, avec le contexte sur la MÊME ligne.

    Titre à gauche (avec son soulignement dégradé), pastilles identité /
    environnement / rôles à droite : une seule bande au lieu de deux, sur
    toutes les vues. Sans `utilisateur`, seul le titre est rendu."""
    if utilisateur is None:
        st.markdown(f"# {icone} {titre}")
        return
    safe_user = html_lib.escape(utilisateur)
    safe_env = html_lib.escape(environnement.upper())
    role_text = ", ".join(map(html_lib.escape, roles or [])) or "Aucun rôle"
    st.markdown(
        '<div class="bl-entete">'
        f'<h1>{html_lib.escape(icone)} {html_lib.escape(titre)}</h1>'
        '<div class="bl-context-bar">'
        f'<span class="bl-pill" title="{safe_user}">👤 {safe_user}</span>'
        f'<span class="bl-pill">☁️ {safe_env}</span>'
        f'<span class="bl-pill">🔐 {role_text}</span>'
        "</div></div>",
        unsafe_allow_html=True,
    )


def afficher_etapes(etape: int, noms: dict[int, str]) -> None:
    """Stepper sémantique, plus informatif qu'une barre de progression seule."""
    items = []
    for index, label in noms.items():
        state = "done" if index < etape else "current" if index == etape else ""
        current = ' aria-current="step"' if index == etape else ""
        items.append(
            f'<div class="bl-step {state}"{current}><strong>{index:02d}</strong>'
            f"<span>{html_lib.escape(label)}</span></div>"
        )
    st.markdown(
        '<div class="bl-stepper" aria-label="Progression">'
        + "".join(items)
        + "</div>",
        unsafe_allow_html=True,
    )


def barre_contexte(utilisateur: str, environnement: str, roles: list[str]) -> None:
    """Contexte visible : identité, environnement et rôles effectifs."""
    safe_user = html_lib.escape(utilisateur)
    safe_env = html_lib.escape(environnement.upper())
    role_text = ", ".join(map(html_lib.escape, roles)) or "Aucun rôle"
    st.markdown(
        '<div class="bl-context-bar">'
        f'<span class="bl-pill">👤 {safe_user}</span>'
        f'<span class="bl-pill">☁️ {safe_env}</span>'
        f'<span class="bl-pill">🔐 {role_text}</span>'
        "</div>",
        unsafe_allow_html=True,
    )


def section_nav(libelle: str) -> None:
    """Intitulé de section dans la barre latérale (petites capitales)."""
    st.markdown(f'<div class="nav-section">{libelle}</div>', unsafe_allow_html=True)


# --- Messages "flash" : survivent à un st.rerun (sinon le message disparaît
# avant que l'utilisateur ait pu le lire). ---
def set_flash(kind: str, message: str) -> None:
    st.session_state["flash"] = (kind, message)


def show_flash() -> None:
    flash = st.session_state.pop("flash", None)
    if flash:
        kind, message = flash
        getattr(st, kind)(message)


def libelle_statut(statut_bl: str) -> str:
    return "✅ OK" if statut_bl == repository.STATUT_OK else "🟥 EDI NOK"


def afficher_miniatures(pages: list[bytes]) -> None:
    """Miniatures des pages en attente (max 4 par ligne pour rester lisible sur mobile)."""
    for debut in range(0, len(pages), 4):
        cols = st.columns(4)
        for i, img in enumerate(pages[debut : debut + 4]):
            with cols[i]:
                st.image(img, caption=f"Page {debut + i + 1}", width="stretch")


def bouton_imprimer_tableau(df, titre: str, hauteur: int = 46) -> None:
    """Petit bouton « Imprimer » : ouvre une fenêtre contenant le tableau
    (HTML propre) et lance l'impression du navigateur."""
    titre_html = html_lib.escape(titre, quote=True)
    table_html = df.to_html(index=False, border=0, na_rep="—")
    html = f"""
<style>
  .btn-imp {{ background:#0F62A6; color:#fff; border:none; border-radius:8px;
             padding:7px 14px; font-weight:600; cursor:pointer;
             font-family:'Segoe UI',Arial,sans-serif; }}
  .btn-imp:hover {{ background:#0B4A7D; }}
</style>
<button class="btn-imp" onclick="imprimer()">🖨️ Imprimer</button>
<script>
  const contenu = {repr(table_html)};
  function imprimer() {{
    const w = window.open('', '_blank');
    w.document.write(`<html><head><title>{titre_html}</title><style>
      body {{ font-family:'Segoe UI',Arial,sans-serif; margin:24px; }}
      h2 {{ color:#0F62A6; }}
      table {{ border-collapse:collapse; width:100%; font-size:12px; }}
      th, td {{ border:1px solid #C9D6E4; padding:5px 8px; text-align:left; }}
      th {{ background:#EDF3FA; }}
    </style></head><body><h2>{titre_html}</h2>` + contenu + '</body></html>');
    w.document.close();
    w.focus();
    setTimeout(() => w.print(), 300);
  }}
</script>
"""
    components.html(html, height=hauteur)


# =====================================================================
# Visionneuse d'images (boîte de dialogue « Voir les images »)
# =====================================================================
def visionneuse_images(pages: list[bytes], titre: str, hauteur: int = 620,
                       etiquettes: list[str] | None = None,
                       avec_impression: bool = True) -> None:
    """Visionneuse HTML autonome : zoom +/- et ajustement, rotation,
    impression et téléchargement page par page. Tout est côté client
    (les images sont incorporées en data URI) — donc **sans rerun Streamlit** :
    zoomer ou pivoter ne relance pas le script, ce qui la rend utilisable
    pendant la relecture d'un lot.

    `etiquettes` remplace le libellé « Page i / n » (utile quand l'image
    affichée est la page N d'un document plus grand). `avec_impression=False`
    retire les actions d'impression et de téléchargement."""
    titre_html = html_lib.escape(titre, quote=True)
    bouton_impression = (
        '<button class="btn" onclick="imprimer()" '
        'title="Imprimer toutes les pages">🖨️ Imprimer</button>'
        if avec_impression else "")
    figures = []
    for i, page in enumerate(pages):
        b64 = base64.b64encode(page).decode("ascii")
        libelle = html_lib.escape(
            etiquettes[i] if etiquettes and i < len(etiquettes)
            else f"Page {i + 1} / {len(pages)}")
        telechargement = (
            f'<a class="btn" href="data:image/jpeg;base64,{b64}" '
            f'download="{titre_html}_page{i + 1}.jpg" '
            'title="Télécharger cette page">⬇️</a>' if avec_impression else "")
        figures.append(
            f'<figure class="page">'
            f"<figcaption><span>{libelle}</span>{telechargement}</figcaption>"
            f'<img src="data:image/jpeg;base64,{b64}" alt="{libelle}"/>'
            f"</figure>"
        )
    html = f"""
<style>
  * {{ box-sizing: border-box; margin: 0; font-family: 'Segoe UI', Arial, sans-serif; }}
  .barre {{
    display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
    padding: 8px 10px; background: #10263C; border-radius: 10px 10px 0 0;
  }}
  .barre .titre {{ color: #E8EFF7; font-weight: 700; margin-right: auto; font-size: 0.95rem; }}
  .btn {{
    background: rgba(255,255,255,0.12); color: #E8EFF7; border: none;
    border-radius: 8px; padding: 6px 11px; font-size: 0.95rem; cursor: pointer;
    text-decoration: none; line-height: 1.2; font-weight: 600;
  }}
  .btn:hover {{ background: rgba(255,255,255,0.25); }}
  #pct {{ color: #E8EFF7; min-width: 52px; text-align: center; font-weight: 700; }}
  .scene {{
    height: {hauteur - 64}px; overflow: auto; background: #263447;
    border-radius: 0 0 10px 10px; padding: 14px; text-align: center;
  }}
  .page {{ margin: 0 auto 18px; display: block; }}
  .page figcaption {{
    display: flex; justify-content: center; gap: 10px; align-items: center;
    color: #C9D6E4; font-size: 0.85rem; margin-bottom: 6px;
  }}
  .page figcaption .btn {{ padding: 3px 8px; font-size: 0.85rem; }}
  .page img {{
    width: var(--zoom, 100%); max-width: none; border-radius: 6px;
    box-shadow: 0 4px 18px rgba(0,0,0,0.45); background: #fff;
    transform: rotate(var(--rot, 0deg)); transition: transform 0.15s ease;
  }}
</style>
<div class="barre">
  <span class="titre">🖼️ {titre_html}</span>
  <button class="btn" onclick="zoomer(-15)" title="Réduire">➖</button>
  <span id="pct">100 %</span>
  <button class="btn" onclick="zoomer(15)" title="Agrandir">➕</button>
  <button class="btn" onclick="ajuster()" title="Ajuster à la largeur">🞑 Ajuster</button>
  <button class="btn" onclick="pivoter()" title="Pivoter de 90°">🔄 Pivoter</button>
  {bouton_impression}
</div>
<div class="scene" id="scene">{''.join(figures)}</div>
<script>
  let zoom = 100, rot = 0;
  const scene = document.getElementById('scene');
  function appliquer() {{
    scene.style.setProperty('--zoom', zoom + '%');
    scene.style.setProperty('--rot', rot + 'deg');
    document.getElementById('pct').textContent = Math.round(zoom) + ' %';
  }}
  function zoomer(delta) {{ zoom = Math.min(400, Math.max(25, zoom + delta)); appliquer(); }}
  function ajuster() {{ zoom = 100; rot = 0; appliquer(); }}
  function pivoter() {{ rot = (rot + 90) % 360; appliquer(); }}
  function imprimer() {{
    const w = window.open('', '_blank');
    const imgs = Array.from(document.querySelectorAll('.page img'))
      .map(i => `<img src="${{i.src}}" style="width:100%;page-break-after:always;"/>`).join('');
    w.document.write(`<html><head><title>{titre_html}</title></head><body>${{imgs}}</body></html>`);
    w.document.close();
    w.focus();
    setTimeout(() => {{ w.print(); }}, 350);
  }}
  appliquer();
</script>
"""
    components.html(html, height=hauteur, scrolling=False)
