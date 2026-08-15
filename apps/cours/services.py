"""Contrôle d'accès (scoping) du module Cours.

- Publier sur un EM : enseignant de l'EM (SuiviePointage prof↔em↔année) OU admin.
- Voir / répondre : enseignant de l'EM OU étudiant inscrit à l'EM pour l'année OU admin.
Le rattachement est DÉRIVÉ à la volée (jamais stocké).
"""


def _is_admin(user):
    return bool(getattr(user, 'is_superuser', False) or getattr(user, 'role', None) == 'admin')


def _nom_user(u):
    """Nom affichable : priorité au profil métier (prof / étudiant), puis nom du
    compte, puis identifiant."""
    if not u:
        return ''
    for attr in ('prof_profile', 'etudiant_profile'):
        profil = getattr(u, attr, None)
        if not profil:
            continue
        nom = (getattr(profil, 'nom_complet', '') or '').strip()
        if not nom:
            prenom = (getattr(profil, 'prenom', '') or '').strip()
            base   = (getattr(profil, 'nom', '') or '').strip()
            nom = f'{prenom} {base}'.strip()
        if nom:
            return nom
    full = (getattr(u, 'get_full_name', lambda: '')() or '').strip()
    return full or u.get_username()


def snapshot_auteur(user):
    """(auteur_id, auteur_nom, auteur_role) figés à l'écriture (table inbox, pas de FK)."""
    if not user or not getattr(user, 'is_authenticated', False):
        return (None, '', '')
    return (user.pk, _nom_user(user), getattr(user, 'role', '') or '')


def snapshot_em(em_id):
    """(em_code, em_intitule) figés à l'écriture."""
    from apps.em.models import EM
    em = EM.objects.filter(pk=em_id).values('code_em', 'intitule').first()
    return (em['code_em'], em['intitule']) if em else ('', '')


def annee_courante(user):
    """Année de travail de l'utilisateur (contexte), repli sur l'année active."""
    try:
        a = user.contexte.annee_universitaire
        if a:
            return a
    except Exception:
        pass
    from apps.parametres.models import Year
    y = (Year.objects.filter(est_active=True).order_by('-annee').first()
         or Year.objects.order_by('-annee').first())
    return y.annee if y else ''


def prof_de_em(user, em_id, annee):
    """True si l'utilisateur enseigne cet EM (via ses pointages) pour l'année."""
    prof = getattr(user, 'prof_profile', None)
    if not prof or not em_id:
        return False
    from apps.suivi.models import SuiviePointage
    qs = SuiviePointage.objects.filter(prof_id=prof.pk, em_id=em_id)
    if annee:
        qs = qs.filter(annee_universitaire=annee)
    return qs.exists()


def etudiant_inscrit_em(user, em_id, annee):
    """True si l'utilisateur est un étudiant inscrit à cet EM pour l'année."""
    etu = getattr(user, 'etudiant_profile', None)
    if not etu or not em_id:
        return False
    from apps.inscriptions.models import InscriptionElement
    qs = InscriptionElement.objects.filter(
        em_id=em_id, inscription_ped__inscription_admin__etudiant=etu)
    if annee:
        qs = qs.filter(inscription_ped__inscription_admin__annee_univ__annee=annee)
    return qs.exists()


def peut_publier_em(user, em_id, annee):
    return _is_admin(user) or prof_de_em(user, em_id, annee)


def peut_voir_em(user, em_id, annee):
    return (_is_admin(user)
            or prof_de_em(user, em_id, annee)
            or etudiant_inscrit_em(user, em_id, annee))


def ems_utilisateur(user, annee):
    """EMs pertinents pour l'utilisateur : enseignés (prof) et/ou où il est inscrit
    (étudiant). Renvoie une liste de dicts {id, code_em, intitule, peut_publier, nb_cours}."""
    from django.db.models import Count
    from apps.em.models import EM
    from apps.suivi.models import SuiviePointage
    from apps.inscriptions.models import InscriptionElement
    from apps.cours.models import Cours

    prof = getattr(user, 'prof_profile', None)
    etu  = getattr(user, 'etudiant_profile', None)

    ids_prof, ids_etu = set(), set()
    if prof:
        q = SuiviePointage.objects.filter(prof_id=prof.pk)
        if annee:
            q = q.filter(annee_universitaire=annee)
        ids_prof = set(q.values_list('em_id', flat=True))
    if etu:
        q = InscriptionElement.objects.filter(inscription_ped__inscription_admin__etudiant=etu)
        if annee:
            q = q.filter(inscription_ped__inscription_admin__annee_univ__annee=annee)
        ids_etu = set(q.values_list('em_id', flat=True))

    all_ids = {i for i in (ids_prof | ids_etu) if i}
    admin = _is_admin(user)

    # Nombre de cours publiés par EM (pour l'année) — sert au tri/badge côté client.
    cq = Cours.objects.filter(em_id__in=all_ids)
    if annee:
        cq = cq.filter(annee_universitaire=annee)
    nb_par_em = dict(cq.values('em_id').annotate(n=Count('id')).values_list('em_id', 'n'))

    rows = []
    for em in EM.objects.filter(pk__in=all_ids).order_by('code_em').values('id', 'code_em', 'intitule'):
        rows.append({
            **em,
            'peut_publier': admin or em['id'] in ids_prof,
            'nb_cours': nb_par_em.get(em['id'], 0),
        })
    return rows


def ems_visibles_ids(user, annee):
    """IDs des EMs visibles par l'utilisateur (pour filtrer la liste des cours)."""
    return [r['id'] for r in ems_utilisateur(user, annee)]
