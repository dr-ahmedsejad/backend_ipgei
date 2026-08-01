"""
Service d'anonymat — granularité (étudiant × session).
Assigne un numéro aléatoire unique à chaque étudiant inscrit au semestre couvert
par la session, pour garantir l'intégrité des examens.
"""
import random

from django.db import transaction

from apps.evaluations.models import AnonymatSession, SessionEvaluation


class AnonymatService:

    @staticmethod
    @transaction.atomic
    def generer(session: SessionEvaluation, regenerer: bool = False, genere_par=None,
                force: bool = False) -> int:
        """
        Assigne un numéro 1..N aléatoire à chaque étudiant couvert par la session.

        Si regenerer=False et des anonymats existent déjà → lève ValueError (409).
        Si regenerer=True → supprime les anciens et recrée.

        GARDE-FOU : si des notes ont déjà été saisies pour la session, la
        régénération réassignerait l'association numéro↔étudiant et conduirait à
        des notes mal attribuées lors des saisies par anonymat suivantes. On la
        BLOQUE (ValueError → 409), sauf override explicite `force=True` (réservé
        à un administrateur côté vue).

        Retourne N (nombre d'anonymats générés).
        """
        # Session clôturée : aucune (re)génération possible (les notes sont figées).
        if getattr(session, 'est_close', False):
            raise ValueError(
                'Session clôturée : la (re)génération des anonymats est impossible.'
            )

        existants = AnonymatSession.objects.filter(session=session)
        if existants.exists():
            if not regenerer:
                raise ValueError(
                    f'Des anonymats existent déjà pour cette session. '
                    f'Utiliser regenerer=1 pour les remplacer.'
                )
            from apps.evaluations.models import Note
            if not force and Note.objects.filter(session=session).exists():
                raise ValueError(
                    'Des notes ont déjà été saisies pour cette session : la '
                    'régénération des anonymats est bloquée pour éviter une '
                    'mauvaise attribution des notes. Contactez un administrateur '
                    'si une régénération est réellement nécessaire.'
                )
            existants.delete()

        # Récupérer tous les InscriptionAdministrative couverts par la session
        from apps.inscriptions.models import InscriptionAdministrative

        TYPE_MAP = {'Impairs': 'I', 'Pairs': 'P'}
        sem_type = TYPE_MAP.get(session.type_semestre, 'I')

        insc_admins = list(
            InscriptionAdministrative.objects.filter(
                annee_univ=session.annee_univ,
                inscriptions_ped__semestre__type_semestre=sem_type,
            ).distinct()
        )

        if not insc_admins:
            return 0

        # Shuffle → numéros aléatoires non prédictibles
        numeros = list(range(1, len(insc_admins) + 1))
        random.shuffle(numeros)

        objets = [
            AnonymatSession(
                session=session,
                inscription_admin=ia,
                numero_anonymat=n,
                genere_par=genere_par,
            )
            for ia, n in zip(insc_admins, numeros)
        ]
        AnonymatSession.objects.bulk_create(objets)
        return len(objets)

    @staticmethod
    def resoudre(session: SessionEvaluation, numero: int):
        """
        Retourne l'InscriptionAdministrative correspondant à un numéro d'anonymat.
        Lève AnonymatSession.DoesNotExist si introuvable.
        """
        return AnonymatSession.objects.select_related('inscription_admin').get(
            session=session,
            numero_anonymat=numero,
        ).inscription_admin
