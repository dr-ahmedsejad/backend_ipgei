"""Opérations de migration vendor-aware pour la portabilité MySQL → PostgreSQL/sqlite.

Contexte : les tables legacy de gesafped26 existaient AVANT les migrations Django.
Beaucoup de migrations utilisent donc SeparateDatabaseAndState avec
database_operations=[] (« la prod MySQL est déjà conforme, on ne touche que
l'état Django »). Sur une base NEUVE (sqlite de test, PostgreSQL cible), ces
opérations doivent au contraire être exécutées physiquement, sinon le schéma
réel diverge de l'état Django (tables/colonnes legacy manquantes ou en trop).

`DejaAppliqueeSurMySQL` conserve le comportement historique sur MySQL
(zéro SQL — octet pour octet identique) et rejoue les state_operations
physiquement sur tout autre vendor, via le schema_editor du backend courant
(le dialecte est donc géré par Django lui-même).

`sql_mysql` / `reverse_sql_mysql` (optionnels) couvrent le cas inverse :
un SQL spécifique MySQL à exécuter sur MySQL uniquement, pendant que les
autres vendors rejouent les state_operations standard (ex. contournement du
bug d'index MySQL sur AlterUniqueTogether — cf. evaluations/0013).
"""
from django.db import migrations


class DejaAppliqueeSurMySQL(migrations.SeparateDatabaseAndState):
    """State-only sur MySQL (base de prod déjà conforme) ;
    exécution physique réelle des state_operations sur les autres vendors."""

    def __init__(self, state_operations=None, database_operations=None,
                 sql_mysql=None, reverse_sql_mysql=None):
        super().__init__(state_operations=state_operations,
                         database_operations=database_operations)
        self.sql_mysql = sql_mysql
        self.reverse_sql_mysql = reverse_sql_mysql

    def deconstruct(self):
        name, args, kwargs = super().deconstruct()
        if self.sql_mysql is not None:
            kwargs['sql_mysql'] = self.sql_mysql
        if self.reverse_sql_mysql is not None:
            kwargs['reverse_sql_mysql'] = self.reverse_sql_mysql
        return name, args, kwargs

    # ── forward ────────────────────────────────────────────────────────────
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == 'mysql':
            # Comportement historique : database_operations (généralement vide),
            # plus l'éventuel SQL MySQL spécifique — inchangé octet pour octet.
            super().database_forwards(app_label, schema_editor, from_state, to_state)
            if self.sql_mysql:
                # params=None : évite l'interpolation '%' de MySQLdb sur un SQL
                # contenant un '%' littéral (ex. LIKE '%…%') — comme RunSQL.
                schema_editor.execute(self.sql_mysql, params=None)
            return
        # Base neuve non-MySQL : rejouer physiquement les state_operations,
        # en recalculant les états intermédiaires comme le ferait un
        # Migration.apply standard.
        state = from_state
        for op in self.state_operations:
            new_state = state.clone()
            op.state_forwards(app_label, new_state)
            op.database_forwards(app_label, schema_editor, state, new_state)
            state = new_state

    # ── backward ───────────────────────────────────────────────────────────
    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == 'mysql':
            super().database_backwards(app_label, schema_editor, from_state, to_state)
            if self.reverse_sql_mysql:
                schema_editor.execute(self.reverse_sql_mysql, params=None)
            return
        # Recalcule la chaîne d'états forward depuis to_state (état AVANT
        # l'opération), puis rejoue chaque state_operation à l'envers.
        states = [to_state]
        state = to_state
        for op in self.state_operations:
            state = state.clone()
            op.state_forwards(app_label, state)
            states.append(state)
        for i in range(len(self.state_operations) - 1, -1, -1):
            self.state_operations[i].database_backwards(
                app_label, schema_editor, states[i + 1], states[i]
            )
