"""Orchestration des notifications : journal en base puis envoi Teams.

Toujours dans cet ordre, et toujours en *best effort* : la trace en base est
écrite d'abord (elle fait foi), l'envoi Teams ensuite. Si Teams est
indisponible, l'erreur est enregistrée dans `notifications.erreur_envoi` et
visible dans la vue Gestion ▸ Notifications — mais l'opération métier
(création du BL, passage à OK) n'est jamais annulée pour autant.
"""

from __future__ import annotations

import logging

from . import repository, teams

logger = logging.getLogger("bl.notifications")

TYPE_NOUVELLE_RECEPTION = "NOUVELLE_RECEPTION"
TYPE_EDI_NOK_OK = "EDI_NOK_OK"


def _tracer(type_notif: str, numero_bl: str, message: str, utilisateur: str,
            envoi, commentaire: str = "", destinataires: str = "") -> tuple[bool, str]:
    try:
        notification_id = repository.enregistrer_notification(
            type_notif, numero_bl, message, utilisateur,
            commentaire=commentaire, destinataires=destinataires)
    except Exception as exc:
        logger.exception("Notification non journalisée pour le BL %s", numero_bl)
        return False, f"Journal indisponible : {exc}"

    try:
        succes, detail = envoi()
    except Exception as exc:                    # ne doit jamais remonter
        succes, detail = False, f"{type(exc).__name__} : {exc}"
    repository.marquer_notification_envoyee(notification_id, succes, detail)
    if not succes:
        logger.warning("Notification Teams non envoyée (BL %s) : %s", numero_bl, detail)
    return succes, detail


def notifier_nouvelle_reception(numero_bl: str, fournisseur: str, quai: str,
                                date_reception, plage_horaire: str,
                                statut_libelle: str, nb_pages: int,
                                utilisateur: str) -> tuple[bool, str]:
    """Carte adaptative dans le canal Teams, avec @mention des gestionnaires
    du portefeuille de ce fournisseur."""
    try:
        destinataires = repository.gestionnaires_pour_fournisseur(fournisseur)
    except Exception:
        logger.exception("Gestionnaires introuvables pour %s", fournisseur)
        destinataires = []
    noms = ", ".join(d["nom"] for d in destinataires) or "—"
    message = (f"Nouvelle réception BL {numero_bl} ({fournisseur or '—'}, "
               f"quai {quai or '—'}, {date_reception or '—'} {plage_horaire or ''}) "
               f"saisie par {utilisateur}.")

    def envoi():
        return teams.envoyer_nouvelle_reception(
            numero_bl=numero_bl, fournisseur=fournisseur, quai=quai,
            date_reception=date_reception, plage_horaire=plage_horaire,
            statut_libelle=statut_libelle, nb_pages=nb_pages,
            saisi_par=utilisateur, destinataires=destinataires)

    return _tracer(TYPE_NOUVELLE_RECEPTION, numero_bl, message, utilisateur,
                   envoi, destinataires=noms)


def notifier_passage_ok(numero_bl: str, fournisseur: str, quai: str,
                        date_reception, utilisateur: str,
                        commentaire: str = "") -> tuple[bool, str]:
    """MessageCard « EDI NOK → OK » (format historique) + commentaire libre."""
    message = (f"BL {numero_bl} ({fournisseur or '—'}, quai {quai or '—'}, "
               f"reçu le {date_reception or '—'}) : état passé de EDI NOK à OK "
               f"par {utilisateur}.")
    quand = repository.maintenant_local().strftime("%d/%m/%Y %H:%M")

    def envoi():
        return teams.envoyer_passage_ok(
            numero_bl=numero_bl, message=message, cree_par=utilisateur,
            quand=quand, commentaire=commentaire)

    return _tracer(TYPE_EDI_NOK_OK, numero_bl, message, utilisateur, envoi,
                   commentaire=commentaire)
