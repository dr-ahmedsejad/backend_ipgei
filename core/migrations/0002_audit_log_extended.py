"""
State-only : aligne le state Django avec l'extension de la table core_audit_log.

DDL deja applique manuellement (pre-Sprint 1 audit log) :
  - 6 colonnes ajoutees : institution_id, request_id, label, endpoint,
    http_method, keep_forever
  - action : VARCHAR(10) -> VARCHAR(20)
  - 3 nouveaux index : (model_name, object_id, timestamp), (action, timestamp),
    (institution, timestamp)
  - FK constraint sur institution_id

A appliquer en --fake (la DB MySQL est deja alignee).
Sur base neuve non-MySQL : colonnes + index ajoutes reellement.
"""
from django.db import migrations, models
import django.db.models.deletion

from core.migration_ops import DejaAppliqueeSurMySQL


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
        ('parametres', '0006_semestre_simplification'),
    ]

    operations = [
        DejaAppliqueeSurMySQL(
            database_operations=[],
            state_operations=[
                migrations.AlterField(
                    model_name='auditlog', name='action',
                    field=models.CharField(
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
                    ),
                ),
                migrations.AddField(
                    model_name='auditlog', name='institution',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to='parametres.institution',
                    ),
                ),
                migrations.AddField(
                    model_name='auditlog', name='request_id',
                    field=models.CharField(blank=True, default='', max_length=36),
                ),
                migrations.AddField(
                    model_name='auditlog', name='label',
                    field=models.CharField(blank=True, default='', max_length=200),
                ),
                migrations.AddField(
                    model_name='auditlog', name='endpoint',
                    field=models.CharField(blank=True, default='', max_length=200),
                ),
                migrations.AddField(
                    model_name='auditlog', name='http_method',
                    field=models.CharField(blank=True, default='', max_length=10),
                ),
                migrations.AddField(
                    model_name='auditlog', name='keep_forever',
                    field=models.BooleanField(default=False),
                ),
                migrations.AddIndex(
                    model_name='auditlog',
                    index=models.Index(
                        fields=['model_name', 'object_id', 'timestamp'],
                        name='idx_audit_model_obj_ts',
                    ),
                ),
                migrations.AddIndex(
                    model_name='auditlog',
                    index=models.Index(
                        fields=['action', 'timestamp'],
                        name='idx_audit_action_ts',
                    ),
                ),
                migrations.AddIndex(
                    model_name='auditlog',
                    index=models.Index(
                        fields=['institution', 'timestamp'],
                        name='idx_audit_inst_ts',
                    ),
                ),
            ],
        ),
    ]
