"""Identité de l'utilisateur connecté (SSO Databricks Apps).

Databricks Apps authentifie chaque visiteur en amont (OAuth SSO) et transmet
son identité dans les en-têtes HTTP de la requête. Streamlit les expose via
st.context.headers. Aucune gestion de mot de passe côté app.
"""

import os

import streamlit as st


def get_current_user() -> str:
    """Nom de l'utilisateur SSO, pour la traçabilité (saisie_par / modifie_par)."""
    try:
        headers = st.context.headers
        user = headers.get("X-Forwarded-Preferred-Username") or headers.get("X-Forwarded-Email")
        if user:
            return user
    except Exception:
        pass
    # Exécution locale uniquement : identité explicite, jamais un utilisateur
    # implicite qui pourrait accidentellement recevoir des droits.
    local_user = os.environ.get("BL_LOCAL_USER", "").strip().lower()
    if os.environ.get("BL_ENVIRONMENT", "local").lower() == "local" and local_user:
        return local_user
    raise RuntimeError(
        "Identité SSO Databricks absente. En local, définissez BL_LOCAL_USER ; "
        "dans Databricks, vérifiez le proxy d'authentification de l'app."
    )
