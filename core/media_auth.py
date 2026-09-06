"""
Contrôle d'accès aux fichiers /media/ — sous-requête Nginx `auth_request`.

Nginx ne sert jamais un fichier de /media/ sans avoir d'abord demandé ici :
« ce porteur de cookie a-t-il le droit de lire CE chemin ? ». 204 = oui,
403 = non. Il transmet le chemin demandé dans `X-Original-URI`.

La première version répondait 204 à quiconque était connecté, sans lire le
chemin. Les numéros de série des documents officiels étant séquentiels, un
étudiant connecté pouvait énumérer `documents/officiels/AI-2026-00001.pdf`,
`…00002.pdf`, et télécharger les relevés de toute la promotion, puis les
pièces d'identité de la préinscription. Ce module pose la question qui manquait :
le fichier est-il le sien, ou détient-il le droit de le voir ?

Règle par préfixe de chemin (`upload_to` des modèles) :

  - admin, IT, superuser ................. tout ;
  - préfixe public (logos, sceaux) ....... tout connecté ;
  - préfixe partagé (cours, avatars) ..... tout connecté ;
  - préfixe protégé ...................... le PROPRIÉTAIRE de l'objet qui porte
                                            le fichier, ou un porteur d'un des
                                            modules RBAC qui gèrent ce type ;
  - préfixe inconnu ...................... refusé — on ferme, on n'ouvre pas.

Sans `X-Original-URI` (appel direct, hors Nginx), on garde la réponse
historique pour un connecté : il n'y a rien à protéger, la route est `internal`
côté Nginx et ne livre aucun fichier.
"""
import logging
from urllib.parse import unquote, urlsplit

from django.apps import apps
from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import _has_access

logger = logging.getLogger('siga')

ROLES_SANS_RESTRICTION = ('admin', 'IT')

#: Visibles avant même la connexion côté Nginx ; ici, simple cohérence.
PREFIXES_PUBLICS = ('institutions/',)

#: Contenu destiné à être vu par tout connecté : supports de cours déposés
#: pour les inscrits, avatars affichés dans les listes d'utilisateurs.
PREFIXES_PARTAGES = ('cours/', 'avatars/')

#: Préfixe → (modules RBAC dont `voir` ouvre le type de fichier,
#:            [(modèle, champ fichier, chemin vers l'utilisateur propriétaire)])
#:
#: Le chemin propriétaire va de l'objet au `CustomUser` : `etudiant__user` pour
#: un document d'élève, `user` pour le CV d'un enseignant. Vide = pas de
#: propriétaire possible (une préinscription n'a pas encore de compte).
REGLES = {
    'documents/officiels/': (
        ('doc_registre', 'ipgei_documents'),
        [('documents.DocumentOfficiel', 'fichier_pdf', 'etudiant__user')],
    ),
    'etudiants/photos/': (
        ('scolarite_etudiants', 'ipgei_inscriptions', 'ipgei_absences', 'absences', 'abs_saisie'),
        [('absence.Etudiant', 'photo', 'user')],
    ),
    'justificatifs/': (
        ('absences', 'abs_saisie', 'ipgei_absences', 'reclamations'),
        [('absence.Presence', 'justificatif', 'etudiant__user')],
    ),
    'reclamations/justificatifs/': (
        ('reclamations',),
        [('reclamations.Reclamation', 'justificatif', 'etudiant__user')],
    ),
    'preinscriptions/': (
        ('insc_administrative', 'ipgei_inscriptions'),
        [],
    ),
    'derogations/': (
        ('insc_derogation', 'ipgei_inscriptions'),
        [('inscriptions.Derogation', 'justificatif', 'etudiant__user')],
    ),
    'justificatifs_annee_blanche/': (
        ('evaluations_delib', 'ipgei_deliberation'),
        [('evaluations.JustificatifAnneeBlanche', 'document',
          'ligne_deliberation__inscription__etudiant__user')],
    ),
    'cvs/': (
        ('profs',),
        [('prof.Prof', 'cv', 'user')],
    ),
    'diplomes/': (
        ('profs',),
        [('prof.Prof', 'diplome', 'user')],
    ),
    'stages/': (
        ('stage_convention', 'stage_derogation'),
        [('stages.ConventionStage', 'convention_fichier', 'etudiant__user'),
         ('stages.DerogationMedicale', 'justificatif', 'etudiant__user')],
    ),
}


def chemin_media(uri: str):
    """
    `/media/documents/officiels/X.pdf?x=1` → `documents/officiels/X.pdf`.

    Rend None si l'URI ne désigne pas un fichier de /media/ ou tente de
    remonter dans l'arborescence.
    """
    if not uri:
        return None
    chemin = unquote(urlsplit(uri).path)
    media_url = settings.MEDIA_URL or '/media/'
    if not chemin.startswith(media_url):
        return None
    relatif = chemin[len(media_url):].lstrip('/')
    if not relatif or '..' in relatif.split('/') or '\\' in relatif:
        return None
    return relatif


def _est_proprietaire(user, chemin: str, porteurs) -> bool:
    for modele, champ, vers_user in porteurs:
        try:
            Modele = apps.get_model(modele)
            if Modele.objects.filter(**{champ: chemin, vers_user: user.pk}).exists():
                return True
        except Exception:                                   # noqa: BLE001
            # Un modèle ou un champ renommé ne doit pas OUVRIR l'accès.
            logger.exception('media-auth : propriété indéterminable (%s.%s)', modele, champ)
    return False


def acces_autorise(user, chemin: str) -> bool:
    """Le cœur de la décision — sans HTTP, pour être testable directement."""
    if user.is_superuser or getattr(user, 'role', '') in ROLES_SANS_RESTRICTION:
        return True
    if chemin.startswith(PREFIXES_PUBLICS) or chemin.startswith(PREFIXES_PARTAGES):
        return True

    for prefixe, (modules, porteurs) in REGLES.items():
        if not chemin.startswith(prefixe):
            continue
        if any(_has_access(user, module, 'voir') for module in modules):
            return True
        return _est_proprietaire(user, chemin, porteurs)

    logger.warning('media-auth : préfixe inconnu refusé — %s', chemin)
    return False


class MediaAuthView(APIView):
    """Sous-requête Nginx : 204 si l'appelant peut lire le fichier, 403 sinon."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        uri = request.headers.get('X-Original-URI', '')
        if not uri:
            return Response(status=204)
        chemin = chemin_media(uri)
        if chemin is None:
            return Response(status=403)
        if acces_autorise(request.user, chemin):
            return Response(status=204)
        return Response(status=403)
