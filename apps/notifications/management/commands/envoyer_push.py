"""Pousse vers les téléphones les notifications récentes pas encore poussées.

Reprise de l'ISS, pour l'app Groupe Polytechnique (une app pour tous les
établissements, un projet Firebase commun ; la clé d'envoi de l'IPGEI dans ce
projet : FIREBASE_CREDENTIALS_GP). Cron toutes les 2 minutes
(deploy/cron/ipgei-push). Le sigle de l'établissement ouvre le titre
(« IPGEI — … ») ; le code de l'établissement et le profil partent dans les
données, pour que le toucher ouvre le bon espace.

Chaque notification n'est traitée qu'une fois (PushEnvoye) : la ligne est prise
AVANT l'envoi, et rendue si l'envoi échoue pour une raison passagère. Une
notification déjà lue, ou plus ancienne que la fenêtre, n'est pas poussée.
Envois par 10 en parallèle ; la base n'est lue et écrite que par le fil principal.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import IntegrityError
from django.utils import timezone

from apps.authentication.models import CustomUser
from apps.notifications import push
from apps.notifications.models import AppareilPush, Notification, PushEnvoye

ENVOIS_SIMULTANES = 10


def _envoyer(job):
    """Un envoi (dans un fil, sans base) : 'ok', 'invalide' ou le message d'erreur."""
    n, appareil, cle, profil = job
    try:
        push.envoyer(appareil.jeton, f'{settings.ETABLISSEMENT_SIGLE} — {n.titre}', n.message,
                     {'notification_id': n.pk, 'type': n.type, 'lien': n.lien,
                      'etablissement': settings.ETABLISSEMENT_CODE, 'profil': profil},
                     chemin=cle)
        return 'ok'
    except push.JetonInvalide:
        return 'invalide'
    except Exception as e:  # réseau, quota : on réessaiera
        return f'{e}' or e.__class__.__name__


class Command(BaseCommand):
    help = 'Envoie les notifications push en attente (Firebase Cloud Messaging).'

    def add_arguments(self, parser):
        parser.add_argument('--depuis-heures', type=int, default=48,
                            help='Fenêtre : notifications créées depuis N heures (défaut 48).')
        parser.add_argument('--essai', action='store_true',
                            help="Affiche ce qui serait envoyé, sans rien envoyer ni noter.")

    def handle(self, *args, depuis_heures, essai, **opts):
        cle = push.cle_gp()
        if not cle or not push.configure(cle):
            self.stdout.write('Push désactivé (FIREBASE_CREDENTIALS_GP absent ou illisible).')
            return

        depuis = timezone.now() - timedelta(hours=depuis_heures)
        notifs = list(Notification.objects.filter(created_at__gte=depuis).order_by('created_at'))
        if not notifs:
            return
        deja = set(PushEnvoye.objects.filter(notification_le__gte=depuis)
                   .values_list('notification_id', 'notification_le'))
        appareils = {}
        for a in AppareilPush.objects.filter(user_id__in={n.destinataire_id for n in notifs}):
            appareils.setdefault(a.user_id, []).append(a)
        enseignants = set(CustomUser.objects.filter(pk__in=appareils, role='enseignant')
                          .values_list('pk', flat=True))

        envoyees = sans_appareil = erreurs = 0
        a_envoyer = []   # (notification, sa trace PushEnvoye, ses appareils)
        for n in notifs:
            if (n.pk, n.created_at) in deja:
                continue
            cibles = appareils.get(n.destinataire_id, [])
            if essai:
                self.stdout.write(f'#{n.pk} → user #{n.destinataire_id} : {len(cibles)} appareil(s)'
                                  f'{" (déjà lue)" if n.lue else ""} — {n.titre}')
                continue
            try:
                trace = PushEnvoye.objects.create(notification_id=n.pk, notification_le=n.created_at)
            except IntegrityError:
                continue  # prise par une exécution concurrente
            if n.lue or not cibles:
                sans_appareil += 1
                continue
            a_envoyer.append((n, trace, cibles))

        # Envois en parallèle (sans base) ; résultats dans l'ordre des jobs.
        jobs = [(n, a, cle, 'enseignant' if n.destinataire_id in enseignants else 'etudiant')
                for n, _, cibles in a_envoyer for a in cibles]
        with ThreadPoolExecutor(max_workers=ENVOIS_SIMULTANES) as pool:
            resultats = list(pool.map(_envoyer, jobs))
        par_appareil = {(id(n), a.pk): r for (n, a, _, _), r in zip(jobs, resultats)}

        # Bilan par notification (fil principal : base).
        for n, trace, cibles in a_envoyer:
            ok = echecs = 0
            for a in cibles:
                r = par_appareil[(id(n), a.pk)]
                if r == 'ok':
                    ok += 1
                elif r == 'invalide':
                    a.delete()  # app désinstallée / jeton périmé
                else:
                    echecs += 1
                    erreurs += 1
                    self.stderr.write(f'#{n.pk} : {r}')
            if ok == 0 and echecs:
                trace.delete()  # rendue : réessayée au prochain passage
            else:
                trace.nb_appareils = ok
                trace.save(update_fields=['nb_appareils'])
                envoyees += ok

        if envoyees or erreurs:
            self.stdout.write(f'{envoyees} envoi(s), {sans_appareil} sans appareil, {erreurs} erreur(s).')
