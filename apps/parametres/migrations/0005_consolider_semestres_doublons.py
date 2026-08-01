"""
Section 1 — Migration A : consolider les Semestres doublonnés.

Avant de retirer les FK `filiere` et `annee_univ` (Migration B), on s'assure
qu'un seul Semestre existe par tuple (code_semestre, niveau_semestre, type_semestre).

Pour chaque doublon : choisir un canonique (priorité aux templates filiere=NULL/annee_univ=NULL,
sinon le plus ancien par id), puis rediriger toutes les InscriptionPedagogique vers le canonique.

Si aucun doublon n'existe (cas observé en audit), la migration est un no-op.
"""
from django.db import migrations
from django.db.models import Count


def consolider_semestres(apps, schema_editor):
    Semestre = apps.get_model('parametres', 'Semestre')
    InscriptionPedagogique = apps.get_model('inscriptions', 'InscriptionPedagogique')

    groupes = (
        Semestre.objects
        .values('code_semestre', 'niveau_semestre_id', 'type_semestre')
        .annotate(c=Count('id'))
        .filter(c__gt=1)
    )

    consolides = 0
    redirections = 0

    for g in groupes:
        qs = (
            Semestre.objects
            .filter(
                code_semestre=g['code_semestre'],
                niveau_semestre_id=g['niveau_semestre_id'],
                type_semestre=g['type_semestre'],
            )
            .order_by('filiere_id', 'annee_univ_id', 'id')
        )

        # Priorite : template pur (filiere=NULL, annee_univ=NULL), sinon le plus ancien.
        canonique = qs.filter(filiere__isnull=True, annee_univ__isnull=True).first() or qs.first()
        doublons_ids = list(qs.exclude(id=canonique.id).values_list('id', flat=True))

        # Rediriger les InscriptionPedagogique pointant vers les doublons.
        nb_redir = InscriptionPedagogique.objects.filter(
            semestre_id__in=doublons_ids
        ).update(semestre_id=canonique.id)
        redirections += nb_redir

        # Supprimer les doublons.
        Semestre.objects.filter(id__in=doublons_ids).delete()
        consolides += len(doublons_ids)

    if consolides:
        print(f"  -> {consolides} Semestres doublons supprimes, {redirections} InscriptionPedagogique redirigees")


def reverse_consolidation(apps, schema_editor):
    """Aucun rollback possible — restaurer le dump mysqldump."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('parametres', '0004_institution_groupe_tutelle'),
        ('inscriptions', '0010_short_derogation_labels'),
    ]

    operations = [
        migrations.RunPython(consolider_semestres, reverse_consolidation),
    ]
