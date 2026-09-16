"""
La saisie de notes en ligne vise une note IPGEI, non une inscription LMD.

`SaisieNoteEnLigne` s'indexait sur `InscriptionElement` et `SessionEvaluation`,
de la couche que le fork a remplacée et dont les tables portent **zéro ligne**.
L'écran « Saisie de notes en ligne » rendait donc une grille vide à tout
enseignant, sans erreur — le même défaut que le portail étudiant, corrigé la
veille.

ÉCRIT À LA MAIN. `makemigrations` bloque sur l'ajout d'un champ non nul et
attend une valeur de reprise. Il n'y en a pas à donner : la table est **vide**
(0 ligne au 16/09/2026) et les colonnes LMD n'ont jamais rien porté. On les
retire plutôt que de les laisser — c'est précisément ce genre de colonne morte
qui rend un défaut invisible.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reclamations', '0004_saisienoteenligne_reclamationseance'),
    ]

    operations = [
        # L'unicité et les index d'abord : ils portent sur des colonnes qui
        # disparaissent, PostgreSQL refuserait de les retirer après coup.
        migrations.AlterUniqueTogether(name='saisienoteenligne', unique_together=set()),
        migrations.RemoveIndex(model_name='saisienoteenligne',
                               name='saisie_note_prof_id_9888f2_idx'),

        migrations.RemoveField(model_name='saisienoteenligne', name='inscription_element_id'),
        migrations.RemoveField(model_name='saisienoteenligne', name='session_id'),
        migrations.RemoveField(model_name='saisienoteenligne', name='session_code'),
        migrations.RemoveField(model_name='saisienoteenligne', name='em_id'),
        migrations.RemoveField(model_name='saisienoteenligne', name='em_code'),
        migrations.RemoveField(model_name='saisienoteenligne', name='em_intitule'),

        migrations.AddField(
            model_name='saisienoteenligne', name='note_ipgei_id',
            field=models.IntegerField(db_index=True, default=0),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='saisienoteenligne', name='numero',
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='saisienoteenligne', name='matiere_id',
            field=models.IntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='saisienoteenligne', name='classe_id',
            field=models.IntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='saisienoteenligne', name='semestre_id',
            field=models.IntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='saisienoteenligne', name='annee_universitaire',
            field=models.CharField(blank=True, db_index=True, default='', max_length=20),
        ),
        migrations.AddField(
            model_name='saisienoteenligne', name='matiere_code',
            field=models.CharField(blank=True, default='', max_length=50),
        ),
        migrations.AddField(
            model_name='saisienoteenligne', name='matiere_intitule',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='saisienoteenligne', name='classe_nom',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='saisienoteenligne', name='semestre_code',
            field=models.CharField(blank=True, default='', max_length=10),
        ),

        migrations.AlterUniqueTogether(
            name='saisienoteenligne',
            unique_together={('prof_id', 'note_ipgei_id', 'type_note', 'numero')},
        ),
        migrations.AddIndex(
            model_name='saisienoteenligne',
            index=models.Index(fields=['prof_id', 'semestre_id', 'statut'],
                               name='saisie_note_prof_sem_idx'),
        ),
        migrations.AddIndex(
            model_name='saisienoteenligne',
            index=models.Index(fields=['classe_id', 'matiere_id', 'semestre_id'],
                               name='saisie_note_feuille_idx'),
        ),
    ]
