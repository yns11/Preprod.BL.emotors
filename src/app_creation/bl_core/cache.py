"""Mémoïsation des lectures de référentiel, indépendante de Streamlit.

`repository` est utilisé par les deux applications **et** par les tâches
Lakeflow (rapports d'activité). Dans une app, la mise en cache doit être celle
de Streamlit — partagée entre les sessions et invalidable par `.clear()`. Dans
un job, Streamlit n'est pas installé : le décorateur se replie alors sur
`functools.lru_cache`, qui offre la même interface minimale (`.clear()`).

Aucune logique métier ici : uniquement le choix du bon décorateur.
"""

from __future__ import annotations

import functools

try:                                   # applications Streamlit
    import streamlit as _st

    _cache_streamlit = _st.cache_data
except Exception:                      # tâches Lakeflow, tests unitaires
    _cache_streamlit = None


def _lru(ttl: int, max_entries: int | None):
    """Repli hors Streamlit. Le TTL n'a pas d'équivalent en `lru_cache` : un
    processus de job vit quelques minutes, la donnée n'a pas le temps de
    devenir obsolète — la borne de taille suffit."""
    def decorateur(fonction):
        enveloppe = functools.lru_cache(maxsize=max_entries or 128)(fonction)
        enveloppe.clear = enveloppe.cache_clear      # même API que Streamlit
        return enveloppe
    return decorateur


def cache_lecture(ttl: int = 300, max_entries: int | None = None):
    """Décorateur de mémoïsation à utiliser dans `repository`."""
    if _cache_streamlit is not None:
        if max_entries is not None:
            return _cache_streamlit(ttl=ttl, show_spinner=False,
                                    max_entries=max_entries)
        return _cache_streamlit(ttl=ttl, show_spinner=False)
    return _lru(ttl, max_entries)
