"""Accès mutualisé à l'endpoint de model serving Databricks.

Un seul endroit sait parler au modèle : `extraction.py` (vision, lecture des
BL) et `rapports.py` (texte, analyse d'activité) s'appuient tous deux dessus.
L'authentification est celle du runtime de l'app — aucun jeton en dur.

Le décodage de la réponse accepte les deux formats rencontrés en production :
chat OpenAI-compatible (`choices[].message.content`) et Anthropic natif
(`content[].text`).
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

from .config import get_settings

logger = logging.getLogger("bl.llm")


def endpoint_configure() -> Optional[str]:
    """Nom de l'endpoint à utiliser, ou None si l'IA n'est pas activée
    (les applications basculent alors sur un fonctionnement sans IA)."""
    return get_settings().llm_endpoint or None


def _texte_reponse(contenu) -> str:
    """Le contenu d'un message peut être une chaîne ou une liste de blocs."""
    if isinstance(contenu, list):
        return " ".join(bloc.get("text", "") for bloc in contenu
                        if isinstance(bloc, dict))
    return contenu or ""


def extraire_contenu(resp) -> str:
    """Texte de la réponse, quel que soit le format de l'endpoint. Lève une
    erreur explicite sinon, en exposant les clés reçues pour le diagnostic."""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        choix = resp.get("choices")
        if choix:
            msg = choix[0].get("message") or {}
            if "content" in msg:
                return _texte_reponse(msg["content"])
            if "text" in choix[0]:
                return choix[0]["text"]
        if "content" in resp:
            return _texte_reponse(resp["content"])
        for cle in ("completion", "output_text", "predictions", "result"):
            if resp.get(cle):
                return _texte_reponse(resp[cle])
    raise ValueError(
        "Réponse de l'endpoint au format inattendu "
        f"(clés : {list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__})."
    )


def _client():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def invoquer(contenu: list, max_tokens: int = 900, client=None) -> str:
    """Appel brut : `contenu` est la liste de blocs du message utilisateur
    (texte et/ou images). Renvoie le texte de la réponse."""
    endpoint = endpoint_configure()
    if not endpoint:
        raise RuntimeError("Aucun endpoint LLM configuré (variable BL_LLM_ENDPOINT).")
    body = {"messages": [{"role": "user", "content": contenu}],
            "max_tokens": max_tokens}
    resp = (client or _client()).api_client.do(
        "POST", f"/serving-endpoints/{endpoint}/invocations", body=body)
    return extraire_contenu(resp)


def bloc_image(image: bytes) -> dict:
    """Bloc « image » au format attendu par les endpoints multimodaux."""
    b64 = base64.b64encode(image).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def completer(prompt: str, max_tokens: int = 1200, client=None) -> str:
    """Complétion texte simple (analyse, synthèse, commentaire)."""
    return invoquer([{"type": "text", "text": prompt}],
                    max_tokens=max_tokens, client=client)
