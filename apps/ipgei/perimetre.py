"""
Périmètre disciplinaire d'un responsable — chef de département ou coordinateur.

L'IPGEI a deux départements : **ST** (sciences et techniques — maths, physique,
chimie, informatique, SI, EPS) et **Langues** (anglais, français). Un chef de
département répond de toutes les matières du sien ; un coordinateur, de la ou
des matières qu'on lui confie. Les deux portent le même rôle RBAC
(`chef_departement`) : ce qui les distingue n'est pas un droit, c'est un
PÉRIMÈTRE — et c'est ici qu'il se calcule.

Le RBAC ouvre des modules ; le périmètre dit, dans ces modules, QUELLES matières.
Sans lui, `ipgei_edt:modifier` aurait permis au chef des langues de déplacer un
TP de physique. Avec lui, il ne voit et ne touche que l'anglais et le français.

Règle de sûreté : un responsable dont le périmètre n'est pas encore configuré ne
voit RIEN — plutôt que tout. Les autres rôles ne sont jamais restreints : ce
module ne change rien pour eux.
"""
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Matiere, PerimetreResponsable

ROLE_RESPONSABLE = 'chef_departement'


def matieres_autorisees(user):
    """
    Les identifiants de matières qu'un utilisateur peut voir et toucher.

    Rend `None` quand rien ne le restreint — admin, direction, scolarité :
    tout ce qui n'est pas un responsable disciplinaire. Rend un ensemble
    (éventuellement vide) pour un responsable : l'union des matières de son
    département et de celles qu'on lui a confiées une à une.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return set()
    if user.is_superuser or getattr(user, 'role', '') != ROLE_RESPONSABLE:
        return None

    try:
        perimetre = user.perimetre_ipgei
    except PerimetreResponsable.DoesNotExist:
        # Rôle attribué, périmètre pas encore posé : on ferme, on n'ouvre pas.
        return set()

    ids = set(perimetre.matieres.values_list('id', flat=True))
    if perimetre.departement:
        ids |= set(Matiere.objects
                   .filter(departement=perimetre.departement)
                   .values_list('id', flat=True))
    return ids


def ems_autorises(user):
    """
    Même périmètre, exprimé en EM du socle — pour les écrans qui lisent le
    miroir (réclamations, suivi) et non la matière IPGEI.
    """
    ids = matieres_autorisees(user)
    if ids is None:
        return None
    return set(Matiere.objects
               .filter(id__in=ids, em_miroir__isnull=False)
               .values_list('em_miroir_id', flat=True))


class PerimetreMatiereMixin:
    """
    Restreint un ViewSet aux matières du responsable connecté.

    `perimetre_champ` nomme le chemin vers la matière : `'matiere'` pour une
    séance ou une note, `'pk'` quand la ressource EST la matière. Les autres
    rôles passent sans filtre — `matieres_autorisees` rend `None` pour eux.

    Les écritures sont gardées aussi : lister les seules matières du périmètre
    n'empêcherait pas d'envoyer l'identifiant d'une autre dans un POST.
    """
    perimetre_champ = 'matiere'

    def get_queryset(self):
        qs = super().get_queryset()
        ids = matieres_autorisees(self.request.user)
        if ids is None:
            return qs
        return qs.filter(**{self.perimetre_champ + '__in': ids})

    def _matiere_ecrite(self, serializer):
        if self.perimetre_champ == 'pk':
            return None
        donnees = serializer.validated_data
        if 'matiere' in donnees:
            return donnees['matiere']
        return getattr(serializer.instance, 'matiere', None)

    def _verifier_perimetre(self, serializer):
        ids = matieres_autorisees(self.request.user)
        if ids is None:
            return
        if self.perimetre_champ == 'pk':
            # La ressource est la matière : elle doit relever du département du
            # responsable. Un coordinateur, lié à des matières nommées, ne peut
            # pas en créer de nouvelles — elles ne lui auraient pas été confiées.
            perimetre = getattr(self.request.user, 'perimetre_ipgei', None)
            departement = serializer.validated_data.get(
                'departement', getattr(serializer.instance, 'departement', ''))
            if not perimetre or not perimetre.departement \
                    or departement != perimetre.departement:
                raise ValidationError(
                    'Cette matière ne relève pas de votre département.')
            return
        matiere = self._matiere_ecrite(serializer)
        if matiere is None or matiere.pk not in ids:
            raise ValidationError(
                "Cette séance porte une matière qui n'est pas de votre "
                'département : vous ne pouvez ni la créer ni la modifier.')

    def perform_create(self, serializer):
        self._verifier_perimetre(serializer)
        super().perform_create(serializer)

    def perform_update(self, serializer):
        self._verifier_perimetre(serializer)
        super().perform_update(serializer)


# ═════════════════════════════════════════════════════════════════════════════
# API — poser le périmètre d'un responsable
# ═════════════════════════════════════════════════════════════════════════════
class EstAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated
                    and (u.is_superuser or getattr(u, 'role', '') == 'admin'))


class PerimetreResponsableView(APIView):
    """
    GET  /api/v1/ipgei/perimetres/<user_id>/   → { departement, matieres: [ids] }
    PUT  /api/v1/ipgei/perimetres/<user_id>/   ← { departement, matieres }

    Réservé à l'admin : c'est lui qui distribue les responsabilités. Le
    périmètre est créé au premier PUT ; le vider revient à fermer l'accès.
    """
    permission_classes = [EstAdmin]

    def _perimetre(self, user_id):
        from django.contrib.auth import get_user_model
        utilisateur = get_user_model().objects.filter(pk=user_id).first()
        if utilisateur is None:
            return None, None
        perimetre = PerimetreResponsable.objects.filter(utilisateur=utilisateur).first()
        return utilisateur, perimetre

    def _serialiser(self, utilisateur, perimetre):
        return {
            'utilisateur': utilisateur.pk,
            'role':        utilisateur.role,
            'departement': perimetre.departement if perimetre else '',
            'matieres':    (sorted(perimetre.matieres.values_list('id', flat=True))
                            if perimetre else []),
        }

    def get(self, request, user_id):
        utilisateur, perimetre = self._perimetre(user_id)
        if utilisateur is None:
            return Response({'error': 'Utilisateur introuvable.'}, status=404)
        return Response(self._serialiser(utilisateur, perimetre))

    def put(self, request, user_id):
        utilisateur, perimetre = self._perimetre(user_id)
        if utilisateur is None:
            return Response({'error': 'Utilisateur introuvable.'}, status=404)

        departement = (request.data.get('departement') or '').upper()
        codes = {code for code, _ in Matiere.DEPARTEMENT_CHOICES}
        if departement and departement not in codes:
            return Response({'departement': f'Département inconnu : {departement}.'},
                            status=400)
        try:
            ids = [int(x) for x in (request.data.get('matieres') or [])]
        except (TypeError, ValueError):
            return Response({'matieres': 'Identifiants de matières attendus.'}, status=400)
        connues = set(Matiere.objects.filter(id__in=ids).values_list('id', flat=True))
        inconnues = [i for i in ids if i not in connues]
        if inconnues:
            return Response({'matieres': f'Matière introuvable : {inconnues[0]}.'}, status=400)

        if perimetre is None:
            perimetre = PerimetreResponsable(utilisateur=utilisateur)
        perimetre.departement = departement
        perimetre.save()
        perimetre.matieres.set(ids)
        return Response(self._serialiser(utilisateur, perimetre))
