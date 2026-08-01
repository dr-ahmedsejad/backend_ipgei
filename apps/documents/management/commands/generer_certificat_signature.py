"""
Génère un certificat AUTO-SIGNÉ (PKCS#12) pour la signature numérique des PDF
officiels. Temporaire : à remplacer par un certificat d'AC officielle dès qu'il
est disponible (il suffira alors de poser le nouveau .p12 au même chemin).

Le fichier est écrit à `settings.PDF_SIGN_PKCS12_PATH` (hors-git, voir .gitignore).
La clé privée n'est JAMAIS commitée.

Usage :
  python manage.py generer_certificat_signature                 # défaut : institution principale, 5 ans
  python manage.py generer_certificat_signature --cn "Institut Supérieur de la Statistique"
  python manage.py generer_certificat_signature --force         # écrase l'existant
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Génère un certificat auto-signé (PKCS#12) pour signer les PDF officiels."

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', default=False,
                            help='Écrase le certificat existant.')
        parser.add_argument('--cn', type=str, default=None,
                            help='Common Name / Organisation (défaut : institution principale).')
        parser.add_argument('--ans', type=int, default=5,
                            help='Durée de validité en années (défaut : 5).')

    def handle(self, *args, **opts):
        import datetime
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import (
            pkcs12, BestAvailableEncryption, NoEncryption,
        )

        path = settings.PDF_SIGN_PKCS12_PATH
        if os.path.exists(path) and not opts['force']:
            self.stdout.write(self.style.WARNING(
                f'Certificat déjà présent : {path}\n'
                f'Utiliser --force pour le régénérer (ATTENTION : invalide les signatures précédentes).'))
            return

        # CN par défaut = institution principale.
        cn = opts.get('cn')
        if not cn:
            try:
                from apps.parametres.models import Institution
                inst = Institution.objects.filter(est_principale=True).first()
                cn = (getattr(inst, 'nom_fr', '') or getattr(inst, 'nom', '') or 'Etablissement') if inst else 'Etablissement'
            except Exception:
                cn = 'Etablissement'

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, cn),
            x509.NameAttribute(NameOID.COUNTRY_NAME, 'MR'),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365 * max(1, opts['ans'])))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=True,
                    key_encipherment=False, data_encipherment=False, key_agreement=False,
                    key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False),
                critical=True)
            .sign(key, hashes.SHA256())
        )

        pwd = (settings.PDF_SIGN_PKCS12_PASSWORD or '').encode()
        encryption = BestAvailableEncryption(pwd) if pwd else NoEncryption()
        p12 = pkcs12.serialize_key_and_certificates(
            name=cn.encode('utf-8'), key=key, cert=cert, cas=None,
            encryption_algorithm=encryption)

        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(p12)
        try:
            os.chmod(path, 0o600)   # lecture seule propriétaire (sans effet réel sous Windows)
        except OSError:
            pass

        self.stdout.write(self.style.SUCCESS(
            f'Certificat auto-signé généré : {path}\n'
            f'  CN/Organisation : {cn}\n'
            f'  Validité        : {opts["ans"]} an(s)\n'
            f'  Mot de passe    : {"(défini via PDF_SIGN_PKCS12_PASSWORD)" if pwd else "(aucun)"}\n'
            f'Redémarre le serveur pour que la signature prenne effet.'))
