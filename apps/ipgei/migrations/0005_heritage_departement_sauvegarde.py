"""
Étape 1/3 du passage des classes à l'héritage de `departement.Departement`.

`ipgei.Classe` et `ipgei.SousGroupeTP` cessent d'être des modèles autonomes
doublés d'un département miroir : ils DEVIENNENT des départements (héritage
multi-table). Leur clé primaire passe donc de leur propre séquence à celle du
département parent, ce qui impose de reconstruire les lignes plutôt que de les
modifier sur place.

Cette première migration met de côté tout ce qui dépend des classes, puis vide
les tables concernées : la conversion de schéma (migration 0006) ne peut
s'appliquer que sur des tables vides, faute de quoi Django devrait inventer une
valeur de clé étrangère parente pour chaque ligne existante.

La migration 0007 restaure ensuite les données en les rattachant aux
départements miroir DÉJÀ existants — qui deviennent les lignes parentes. C'est
ce qui préserve les pointages de Suivi : ils référencent ces mêmes
départements, et ne sont donc jamais touchés.
"""
from django.db import migrations


SAUVEGARDE = [
    # Le miroir devient la ligne parente : on garde la correspondance.
    """
    CREATE TABLE _ipgei_bak_classe AS
    SELECT id AS ancien_id, departement_miroir_id AS nouveau_id,
           institution_id, annee_universitaire, niveau, libelle,
           capacite, professeur_principal_id, actif, date_creation
      FROM ipgei_classe;
    """,
    # `nouveau_id` reste vide : les sous-groupes n'avaient aucune contrepartie
    # dans le socle, leurs départements seront créés par la migration 0007.
    """
    CREATE TABLE _ipgei_bak_sous_groupe AS
    SELECT id AS ancien_id, CAST(NULL AS integer) AS nouveau_id,
           classe_id, libelle
      FROM ipgei_sous_groupe_tp;
    """,
    "CREATE TABLE _ipgei_bak_sg_matieres  AS SELECT * FROM ipgei_sous_groupe_tp_matieres;",
    "CREATE TABLE _ipgei_bak_grille       AS SELECT * FROM ipgei_grille_type;",
    "CREATE TABLE _ipgei_bak_seance_type  AS SELECT * FROM ipgei_seance_type;",
    "CREATE TABLE _ipgei_bak_seance_reelle AS SELECT * FROM ipgei_seance_reelle;",
]

# Ordre imposé par les clés étrangères : du plus dépendant vers la classe.
PURGE = [
    "DELETE FROM ipgei_absence;",
    "DELETE FROM ipgei_permutation_prof;",
    "DELETE FROM ipgei_seance_reelle;",
    "DELETE FROM ipgei_seance_type;",
    "DELETE FROM ipgei_grille_type;",
    "DELETE FROM ipgei_sous_groupe_tp_matieres;",
    "DELETE FROM ipgei_sous_groupe_tp;",
    "DELETE FROM ipgei_classe;",
]


class Migration(migrations.Migration):

    dependencies = [
        ('ipgei', '0004_matiere_volumes_cm_td_tp'),
    ]

    operations = [
        migrations.RunSQL(
            sql=SAUVEGARDE + PURGE,
            # Irréversible : les tables de sauvegarde sont consommées par 0007.
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
