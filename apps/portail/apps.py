from django.apps import AppConfig


class PortailConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.portail'
    verbose_name = 'Portail Étudiant'

    def ready(self):
        # Toute écriture ORM invalide les copies du cache des consultations.
        from django.db.models.signals import m2m_changed, post_delete, post_save
        from .cache_portail import signaler_ecriture
        post_save.connect(signaler_ecriture, dispatch_uid='portail_cache_save')
        post_delete.connect(signaler_ecriture, dispatch_uid='portail_cache_delete')
        m2m_changed.connect(signaler_ecriture, dispatch_uid='portail_cache_m2m')
