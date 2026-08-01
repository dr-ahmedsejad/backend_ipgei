from django.apps import AppConfig


class EvaluationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.evaluations'
    verbose_name = 'Évaluations & Notes'

    def ready(self):
        # Charger les signals (recalcul automatique des Resultats apres save/delete d'une Note)
        from . import signals  # noqa: F401
