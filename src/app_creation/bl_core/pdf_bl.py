"""Export PDF d'un BL : métadonnées + pages scannées (archivage / litiges).

Génération 100 % en mémoire avec fpdf2 (aucun fichier temporaire). La police
Helvetica (latin-1) couvre le français courant ; les caractères hors latin-1
sont remplacés par « ? » (aucune erreur d'export).
"""

import io

from fpdf import FPDF
from fpdf.enums import XPos, YPos

_BLEU = (15, 98, 166)
_ENCRE = (27, 42, 58)
_GRIS = (91, 107, 124)


def _latin(texte) -> str:
    return str("—" if texte in (None, "") else texte).encode(
        "latin-1", "replace").decode("latin-1")


def generer_pdf_bl(meta: list[tuple[str, str]], pages: list[bytes],
                   titre: str) -> bytes:
    """PDF : page de garde (métadonnées en tableau) + une page par image.
    `meta` est une liste ordonnée (libellé, valeur)."""
    pdf = FPDF(unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)

    # --- Page de garde : titre + tableau des métadonnées ---
    pdf.add_page()
    pdf.set_text_color(*_BLEU)
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 12, _latin(titre), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_draw_color(*_BLEU)
    pdf.set_line_width(0.8)
    pdf.line(10, pdf.get_y(), 90, pdf.get_y())
    pdf.ln(6)

    pdf.set_font("Helvetica", "", 11)
    for libelle, valeur in meta:
        pdf.set_text_color(*_GRIS)
        pdf.cell(52, 8, _latin(libelle), new_x=XPos.RIGHT)
        pdf.set_text_color(*_ENCRE)
        pdf.set_font("Helvetica", "B", 11)
        pdf.multi_cell(0, 8, _latin(valeur), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 11)
    pdf.ln(4)
    pdf.set_text_color(*_GRIS)
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(0, 6, _latin(f"{len(pages)} page(s) scannée(s) en annexe."),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # --- Une page A4 par image scannée ---
    for i, image in enumerate(pages):
        pdf.add_page()
        pdf.set_text_color(*_GRIS)
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 6, _latin(f"{titre} — page scannée {i + 1}/{len(pages)}"),
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        try:
            pdf.image(io.BytesIO(image), x=10, y=pdf.get_y() + 2, w=190)
        except Exception:
            pdf.set_font("Helvetica", "", 11)
            pdf.cell(0, 10, _latin("Image illisible pour l'export PDF."),
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return bytes(pdf.output())
