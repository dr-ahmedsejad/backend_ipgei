"""Prévisualisation de liens externes (OpenGraph) pour le module Cours.

- Détecte les vidéos (YouTube / Vimeo) et fournit une URL d'embed.
- Sinon récupère les métadonnées OpenGraph/Twitter/<title> de la page.
- Durci contre le SSRF : http(s) uniquement, hôtes privés/loopback rejetés,
  redirections suivies manuellement (chaque saut est revalidé), taille et délai bornés.
"""
import base64
import ipaddress
import re
import socket
from html import unescape
from urllib.parse import urljoin, urlparse

import requests

_TIMEOUT = (4, 6)               # (connexion, lecture) secondes
_MAX_BYTES = 300_000            # on ne lit que le <head> en pratique
_MAX_IMG_BYTES = 1_500_000      # miniature OG convertie en data URI (cap 1,5 Mo)
_MAX_REDIRECTS = 3
_UA = 'Mozilla/5.0 (compatible; SIGA-LinkPreview/1.0)'
_YT = re.compile(r'(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,})')
_VIMEO = re.compile(r'vimeo\.com/(?:video/)?(\d+)')


def _host_autorise(host):
    """False si l'hôte résout vers une IP privée / loopback / réservée (anti-SSRF)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _url_sure(url):
    p = urlparse(url)
    return p.scheme in ('http', 'https') and bool(p.hostname) and _host_autorise(p.hostname)


def _embed_video(url):
    m = _YT.search(url)
    if m:                                            # mode « nocookie » (vie privée)
        return f'https://www.youtube-nocookie.com/embed/{m.group(1)}'
    m = _VIMEO.search(url)
    if m:
        return f'https://player.vimeo.com/video/{m.group(1)}'
    return ''


def _meta(html, *props):
    """Cherche <meta property|name="prop" content="…"> pour la 1re prop trouvée."""
    for prop in props:
        pat = (r'<meta[^>]+(?:property|name)\s*=\s*["\']' + re.escape(prop)
               + r'["\'][^>]+content\s*=\s*["\']([^"\']+)["\']')
        m = re.search(pat, html, re.IGNORECASE)
        if not m:                                   # ordre d'attributs inversé
            pat2 = (r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]+(?:property|name)\s*=\s*["\']'
                    + re.escape(prop) + r'["\']')
            m = re.search(pat2, html, re.IGNORECASE)
        if m:
            return unescape(m.group(1).strip())
    return ''


def _fetch(url):
    """Suit les redirections à la main en revalidant chaque hôte (anti-SSRF)."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not _url_sure(current):
            return None
        resp = requests.get(current, headers={'User-Agent': _UA, 'Accept-Language': 'fr,en;q=0.8'},
                            timeout=_TIMEOUT, stream=True, allow_redirects=False)
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get('Location')
            resp.close()
            if not loc:
                return None
            current = urljoin(current, loc)
            continue
        return resp, current
    return None


def _image_data_uri(url):
    """Télécharge une image (anti-SSRF) et la renvoie en data URI base64.

    Évite d'assouplir la CSP `img-src` (data: est déjà autorisé) et de divulguer
    l'IP de l'étudiant à des hôtes d'images tiers. '' si échec / trop volumineux."""
    if not _url_sure(url):
        return ''
    got = _fetch(url)
    if not got:
        return ''
    resp, _final = got
    try:
        ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if not ctype.startswith('image/'):
            return ''
        data = resp.raw.read(_MAX_IMG_BYTES + 1, decode_content=True) or b''
        if not data or len(data) > _MAX_IMG_BYTES:
            return ''
        return f'data:{ctype};base64,' + base64.b64encode(data).decode('ascii')
    except Exception:
        return ''
    finally:
        resp.close()


def apercu_lien(url):
    """Renvoie {url, titre, description, image, site, type, embed_url} (best-effort)."""
    url = (url or '').strip()
    base = {'url': url, 'titre': '', 'description': '', 'image': '',
            'site': urlparse(url).hostname or '', 'type': 'lien', 'embed_url': ''}
    if not _url_sure(url):
        return base

    embed = _embed_video(url)
    if embed:
        base.update(type='video', embed_url=embed)
        # on tente quand même les métadonnées (titre/miniature) plus bas

    got = _fetch(url)
    if not got:
        return base
    resp, final_url = got
    try:
        ctype = (resp.headers.get('Content-Type') or '').lower()
        if ctype.startswith('image/'):             # l'URL pointe directement une image
            raw = resp.raw.read(_MAX_IMG_BYTES + 1, decode_content=True) or b''
            if raw and len(raw) <= _MAX_IMG_BYTES:
                mime = ctype.split(';')[0].strip()
                base.update(type='image',
                            image=f'data:{mime};base64,' + base64.b64encode(raw).decode('ascii'))
            return base
        if 'html' not in ctype:
            return base
        chunk = resp.raw.read(_MAX_BYTES, decode_content=True) or b''
        html = chunk.decode(resp.encoding or 'utf-8', errors='replace')
    finally:
        resp.close()

    titre = _meta(html, 'og:title', 'twitter:title')
    if not titre:
        m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
        titre = unescape(m.group(1).strip()) if m else ''
    image = _meta(html, 'og:image', 'og:image:url', 'twitter:image', 'twitter:image:src')
    if image:
        image = urljoin(final_url, image)
        image = _image_data_uri(image)          # -> data URI (ou '' si échec/trop lourd)
    base.update(
        titre=titre,
        description=_meta(html, 'og:description', 'twitter:description', 'description'),
        image=image,
        site=_meta(html, 'og:site_name') or (urlparse(final_url).hostname or base['site']),
    )
    return base
