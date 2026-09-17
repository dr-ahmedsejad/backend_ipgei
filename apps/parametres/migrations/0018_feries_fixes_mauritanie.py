"""
Les jours fériés FIXES de Mauritanie — ceux qui reviennent à la même date.

Posés par migration et non à la main dans la base : un déploiement neuf doit
les avoir aussi.

LISTE À CONFIRMER. Elle vient d'un autre dépôt et n'a pas été vérifiée contre
le texte officiel qui fixe les jours fériés en Mauritanie.

Les fêtes RELIGIEUSES n'y sont pas — Aïd el-Fitr, Aïd el-Adha, Nouvel An de
l'Hégire, Mawlid : elles suivent le calendrier lunaire, leur date grégorienne
change chaque année. Elles se marquent jour par jour.

Cette migration NE TOUCHE PAS au calendrier des semaines. Appliquer un férié à
un jour existant annule ses séances ; c'est une décision qui se prend à l'écran,
avec le décompte sous les yeux, jamais en silence au passage d'une migration.
"""
from django.db import migrations

FERIES = [
    (1, 1,   "Jour de l'An"),
    (1, 5,   'Fête du Travail'),
    (25, 5,  "Journée de l'Afrique"),
    (28, 11, "Fête de l'Indépendance"),
]


def creer(apps, schema_editor):
    JourFerieFixe = apps.get_model('parametres', 'JourFerieFixe')
    for jour, mois, libelle in FERIES:
        # `get_or_create` sur (jour, mois) seulement : rejouer ne crée pas de
        # doublon, et un libellé corrigé depuis à l'écran n'est pas écrasé.
        JourFerieFixe.objects.get_or_create(
            jour=jour, mois=mois, defaults={'libelle': libelle, 'actif': True})


def retirer(apps, schema_editor):
    """Ne retire que CES quatre lignes, reconnues à leur date."""
    JourFerieFixe = apps.get_model('parametres', 'JourFerieFixe')
    for jour, mois, _ in FERIES:
        JourFerieFixe.objects.filter(jour=jour, mois=mois).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('parametres', '0017_jour_ferie_fixe'),
    ]

    operations = [migrations.RunPython(creer, retirer)]
