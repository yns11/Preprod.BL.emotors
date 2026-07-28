"""Règles de validation métier réutilisées par l'UI et les services."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import get_settings

_BL_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+\- ]{0,79}$")


@dataclass(frozen=True)
class PageValidation:
    count: int
    total_bytes: int


def normalize_bl_number(value: str) -> str:
    number = " ".join((value or "").strip().split())
    if not _BL_NUMBER.fullmatch(number):
        raise ValueError(
            "Le numéro de BL doit contenir 1 à 80 caractères "
            "(lettres, chiffres, espace, . _ / @ + ou -)."
        )
    return number


def validate_pages(pages: list[bytes]) -> PageValidation:
    settings = get_settings()
    if not pages:
        raise ValueError("Au moins une page est obligatoire.")
    if len(pages) > settings.max_pages:
        raise ValueError(f"Le BL ne peut pas dépasser {settings.max_pages} pages.")
    total = 0
    for index, page in enumerate(pages, start=1):
        if not page:
            raise ValueError(f"La page {index} est vide.")
        if len(page) > settings.max_image_bytes:
            raise ValueError(
                f"La page {index} dépasse {settings.max_image_bytes // 1024 // 1024} Mo."
            )
        total += len(page)
    if total > settings.max_total_bytes:
        raise ValueError(
            f"Le document dépasse la limite totale de "
            f"{settings.max_total_bytes // 1024 // 1024} Mo."
        )
    return PageValidation(count=len(pages), total_bytes=total)
