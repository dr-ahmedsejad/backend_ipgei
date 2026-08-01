"""Backfill du taux_paiement stocké (alignement nouveau modèle stat vacations).

Contexte : depuis le commit 8988257 (15/05/2026), la stat `/statistiques/vacations`
et les fiches de paie calculent `montant = duree × taux_paiement STOCKÉ` par ligne
(plus de recalcul eq_CM via barème). Pour que ce modèle ne sous-compte pas, chaque
ligne payable DOIT avoir son taux_paiement renseigné depuis le barème `paiement`.

Cette migration :
  1. Remplit taux_paiement (là où il vaut 0 ou NULL) sur Vacation et SuiviePointage,
     pour les séances ayant un barème — donc TOUT SAUF DS / EF / ER (séances d'examen
     sans tarif) et les types absents du barème (Sport, Instruction militaire…).
     Le taux est résolu par DATE (gère Surveillance 300 → 200 au 01/02/2026).
  2. Enforce la règle Surveillance par date (300 avant le 01/02/2026, 200 ensuite)
     même sur les lignes déjà non nulles, pour garantir la cohérence.

Idempotente : la passe 1 ne touche que les 0/NULL ; la passe 2 réécrit la même valeur.
Non réversible proprement (on ne sait pas restaurer quelles lignes étaient à 0) : reverse = noop.
"""
from django.db import migrations

EXCLU = {'DS', 'EF', 'ER'}          # séances sans barème — laissées à 0 volontairement
SURVEILLANCE = 'Surveillance'
BATCH = 500


def _bareme(Paiement):
    """type -> liste triée de (date_debut, taux)."""
    table = {}
    for p in Paiement.objects.all().order_by('type', 'date_debut'):
        table.setdefault(p.type, []).append((p.date_debut, float(p.taux)))
    return table


def _taux_at(table, type_name, d):
    """Taux applicable au type `type_name` à la date `d` (None si pas de barème)."""
    entries = table.get(type_name)
    if not entries or d is None:
        return None
    applicable = None
    for date_debut, taux in entries:           # déjà trié par date_debut croissant
        if date_debut is None or date_debut <= d:
            applicable = taux
    return applicable


def forwards(apps, schema_editor):
    Vacation = apps.get_model('vacation', 'Vacation')
    SuiviePointage = apps.get_model('suivi', 'SuiviePointage')
    Seance = apps.get_model('parametres', 'Seance')
    Paiement = apps.get_model('parametres', 'Paiement')

    table = _bareme(Paiement)
    seance_name = {s.id: s.type_seance for s in Seance.objects.all()}

    def run(model, type_attr, date_attr):
        filled = 0
        surveillance_set = 0
        buf = []

        def flush():
            if buf:
                model.objects.bulk_update(buf, ['taux_paiement'])
                buf.clear()

        for obj in model.objects.all().iterator():
            tname = seance_name.get(getattr(obj, type_attr))
            if tname is None:
                continue
            d = getattr(obj, date_attr)
            current = obj.taux_paiement
            new_val = None

            if tname == SURVEILLANCE:
                # Passe 2 : enforce la règle date, même si non nul
                tx = _taux_at(table, SURVEILLANCE, d)
                if tx is not None and float(current or 0) != tx:
                    new_val = tx
                    surveillance_set += 1
            elif tname not in EXCLU and (current is None or current == 0):
                # Passe 1 : remplir les 0/NULL des types à barème
                tx = _taux_at(table, tname, d)
                if tx:
                    new_val = tx
                    filled += 1

            if new_val is not None:
                obj.taux_paiement = new_val
                buf.append(obj)
                if len(buf) >= BATCH:
                    flush()
        flush()
        print("  [%s] taux remplis=%s | Surveillance corrigés=%s"
              % (model.__name__, filled, surveillance_set))

    print("Backfill taux_paiement (nouveau modèle stat vacations) :")
    run(Vacation, 'type_id', 'date')
    run(SuiviePointage, 'type_seance_fk_id', 'date_suivie')


def backwards(apps, schema_editor):
    # Non réversible : on ne ré-impose pas 0 sur des taux légitimes.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('vacation', '0004_alter_surveillance_departement'),
        ('suivi', '0013_alter_suivie_prof_alter_suiviepointage_prof'),
        ('parametres', '0012_alter_semestre_niveau_semestre'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
