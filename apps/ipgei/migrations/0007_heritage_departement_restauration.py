"""
Étape 3/3 : restauration des données sur les départements devenus parents.

Le point clé : chaque classe **réutilise son ancien département miroir** comme
ligne parente. Les lignes de `suivi.Suivie`, les pointages et les vacations
référencent ces départements ; en les recyclant plutôt qu'en créant des lignes
neuves, rien de tout cela n'est touché ni ne devient orphelin.

Les sous-groupes de TP, eux, n'avaient aucune contrepartie dans le socle : leur
département est créé ici. C'est ce qui leur donne, pour la première fois, une
existence dans Emplois, Suivi et Vacations — donc la possibilité de départager
les deux moitiés d'un TP dédoublé.

Tout passe par du SQL explicite plutôt que par l'ORM : les modèles historiques
d'une migration ne reflètent pas le changement de classe de base, s'appuyer sur
leur comportement d'héritage donnerait un résultat dépendant de détails
internes de Django.
"""
from django.db import migrations


TABLES_SAUVEGARDE = (
    '_ipgei_bak_classe', '_ipgei_bak_sous_groupe', '_ipgei_bak_sg_matieres',
    '_ipgei_bak_grille', '_ipgei_bak_seance_type', '_ipgei_bak_seance_reelle',
)


def _colonnes(cursor, table):
    """Colonnes d'une table, dans l'ordre du schéma."""
    cursor.execute(
        'SELECT column_name FROM information_schema.columns '
        'WHERE table_name = %s ORDER BY ordinal_position',
        [table],
    )
    return [ligne[0] for ligne in cursor.fetchall()]


def _niveau_id(cursor, code):
    """`parametres.Niveau` correspondant à MPSI / MP, créé au besoin."""
    cursor.execute('SELECT id FROM niveau WHERE niveau = %s', [code])
    ligne = cursor.fetchone()
    if ligne:
        return ligne[0]
    cursor.execute('INSERT INTO niveau (niveau) VALUES (%s) RETURNING id', [code])
    return cursor.fetchone()[0]


def restaurer(apps, schema_editor):
    cursor = schema_editor.connection.cursor()

    cursor.execute('SELECT id FROM scolarite_filiere WHERE code = %s', ['IPGEI'])
    ligne    = cursor.fetchone()
    filiere_id = ligne[0] if ligne else None

    # ── Classes : le miroir devient la ligne parente ─────────────────────────
    cursor.execute(
        'SELECT ancien_id, nouveau_id, institution_id, annee_universitaire, '
        '       niveau, libelle, capacite, professeur_principal_id, actif, date_creation '
        '  FROM _ipgei_bak_classe'
    )
    for (ancien_id, parent_id, institution_id, annee, code_niveau, groupe,
         capacite, prof_id, actif, date_creation) in cursor.fetchall():
        niveau_id = _niveau_id(cursor, code_niveau)
        nom       = f'{code_niveau} {groupe}'.strip()
        code      = f'{code_niveau}-{groupe}'.strip('-')

        if parent_id is None:
            # Classe jamais synchronisée : il lui faut un département neuf.
            cursor.execute(
                'INSERT INTO departement (nom, description, code, annee_universitaire, '
                '  institution_id, niveau_id, filiere_id, groupe, is_container, '
                '  decalage_impair, decalage_pair) '
                "VALUES (%s, '', %s, %s, %s, %s, %s, %s, FALSE, 0, 0) RETURNING id",
                [nom, code, annee, institution_id, niveau_id, filiere_id, groupe],
            )
            parent_id = cursor.fetchone()[0]
        else:
            cursor.execute(
                'UPDATE departement SET nom = %s, code = %s, annee_universitaire = %s, '
                '  institution_id = %s, niveau_id = %s, groupe = %s, is_container = FALSE '
                'WHERE id = %s',
                [nom, code, annee, institution_id, niveau_id, groupe, parent_id],
            )

        cursor.execute(
            'INSERT INTO ipgei_classe (departement_ptr_id, capacite, '
            '  professeur_principal_id, actif, date_creation, cle_unicite) '
            'VALUES (%s, %s, %s, %s, %s, %s)',
            [parent_id, capacite, prof_id, actif, date_creation,
             f'{institution_id}|{annee}|{code_niveau}|{groupe}'],
        )
        cursor.execute(
            'UPDATE _ipgei_bak_classe SET nouveau_id = %s WHERE ancien_id = %s',
            [parent_id, ancien_id],
        )

    # ── Sous-groupes : un département neuf, rattaché à sa classe ─────────────
    cursor.execute(
        'SELECT sg.ancien_id, sg.libelle, c.nouveau_id '
        '  FROM _ipgei_bak_sous_groupe sg '
        '  JOIN _ipgei_bak_classe c ON c.ancien_id = sg.classe_id'
    )
    for ancien_id, libelle, classe_id in cursor.fetchall():
        cursor.execute(
            'SELECT nom, code, annee_universitaire, institution_id, niveau_id, filiere_id '
            '  FROM departement WHERE id = %s',
            [classe_id],
        )
        nom_classe, code_classe, annee, institution_id, niveau_id, fil_id = cursor.fetchone()

        cursor.execute(
            'INSERT INTO departement (nom, description, code, annee_universitaire, '
            '  institution_id, niveau_id, filiere_id, groupe, is_container, '
            '  decalage_impair, decalage_pair) '
            "VALUES (%s, '', %s, %s, %s, %s, %s, %s, FALSE, 0, 0) RETURNING id",
            [f'{nom_classe} — {libelle}', f'{code_classe}-{libelle}', annee,
             institution_id, niveau_id, fil_id, libelle],
        )
        parent_id = cursor.fetchone()[0]

        cursor.execute(
            'INSERT INTO ipgei_sous_groupe_tp (departement_ptr_id, classe_id, cle_unicite) '
            'VALUES (%s, %s, %s)',
            [parent_id, classe_id, f'{classe_id}|{libelle}'],
        )
        cursor.execute(
            'UPDATE _ipgei_bak_sous_groupe SET nouveau_id = %s WHERE ancien_id = %s',
            [parent_id, ancien_id],
        )

    # ── Dépendances : on remappe les clés dans la sauvegarde, puis on réinsère
    # telle quelle. Ces tables n'ont pas changé de schéma entre-temps.
    for table, colonne, correspondance in (
        ('_ipgei_bak_grille',         'classe_id',       '_ipgei_bak_classe'),
        ('_ipgei_bak_seance_reelle',  'classe_id',       '_ipgei_bak_classe'),
        ('_ipgei_bak_seance_reelle',  'sous_groupe_id',  '_ipgei_bak_sous_groupe'),
        ('_ipgei_bak_seance_type',    'sous_groupe_id',  '_ipgei_bak_sous_groupe'),
        ('_ipgei_bak_sg_matieres',    'sousgroupetp_id', '_ipgei_bak_sous_groupe'),
    ):
        cursor.execute(
            f'UPDATE {table} AS t SET {colonne} = m.nouveau_id '
            f'  FROM {correspondance} AS m '
            f' WHERE t.{colonne} = m.ancien_id'
        )

    for cible, source in (
        ('ipgei_grille_type',             '_ipgei_bak_grille'),
        ('ipgei_seance_type',             '_ipgei_bak_seance_type'),
        ('ipgei_seance_reelle',           '_ipgei_bak_seance_reelle'),
        ('ipgei_sous_groupe_tp_matieres', '_ipgei_bak_sg_matieres'),
    ):
        colonnes = ', '.join(f'"{c}"' for c in _colonnes(cursor, cible))
        cursor.execute(f'INSERT INTO {cible} ({colonnes}) SELECT {colonnes} FROM {source}')
        # Les identifiants sont réinsérés tels quels : la séquence doit repartir
        # au-delà, sans quoi la prochaine création lèverait un doublon.
        cursor.execute(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
            f'  COALESCE((SELECT MAX(id) FROM {cible}), 1))',
            [cible],
        )

    for table in TABLES_SAUVEGARDE:
        cursor.execute(f'DROP TABLE {table}')


class Migration(migrations.Migration):

    dependencies = [
        ('ipgei', '0006_heritage_departement_schema'),
    ]

    operations = [
        migrations.RunPython(restaurer, migrations.RunPython.noop),
    ]
