"""
Data migration — volumes horaires alignés sur « VH IPGEI.xlsx », et SI sans TP.

## Les volumes

Le fichier VH de la scolarité ne porte qu'UNE grille hebdomadaire, de 33 h
réparties en 22 séances de 1 h 30, valable pour les quatre semestres :

    Maths 10,5 CM + 1,5 TD      SI   3 CM
    PC     6   CM + 1,5 TD + 1,5 TP  Info 1,5 CM + 1,5 TP
    FR     1,5 CM + 1,5 TD      Ang  1,5 CM + 1,5 TD

**Le volume horaire s'entend sur 14 semaines** (décision de la scolarité), d'où
les valeurs posées ici = hebdomadaire × 14. Le calendrier, lui, garde ses
16 semaines (`SemestreIPGEI.nb_semaines`) : la durée du semestre et la charge
d'enseignement sont deux choses distinctes, et seule la seconde change ici.

Les volumes actuels ne venaient pas de ce fichier : c'étaient des valeurs de
seed retouchées à la main (aucun facteur de conversion ne les reliait à la
grille hebdomadaire — 90/10,5 = 8,57 ; 75/6 = 12,5 ; 30/1,5 = 20). Surtout,
`volume_td` et `volume_tp` valaient ZÉRO sur les 26 matières, alors que le
fichier attribue du TD à quatre matières et du TP à deux.

MATH2 et PHYS2 restent à 0 : elles partagent l'enseignement de MATH et de PHYS
(cf. `Matiere.partage_avec`), et compter leur volume une seconde fois
doublerait la charge du même cours.

## SI n'a pas de TP

SI est 100 % CM au fichier, et l'est déjà en S1, S2 et S4 (`has_tp=False`,
30/0/70). Seul S3 avait été passé à 20/10/70 lors du basculement en bloc de la
maquette de deuxième année. On le remet dans le rang.

Sans effet rétroactif sur les notes : la pondération est RECOPIÉE dans chaque
ligne au moment du calcul (`Note.pct_*`), et les 23 lignes de SI S3 sont de
surcroît verrouillées par leur jury validé.

## Le miroir EM

Une migration manipule des modèles HISTORIQUES : le signal `post_save` qui
projette la matière vers son EM du socle ne se déclenche pas. On reporte donc
CM/TD/TP à la main, faute de quoi l'écran IPGEI et le catalogue du socle
divergeraient en silence — exactement le défaut que `miroir.py` cherche à éviter.
"""
from django.db import migrations

SEMESTRES = ['S1', 'S2', 'S3', 'S4']

#: code → (CM, TD, TP) pour 14 semaines. MATH2 et PHYS2 en sont absentes : leur
#: enseignement est celui de MATH et de PHYS.
VOLUMES = {
    'MATH': (147, 21, 0),
    'PHYS': (84, 21, 21),
    'SI':   (42, 0, 0),
    'INFO': (21, 0, 21),
    'FRAN': (21, 21, 0),
    'ANGL': (21, 21, 0),
}

#: L'état d'avant, relevé en base le 05/09/2026 — pour le retour arrière.
#: (code, semestre) → (CM, TD, TP)
ANCIENS = {
    ('ANGL', 'S1'): (22, 0, 0), ('FRAN', 'S1'): (22, 0, 0), ('INFO', 'S1'): (30, 0, 0),
    ('MATH', 'S1'): (90, 0, 0), ('PHYS', 'S1'): (75, 0, 0), ('SI',   'S1'): (37, 0, 0),
    ('ANGL', 'S2'): (22, 0, 0), ('FRAN', 'S2'): (22, 0, 0), ('INFO', 'S2'): (30, 0, 0),
    ('MATH', 'S2'): (90, 0, 0), ('PHYS', 'S2'): (75, 0, 0), ('SI',   'S2'): (37, 0, 0),
    ('ANGL', 'S3'): (22, 0, 0), ('FRAN', 'S3'): (22, 0, 0), ('INFO', 'S3'): (30, 0, 0),
    ('MATH', 'S3'): (95, 0, 0), ('PHYS', 'S3'): (75, 0, 0), ('SI',   'S3'): (37, 0, 0),
    ('ANGL', 'S4'): (22, 0, 0), ('FRAN', 'S4'): (22, 0, 0), ('INFO', 'S4'): (30, 0, 0),
    ('MATH', 'S4'): (95, 0, 0), ('PHYS', 'S4'): (75, 0, 0), ('SI',   'S4'): (37, 0, 0),
}

#: SI en S3, tel qu'il était : la seule des quatre lignes SI à porter du TP.
SI_S3_AVANT = {'has_tp': True,  'pct_ds': 20, 'pct_tp': 10, 'pct_exam': 70}
SI_S3_APRES = {'has_tp': False, 'pct_ds': 30, 'pct_tp': 0,  'pct_exam': 70}


def _reporter_au_miroir(apps, matiere):
    """CM/TD/TP vers l'EM du socle — le signal ne joue pas en migration."""
    if not matiere.em_miroir_id:
        return
    EM = apps.get_model('em', 'EM')
    EM.objects.filter(pk=matiere.em_miroir_id).update(
        CM=matiere.volume_cm, TD=matiere.volume_td, TP=matiere.volume_tp)


def appliquer(apps, schema_editor):
    Matiere = apps.get_model('ipgei', 'Matiere')
    touchees = 0
    for code, (cm, td, tp) in VOLUMES.items():
        for semestre in SEMESTRES:
            matiere = Matiere.objects.filter(code=code, code_semestre=semestre).first()
            if matiere is None:
                continue
            matiere.volume_cm, matiere.volume_td, matiere.volume_tp = cm, td, tp
            matiere.save(update_fields=['volume_cm', 'volume_td', 'volume_tp'])
            _reporter_au_miroir(apps, matiere)
            touchees += 1

    si = Matiere.objects.filter(code='SI', code_semestre='S3').first()
    if si is not None:
        for champ, valeur in SI_S3_APRES.items():
            setattr(si, champ, valeur)
        si.save(update_fields=list(SI_S3_APRES))
    print(f'[migration 0037] {touchees} matieres alignees sur le VH (14 semaines) ; '
          f'SI S3 repasse sans TP.')


def revenir(apps, schema_editor):
    Matiere = apps.get_model('ipgei', 'Matiere')
    for (code, semestre), (cm, td, tp) in ANCIENS.items():
        matiere = Matiere.objects.filter(code=code, code_semestre=semestre).first()
        if matiere is None:
            continue
        matiere.volume_cm, matiere.volume_td, matiere.volume_tp = cm, td, tp
        matiere.save(update_fields=['volume_cm', 'volume_td', 'volume_tp'])
        _reporter_au_miroir(apps, matiere)

    si = Matiere.objects.filter(code='SI', code_semestre='S3').first()
    if si is not None:
        for champ, valeur in SI_S3_AVANT.items():
            setattr(si, champ, valeur)
        si.save(update_fields=list(SI_S3_AVANT))


class Migration(migrations.Migration):
    dependencies = [('ipgei', '0036_rattachement_math2_phys2')]
    operations = [migrations.RunPython(appliquer, revenir)]
