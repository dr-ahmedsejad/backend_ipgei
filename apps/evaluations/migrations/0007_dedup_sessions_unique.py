"""
Migration 0007 — Déduplique les SessionEvaluation et ajoute la contrainte
unique_together (annee_univ, type_session, type_semestre).

Stratégie de déduplication :
- Pour chaque groupe (annee_univ_id, type_session, type_semestre) on tente
  de supprimer les doublons (id inférieur en premier).
- Si la suppression est bloquée par une FK PROTECT, on fait pivoter
  le doublon vers 'Pairs' pour libérer la contrainte.
"""
from django.db import migrations


def dedup_sessions(apps, schema_editor):
    SessionEvaluation = apps.get_model('evaluations', 'SessionEvaluation')

    from collections import defaultdict
    groups: dict = defaultdict(list)
    for sess in SessionEvaluation.objects.order_by('id'):
        key = (sess.annee_univ_id, sess.type_session, sess.type_semestre)
        groups[key].append(sess)

    for key, sessions_list in groups.items():
        if len(sessions_list) <= 1:
            continue
        # Garder le dernier (id le plus grand), traiter les autres
        to_remove = sessions_list[:-1]
        for sess in to_remove:
            try:
                sess.delete()
            except Exception:
                # Impossible de supprimer (FK PROTECT) → faire pivoter vers l'autre parité
                alt = 'Pairs' if sess.type_semestre == 'Impairs' else 'Impairs'
                sess.type_semestre = alt
                sess.save(update_fields=['type_semestre'])


class Migration(migrations.Migration):

    dependencies = [
        ('evaluations', '0006_session_type_semestre'),
    ]

    operations = [
        migrations.RunPython(dedup_sessions, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name='sessionevaluation',
            unique_together={('annee_univ', 'type_session', 'type_semestre')},
        ),
    ]
