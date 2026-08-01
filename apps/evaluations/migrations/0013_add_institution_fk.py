"""
Section 1bis institution_V1 — FK institution sur SessionEvaluation et PVDeliberation.

Cas particulier SessionEvaluation : doit avoir un unique_together élargi
(institution, annee_univ, type_session, type_semestre) — fait après backfill.
"""
from django.db import migrations, models

from core.migration_ops import DejaAppliqueeSurMySQL


def backfill_evaluations_institution(apps, schema_editor):
    Institution = apps.get_model('parametres', 'Institution')
    SessionEvaluation = apps.get_model('evaluations', 'SessionEvaluation')
    PVDeliberation = apps.get_model('evaluations', 'PVDeliberation')

    # Base neuve (rien à backfiller) : ne pas exiger d'institution principale.
    if not SessionEvaluation.objects.exists() and not PVDeliberation.objects.exists():
        return

    principales = list(Institution.objects.filter(est_principale=True))
    if len(principales) != 1:
        raise RuntimeError(f"{len(principales)} institutions principales — corriger.")
    principale = principales[0]

    # SessionEvaluation : pas de FK filière → fallback direct sur principale
    nb_s = SessionEvaluation.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> SessionEvaluation: {nb_s} via principale")

    # PVDeliberation : via filiere.institution (FK PROTECT toujours rempli)
    nb_via_filiere = 0
    for obj in PVDeliberation.objects.filter(
        institution__isnull=True,
        filiere__institution__isnull=False,
    ).select_related('filiere').iterator(chunk_size=200):
        obj.institution_id = obj.filiere.institution_id
        obj.save(update_fields=['institution'])
        nb_via_filiere += 1
    nb_pv_fallback = PVDeliberation.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> PVDeliberation: {nb_via_filiere} via filiere.institution, {nb_pv_fallback} via principale")


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('evaluations', '0012_remove_lignedeliberation_verrou_l3_and_more'),
        ('parametres', '0006_semestre_simplification'),
    ]

    operations = [
        # 1. AddField nullable (institution n'est pas dans l'ancien unique_together)
        migrations.AddField(
            model_name='sessionevaluation',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='sessions_evaluation', null=True, blank=True,
            ),
        ),
        migrations.AddField(
            model_name='pvdeliberation',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='pvs', null=True, blank=True,
            ),
        ),
        # 3. Backfill
        migrations.RunPython(backfill_evaluations_institution, reverse_backfill),
        # 4. Passer en NOT NULL
        migrations.AlterField(
            model_name='sessionevaluation',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='sessions_evaluation',
            ),
        ),
        migrations.AlterField(
            model_name='pvdeliberation',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='pvs',
            ),
        ),
        # 5. Remplacement direct du unique_together (Django gère drop+create en une passe).
        # Sur MySQL : SQL brut historique (inchangé) pour éviter le bug
        # "index name conflict" où AlterUniqueTogether laisse traîner l'index
        # FK auto-créé. Sur base neuve non-MySQL : l'AlterUniqueTogether
        # standard est rejoué physiquement (drop de l'ancienne contrainte +
        # création de la nouvelle, nom généré par Django — le nom
        # uniq_sess_inst_annee_session_sem n'existe que sur MySQL).
        DejaAppliqueeSurMySQL(
            state_operations=[
                migrations.AlterUniqueTogether(
                    name='sessionevaluation',
                    unique_together={('institution', 'annee_univ', 'type_session', 'type_semestre')},
                ),
            ],
            database_operations=[],
            sql_mysql="""
                ALTER TABLE evaluations_session
                ADD CONSTRAINT uniq_sess_inst_annee_session_sem
                UNIQUE (institution_id, annee_univ_id, type_session, type_semestre);
            """,
            reverse_sql_mysql="""
                ALTER TABLE evaluations_session
                DROP INDEX uniq_sess_inst_annee_session_sem;
            """,
        ),
    ]
