"""
Diagnostic complet du drift schema entre les modeles Django siga
et la structure reelle de la BD.

Pour chaque modele Django (managed=True), liste :
  - Colonnes attendues (db_column ou auto)
  - Colonnes presentes en DB
  - Colonnes manquantes en DB (= drift critique)
  - Colonnes orphelines en DB (= ne posent pas probleme mais pollution)

Genere les ALTER TABLE recommandes pour chaque colonne manquante,
en deduisant le type SQL depuis le type de field Django.

Usage :
    python manage.py schema_diff               # rapport complet
    python manage.py schema_diff --apply       # applique les ALTER ADD COLUMN
    python manage.py schema_diff --apps suivi,emplois,prof   # filtrer
"""
from __future__ import annotations
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection, models


# Mapping Django field -> SQL type (defauts conservateurs : NULL pour minimiser le risque)
def field_to_sql(field) -> str:
    """Retourne le type SQL pour un champ Django, en mode permissif (NULL OK)."""
    null_part = ' NULL'
    default_part = ''

    # Determiner s'il y a un default
    if field.has_default() and not callable(field.default):
        d = field.default
        if isinstance(d, str):
            default_part = f" DEFAULT '{d}'"
        elif isinstance(d, (int, float)):
            default_part = f' DEFAULT {d}'
        elif d is None:
            pass  # NULL par defaut

    if isinstance(field, models.BigIntegerField) or isinstance(field, models.AutoField) or 'BigAuto' in field.__class__.__name__:
        return f'BIGINT{null_part}{default_part}'
    if isinstance(field, models.IntegerField) or isinstance(field, models.PositiveIntegerField):
        return f'INT{null_part}{default_part}'
    if isinstance(field, models.FloatField):
        return f'DOUBLE{null_part}{default_part}'
    if isinstance(field, models.DecimalField):
        return f'DECIMAL({field.max_digits},{field.decimal_places}){null_part}{default_part}'
    if isinstance(field, models.BooleanField):
        return f'TINYINT(1){null_part}{default_part}'
    if isinstance(field, models.DateTimeField):
        return f'DATETIME(6){null_part}'
    if isinstance(field, models.DateField):
        return f'DATE{null_part}'
    if isinstance(field, models.TextField):
        return f'LONGTEXT{null_part}'
    if isinstance(field, models.EmailField):
        return f'VARCHAR(254){null_part}{default_part}'
    if isinstance(field, models.CharField):
        return f'VARCHAR({field.max_length}){null_part}{default_part}'
    if isinstance(field, models.FileField):
        return f'VARCHAR(100){null_part}'
    if isinstance(field, models.ForeignKey) or isinstance(field, models.OneToOneField):
        return f'BIGINT{null_part}'
    return f'/* unknown : {field.__class__.__name__} */ TEXT NULL'


class Command(BaseCommand):
    help = 'Diagnostique et corrige le drift schema model<->DB.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Applique les ALTER ADD COLUMN.')
        parser.add_argument('--apps',  type=str, default='', help='CSV des apps a auditer.')

    def handle(self, *args, **opts):
        cur = connection.cursor()
        only_apps = set(filter(None, opts['apps'].split(','))) if opts['apps'] else None

        # Charger toutes les colonnes existantes par table
        cur.execute("""
            SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
        """)
        cols_by_table = {}
        for t, c in cur.fetchall():
            cols_by_table.setdefault(t, set()).add(c)

        all_models = []
        for model in apps.get_models():
            meta = model._meta
            if not meta.managed:
                continue
            if only_apps and meta.app_label not in only_apps:
                continue
            all_models.append(model)

        total_missing = 0
        applied = []
        ddl_buffer = []

        for model in all_models:
            meta = model._meta
            tbl = meta.db_table
            if tbl not in cols_by_table:
                self.stdout.write(self.style.WARNING(f'\n!! {meta.app_label}.{meta.object_name} ({tbl}) - TABLE ABSENTE'))
                continue

            existing = cols_by_table[tbl]
            expected = {}    # col_name -> field
            for f in meta.concrete_fields:
                if not f.column:
                    continue
                expected[f.column] = f

            missing = sorted(set(expected.keys()) - existing)
            extra   = sorted(existing - set(expected.keys()))

            if not missing and not extra:
                continue  # rien a signaler

            self.stdout.write(self.style.MIGRATE_HEADING(f'\n{meta.app_label}.{meta.object_name} ({tbl})'))
            if missing:
                self.stdout.write(self.style.ERROR(f'  Manquantes en DB ({len(missing)}) :'))
                total_missing += len(missing)
                for col in missing:
                    field = expected[col]
                    sql_type = field_to_sql(field)
                    sql = f'ALTER TABLE `{tbl}` ADD COLUMN `{col}` {sql_type}'
                    self.stdout.write(f'    {sql}')
                    ddl_buffer.append((tbl, col, sql))
            if extra:
                self.stdout.write(f'  Orphelines en DB (ignoree par Django) ({len(extra)}) : {", ".join(extra)}')

        # Resume + apply
        self.stdout.write(self.style.MIGRATE_HEADING(f'\n=== Total ==='))
        self.stdout.write(f'  Modeles audites    : {len(all_models)}')
        self.stdout.write(f'  Colonnes manquantes: {total_missing}')

        if opts['apply'] and ddl_buffer:
            self.stdout.write(self.style.MIGRATE_HEADING('\n=== Application ==='))
            for tbl, col, sql in ddl_buffer:
                try:
                    cur.execute(sql)
                    applied.append((tbl, col))
                    self.stdout.write(f'  OK  {tbl}.{col}')
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'  KO  {tbl}.{col} : {e}'))
            self.stdout.write(self.style.SUCCESS(f'\n{len(applied)} colonnes ajoutees.'))
        elif ddl_buffer:
            self.stdout.write(self.style.WARNING('\n  --apply pour executer.'))
        else:
            self.stdout.write(self.style.SUCCESS('\nOK Aucun drift critique.'))
