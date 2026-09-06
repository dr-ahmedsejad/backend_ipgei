"""
Data migration — range chaque matiere existante dans son departement.

L'IPGEI a deux departements, et la partition suit le code de la matiere :
  - ST      : mathematiques, physique, chimie, informatique, SI — et l'EPS,
              rattachee a ST par decision de la direction ;
  - LANGUES : anglais, francais.

Le rangement se fait par PREFIXE de code (MATH2 et PHYS2 sont des maths et de la
physique), et ne touche pas une matiere deja rangee : l'ecran des matieres
permet de corriger a la main, une migration ne doit pas defaire ce choix.

Idempotente ; le retour vide le champ.
"""
from django.db import migrations

PAR_PREFIXE = {
    'MATH': 'ST', 'PHYS': 'ST', 'CHIM': 'ST', 'INFO': 'ST', 'SI': 'ST', 'EPS': 'ST',
    'ANGL': 'LANGUES', 'FRAN': 'LANGUES',
}


def ranger(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return

    Matiere = apps.get_model('ipgei', 'Matiere')
    compte = {}
    for matiere in Matiere.objects.filter(departement=''):
        code = (matiere.code or '').upper()
        # Le prefixe le plus long d'abord : « SI » ne doit pas attraper « SIxx »
        # d'une autre discipline si un tel code apparait un jour.
        for prefixe in sorted(PAR_PREFIXE, key=len, reverse=True):
            if code.startswith(prefixe):
                matiere.departement = PAR_PREFIXE[prefixe]
                matiere.save(update_fields=['departement'])
                compte[PAR_PREFIXE[prefixe]] = compte.get(PAR_PREFIXE[prefixe], 0) + 1
                break
    print(f"[migration 0034] matieres rangees : {compte or 'aucune'}")


def defaire(apps, schema_editor):
    apps.get_model('ipgei', 'Matiere').objects.update(departement='')


class Migration(migrations.Migration):
    dependencies = [('ipgei', '0033_departement_matiere_et_perimetre')]
    operations = [migrations.RunPython(ranger, defaire)]
