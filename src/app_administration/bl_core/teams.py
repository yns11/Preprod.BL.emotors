"""Notifications Microsoft Teams envoyées directement par les applications.

Deux formats, deux usages :

* **Carte adaptative** (`carte_nouvelle_reception`) — nouvelle réception. Elle
  **mentionne** (`@`) les gestionnaires du portefeuille du fournisseur, ce qui
  les notifie personnellement dans le canal. L'app ne pose PAS les entités de
  mention elle-même (Teams les rejette) : elle place le marqueur
  ``{{MENTIONS}}`` dans la carte et joint les e-mails ; le flux Power Automate
  les convertit en jetons via « Obtenir un jeton @mention pour un utilisateur ».
* **MessageCard** (`carte_passage_ok`) — passage EDI NOK → OK, format
  historique conservé, enrichi du commentaire éventuel du gestionnaire.

L'envoi est **direct** (aucun job) et **best effort** : une indisponibilité de
Teams ne doit jamais empêcher l'enregistrement d'un BL. L'appelant reçoit
(succès, message d'erreur) et journalise le résultat en base.

Prérequis côté Teams : un flux « Workflows » du canal, déclencheur
« Lorsqu'une requête webhook Teams est reçue », publiant la carte reçue. Les
personnes mentionnées doivent être membres de l'équipe/du canal.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .config import get_settings

logger = logging.getLogger("bl.teams")

_VERT = "43B02A"
_BLEU = "0F62A6"


def _poster(url: str, charge: dict) -> tuple[bool, str]:
    """POST JSON vers un webhook Teams. Renvoie (succès, message)."""
    if not url:
        return False, "Aucun webhook Teams configuré."
    # `mentions` est TOUJOURS présent, même vide : le « Appliquer à chacun »
    # du flux échoue sur une valeur absente (« expression is of type Null.
    # The result must be a valid array »).
    charge.setdefault("mentions", [])
    requete = urllib.request.Request(
        url,
        data=json.dumps(charge).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(requete, timeout=get_settings().teams_timeout_s) as reponse:
            if 200 <= reponse.status < 300:
                return True, "OK"
            return False, f"HTTP {reponse.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} : {exc.reason}"
    except Exception as exc:                       # réseau, DNS, timeout…
        return False, f"{type(exc).__name__} : {exc}"


# Marqueur remplacé par le flux Power Automate (mode « flow ») par les jetons
# de mention produits par l'action « Obtenir un jeton @mention pour un
# utilisateur ». Ne jamais le modifier sans adapter le flux.
MARQUEUR_MENTIONS = "{{MENTIONS}}"


def _mentions(destinataires: list[dict]) -> tuple[str, list[str]]:
    """Prépare la mention des destinataires selon `BL_TEAMS_MENTION_MODE`.

    Renvoie (texte à insérer dans la carte, e-mails à mentionner) :

    * **flow** (défaut) — le texte est le marqueur `{{MENTIONS}}` et les
      e-mails sont joints à la charge utile. Le flux appelle « Obtenir un
      jeton @mention pour un utilisateur » pour chacun et remplace le
      marqueur : c'est le **Flow bot** qui pose les entités, seule méthode
      fiable (écrire soi-même `msteams.entities` est rejeté par Teams avec
      « One or more mention entity could not be found in card text »).
      L'action exige l'**e-mail** de la personne, jamais son AAD Object ID.
    * **texte** — noms en clair, aucune mention (repli si le flux ne peut pas
      être modifié).

    Un gestionnaire sans e-mail est simplement cité, sans être notifié.
    """
    mode = get_settings().teams_mention_mode
    noms, emails = [], []
    for destinataire in destinataires:
        nom = (destinataire.get("nom") or destinataire.get("code") or "").strip()
        email = (destinataire.get("email") or "").strip()
        if not nom:
            continue
        if mode == "flow" and email:
            # Minuscules : le flux compare cet e-mail aux membres de l'équipe
            # Teams avec « contains », qui est sensible à la casse (le flux
            # applique toLower() de son côté).
            emails.append(email.lower())
        else:
            noms.append(nom)

    if emails:
        # Le marqueur précède les éventuels noms non mentionnables.
        texte = " ".join([MARQUEUR_MENTIONS] + noms)
    else:
        texte = " ".join(noms)
    return texte, emails


def carte_nouvelle_reception(numero_bl: str, fournisseur: str, quai: str,
                             date_reception, plage_horaire: str, statut_libelle: str,
                             nb_pages: int, saisi_par: str,
                             destinataires: list[dict]) -> dict:
    """Carte adaptative « nouvelle réception », avec mentions des gestionnaires."""
    texte_mentions, emails = _mentions(destinataires)
    faits = [
        {"title": "Fournisseur", "value": fournisseur or "—"},
        {"title": "Quai", "value": quai or "—"},
        {"title": "Réception", "value": f"{date_reception or '—'} · {plage_horaire or '—'}"},
        {"title": "État", "value": statut_libelle},
        {"title": "Pages", "value": str(nb_pages)},
        {"title": "Saisi par", "value": saisi_par or "—"},
    ]
    corps = [
        {
            "type": "TextBlock",
            "text": f"📥 Nouvelle réception — BL {numero_bl}",
            "weight": "Bolder",
            "size": "Large",
            "color": "Good",
            "wrap": True,
        },
        {"type": "FactSet", "facts": faits},
    ]
    if texte_mentions:
        corps.append({
            "type": "TextBlock",
            "text": f"Gestionnaire(s) : {texte_mentions}",
            "wrap": True,
        })
    carte = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": corps,
    }
    # Enveloppe attendue par le flux Teams « Workflows ». En mode « flow »,
    # « mentions » porte les e-mails que le flux transforme en jetons.
    charge = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": carte,
        }],
    }
    if emails:
        charge["mentions"] = emails
    return charge


def carte_passage_ok(numero_bl: str, message: str, cree_par: str,
                     quand: str, commentaire: str = "") -> dict:
    """MessageCard « EDI NOK → OK » — structure historique, plus le
    commentaire facultatif laissé par le gestionnaire."""
    faits = [
        {"name": "Type", "value": "EDI_NOK_OK"},
        {"name": "Quand", "value": quand},
        {"name": "Par", "value": cree_par or "—"},
    ]
    if commentaire:
        faits.append({"name": "Commentaire", "value": commentaire})
    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": _VERT,
        "summary": f"BL {numero_bl} passé de EDI NOK à OK",
        "sections": [{
            "activityTitle": f"✅ BL {numero_bl} passé de EDI NOK à OK",
            "text": message,
            "facts": faits,
        }],
    }


def envoyer_nouvelle_reception(**kwargs) -> tuple[bool, str]:
    return _poster(get_settings().teams_webhook_reception,
                   carte_nouvelle_reception(**kwargs))


def envoyer_passage_ok(**kwargs) -> tuple[bool, str]:
    return _poster(get_settings().teams_webhook_edi, carte_passage_ok(**kwargs))
