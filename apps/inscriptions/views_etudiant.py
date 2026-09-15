"""
Vues API pour le portail étudiant — accès en lecture seule.

Endpoints :
  GET /inscriptions/etudiant/releve/?annee=2025-2026  → Relevé annuel IPGEI
  GET /inscriptions/etudiant/progression/            → Décision du jury annuel

CE QUI A ÉTÉ CORRIGÉ. Ces deux vues lisaient la couche LMD du socle —
`InscriptionPedagogique`, `ResultatElement`, `Progression` — que l'IPGEI a
remplacée et dont les tables sont VIDES. L'étudiant recevait un relevé sans
ligne et une progression introuvable, sans la moindre erreur pour l'en avertir.

Pire : l'écran `/dashboard/portail/releve` attendait déjà la forme IPGEI —
`semestres[].notes[]`, `total_coefficients`, `seuil`, `valide` — quand cette vue
rendait encore la forme LMD, `semestres → modules → éléments`. La migration
avait été commencée côté frontend et jamais finie ici : l'écran aurait été cassé
même avec des données.

On rend désormais ce que `_serialiser_releve_annuel` produit pour la scolarité :
le même code qui a émis les huit relevés annuels déjà délivrés.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status


def _get_etudiant_or_403(request):
    """
    Retourne l'Etudiant lié au CustomUser courant ou (None, Response 403).
    """
    try:
        return request.user.etudiant_profile, None
    except Exception:
        return None, Response(
            {'error': "Aucun profil étudiant lié à votre compte."},
            status=status.HTTP_403_FORBIDDEN,
        )


class ReleveAnnuelEtudiantView(APIView):
    """
    GET /inscriptions/etudiant/releve/?annee=2025-2026

    Le relevé annuel de l'étudiant, dans la forme qu'attend l'écran du portail.

    `annee` est le LIBELLÉ de l'année — « 2025-2026 » —, celui que l'écran
    envoie déjà. L'ancienne vue attendait un identifiant : elle n'aurait rien
    trouvé même si ses tables avaient été pleines.

    Visible seulement après DÉLIBÉRATION VALIDÉE. Un relevé remis avant le jury
    porte des moyennes qui ne sont pas encore des décisions, et c'est le guichet
    qui reçoit les réclamations.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.ipgei import services
        from apps.ipgei.views import _serialiser_releve_annuel
        from apps.portail.ipgei_source import inscription_courante, jury_a_statue

        etudiant, err = _get_etudiant_or_403(request)
        if err is not None:
            return err

        annee = request.query_params.get('annee')
        if not annee:
            return Response(
                {'error': 'Le paramètre annee est requis.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        inscription = inscription_courante(etudiant, annee)
        if inscription is None:
            return Response(
                {'error': f"Aucune inscription en {annee}."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not jury_a_statue(inscription):
            return Response(
                {'error': "Votre relevé sera disponible après la délibération."},
                status=status.HTTP_403_FORBIDDEN,
            )

        donnees = services.releve_annuel(inscription, annee)
        return Response(_serialiser_releve_annuel(donnees))


class ProgressionEtudiantView(APIView):
    """
    GET /inscriptions/etudiant/progression/

    La décision du dernier jury ANNUEL validé — passage, redoublement,
    autorisation à concourir.

    Elle remplace la lecture de `inscriptions.Progression`, table du socle que
    l'IPGEI n'alimente pas : la décision de passage vit désormais sur la ligne
    de délibération.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.ipgei.models import Deliberation, LigneDeliberation
        from apps.portail.ipgei_source import inscriptions_de

        etudiant, err = _get_etudiant_or_403(request)
        if err is not None:
            return err

        ligne = (
            LigneDeliberation.objects
            .filter(inscription__in=inscriptions_de(etudiant),
                    deliberation__statut=Deliberation.STATUT_VALIDEE,
                    deliberation__portee=Deliberation.PORTEE_ANNUELLE)
            .select_related('deliberation', 'inscription__classe')
            .order_by('-deliberation__annee_universitaire',
                      '-deliberation__date_validation')
            .first()
        )
        if ligne is None:
            return Response(
                {'error': "Aucune décision disponible : la délibération annuelle "
                          "n'a pas encore été validée."},
                status=status.HTTP_404_NOT_FOUND,
            )

        inscription = ligne.inscription
        return Response({
            'decision':       ligne.decision,
            'decision_label': ligne.get_decision_display(),
            # Le vocabulaire du socle — filière, crédits, taux de
            # capitalisation — n'a pas d'équivalent à l'IPGEI : on rend la
            # classe et la moyenne, qui sont ce que la décision a pesé.
            'filiere_source': {
                'code':        inscription.classe.code,
                'intitule_fr': inscription.classe.nom,
            },
            'filiere_cible':      None,
            'niveau_source':      inscription.niveau,
            'niveau_cible':       None,
            'moyenne_generale':   (str(ligne.moyenne_generale)
                                   if ligne.moyenne_generale is not None else None),
            'rang':               ligne.rang,
            'seuil':              str(ligne.deliberation.seuil_validation),
            'annee_source':       ligne.deliberation.annee_universitaire,
            'annee_cible':        None,
            'date_jury':          (ligne.deliberation.date_validation.isoformat()
                                   if ligne.deliberation.date_validation else None),
        })
