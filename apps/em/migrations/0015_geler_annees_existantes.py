"""
Gèle la maquette des années qui ont DÉJÀ un suivi.

Le gel normal se fait à la génération du suivi (`apps/suivi/views.py`). Les
années antérieures à ce mécanisme ne sont jamais repassées par là : sans cette
migration, elles resteraient indéfiniment sur les volumes vivants, donc
exposées à la prochaine retouche de maquette — exactement le défaut qu'on
répare.

CE QU'ON GÈLE, ET CE QU'IL FAUT EN SAVOIR. On inscrit les volumes
d'AUJOURD'HUI. Ce qu'était la maquette en 2024-2025 n'est écrit nulle part —
ni dans le registre, ni dans un historique : il n'y a jamais eu qu'une seule
ligne par matière. Ce gel n'est donc pas une reconstitution du passé, c'est
l'arrêt de sa réécriture. Il est faux si la maquette a bougé depuis, et le
rester est encore préférable à changer de valeur à chaque correction.

Rien n'est gelé pour une année sans suivi : elle passera par le gel normal le
jour où elle en produira un, avec la maquette qui vaudra alors.
"""
from django.db import migrations


def geler(apps, schema_editor):
    EM     = apps.get_model('em', 'EM')
    Volume = apps.get_model('em', 'VolumeAnnuelEM')
    Suivie = apps.get_model('suivi', 'Suivie')

    annees = sorted({
        a for a in Suivie.objects.values_list('annee_universitaire', flat=True)
        if a
    })
    # `matiere_ipgei` est l'accesseur inverse de `ipgei.Matiere.em_miroir` : on
    # ne gèle que les EM miroirs d'une matière IPGEI. Le catalogue socle (CHIM,
    # EPS…) n'appartient à aucune maquette de prépa.
    ems = list(EM.objects.filter(matiere_ipgei__isnull=False))

    lignes = [
        Volume(annee_universitaire=annee, em=em,
               CM=em.CM, TD=em.TD, TP=em.TP, PR=em.PR)
        for annee in annees for em in ems
    ]
    if lignes:
        Volume.objects.bulk_create(lignes, ignore_conflicts=True)


def degeler(apps, schema_editor):
    """Le retour arrière vide la table : le repli sur l'EM reprend la main."""
    apps.get_model('em', 'VolumeAnnuelEM').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('em', '0014_volume_annuel_fige'),
        ('suivi', '0015_creer_pointage_departements'),
        ('ipgei', '0042_ponderation_par_classe'),
    ]

    operations = [migrations.RunPython(geler, degeler)]
