"""
Rejoue le miroir IPGEI → socle SIGA sur toutes les classes et matières.

    python manage.py sync_miroir_siga

Les signaux `post_save` maintiennent le miroir au fil de l'eau ; cette commande
sert au premier branchement, après un import en masse, ou pour rattraper une
synchronisation qui aurait échoué. Elle est idempotente.

Les classes n'y figurent plus : depuis qu'elles héritent de
`departement.Departement`, elles SONT des départements et n'ont donc aucune
contrepartie à tenir à jour.
"""
import unicodedata

from django.core.management.base import BaseCommand

from apps.departement.models import Departement
from apps.em.models import EM
from apps.ipgei.miroir import synchroniser_tout


class Command(BaseCommand):
    help = "Aligne les Departement / EM du socle sur les classes et matières IPGEI."

    def _dire(self, message, style=None):
        """Console Windows non-UTF8 : on translittère plutôt que d'échouer."""
        try:
            self.stdout.write(style(message) if style else message)
        except UnicodeEncodeError:
            ascii_ = unicodedata.normalize('NFKD', message).encode('ascii', 'ignore').decode()
            self.stdout.write(style(ascii_) if style else ascii_)

    def handle(self, *args, **options):
        bilan = synchroniser_tout()
        self._dire(
            f"{bilan['matieres']} matière(s), {bilan['semaines']} jour(s) de "
            f"calendrier et {bilan['seances']} séance(s) de grille type "
            f"synchronisés — {bilan['classes']} classe(s) sont déjà des "
            "départements, rien à y faire.",
        )
        from apps.emplois.models import Emplois
        self._dire(
            f'Socle SIGA : {Departement.objects.count()} département(s), '
            f'{EM.objects.count()} matière(s) et {Emplois.objects.count()} '
            "ligne(s) d'emploi du temps — le Suivi peut générer ses semaines.",
            self.style.SUCCESS,
        )
