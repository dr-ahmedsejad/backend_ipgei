"""
ETL de migration des DONNÉES MySQL -> PostgreSQL (dry-run, hors cutover).

Copie table par table depuis la connexion 'mysql_source' (LECTURE SEULE) vers
'default' (PostgreSQL cible), en s'appuyant sur le marshalling de types de
Django (chaque backend adapte bool/uuid/json/datetime correctement).

Prérequis : le schéma Django a déjà été créé sur la cible via
    DJANGO_SETTINGS_MODULE=siga.settings.pg_load python manage.py migrate

Puis :
    DJANGO_SETTINGS_MODULE=siga.settings.pg_load python manage.py migrate_data_from_mysql

Points délicats traités :
  - `auto_now` / `auto_now_add` : neutralisés le temps de la copie, sinon
    bulk_create réécrirait les horodatages à NOW() (corruption silencieuse).
  - Intégrité référentielle : `session_replication_role = replica` sur la
    session PG désactive les triggers FK le temps du load (ordre des tables
    indifférent). Réactivé automatiquement à la fermeture de session.
  - Tables déjà peuplées par `migrate` (content_types, permissions, seeds RBAC,
    django_migrations) : TRUNCATE de la cible AVANT copie, SAUF django_migrations
    (on conserve l'historique réel de migration PG pour la cohérence Django).
  - Tables `managed=False` (prof_type_history, suivi_pointage_departements) :
    tables créées si absentes, puis copiées comme les autres.
  - core_audit_log (volumineux) : copie par lots via itérateur serveur.

READ-ONLY côté MySQL : uniquement des SELECT (.using('mysql_source')).
"""
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.db.models import CharField, TextField

SOURCE = 'mysql_source'
CIBLE = 'default'

# On ne recopie PAS django_migrations : l'historique PG a été écrit par le
# `migrate` réel sur la cible et doit refléter ce qui a tourné sur PG.
TABLES_EXCLUES = {'django_migrations'}


class Command(BaseCommand):
    help = "Copie les données MySQL (mysql_source) -> PostgreSQL (default)."

    def add_arguments(self, parser):
        parser.add_argument('--batch', type=int, default=1000,
                            help='Taille des lots bulk_create (défaut 1000).')
        parser.add_argument('--tables', type=str, default=None,
                            help='Restreint à une liste de db_table (CSV) — debug.')
        parser.add_argument('--skip-truncate', action='store_true',
                            help='Ne pas TRUNCATE la cible avant copie (cible déjà vide).')

    # ── utilitaires ──────────────────────────────────────────────────────────
    def _modeles_concrets(self):
        """Modèles à copier : concrets, non-proxy, incluant les through M2M.
        Ordonnés par label pour un log stable."""
        vus, res = set(), []
        for m in apps.get_models(include_auto_created=True):
            if m._meta.proxy:
                continue
            t = m._meta.db_table
            if t in vus:
                continue
            vus.add(t)
            res.append(m)
        return sorted(res, key=lambda m: m._meta.db_table)

    def _relacher_nullabilite(self, cibles):
        """Copie FIDÈLE : on ne modifie JAMAIS une valeur (pas de coercition
        NULL->''). Pour qu'un NULL présent en MySQL puisse être écrit tel quel,
        on retire NOT NULL des colonnes texte NON-nullables (Django) qui
        contiennent des NULL en source. La cible PG reflète alors la nullabilité
        RÉELLE de MySQL (même logique que le retrait des contraintes uniques).
        Retourne la liste des colonnes relâchées (pour le rapport)."""
        pg, src = connections[CIBLE], connections[SOURCE]
        relachees = []
        for m in cibles:
            table = m._meta.db_table
            for f in m._meta.concrete_fields:
                if f.null or not isinstance(f, (CharField, TextField)):
                    continue
                # FileField/ImageField hérite de FileField(CharField) mais est
                # traité séparément (restauration post-copie) -> on le relâche
                # aussi au cas où, sans risque.
                try:
                    with src.cursor() as c:
                        c.execute(
                            f"SELECT 1 FROM {src.ops.quote_name(table)} "
                            f"WHERE {src.ops.quote_name(f.column)} IS NULL LIMIT 1")
                        a_des_nulls = c.fetchone() is not None
                except Exception:
                    continue
                if a_des_nulls:
                    with pg.cursor() as c:
                        c.execute(
                            f"ALTER TABLE {pg.ops.quote_name(table)} "
                            f"ALTER COLUMN {pg.ops.quote_name(f.column)} DROP NOT NULL")
                    relachees.append(f"{table}.{f.column}")
        return relachees

    def _colonnes_fichier(self, m):
        """Colonnes FileField/ImageField : l'ORM Django y écrit '' pour un
        fichier vide, ce qui DÉTRUIT la distinction NULL vs '' de MySQL. On
        restaure les NULL exacts en post-copie (voir _restaurer_nulls_fichiers)."""
        from django.db.models import FileField
        return [f for f in m._meta.concrete_fields if isinstance(f, FileField)]

    def _restaurer_nulls_fichiers(self, cibles):
        """Pour chaque colonne FileField, remet à NULL EXACTEMENT les lignes qui
        sont NULL en MySQL (par pk), sans toucher aux '' légitimes."""
        pg, src = connections[CIBLE], connections[SOURCE]
        total = 0
        for m in cibles:
            cols = self._colonnes_fichier(m)
            if not cols:
                continue
            table = m._meta.db_table
            pkcol = m._meta.pk.column
            for f in cols:
                with src.cursor() as c:
                    c.execute(
                        f"SELECT {src.ops.quote_name(pkcol)} FROM {src.ops.quote_name(table)} "
                        f"WHERE {src.ops.quote_name(f.column)} IS NULL")
                    ids = [r[0] for r in c.fetchall()]
                if not ids:
                    continue
                with pg.cursor() as c:
                    for i in range(0, len(ids), 1000):
                        lot = ids[i:i + 1000]
                        ph = ','.join(['%s'] * len(lot))
                        c.execute(
                            f"UPDATE {pg.ops.quote_name(table)} "
                            f"SET {pg.ops.quote_name(f.column)} = NULL "
                            f"WHERE {pg.ops.quote_name(pkcol)} IN ({ph})", lot)
                total += len(ids)
                self.stdout.write(f"  NULL fichier restaurés : {table}.{f.column} ({len(ids)})")
        return total

    def _neutraliser_auto_now(self):
        """Désactive auto_now/auto_now_add sur TOUS les champs (mutation en
        mémoire, process one-shot) pour que bulk_create garde les vraies
        valeurs lues dans MySQL. Retourne la liste pour restauration."""
        modifies = []
        for m in apps.get_models(include_auto_created=True):
            for f in m._meta.get_fields():
                if getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False):
                    modifies.append((f, f.auto_now, f.auto_now_add))
                    f.auto_now = False
                    f.auto_now_add = False
        return modifies

    def _restaurer_auto_now(self, modifies):
        for f, an, ana in modifies:
            f.auto_now = an
            f.auto_now_add = ana

    def _creer_tables_unmanaged(self):
        """Crée en cible les tables managed=False absentes (migrate ne les crée
        pas). Idempotent."""
        cur = connections[CIBLE].introspection
        existantes = set(cur.table_names())
        with connections[CIBLE].schema_editor() as se:
            for m in apps.get_models():
                if not m._meta.managed and m._meta.db_table not in existantes:
                    se.create_model(m)
                    self.stdout.write(f"  table managed=False créée : {m._meta.db_table}")

    # ── point d'entrée ───────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        # Garde-fou : ne jamais tourner si 'default' n'est pas PostgreSQL.
        if connections[CIBLE].vendor != 'postgresql':
            raise CommandError("La connexion 'default' n'est pas PostgreSQL — "
                               "lancer avec DJANGO_SETTINGS_MODULE=siga.settings.pg_load.")
        if SOURCE not in connections:
            raise CommandError("Connexion 'mysql_source' absente des settings.")
        if connections[SOURCE].vendor != 'mysql':
            raise CommandError("La connexion 'mysql_source' n'est pas MySQL.")

        filtre = None
        if opts['tables']:
            filtre = {t.strip() for t in opts['tables'].split(',') if t.strip()}

        self._creer_tables_unmanaged()

        modeles = self._modeles_concrets()
        if filtre:
            modeles = [m for m in modeles if m._meta.db_table in filtre]

        cibles = [m for m in modeles if m._meta.db_table not in TABLES_EXCLUES]
        tables_cibles = [m._meta.db_table for m in cibles]

        pg = connections[CIBLE]
        batch = opts['batch']

        # Désactive les contrôles FK pour toute la session de load.
        with pg.cursor() as c:
            c.execute("SET session_replication_role = 'replica';")

        # Vide la cible (tables créées + seedées par migrate) avant copie.
        # DELETE (et non TRUNCATE ... CASCADE) : sous session_replication_role
        # ='replica' les FK sont neutralisées, donc DELETE fonctionne sans
        # cascader HORS du périmètre demandé. Indispensable avec --tables :
        # un TRUNCATE CASCADE y viderait tout le graphe FID en aval.
        if not opts['skip_truncate'] and tables_cibles:
            with pg.cursor() as c:
                for t in tables_cibles:
                    c.execute(f"DELETE FROM {pg.ops.quote_name(t)};")
            self.stdout.write(self.style.WARNING(
                f"Purge (DELETE) de {len(tables_cibles)} table(s) cible avant copie."))

        # Relâche NOT NULL sur les colonnes texte qui ont des NULL en source
        # (copie fidèle sans coercition -> le NULL doit pouvoir être écrit).
        relachees = self._relacher_nullabilite(cibles)
        if relachees:
            self.stdout.write(self.style.WARNING(
                f"NOT NULL relâché (miroir du MySQL réel) sur : {', '.join(relachees)}"))

        auto_now_modifies = self._neutraliser_auto_now()
        resume = []          # (table, nb_source, nb_copiees, statut)
        try:
            for m in cibles:
                table = m._meta.db_table
                try:
                    nb_src = m._base_manager.using(SOURCE).count()
                except Exception as exc:
                    resume.append((table, '?', 0, f'SOURCE illisible: {type(exc).__name__}'))
                    continue

                copiees = 0
                lot = []
                qs = m._base_manager.using(SOURCE).all().iterator(chunk_size=batch)
                try:
                    with transaction.atomic(using=CIBLE):
                        # Re-poser replica DANS la transaction (chaque connexion
                        # garde le GUC de session, mais on s'assure au cas où).
                        with pg.cursor() as c:
                            c.execute("SET LOCAL session_replication_role = 'replica';")
                        # Copie FIDÈLE : aucune valeur n'est modifiée (les NULL
                        # sont préservés tels quels ; la nullabilité cible a été
                        # relâchée en amont pour les colonnes concernées).
                        for obj in qs:
                            lot.append(obj)
                            if len(lot) >= batch:
                                m._base_manager.using(CIBLE).bulk_create(lot)
                                copiees += len(lot)
                                lot = []
                        if lot:
                            m._base_manager.using(CIBLE).bulk_create(lot)
                            copiees += len(lot)
                    statut = 'OK' if copiees == nb_src else 'ÉCART'
                    resume.append((table, nb_src, copiees, statut))
                    self.stdout.write(
                        f"  {table:<45} {nb_src:>8} -> {copiees:>8}  {statut}")
                except Exception as exc:
                    resume.append((table, nb_src, copiees, f'ERREUR: {type(exc).__name__}: {exc}'))
                    self.stdout.write(self.style.ERROR(
                        f"  {table:<45} ÉCHEC après {copiees} : {exc}"))
        finally:
            self._restaurer_auto_now(auto_now_modifies)

        # Restaure les NULL exacts sur les colonnes FileField/ImageField
        # (l'ORM Django y écrit '' pour un fichier vide -> on remet NULL là où
        # MySQL est NULL, sans toucher aux '' légitimes).
        self.stdout.write("Restauration des NULL fichier…")
        nb_nulls = self._restaurer_nulls_fichiers(cibles)

        # Reset des séquences (setval > MAX(pk)) — indispensable après copie de
        # PK explicites, sinon le prochain INSERT collisionne.
        self.stdout.write("Reset des séquences…")
        seqs = self._reset_sequences(cibles)

        # Bilan
        nb_ok = sum(1 for _, _, _, s in resume if s == 'OK')
        nb_ko = [r for r in resume if r[3] not in ('OK',)]
        self.stdout.write(self.style.SUCCESS(
            f"\nCopie terminée : {nb_ok}/{len(resume)} tables OK, "
            f"{len(seqs)} séquence(s) resynchronisée(s)."))
        if nb_ko:
            self.stdout.write(self.style.ERROR(f"{len(nb_ko)} table(s) en écart/erreur :"))
            for t, ns, nc, s in nb_ko:
                self.stdout.write(self.style.ERROR(f"  {t} : source={ns} copiées={nc} — {s}"))
            raise CommandError("Copie incomplète — voir écarts ci-dessus.")

    def _reset_sequences(self, modeles):
        """setval sur chaque séquence serial > MAX(pk)."""
        pg = connections[CIBLE]
        faites = []
        with pg.cursor() as c:
            for m in modeles:
                pk = m._meta.pk
                if pk is None:
                    continue
                table = m._meta.db_table
                col = pk.column
                # pg_get_serial_sequence renvoie NULL si la PK n'est pas serial
                # (ex. UUID, clé naturelle) -> on saute proprement.
                # 1er argument = identifiant SQL : DOIT être quoté pour les
                # db_table à casse mixte (ex. "Seance"), sinon PG le replie en
                # minuscules et lève « relation inexistante ».
                c.execute(
                    "SELECT pg_get_serial_sequence(%s, %s)",
                    [pg.ops.quote_name(table), col],
                )
                seq = c.fetchone()[0]
                if not seq:
                    continue
                c.execute(
                    f'SELECT setval(%s, COALESCE((SELECT MAX({pg.ops.quote_name(col)}) '
                    f'FROM {pg.ops.quote_name(table)}), 1), true)',
                    [seq],
                )
                faites.append(table)
        return faites
