"""Archivage en lot : lecture d'un PDF multipage et pré-analyse IA anticipée.

Deux responsabilités, sans aucune dépendance à Streamlit :

* `rasteriser` — transforme le PDF déposé par l'utilisateur en une image JPEG
  par page, calibrée pour l'affichage et pour le modèle de vision ;
* `Prechargeur` — fait analyser les pages **une par une, en avance** sur la
  progression de l'opérateur. Pendant qu'il vérifie la page N, les pages
  suivantes sont déjà en cours d'analyse : le clic « page suivante » n'attend
  donc pratiquement jamais le modèle. Chaque page emprunte la procédure
  d'extraction unitaire complète (passes de raffinement sur le référentiel).

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
    """Analyse les pages **une par une**, avec de l'avance sur l'opérateur.

    Chaque page passe par `extraction.extraire_infos_bl`, c'est-à-dire
    EXACTEMENT la procédure de la saisie unitaire : premier appel sans
    contexte, puis passes de raffinement injectant la liste des tiers si le
    tiers n'est pas reconnu, puis les numéros de BL connus du tiers reconnu.
    Analyser plusieurs BL en un seul appel coûtait moins cher mais dégradait
    nettement la lecture — un BL par appel est le bon compromis.

    L'anticipation reste : pendant que l'opérateur vérifie la page N, les
    pages N+1 et N+2 sont déjà en cours d'analyse, de sorte que le passage à
    la page suivante n'attend pratiquement jamais le modèle.

    Utilisation ::

        p = Prechargeur(pages, "fournisseur", referentiel)
        p.amorcer(0)                 # lance la page 0 et les suivantes
        infos = p.resultat(0)        # attend seulement si nécessaire
        p.amorcer(1)                 # entretient l'avance à chaque page

    Aucune exception n'est propagée : une page dont l'analyse a échoué revient
    avec des champs vides et le motif dans `erreurs`, l'opérateur saisit alors
    manuellement."""

    def __init__(self, pages: list[bytes], tiers_libelle: str,
                 referentiel=None, pages_d_avance: int | None = None) -> None:
        parametres = get_settings()
        self.pages = pages
        self.tiers_libelle = tiers_libelle
        self.referentiel = referentiel
        self.pages_d_avance = (parametres.lot_pages_avance
                               if pages_d_avance is None else max(0, pages_d_avance))
        self.erreurs: dict[int, str] = {}
        self._resultats: dict[int, dict] = {}
        self._futures: dict[int, Future] = {}
        # 2 threads : au-delà, on sature l'endpoint sans gagner en fluidité,
        # l'opérateur ne pouvant vérifier qu'une page à la fois.
        self._executeur = ThreadPoolExecutor(max_workers=2,
                                             thread_name_prefix="bl-lot")
        self.actif = bool(extraction.endpoint_configure())

    def _analyser(self, index: int) -> dict:
        return extraction.extraire_infos_bl(
            [self.pages[index]], self.tiers_libelle, referentiel=self.referentiel)

    def amorcer(self, index: int) -> None:
        """Soumet la page `index` et celles d'avance, si ce n'est pas déjà fait."""
        if not self.actif:
            return
        for page in range(index, min(index + self.pages_d_avance + 1,
                                     len(self.pages))):
            if page not in self._futures:
                self._futures[page] = self._executeur.submit(self._analyser, page)

    def en_cours(self, index: int) -> bool:
        """Vrai si l'analyse de cette page n'est pas encore disponible."""
        if not self.actif or index in self._resultats:
            return False
        future = self._futures.get(index)
        return future is not None and not future.done()

    def resultat(self, index: int, delai: float = 120.0) -> dict:
        """Champs détectés pour la page `index` ; attend son analyse au besoin."""
        if index in self._resultats:
            return self._resultats[index]
        if not self.actif or index >= len(self.pages):
            return {}
        self.amorcer(index)
        try:
            self._resultats[index] = self._futures[index].result(timeout=delai)
        except Exception as exc:
            logger.warning("Analyse de la page %d en échec : %s", index, exc,
                           exc_info=True)
            self._resultats[index] = {}
            self.erreurs[index] = f"{type(exc).__name__} : {exc}"
        return self._resultats[index]

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
