"""
Data migration — MATH2 et PHYS2 de S3 rattachées à leur enseignement.

La scolarité l'a précisé : en S3, « Mathématiques » et « Mathématiques 2 » ne
sont pas deux matières. C'est un seul cours, un seul volume horaire, un seul
professeur ; la maquette le scinde en deux lignes parce que chacune porte SON
examen et SON coefficient. Les devoirs surveillés — il peut y en avoir
plusieurs — et le TP sont composés une seule fois et valent pour les deux.
Même chose pour « Physique & Chimie » / « Physique ».

La PORTEUSE est MATH et PHYS : ce sont elles qui déclarent le volume horaire
(95 h et 75 h), les rattachées en portent zéro — la règle était déjà là, en
convention de saisie, sans que le logiciel la connaisse.

**S3 uniquement** : S4 n'a ni MATH2 ni PHYS2.

Sans effet rétroactif : `calculer_note` refuse les notes verrouillées, et les
23 lignes de S3 2025-2026 le sont toutes par leur jury validé. Le rattachement
vaut pour les saisies à venir.
"""
from django.db import migrations

#: (code rattaché, code porteur, semestre)
RATTACHEMENTS = [
    ('MATH2', 'MATH', 'S3'),
    ('PHYS2', 'PHYS', 'S3'),
]


def rattacher(apps, schema_editor):
    Matiere = apps.get_model('ipgei', 'Matiere')
    faits = []
    for code_rattache, code_porteur, semestre in RATTACHEMENTS:
        rattachee = Matiere.objects.filter(code=code_rattache, code_semestre=semestre).first()
        porteuse  = Matiere.objects.filter(code=code_porteur,  code_semestre=semestre).first()
        if rattachee is None or porteuse is None:
            # Une base qui n'a pas ces matières — installation neuve, autre
            # établissement — ne doit pas voir la migration échouer.
            continue
        if rattachee.niveau_ref_id != porteuse.niveau_ref_id:
            continue
        rattachee.partage_avec = porteuse
        rattachee.save(update_fields=['partage_avec'])
        faits.append(f'{code_rattache} -> {code_porteur}')
    print(f"[migration 0036] enseignements partages : {', '.join(faits) or 'aucun'}")


def detacher(apps, schema_editor):
    Matiere = apps.get_model('ipgei', 'Matiere')
    for code_rattache, _, semestre in RATTACHEMENTS:
        Matiere.objects.filter(code=code_rattache, code_semestre=semestre).update(
            partage_avec=None)


class Migration(migrations.Migration):
    dependencies = [('ipgei', '0035_enseignement_partage')]
    operations = [migrations.RunPython(rattacher, detacher)]
