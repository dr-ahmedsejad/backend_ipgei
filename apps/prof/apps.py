from django.apps import AppConfig


class ProfConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.prof'
    verbose_name = 'Professeurs'

    def ready(self):
        # Importe les signaux qui maintiennent prof_type_history en sync
        # quand prof.type est cree ou modifie. cf apps/prof/signals.py.
        from . import signals  # noqa: F401
