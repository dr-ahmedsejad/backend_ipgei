"""
L'adresse du client, telle que nginx l'a vue.

Derrière le conteneur nginx, REMOTE_ADDR est l'adresse INTERNE de nginx — la
même pour tout le monde. Compter les échecs de connexion sur cette adresse
revenait à bloquer tout l'établissement au cinquième mot de passe faux, d'où
qu'il vienne.

nginx pose `X-Real-IP` ($remote_addr) dans les trois configurations (VPS,
serveur public, réseau local) et l'ÉCRASE toujours : un client ne peut pas le
choisir. On ne le croit pourtant que si la requête arrive d'une adresse privée
ou locale, donc d'un proxy du même réseau. Une requête venue directement
d'Internet garde son adresse, quel que soit l'en-tête qu'elle envoie.

`X-Forwarded-For` n'est jamais lu : nginx y AJOUTE l'adresse à ce que le
client a envoyé, et le client peut y écrire ce qu'il veut.
"""
import ipaddress


def _adresse(valeur):
    try:
        return ipaddress.ip_address((valeur or '').strip())
    except ValueError:
        return None


def adresse_client(request):
    directe = (request.META.get('REMOTE_ADDR') or '').strip()
    proxy = _adresse(directe)
    if proxy is not None and (proxy.is_private or proxy.is_loopback):
        reelle = _adresse(request.META.get('HTTP_X_REAL_IP'))
        if reelle is not None:
            return str(reelle)
    return directe or None
