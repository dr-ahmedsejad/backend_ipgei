"""
Photos des étudiants déposées en une fois — chaque fichier porte le matricule.
Demande du 07/10/2026. Voir apps/absence/photos.py.
"""
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

URL = '/api/v1/absences/photos-etudiants/'


def image(nom, couleur='red', fmt='JPEG'):
    from PIL import Image
    tampon = io.BytesIO()
    Image.new('RGB', (60, 80), couleur).save(tampon, fmt)
    return SimpleUploadedFile(nom, tampon.getvalue(), content_type='image/jpeg')


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def monde(db):
    from apps.absence.models import Etudiant
    from apps.authentication.models import CustomUser
    from apps.departement.models import Departement
    from tests.factories.parametres import InstitutionFactory
    dep = Departement.objects.create(nom='G1', annee_universitaire='2026-2027',
                                     institution=InstitutionFactory())
    etus = {m: Etudiant.objects.create(matricule=m, nom='Nom ' + m, departement=dep)
            for m in ('24607', '24608', 'AB12')}
    admin = CustomUser.objects.create_user(username='adm', email='adm@iss.mr', password='x',
                                           role='admin', is_superuser=True)
    prof = CustomUser.objects.create_user(username='ens', email='ens@iss.mr', password='x',
                                          role='enseignant')
    return {'etus': etus, 'admin': admin, 'prof': prof}


def deposer(user, fichiers, **params):
    c = APIClient()
    c.force_authenticate(user)
    return c.post(URL, {'photos': fichiers, **params}, format='multipart')


def statuts(r):
    return {l['fichier']: l['statut'] for l in r.data['lignes']}


def photo_de(monde, matricule):
    e = monde['etus'][matricule]
    e.refresh_from_db()
    return e.photo


class TestDepot:

    def test_chaque_photo_va_a_son_etudiant(self, monde):
        r = deposer(monde['admin'], [image('24607.jpg'), image('24608.png', fmt='PNG')])
        assert r.status_code == 200, r.data
        assert statuts(r) == {'24607.jpg': 'posee', '24608.png': 'posee'}
        assert photo_de(monde, '24607').name.startswith('etudiants/photos/24607')
        assert photo_de(monde, '24608')
        assert r.data['bilan'] == {'posee': 2}
        assert r.data['lignes'][0]['etudiant']['nom'] == 'Nom 24607'

    def test_le_matricule_sans_tenir_compte_des_majuscules(self, monde):
        r = deposer(monde['admin'], [image('ab12.JPG')])
        assert statuts(r) == {'ab12.JPG': 'posee'}
        assert photo_de(monde, 'AB12')

    def test_un_matricule_inconnu_est_signale(self, monde):
        assert statuts(deposer(monde['admin'], [image('99999.jpg')])) == {'99999.jpg': 'inconnu'}


class TestPhotoDejaPresente:

    def test_par_defaut_on_complete_seulement(self, monde):
        deposer(monde['admin'], [image('24607.jpg', 'red')])
        avant = photo_de(monde, '24607').name
        r = deposer(monde['admin'], [image('24607.jpg', 'blue')])
        assert statuts(r) == {'24607.jpg': 'deja_photo'}
        assert photo_de(monde, '24607').name == avant

    def test_remplacer_sur_demande(self, monde):
        deposer(monde['admin'], [image('24607.jpg', 'red')])
        avant = photo_de(monde, '24607').name
        r = deposer(monde['admin'], [image('24607.jpg', 'blue')], remplacer='1')
        assert statuts(r) == {'24607.jpg': 'remplacee'}
        assert photo_de(monde, '24607').name != avant


class TestRefus:

    def test_ce_qui_n_est_pas_une_image(self, monde):
        faux = SimpleUploadedFile('24607.jpg', b'pas une image', content_type='image/jpeg')
        r = deposer(monde['admin'], [faux, image('24608.gif', fmt='GIF')])
        assert statuts(r) == {'24607.jpg': 'invalide', '24608.gif': 'invalide'}
        assert not photo_de(monde, '24607') and not photo_de(monde, '24608')

    def test_deux_fichiers_pour_un_meme_matricule(self, monde):
        """Lequel serait le bon ? Aucun n'est posé ; l'écran le dit."""
        r = deposer(monde['admin'], [image('24607.jpg'), image('24607.png', fmt='PNG')])
        assert set(statuts(r).values()) == {'doublon'}
        assert not photo_de(monde, '24607')

    def test_trop_lourd(self, monde, monkeypatch):
        from apps.absence import photos
        monkeypatch.setattr(photos, 'TAILLE_MAX', 10)
        assert statuts(deposer(monde['admin'], [image('24607.jpg')])) == {'24607.jpg': 'trop_lourd'}


class TestApercu:

    def test_l_apercu_n_ecrit_rien(self, monde):
        r = deposer(monde['admin'], [image('24607.jpg'), image('99999.jpg')], apercu='1')
        assert r.data['apercu'] is True
        assert statuts(r) == {'24607.jpg': 'posee', '99999.jpg': 'inconnu'}
        assert not photo_de(monde, '24607')


class TestDroits:

    def test_sans_le_droit_de_modifier_les_etudiants(self, monde):
        assert deposer(monde['prof'], [image('24607.jpg')]).status_code == 403
        assert not photo_de(monde, '24607')

    def test_sans_fichier(self, monde):
        assert deposer(monde['admin'], []).status_code == 400

    def test_refuse_sur_le_miroir(self, monde, settings):
        """Le miroir est en lecture seule : les photos se déposent au travail."""
        settings.MIRROR_MODE = True
        assert deposer(monde['admin'], [image('24607.jpg')]).status_code == 403
        assert not photo_de(monde, '24607')
