"""Traitement d'image : photo brute de terrain -> document "scanné".

Pipeline conforme au cahier des charges (annexe) :
1. Redressement de perspective (détection du contour de la feuille),
   en se rapprochant du ratio A4 quand le quadrilatère détecté en est proche.
2. Limitation de la plus grande dimension à MAX_DIMENSION_PX (3508 par défaut,
   soit un A4 à 300 dpi).
3. Un des 4 modes de rendu : Sans filtre, Couleurs réhaussées, Gris réhaussé,
   Contraste noir & blanc.
4. Boucle de compression JPEG pour respecter la limite configurée.
Le résultat est mis en cache par (contenu d'image, mode) — pas de recalcul à
chaque rerun Streamlit.
"""

import io

import cv2
import numpy as np
import streamlit as st
from PIL import Image, ImageOps

from .config import get_settings

try:
    # Photos "haute efficacité" (HEIC/HEIF), format par défaut des iPhone et de
    # nombreux Android : OpenCV ne sait pas les décoder, Pillow oui via ce plugin.
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # dépendance absente : le repli Pillow reste limité aux formats natifs
    pass

MODES_SCAN = ["Sans filtre", "Couleurs réhaussées", "Gris réhaussé", "Contraste noir & blanc"]

_RATIO_A4 = 297.0 / 210.0  # hauteur / largeur en portrait


def _decoder_image(image_bytes: bytes):
    """Octets -> image BGR, orientation EXIF appliquée. Pillow en premier
    (JPEG/PNG, HEIC/HEIF via pillow-heif) : les photos de smartphone stockent
    souvent la rotation dans l'EXIF, qu'OpenCV ignore — sans cette étape le
    document ressort couché. Repli OpenCV pour les cas que Pillow refuse."""
    try:
        pil = Image.open(io.BytesIO(image_bytes))
        pil = ImageOps.exif_transpose(pil).convert("RGB")
        return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
    except Exception:
        pass
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Image illisible ou format non supporté.")
    return img


def _order_points(pts):
    """Range les 4 coins dans l'ordre : haut-gauche, haut-droit, bas-droit, bas-gauche."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]      # haut-gauche  (x+y minimal)
    rect[2] = pts[np.argmax(s)]      # bas-droit    (x+y maximal)
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]   # haut-droit   (x-y minimal)
    rect[3] = pts[np.argmax(diff)]   # bas-gauche   (x-y maximal)
    return rect


def _four_point_transform(image, pts):
    """Aplatit le document (corrige la perspective) à partir de ses 4 coins."""
    rect = _order_points(pts)
    (tl, tr, br, bl) = rect
    max_width = max(int(np.linalg.norm(br - bl)), int(np.linalg.norm(tr - tl)), 1)
    max_height = max(int(np.linalg.norm(tr - br)), int(np.linalg.norm(tl - bl)), 1)

    # Rapprochement du format A4 (CDC) : si le ratio détecté est proche de
    # celui d'un A4 (portrait ou paysage), on force le ratio exact — le léger
    # étirement corrige l'imprécision de la détection de coins.
    ratio = max_height / float(max_width) if max_width else 1.0
    if 0.85 * _RATIO_A4 <= ratio <= 1.15 * _RATIO_A4:
        max_height = int(round(max_width * _RATIO_A4))
    elif 0.85 / _RATIO_A4 <= ratio <= 1.15 / _RATIO_A4:
        max_height = int(round(max_width / _RATIO_A4))

    dst = np.array(
        [[0, 0], [max_width - 1, 0], [max_width - 1, max_height - 1], [0, max_height - 1]],
        dtype="float32",
    )
    M = cv2.getPerspectiveTransform(rect, dst)
    return cv2.warpPerspective(image, M, (max_width, max_height))


def _quadrilatere_plausible(quad, aire_image: float) -> bool:
    """Garde-fous contre les faux positifs (tampon, fenêtre, ombre…) : le
    quadrilatère doit être convexe, couvrir une part significative de la photo
    et avoir des proportions de document. Sinon, mieux vaut conserver la photo
    entière qu'un recadrage aberrant."""
    if not cv2.isContourConvex(quad.reshape(4, 1, 2).astype(np.int32)):
        return False
    if cv2.contourArea(quad.astype("float32")) < 0.20 * aire_image:
        return False
    rect = _order_points(quad.astype("float32"))
    (tl, tr, br, bl) = rect
    largeur = max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl))
    hauteur = max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl))
    if min(largeur, hauteur) < 1:
        return False
    ratio = hauteur / largeur
    return 0.4 <= ratio <= 2.5


def _detecter_contour_feuille(small):
    """Cherche le contour de la feuille sur l'image réduite. Renvoie le
    quadrilatère (4, 2) ou None si aucun candidat plausible."""
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(gray, 75, 200)
    # Referme les petites coupures du tracé (pli, ombre, doigt sur le bord)
    # pour que le contour de la feuille reste un polygone fermé.
    edged = cv2.morphologyEx(edged, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    contours, _ = cv2.findContours(edged, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
    aire_image = float(small.shape[0] * small.shape[1])

    for c in contours:
        peri = cv2.arcLength(c, True)
        # Deux tolérances d'approximation : 2 % (contour net) puis 4 %
        # (coins légèrement arrondis ou tracé bruité).
        for epsilon in (0.02, 0.04):
            approx = cv2.approxPolyDP(c, epsilon * peri, True)
            if len(approx) == 4 and _quadrilatere_plausible(approx.reshape(4, 2), aire_image):
                return approx.reshape(4, 2)
    return None


def _detecter_et_redresser(img):
    """Détecte le contour de la feuille et corrige la perspective.
    Renvoie (image, True) si un contour plausible a été redressé,
    (image d'origine, False) sinon — jamais de recadrage hasardeux."""
    # Détection des bords sur une version réduite (plus rapide et plus robuste)
    ratio = img.shape[0] / 500.0
    small = cv2.resize(img, (max(1, int(img.shape[1] / ratio)), 500))

    quad = _detecter_contour_feuille(small)
    if quad is None:
        return img, False                    # repli : pas de découpe/redressement
    return _four_point_transform(img, quad.astype("float32") * ratio), True


def _limiter_dimension(img, max_px: int):
    """Réduit proportionnellement si la plus grande dimension dépasse max_px (CDC)."""
    h, w = img.shape[:2]
    plus_grand = max(h, w)
    if plus_grand <= max_px:
        return img
    facteur = max_px / float(plus_grand)
    return cv2.resize(img, (int(w * facteur), int(h * facteur)), interpolation=cv2.INTER_AREA)


def _rehausser_niveaux_gris(gray):
    """Rendu 'scan' en niveaux de gris : 1) normalisation de l'éclairage
    (supprime ombres et fond inégal), 2) contraste local (CLAHE),
    3) accentuation douce (unsharp mask)."""
    sigma = max(gray.shape) / 30.0
    fond = cv2.GaussianBlur(gray, (0, 0), sigmaX=sigma)
    normalise = cv2.divide(gray, fond, scale=255)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    contraste = clahe.apply(normalise)

    flou = cv2.GaussianBlur(contraste, (0, 0), sigmaX=1.0)
    return cv2.addWeighted(contraste, 1.5, flou, -0.5, 0)  # unsharp mask


def _rehausser_couleur(bgr):
    """Comme le rendu niveaux de gris mais en conservant les couleurs (tampons,
    logos) : rehaussement de la seule luminance en espace LAB."""
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    luminance, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    luminance = clahe.apply(luminance)
    return cv2.cvtColor(cv2.merge([luminance, a, b]), cv2.COLOR_LAB2BGR)


def _compresser_jpeg(img, taille_max: int) -> bytes:
    """Encode en JPEG en garantissant taille <= taille_max : baisse de qualité
    progressive, puis réduction de résolution si la qualité minimale ne suffit pas."""
    qualite = 95
    while True:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, qualite])
        if not ok:
            raise RuntimeError("Échec de l'encodage JPEG du document scanné.")
        if buf.nbytes <= taille_max:
            return buf.tobytes()
        if qualite > 40:
            qualite -= 7
        else:
            # Qualité plancher atteinte : on réduit la résolution de 15 % et on repart.
            h, w = img.shape[:2]
            img = cv2.resize(img, (int(w * 0.85), int(h * 0.85)), interpolation=cv2.INTER_AREA)
            qualite = 80


@st.cache_data(show_spinner=False, max_entries=30)
def scanner_document(
    image_bytes: bytes, mode: str = "Gris réhaussé", corriger_perspective: bool = True
) -> tuple[bytes, bool]:
    """Photo brute -> document scanné.

    Renvoie (octets JPEG sous la limite configurée, perspective_corrigee). Limite de dimension et
    compression bornée s'appliquent dans TOUS les cas (exigences du CDC) ; le
    mode ne change que le rendu visuel. `corriger_perspective=False` conserve le
    cadrage d'origine (le redressement est aussi abandonné de lui-même quand
    aucun contour plausible n'est détecté : perspective_corrigee vaut False).
    """
    img = _decoder_image(image_bytes)

    settings = get_settings()
    perspective_corrigee = False
    if corriger_perspective:
        img, perspective_corrigee = _detecter_et_redresser(img)
    redresse = _limiter_dimension(img, settings.max_dimension_px)

    if mode == "Sans filtre":
        rendu = redresse
    elif mode == "Couleurs réhaussées":
        rendu = _rehausser_couleur(redresse)
    else:
        gris = _rehausser_niveaux_gris(cv2.cvtColor(redresse, cv2.COLOR_BGR2GRAY))
        if mode == "Contraste noir & blanc":
            # Binarisation Otsu sur image déjà normalisée : propre, sans moucheture.
            _, gris = cv2.threshold(gris, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        rendu = gris

    return _compresser_jpeg(rendu, settings.max_image_bytes), perspective_corrigee
