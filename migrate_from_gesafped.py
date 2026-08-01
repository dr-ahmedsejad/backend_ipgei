"""
Migration des données de gesafped → siga_db
Gère les différences de noms de tables et colonnes.
Usage : python migrate_from_gesafped.py
"""
import MySQLdb
import sys

SRC  = dict(host='localhost', user='root', passwd='', db='gesafped',  port=3306, charset='utf8')
DST  = dict(host='localhost', user='root', passwd='', db='siga_db',   port=3306, charset='utf8')

src = MySQLdb.connect(**SRC)
dst = MySQLdb.connect(**DST)
src.autocommit(False)
dst.autocommit(False)

cs = src.cursor()
cd = dst.cursor()


def info(msg):  print(f'  ✔  {msg}')
def warn(msg):  print(f'  ⚠  {msg}')


def copy_table(src_table, dst_table, col_map=None, skip_cols=None, extra_vals=None):
    """
    Copie les lignes de src_table vers dst_table.
    col_map   : {dst_col: src_col}  — renommage de colonnes
    skip_cols : colonnes dst à ignorer (valeur NULL)
    extra_vals: {dst_col: valeur_fixe}
    """
    cs.execute(f'SELECT COUNT(*) FROM `{src_table}`')
    n = cs.fetchone()[0]
    if n == 0:
        warn(f'{src_table} vide — ignoré')
        return

    # Colonnes destination
    cd.execute(f'DESCRIBE `{dst_table}`')
    dst_cols = [r[0] for r in cd.fetchall() if r[0] != 'id' or True]
    # On garde toutes les colonnes (y compris id pour préserver les FK)
    cd.execute(f'DESCRIBE `{dst_table}`')
    dst_cols_all = [r[0] for r in cd.fetchall()]

    skip = set(skip_cols or [])
    extra = extra_vals or {}

    insert_cols = []
    select_exprs = []

    for dc in dst_cols_all:
        if dc in skip:
            continue
        if dc in extra:
            insert_cols.append(f'`{dc}`')
            select_exprs.append(f'%s')
            continue
        sc = col_map.get(dc, dc) if col_map else dc
        insert_cols.append(f'`{dc}`')
        select_exprs.append(f'`{sc}`')

    # Vider la table destination
    cd.execute(f'DELETE FROM `{dst_table}`')

    # Lire la source
    cs.execute(f'SELECT {", ".join(f"`{col_map.get(dc,dc)}`" if dc not in extra and dc not in skip else "NULL" for dc in dst_cols_all if dc not in skip)} FROM `{src_table}`')

    # Rebuild proprement
    src_selects = []
    ins_cols = []
    for dc in dst_cols_all:
        if dc in skip:
            continue
        ins_cols.append(f'`{dc}`')
        if dc in extra:
            src_selects.append(None)  # placeholder, handled below
        else:
            sc = col_map.get(dc, dc) if col_map else dc
            src_selects.append(f'`{sc}`')

    select_sql = ', '.join(f'`{col_map.get(dc, dc)}`' if dc not in extra else 'NULL' for dc in dst_cols_all if dc not in skip)
    cs.execute(f'SELECT {select_sql} FROM `{src_table}`')
    rows = cs.fetchall()

    if not rows:
        warn(f'{src_table} → {dst_table}: aucune donnée')
        return

    placeholders = ', '.join(['%s'] * len(ins_cols))
    insert_sql = f'INSERT INTO `{dst_table}` ({", ".join(ins_cols)}) VALUES ({placeholders})'

    # Injecter les extra_vals
    extra_positions = {i: extra[dc] for i, dc in enumerate(dc for dc in dst_cols_all if dc not in skip) if dc in extra}

    new_rows = []
    for row in rows:
        row_list = list(row)
        for pos, val in extra_positions.items():
            row_list[pos] = val
        new_rows.append(tuple(row_list))

    cd.executemany(insert_sql, new_rows)
    dst.commit()
    info(f'{src_table} → {dst_table} : {len(new_rows)} lignes')


def disable_fk():
    cd.execute('SET FOREIGN_KEY_CHECKS = 0')

def enable_fk():
    cd.execute('SET FOREIGN_KEY_CHECKS = 1')
    dst.commit()


# ─────────────────────────────────────────────────────────────────────────────
print('\n=== Migration gesafped → siga_db ===\n')
disable_fk()

# ── Authentification ──────────────────────────────────────────────────────────
print('[ Authentification ]')
# CustomUser : schéma identique
copy_table('authentication_customuser', 'authentication_customuser')

# Module, Action, ModuleAction — identiques
copy_table('authentication_module',      'authentication_module')
copy_table('authentication_action',      'authentication_action')
copy_table('authentication_moduleaction','authentication_moduleaction')

# RoleDefault : gesafped n'a pas la colonne `allowed` → on met True par défaut
copy_table('authentication_roledefault', 'authentication_roledefault',
           extra_vals={'allowed': True})

# UserPermission : identique
copy_table('authentication_userpermission', 'authentication_userpermission')

# ── Paramètres ────────────────────────────────────────────────────────────────
print('\n[ Paramètres ]')
copy_table('annee',            'annee')
copy_table('niveau',           'niveau')
copy_table('semestre',         'semestre')
copy_table('Seance',           'Seance')
copy_table('creneau',          'creneau')
copy_table('jour',             'jour')
copy_table('semaine',          'semaine')
copy_table('paiement',         'paiement')
copy_table('institution',      'institution')
try:
    copy_table('parametres_ramadan', 'parametres_ramadan')
except Exception as e:
    warn(f'parametres_ramadan : {e}')

# ── Entités principales ───────────────────────────────────────────────────────
print('\n[ Entités principales ]')

copy_table('departement', 'departement_departement',
           col_map={
               'id': 'id', 'nom': 'nom', 'description': 'description',
               'niveau_id': 'niveau_id', 'decalage': 'decalage',
               'annee_universitaire': 'annee_universitaire', 'code': 'code',
           })

copy_table('banque', 'banque_banque',
           col_map={'id': 'id', 'nom': 'nom', 'description': 'description'})

copy_table('salle', 'salle_salle',
           col_map={'id': 'id', 'nom': 'nom', 'capacite': 'capacite'})

copy_table('em', 'em_em',
           col_map={
               'id': 'id', 'code_em': 'code_em', 'intitule': 'intitule',
               'CM': 'CM', 'TD': 'TD', 'TP': 'TP', 'PR': 'PR',
               'departement_id': 'departement_id', 'semestre_id': 'semestre_id',
           })

# Prof : colonnes avec accents dans GesAFPED → ASCII dans SIGA
# Récupérer les noms de colonnes réels (avec accents)
cs.execute('DESCRIBE `prof`')
prof_cols_src = {r[0]: r[0] for r in cs.fetchall()}
# Mapping dst → src (les colonnes accentuées)
tel_col   = next((c for c in prof_cols_src if 'l' in c and 'phone' in c.lower()), 't\u00e9l\u00e9phone')
dip_col   = next((c for c in prof_cols_src if 'dipl' in c.lower() and 'description' not in c.lower()), 'niveau_de_dipl\u00f4me')
desc_col  = next((c for c in prof_cols_src if 'description' in c.lower()), 'description_dernier_dipl\u00f4me')
num_col   = next((c for c in prof_cols_src if 'compte' in c.lower()), 'num\u00e9ro_de_compte')
dech_col  = next((c for c in prof_cols_src if 'charge' in c.lower() and 'd' in c.lower()), 'd\u00e9charge')

copy_table('prof', 'prof_prof',
           col_map={
               'id': 'id', 'NNI': 'NNI', 'nom': 'nom',
               'telephone':                   tel_col,
               'email':                       'email',
               'genre':                       'genre',
               'type':                        'type',
               'niveau_de_diplome':           dip_col,
               'description_dernier_diplome': desc_col,
               'banque_id':                   'banque_id',
               'numero_de_compte':            num_col,
               'cv':                          'cv',
               'diplome':                     'diplome',
               'grade':                       'grade',
               'charge':                      'charge',
               'decharge':                    dech_col,
           })

# ── Emplois ───────────────────────────────────────────────────────────────────
print('\n[ Emplois ]')
copy_table('emplois_emplois',       'emplois_emplois')
try:
    copy_table('emplois_emploisarchive', 'emplois_emploisarchive')
except Exception as e:
    warn(f'emplois_emploisarchive : {e}')

# ── Suivi ─────────────────────────────────────────────────────────────────────
print('\n[ Suivi ]')
copy_table('suivi_suivie',           'suivi_suivie')
copy_table('suivi_suivie_pointage',  'suivi_suivie_pointage')
try:
    copy_table('charge_institution', 'suivi_chargeinstitution',
               col_map={
                   'id': 'id', 'charge_cm': 'charge_cm',
                   'annee_universitaire': 'annee_universitaire',
                   'institution_id': 'institution_id', 'prof_id': 'prof_id',
               })
except Exception as e:
    warn(f'charge_institution : {e}')

# ── Absences ──────────────────────────────────────────────────────────────────
print('\n[ Absences ]')
copy_table('absence_etudiant',      'absence_etudiant')
copy_table('absence_presence',      'absence_presence')
try:
    copy_table('absence_seuilabsence', 'absence_seuilabsence')
except Exception as e:
    warn(f'absence_seuilabsence : {e}')

# ── Vacations ─────────────────────────────────────────────────────────────────
print('\n[ Vacations ]')
try:
    copy_table('vacation_surveillance', 'vacation_surveillance')
except Exception as e:
    warn(f'vacation_surveillance : {e}')
copy_table('vacation_vacation', 'vacation_vacation')
try:
    # Table M2M departements
    cs.execute('SELECT COUNT(*) FROM `vacation_vacation_departements`')
    if cs.fetchone()[0] > 0:
        copy_table('vacation_vacation_departements', 'vacation_vacation_departements')
except Exception as e:
    warn(f'vacation_vacation_departements : {e}')

# ── Token blacklist ───────────────────────────────────────────────────────────
print('\n[ Auth tokens ]')
try:
    copy_table('token_blacklist_outstandingtoken', 'token_blacklist_outstandingtoken')
    copy_table('token_blacklist_blacklistedtoken',  'token_blacklist_blacklistedtoken')
except Exception as e:
    warn(f'tokens : {e}')

enable_fk()

src.close()
dst.close()

print('\n=== Migration terminée avec succès ===\n')
