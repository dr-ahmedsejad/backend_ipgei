"""
Photos des étudiants, déposées en une fois.

Chaque fichier porte le matricule de son étudiant : « 24607.jpg ». On en
dépose autant qu'on veut ; chacun va à son étudiant.

POST /api/v1/absences/photos-etudiants/   (multipart)
  photos     : les fichiers (plusieurs) ;
  remplacer  : « 1 » pour remplacer une photo déjà présente — sinon on ne fait
               que COMPLÉTER : un étudiant qui a déjà sa photo la garde ;
  apercu     : « 1 » pour savoir ce qui se passerait, sans rien écrire.

Réponse : une ligne par fichier, avec son statut, et un bilan. L'écran envoie
les fichiers par paquets (5 Mo par fichier au plus, 50 Mo par envoi côté
nginx) : chaque paquet est indépendant.

La photo est compressée à l'enregistrement (`Etudiant.save`, comme à l'unité).
L'ancienne n'est pas effacée du disque : un remplacement se rattrape.
"""
import os

from django.db import transaction
from django.db.models.functions import Lower
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit_helpers import write_audit
from core.models import ACTION_BULK_UPDATE

from .models import Etudiant

EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
TAILLE_MAX = 5 * 1024 * 1024

POSEE, REMPLACEE, GARDEE = 'posee', 'remplacee', 'deja_photo'
INCONNU, INVALIDE, TROP_LOURD, DOUBLON = 'inconnu', 'invalide', 'trop_lourd', 'doublon'
STATUTS_ECRITS = (POSEE, REMPLACEE)


def matricule_du_fichier(nom):
    """« 24607.JPG » → ('24607', '.jpg') ; extension refusée → ('…', None)."""
    base, ext = os.path.splitext(os.path.basename(nom or ''))
    ext = ext.lower()
    return base.strip(), (ext if ext in EXTENSIONS else None)


def est_une_image(fichier):
    from PIL import Image
    try:
        fichier.seek(0)
        Image.open(fichier).verify()
        return True
    except Exception:
        return False
    finally:
        fichier.seek(0)


def _vrai(valeur):
    return str(valeur or '').strip().lower() in ('1', 'true', 'oui', 'on')


class PhotosEtudiantsView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        from .views import _check_abs_module
        # Même droit que la modification d'une fiche étudiant.
        _check_abs_module(request.user, 'scolarite_etudiants', action='modifier')

        fichiers = request.FILES.getlist('photos')
        if not fichiers:
            return Response({'error': 'Aucune photo reçue (champ « photos »).'}, status=400)
        remplacer, apercu = _vrai(request.data.get('remplacer')), _vrai(request.data.get('apercu'))

        cles = {}
        for f in fichiers:
            mat, _ = matricule_du_fichier(f.name)
            cles.setdefault(mat.lower(), []).append(f.name)
        # Le matricule du fichier, sans tenir compte des majuscules.
        etudiants = {e.matricule.strip().lower(): e for e in Etudiant.objects
                     .annotate(cle=Lower('matricule')).filter(cle__in=list(cles))
                     .select_related('departement')}
        # IPGEI : le groupe d'un étudiant est la CLASSE de son inscription la
        # plus récente (le département du socle est vide).
        from apps.ipgei.models import InscriptionIPGEI
        classes = {}
        for i in (InscriptionIPGEI.objects.filter(etudiant__in=etudiants.values(), actif=True)
                  .select_related('classe').order_by('annee_universitaire')):
            classes[i.etudiant_id] = i.classe.nom

        lignes = []
        for f in fichiers:
            mat, ext = matricule_du_fichier(f.name)
            ligne = {'fichier': f.name, 'matricule': mat, 'statut': None, 'etudiant': None}
            e = etudiants.get(mat.lower())
            if e is not None:
                ligne['etudiant'] = {'id': e.pk, 'matricule': e.matricule, 'nom': e.nom,
                                     'groupe': classes.get(e.pk) or (e.departement.nom if e.departement_id else '')}
            if len(cles.get(mat.lower(), [])) > 1:
                ligne['statut'] = DOUBLON
            elif ext is None:
                ligne['statut'] = INVALIDE
            elif f.size > TAILLE_MAX:
                ligne['statut'] = TROP_LOURD
            elif e is None:
                ligne['statut'] = INCONNU
            elif not est_une_image(f):
                ligne['statut'] = INVALIDE
            elif e.photo and not remplacer:
                ligne['statut'] = GARDEE
            else:
                ligne['statut'] = REMPLACEE if e.photo else POSEE
            lignes.append((ligne, f, e))

        if not apercu:
            with transaction.atomic():
                for ligne, f, e in lignes:
                    if ligne['statut'] in STATUTS_ECRITS:
                        f.name = '%s%s' % (e.matricule, matricule_du_fichier(f.name)[1])
                        e.photo = f
                        e.save(update_fields=['photo'])     # compressée par Etudiant.save
            ecrites = [l for l, _, _ in lignes if l['statut'] in STATUTS_ECRITS]
            if ecrites:
                write_audit(
                    action=ACTION_BULK_UPDATE, model_name='Etudiant', object_id='0',
                    changes={'photos': [l['matricule'] for l in ecrites], 'remplacer': remplacer},
                    label='Photos des étudiants : %d déposée(s)' % len(ecrites),
                    user=request.user)

        bilan = {}
        for ligne, _, _ in lignes:
            bilan[ligne['statut']] = bilan.get(ligne['statut'], 0) + 1
        return Response({'apercu': apercu, 'remplacer': remplacer,
                         'lignes': [l for l, _, _ in lignes], 'bilan': bilan})
