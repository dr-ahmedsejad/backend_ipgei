"""
Data migration — Marque comme `is_special=True` les types de seance qui occupent
un creneau d'emploi du temps SANS necessiter prof/EM/salle (cellule affichee
centree avec uniquement le libelle du type).

Idempotent : update sur les types deja existants en BD. Si un type n'existe pas,
il n'est pas cree (a charge de l'admin de l'ajouter via l'UI).

Pour ajouter un nouveau type special ulterieurement, l'admin coche simplement
`is_special` dans l'UI Parametres -> Seances. Aucune modification de code requise.
"""
from django.db import migrations


SPECIAL_TYPES = [
    'Sport',
    'Instruction militaire',
]


def mark_special(apps, schema_editor):
    Seance = apps.get_model('parametres', 'Seance')
    updated = 0
    not_found = []
    for label in SPECIAL_TYPES:
        n = Seance.objects.filter(type_seance__iexact=label).update(is_special=True)
        if n:
            updated += n
        else:
            not_found.append(label)
    print(f'[migration 0010] {updated} type(s) marque(s) is_special=True')
    if not_found:
        print(f'[migration 0010] Non trouves en BD (a creer manuellement) : {not_found}')


def reverse_mark(apps, schema_editor):
    """No-op : on ne demarque pas automatiquement (l'admin peut avoir
    modifie d'autres types via l'UI entre-temps)."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('parametres', '0009_add_seance_is_special'),
    ]

    operations = [
        migrations.RunPython(mark_special, reverse_mark),
    ]
