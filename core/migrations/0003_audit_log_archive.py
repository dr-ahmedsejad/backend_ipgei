"""
Cree la table core_audit_log_archive (warm tier, 90 jours - 1 an).

Operation classique : Django execute le CREATE TABLE + indexes en SQL.
Pas de --fake necessaire ici.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_audit_log_extended'),
        ('parametres', '0006_semestre_simplification'),
        ('authentication', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditLogArchive',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name='ID',
                )),
                ('action', models.CharField(
                    max_length=20,
                    choices=[
                        ('CREATE', 'Création'),
                        ('UPDATE', 'Modification'),
                        ('DELETE', 'Suppression'),
                        ('BULK_CREATE', 'Création en masse'),
                        ('BULK_UPDATE', 'Modification en masse'),
                        ('BULK_DELETE', 'Suppression en masse'),
                        ('ARCHIVE', 'Archivage'),
                        ('RESTORE', 'Restauration'),
                        ('LOGIN_SUCCESS', 'Connexion réussie'),
                        ('LOGIN_FAILED', 'Échec de connexion'),
                        ('LOGOUT', 'Déconnexion'),
                        ('PASSWORD_CHANGED', 'Mot de passe modifié'),
                        ('PASSWORD_RESET', 'Réinitialisation MDP'),
                        ('PERMISSION_DENIED', 'Accès refusé'),
                    ],
                )),
                ('model_name', models.CharField(max_length=100)),
                ('object_id', models.CharField(max_length=50)),
                ('changes', models.JSONField(default=dict)),
                ('ip_address', models.GenericIPAddressField(null=True, blank=True)),
                ('user_agent', models.TextField(blank=True, default='')),
                ('timestamp', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('request_id', models.CharField(blank=True, default='', max_length=36)),
                ('label', models.CharField(blank=True, default='', max_length=200)),
                ('endpoint', models.CharField(blank=True, default='', max_length=200)),
                ('http_method', models.CharField(blank=True, default='', max_length=10)),
                ('keep_forever', models.BooleanField(default=False)),
                ('institution', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+', to='parametres.institution',
                )),
                ('user', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+', to='authentication.customuser',
                )),
            ],
            options={
                'db_table': 'core_audit_log_archive',
                'ordering': ['-timestamp'],
            },
        ),
        migrations.AddIndex(
            model_name='auditlogarchive',
            index=models.Index(
                fields=['model_name', 'object_id', 'timestamp'],
                name='idx_auditarc_mod_obj_ts',
            ),
        ),
        migrations.AddIndex(
            model_name='auditlogarchive',
            index=models.Index(
                fields=['user', 'timestamp'],
                name='core_auditl_user_id_archive_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='auditlogarchive',
            index=models.Index(
                fields=['action', 'timestamp'],
                name='idx_auditarc_action_ts',
            ),
        ),
        migrations.AddIndex(
            model_name='auditlogarchive',
            index=models.Index(
                fields=['institution', 'timestamp'],
                name='idx_auditarc_inst_ts',
            ),
        ),
    ]
