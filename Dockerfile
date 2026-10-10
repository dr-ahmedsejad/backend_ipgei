# ─── Image de base ─────────────────────────────────────────────
FROM python:3.11-slim-bookworm

# ─── Dependances systeme ───────────────────────────────────────
# wkhtmltopdf : retire des depots Debian 12 → installe via .deb officiel
# default-libmysqlclient-dev : pour mysqlclient (driver MySQL)
# locales fr_FR.UTF-8 : pour les dates/montants en francais
# fonts + xfonts : requis par la version "patched Qt" de wkhtmltopdf
#
# ttf-mscorefonts-installer : installe la VRAIE Arial (de Microsoft) + Times,
#   Courier, etc. Les templates PDF utilisent `font-family: Arial` → meme rendu
#   qu'en local Windows. Arial inclut deja les glyphes arabes (depuis Win 7).
#   Necessite contrib activee + acceptation EULA via debconf.
RUN echo "deb http://deb.debian.org/debian bookworm contrib non-free non-free-firmware" > /etc/apt/sources.list.d/contrib.list \
 && apt-get update \
 && echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" \
      | debconf-set-selections \
 && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    fontconfig \
    xfonts-75dpi \
    xfonts-base \
    libxrender1 \
    libjpeg62-turbo \
    libxext6 \
    libssl3 \
    fonts-dejavu \
    fonts-liberation \
    ttf-mscorefonts-installer \
    default-libmysqlclient-dev \
    default-mysql-client \
    gcc \
    pkg-config \
    locales \
 && fc-cache -f -v \
 && sed -i '/fr_FR.UTF-8/s/^# //' /etc/locale.gen && locale-gen \
 && curl -fsSL -o /tmp/wkhtmltox.deb \
      https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/wkhtmltox.deb \
 && rm -f /tmp/wkhtmltox.deb \
 && rm -rf /var/lib/apt/lists/* \
 && wkhtmltopdf --version \
 && fc-match "Arial"

# ─── Hack zero-modif-code ──────────────────────────────────────
# Le code Python utilise un chemin Windows hardcode :
#   r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe'
# Sur Linux, les backslashes sont des caracteres litteraux d'un nom de fichier
# (pas des separateurs). Il faut DEUX symlinks :
# 1. /app/C:\...  → pour pdfkit.configuration() qui valide via open()
#                   (relatif a WORKDIR /app puisque le path commence par 'C:')
# 2. /usr/local/bin/C:\...  → pour subprocess (PATH lookup, car le path
#                              ne contient aucun '/')
WORKDIR /app
RUN ln -s /usr/local/bin/wkhtmltopdf '/app/C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe' \
 && ln -s /usr/local/bin/wkhtmltopdf '/usr/local/bin/C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe' \
 && ls -la '/app/C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe' \
            '/usr/local/bin/C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe'

# ─── Variables d'environnement ─────────────────────────────────
ENV LANG=fr_FR.UTF-8 \
    LC_ALL=fr_FR.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ─── Code applicatif ───────────────────────────────────────────
WORKDIR /app

# Couche separee pour requirements (cache Docker = rebuild rapide)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir gunicorn

# Code source (utilise .dockerignore pour exclure venv, __pycache__, etc.)
COPY . .

# Dossier media et staticfiles (montes en volume par docker-compose)
RUN mkdir -p /app/media /app/staticfiles /app/logs

EXPOSE 8000

# ─── Demarrage ─────────────────────────────────────────────────
# Migrations + collectstatic au demarrage, puis gunicorn.
# gthread : chaque worker sert GUNICORN_THREADS requetes a la fois (une requete
# qui attend PostgreSQL ne bloque plus le worker). 3 x 4 = 12 requetes
# simultanees pour la memoire de 3 workers — le VPS mutualise est juste en RAM.
# Retour a l'ancien comportement sans reconstruire : GUNICORN_THREADS=1 dans
# deploy/.env puis `docker compose -f docker-compose.ipgei.yml up -d backend`.
# --max-requests : chaque worker est recycle apres ~2000 requetes (fuites
# memoire eventuelles), en decale (jitter) pour ne jamais tous redemarrer ensemble.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn siga.wsgi:application --bind 0.0.0.0:8000 --worker-class gthread --workers ${GUNICORN_WORKERS:-3} --threads ${GUNICORN_THREADS:-4} --timeout 120 --max-requests 2000 --max-requests-jitter 200"]
