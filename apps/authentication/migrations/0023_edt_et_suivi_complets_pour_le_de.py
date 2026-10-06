"""
Data migration — le Directeur des etudes tient l'emploi du temps et le suivi.

Il avait « voir » et « modifier » sur `ipgei_edt`, jamais « supprimer » : or
VIDER UNE CASE de la grille, c'est supprimer la seance. Son enregistrement
echouait donc des qu'il retirait un cours, alors que l'ecran lui annonce
« la vider la retire ». Demi-capacite, et piegeuse.

Meme logique pour le SUIVI : il saisissait (`suivi_saisie`) mais ne pouvait ni
corriger une fiche (`suivi_fiches` en lecture seule) ni reprendre une charge
(`suivi_charges` en lecture seule). Planifier et suivre l'execution sont son
metier ; il en a desormais les quatre actions.

Ce qui n'est PAS accorde : `avancement` reste en lecture et export — c'est un
etat calcule depuis le suivi, il ne se modifie pas a la main.

`allowed=True` est pose EXPLICITEMENT : le defaut du modele est False, et
l'oublier cree des droits qui refusent (c'etait le bug de la migration 0014).
"""
from django.db import migrations

ROLE = 'DE'
MODULES = ['ipgei_edt', 'suivi_saisie', 'suivi_fiches', 'suivi_charges']
ACTIONS = ['voir', 'modifier', 'supprimer', 'exporter']

#: Ce qu'il avait AVANT, et qu'une marche arriere doit rendre — retirer tout
#: le lot le laisserait sans rien, pire que l'etat d'origine.
AVANT = {
    'ipgei_edt':     ['voir', 'modifier'],
    'suivi_saisie':  ['voir', 'modifier'],
    'suivi_fiches':  ['voir'],
    'suivi_charges': ['voir', 'exporter'],
}


def poser(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return

    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault  = apps.get_model('authentication', 'RoleDefault')

    poses = 0
    for code_module in MODULES:
        for code_action in ACTIONS:
            ma = (ModuleAction.objects
                  .filter(module__code=code_module, action__code=code_action).first())
            if ma is None:
                continue
            _, cree = RoleDefault.objects.get_or_create(
                role=ROLE, module_action=ma, defaults={'allowed': True})
            if cree:
                poses += 1
            else:
                RoleDefault.objects.filter(role=ROLE, module_action=ma,
                                           allowed=False).update(allowed=True)
    print(f'[migration 0023] {poses} droit(s) accorde(s) au {ROLE} '
          f'sur {", ".join(MODULES)}.')


def retirer(apps, schema_editor):
    RoleDefault = apps.get_model('authentication', 'RoleDefault')
    for code_module, gardees in AVANT.items():
        (RoleDefault.objects
         .filter(role=ROLE, module_action__module__code=code_module)
         .exclude(module_action__action__code__in=gardees)
         .delete())


class Migration(migrations.Migration):

    dependencies = [('authentication', '0022_calendrier_en_lecture_pour_le_de')]

    operations = [migrations.RunPython(poser, retirer)]
