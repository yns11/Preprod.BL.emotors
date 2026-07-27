"""Archivage en lot : lecture d'un PDF multipage et pré-analyse IA anticipée.

Deux responsabilités, sans aucune dépendance à Streamlit :

* `rasteriser` — transforme le PDF déposé par l'utilisateur en une image JPEG
  par page, calibrée pour l'affichage et pour le modèle de vision ;
* `Prechargeur` — fait analyser les pages **par paquets, en avance** sur la
  progression de l'opérateur. Pendant qu'il vérifie la page N, les paquets
  suivants sont déjà en cours d'analyse : le clic « page suivante » n'attend
  donc pratiquement jamais le modèle.

Le pré-chargement s'appuie sur un `ThreadPoolExecutor` conservé dans l'état de
session. Les threads ne touchent JAMAIS `st.session_state` : ils se contentent
de remplir les `Future` détenus par le préchargeur, que le thread principal
consomme au moment voulu.
"""

from __future__ import annotations

import io
import logging
from concurrent.futures import Future, ThreadPoolExecutor

from . import extraction
from .config import get_settings

logger = logging.getLogger("bl.lot")


# ---------------------------------------------------------------------------
# PDF -> images
# ---------------------------------------------------------------------------
def _ouvrir(pdf: bytes):
    try:
        import pymupdf
    except ImportError as exc:                    # dépendance non installée
        raise RuntimeError(
            "La lecture des PDF nécessite le paquet « pymupdf » "
            "(voir requirements.txt de l'app Création)."
        ) from exc
    try:
        return pymupdf.open(stream=pdf, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Fichier PDF illisible ou endommagé : {exc}") from exc


def compter_pages(pdf: bytes) -> int:
    document = _ouvrir(pdf)
    try:
        return document.page_count
    finally:
        document.close()


def rasteriser(pdf: bytes, dpi: int = 150, qualite: int = 72,
               progression=None) -> list[bytes]:
    """Une image JPEG par page du PDF, dans l'ordre du document.

    `dpi` 150 est le compromis retenu : suffisant pour qu'un modèle de vision
    lise un numéro de BL imprimé, assez léger pour 200 pages en session. Les
    images sont ensuite bornées par `BL_MAX_DIMENSION_PX` et recompressées
    tant qu'elles dépassent `BL_MAX_IMAGE_BYTES`.

    `progression(index, total)` est appelé après chaque page (barre d'avancement).
    """
    from PIL import Image

    parametres = get_settings()
    document = _ouvrir(pdf)
    try:
        total = document.page_count
        if total == 0:
            raise ValueError("Ce PDF ne contient aucune page.")
        if total > parametres.lot_max_pages:
            raise ValueError(
                f"Ce PDF contient {total} pages ; la limite est de "
                f"{parametres.lot_max_pages} pages par lot. Découpez-le.")
        pages = []
        for index in range(total):
            pixmap = document[index].get_pixmap(dpi=dpi)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height),
                                    pixmap.samples)
            image.thumbnail((parametres.max_dimension_px,
                             parametres.max_dimension_px))
            pages.append(_compresser(image, qualite, parametres.max_image_bytes))
            if progression:
                progression(index + 1, total)
        logger.info("PDF rasterisé : %d page(s), %.1f Mo",
                    len(pages), sum(map(len, pages)) / 1024 / 1024)
        return pages
    finally:
        document.close()


def _compresser(image, qualite: int, taille_max: int) -> bytes:
    """JPEG sous la taille maximale, en abaissant la qualité par paliers."""
    for essai in (qualite, 60, 45, 32):
        tampon = io.BytesIO()
        image.save(tampon, format="JPEG", quality=essai, optimize=True)
        donnees = tampon.getvalue()
        if len(donnees) <= taille_max:
            return donnees
    return donnees                                 # dernier essai, au plus léger


# ---------------------------------------------------------------------------
# Pré-analyse anticipée
# ---------------------------------------------------------------------------
class Prechargeur:
    """Analyse les pages par paquets, avec de l'avance sur l'opérateur.

    Utilisation :

        p = Prechargeur(pages, "fournisseur", referentiel)
        p.amorcer(0)                 # lance les premiers paquets
        infos = p.resultat(0)        # attend seulement si nécessaire
        p.amorcer(1)                 # entretient l'avance à chaque page

    Aucune exception n'est propagée : une page dont l'analyse a échoué revient
    avec des champs vides et le motif dans `erreurs`, l'opérateur saisit alors
    manuellement."""

    def __init__(self, pages: list[bytes], tiers_libelle: str,
                 referentiel=None, taille_lot: int | None = None,
                 paquets_d_avance: int = 2) -> None:
        parametres = get_settings()
        self.pages = pages
        self.tiers_libelle = tiers_libelle
        self.referentiel = referentiel
        self.taille_lot = taille_lot or parametres.lot_taille_ia
        self.paquets_d_avance = max(1, paquets_d_avance)
        self.erreurs: dict[int, str] = {}
        self._resultats: dict[int, dict] = {}
        self._futures: dict[int, Future] = {}
        # 2 threads : au-delà, on sature l'endpoint sans gagner en fluidité,
        # l'opérateur ne pouvant vérifier qu'une page à la fois.
        self._executeur = ThreadPoolExecutor(max_workers=2,
                                             thread_name_prefix="bl-lot")
        self.actif = bool(extraction.endpoint_configure())

    # -- Découpage en paquets ------------------------------------------------
    def _debut_de_paquet(self, index: int) -> int:
        return (index // self.taille_lot) * self.taille_lot

    def _analyser(self, debut: int) -> list[dict]:
        lot = self.pages[debut:debut + self.taille_lot]
        return extraction.extraire_lot_pages(
            lot, self.tiers_libelle, self.referentiel)

    def amorcer(self, index: int) -> None:
        """Soumet le paquet de `index` et ceux d'avance, s'ils ne le sont pas."""
        if not self.actif or index >= len(self.pages):
            return
        depart = self._debut_de_paquet(index)
        for numero in range(self.paquets_d_avance + 1):
            debut = depart + numero * self.taille_lot
            if debut >= len(self.pages) or debut in self._futures:
                continue
            self._futures[debut] = self._executeur.submit(self._analyser, debut)

    def en_cours(self, index: int) -> bool:
        """Vrai si l'analyse de cette page n'est pas encore disponible."""
        if not self.actif or index in self._resultats:
            return False
        future = self._futures.get(self._debut_de_paquet(index))
        return future is not None and not future.done()

    def resultat(self, index: int, delai: float = 90.0) -> dict:
        """Champs détectés pour la page `index` ; attend le paquet au besoin."""
        if index in self._resultats:
            return self._resultats[index]
        if not self.actif:
            return {}
        self.amorcer(index)
        debut = self._debut_de_paquet(index)
        future = self._futures.get(debut)
        if future is None:
            return {}
        try:
            for decalage, infos in enumerate(future.result(timeout=delai)):
                self._resultats[debut + decalage] = infos
        except Exception as exc:
            logger.warning("Analyse du paquet %d en échec : %s", debut, exc,
                           exc_info=True)
            for decalage in range(min(self.taille_lot, len(self.pages) - debut)):
                self._resultats[debut + decalage] = {}
                self.erreurs[debut + decalage] = f"{type(exc).__name__} : {exc}"
        return self._resultats.get(index, {})

    def avancement(self) -> tuple[int, int]:
        """(pages analysées, pages totales) — pour informer l'utilisateur."""
        return len(self._resultats), len(self.pages)

    def fermer(self) -> None:
        """Libère les threads. Idempotent."""
        self._executeur.shutdown(wait=False, cancel_futures=True)
        self._futures.clear()


# ---------------------------------------------------------------------------
# Validation du lot
# ---------------------------------------------------------------------------
def controler_lot(retenus: list[dict], numero_disponible) -> list[dict]:
    """Complète chaque BL retenu d'un champ `anomalie` (ou None).

    Deux contrôles, dans cet ordre : doublon **à l'intérieur du lot** (le plus
    fréquent quand une page est scannée deux fois), puis numéro déjà présent en
    base. `numero_disponible(numero)` est injecté pour rester testable."""
    vus: dict[str, int] = {}
    controles = []
    for position, bl in enumerate(retenus, start=1):
        numero = (bl.get("numero") or "").strip()
        anomalie = None
        if not numero:
            anomalie = "Numéro de BL manquant"
        elif numero.upper() in vus:
            anomalie = (f"Numéro en double dans ce lot "
                        f"(déjà en position {vus[numero.upper()]})")
        else:
            vus[numero.upper()] = position
            try:
                if not numero_disponible(numero):
                    anomalie = "Numéro déjà enregistré en base"
            except Exception as exc:              # base indisponible : on n'invente rien
                anomalie = f"Vérification impossible ({type(exc).__name__})"
        if not bl.get("fournisseur"):
            anomalie = anomalie or "Tiers non renseigné"
        controles.append({**bl, "anomalie": anomalie})
    return controles
