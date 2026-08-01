from django.apps import AppConfig


class IpgeiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name               = 'apps.ipgei'
    label              = 'ipgei'
    verbose_name       = 'IPGEI — moteur académique classes préparatoires'

    def ready(self):
        # Branche le miroir vers le socle SIGA (Departement / EM), sans quoi
        # les ecrans Suivi, Vacations et Statistiques resteraient vides.
        from . import signals  # noqa: F401
