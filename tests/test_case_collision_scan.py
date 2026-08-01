"""
Tests de la commande READ-ONLY `case_collision_scan` (Phase 1.3 migration PG).

Vérifie la détection des collisions casse/accents qui passent inaperçues sous
MySQL (collations *_ci / *_ai_ci) mais violeraient un index unique fonctionnel
sous PostgreSQL. Les collisions sont synthétiques : sqlite est sensible à la
casse ET aux accents, elles peuvent donc coexister en base de test.
"""
import json
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

pytestmark = pytest.mark.django_db


def _lancer(**kwargs):
    """Lance la commande et retourne (sortie, erreur_ou_None)."""
    out = StringIO()
    try:
        call_command('case_collision_scan', stdout=out, **kwargs)
        return out.getvalue(), None
    except CommandError as exc:
        return out.getvalue(), exc


# ── Collision de casse sur un champ unique simple (Salle.nom) ───────────────────
def test_collision_casse_salle():
    from apps.salle.models import Salle
    s1 = Salle.objects.create(nom='AMPHI a')
    s2 = Salle.objects.create(nom='Amphi A')

    sortie, erreur = _lancer()

    assert erreur is not None, "exit non-zéro attendu (CommandError)"
    assert 'salle.Salle' in sortie
    assert 'AMPHI a' in sortie and 'Amphi A' in sortie
    assert 'pk=%s' % s1.pk in sortie and 'pk=%s' % s2.pk in sortie
    # la clé normalisée casefold + sans accents
    assert "'amphi a'" in sortie


# ── Collision de casse sur Etudiant.matricule ───────────────────────────────────
def test_collision_casse_matricule_etudiant():
    from tests.factories.scolarite import EtudiantFactory
    EtudiantFactory(matricule='ab12')
    EtudiantFactory(matricule='AB12')

    sortie, erreur = _lancer()

    assert erreur is not None
    assert 'absence.Etudiant' in sortie
    assert 'matricule' in sortie
    assert 'ab12' in sortie and 'AB12' in sortie


# ── Collision d'accents (MySQL *_ai_ci est aussi insensible aux accents) ───────
def test_collision_accents_banque():
    from apps.banque.models import Banque
    Banque.objects.create(nom='Département')
    Banque.objects.create(nom='Departement')

    sortie, erreur = _lancer()

    assert erreur is not None
    assert 'banque.Banque' in sortie
    assert 'Département' in sortie and 'Departement' in sortie
    assert "'departement'" in sortie  # clé normalisée sans accent, en minuscules


# ── Contrainte composite : em.EM unique_together ('code_em', 'departement') ────
def test_collision_composite_em():
    from tests.factories.em import DepartementAnnuelFactory, EMLegacyFactory
    dep = DepartementAnnuelFactory()
    EMLegacyFactory(code_em='INFO101', departement=dep)
    EMLegacyFactory(code_em='info101', departement=dep)

    sortie, erreur = _lancer()

    assert erreur is not None
    assert 'em.EM' in sortie
    assert 'code_em' in sortie
    assert 'INFO101' in sortie and 'info101' in sortie


# ── Pas de collision : le même code_em dans DEUX départements différents ───────
def test_composite_pas_de_faux_positif_departements_differents():
    from tests.factories.em import DepartementAnnuelFactory, EMLegacyFactory
    EMLegacyFactory(code_em='INFO101', departement=DepartementAnnuelFactory(nom='DEP_A'))
    EMLegacyFactory(code_em='info101', departement=DepartementAnnuelFactory(nom='DEP_B'))

    sortie, erreur = _lancer()

    assert erreur is None, "aucune collision attendue : départements distincts\n%s" % sortie
    assert 'Aucune collision détectée' in sortie


# ── Cas sans collision : exit 0 ─────────────────────────────────────────────────
def test_sans_collision_exit_zero():
    from apps.salle.models import Salle
    from apps.banque.models import Banque
    Salle.objects.create(nom='Amphi A')
    Salle.objects.create(nom='Salle B12')
    Banque.objects.create(nom='BNM')

    sortie, erreur = _lancer()

    assert erreur is None, "exit 0 attendu quand il n'y a aucune collision"
    assert 'Aucune collision détectée' in sortie
    assert 'COLLISION' not in sortie


# ── Option --json : rapport machine-readable ────────────────────────────────────
def test_option_json(tmp_path):
    from apps.salle.models import Salle
    s1 = Salle.objects.create(nom='AMPHI a')
    s2 = Salle.objects.create(nom='Amphi A')

    fichier = tmp_path / 'rapport_collisions.json'
    sortie, erreur = _lancer(json=str(fichier))

    assert erreur is not None
    assert fichier.exists(), "le rapport JSON doit être écrit même en cas d'exit non-zéro"

    data = json.loads(fichier.read_text(encoding='utf-8'))
    assert data['total_collisions'] >= 1
    assert data['targets_scanned'] > 0

    coll_salle = [c for c in data['collisions'] if c['model'] == 'salle.Salle']
    assert len(coll_salle) == 1
    assert coll_salle[0]['normalized'] == 'amphi a'
    pks = {ligne[0] for ligne in coll_salle[0]['rows']}
    assert pks == {s1.pk, s2.pk}
    bruts = {ligne[1] for ligne in coll_salle[0]['rows']}
    assert bruts == {'AMPHI a', 'Amphi A'}


# ── Garde-fou : la commande n'écrit RIEN en base (read-only) ────────────────────
def test_read_only_aucune_ecriture():
    from apps.salle.models import Salle
    Salle.objects.create(nom='AMPHI a')
    Salle.objects.create(nom='Amphi A')
    avant = list(Salle.objects.order_by('pk').values_list('pk', 'nom'))

    _lancer()

    apres = list(Salle.objects.order_by('pk').values_list('pk', 'nom'))
    assert avant == apres, "case_collision_scan ne doit jamais modifier les données"
