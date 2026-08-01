"""
golden_extract — photographie READ-ONLY des invariants métier + digests de tables.

Harnais golden-dataset de la migration MySQL → PostgreSQL (Phase 3 du plan
docs/migration_postgres.md). Produit un JSON comparable par golden_diff :

Usage :
    python manage.py golden_extract --out mysql.json --label mysql     # sur MySQL staging
    python manage.py golden_extract --out pg.json    --label pg        # sur PG migré
    python manage.py golden_extract --out f.json --label pg --skip-recalc
    python manage.py golden_extract --out f.json --label pg --tables absence_etudiant,em

    python manage.py golden_diff mysql.json pg.json                    # DOIT être vide

Garanties :
  - AUCUNE écriture persistée : toute la commande tourne dans une transaction
    annulée (transaction.set_rollback(True) avant la sortie du bloc atomic).
    Le rejeu de la chaîne de recalcul est en plus isolé dans un atomic imbriqué
    annulé lui aussi, pour que le persisté / les digests soient lus dans l'état
    d'origine de la base. L'audit (core.audit_helpers) passe par
    transaction.on_commit → inerte en rollback.
  - AUCUN timestamp hors de '_meta' (clé ignorée par golden_diff, comme 'label').
  - date_calcul (auto_now) jamais extrait dans les invariants.
  - INTERDITS respectés : pas de _generer_pdf, pas de _creer_document_officiel,
    pas de NumeroSerieConfig.generer_prochain(), pas d'attribuer_diplomes_pv.

Structure du JSON produit :
    {'label': ..., '_meta': {...},
     'invariants': {'resultats_persistes': ..., 'resultats_recalcules': ...,
                    'pv_deliberation': ..., 'documents_officiels': ...,
                    'registre_diplomes': ...},
     'digests': {table: {'count': n, 'sha256_sorted': [...]}},
     'order_signatures': {nom: {'count': n, 'sha256': ...}}}
"""
import datetime
import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

# ── Canonicalisation (module-level : importable par les tests et golden_diff) ──

#: Sentinelle NULL — non confondable avec une chaîne vide ou 'None' littéral.
NULL_SENTINEL = '\x00NULL'

#: Séparateur de colonnes dans le tuple canonique d'une ligne.
SEP = '\x1f'


def canon_value(v):
    """Canonicalise UNE valeur scalaire vers une chaîne stable inter-moteurs.

    Règles (cf. docs/migration_postgres.md §Phase 3) :
      - None → sentinelle ; bool → '0'/'1' (testé AVANT int : bool est un int) ;
      - Decimal → str après quantize(0.0001) ; float → repr canonique (10 déc.) ;
      - datetime aware → UTC ISO µs ; datetime naïf → 'naive:' + ISO ;
      - date/time → ISO ; UUID → str tirets minuscules ; bytes → sha256 hex ;
      - dict/list → json trié ; str → BRUT (on VEUT voir les diffs de casse).
    """
    if v is None:
        return NULL_SENTINEL
    if isinstance(v, bool):                      # AVANT int (bool ⊂ int)
        return '1' if v else '0'
    if isinstance(v, Decimal):
        try:
            return str(v.quantize(Decimal('0.0001')))
        except InvalidOperation:                 # Decimal hors gabarit — brut
            return str(v)
    if isinstance(v, float):
        return repr(round(v, 10))
    if isinstance(v, int):
        return str(v)
    if isinstance(v, datetime.datetime):         # AVANT date (datetime ⊂ date)
        if timezone.is_aware(v):
            return v.astimezone(datetime.timezone.utc).isoformat(timespec='microseconds')
        return 'naive:' + v.isoformat(timespec='microseconds')
    if isinstance(v, (datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)                            # forme tiretée minuscule
    if isinstance(v, (bytes, bytearray, memoryview)):
        return hashlib.sha256(bytes(v)).hexdigest()
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, ensure_ascii=False)
    if isinstance(v, str):
        return v
    return str(v)


def canon_jsonable(obj):
    """Canonicalise récursivement une structure (dict/list/scalaires) pour
    stockage JSON : les conteneurs sont préservés (chemins de diff lisibles),
    les feuilles passent par canon_value()."""
    if isinstance(obj, dict):
        return {str(k): canon_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [canon_jsonable(v) for v in obj]
    return canon_value(obj)


def row_hash(values):
    """sha256 hex du tuple canonique d'une ligne."""
    payload = SEP.join(canon_value(v) for v in values)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def digest_model(model, chunk_size=2000):
    """Digest ligne-à-ligne d'un modèle : count + liste TRIÉE des sha256 du
    tuple canonique de chaque ligne (tous les champs concrets triés par nom).

    Itère par chunks (indispensable pour core_audit_log). Ne dépend JAMAIS de
    l'ordre SQL du moteur : la liste est triée en Python."""
    fields = sorted(f.attname for f in model._meta.concrete_fields)
    qs = model._base_manager.all().order_by().values_list(*fields)
    hashes = []
    for row in qs.iterator(chunk_size=chunk_size):
        hashes.append(row_hash(row))
    hashes.sort()
    return {'count': len(hashes), 'sha256_sorted': hashes}


# ── Clés naturelles ─────────────────────────────────────────────────────────────

def _cle_session(session):
    """Clé naturelle d'une SessionEvaluation (UT institution/année/type/parité)."""
    acro = session.institution.acronyme if session.institution_id else ''
    annee = session.annee_univ.annee if session.annee_univ_id else ''
    return f'{acro}|{annee}|{session.type_session}|{session.type_semestre}'


def _cle_inscription_ped(ip):
    """Clé naturelle (matricule, code_semestre, annee, acronyme) d'une IP."""
    ia = ip.inscription_admin
    acro = ia.institution.acronyme if ia.institution_id else ''
    annee = ia.annee_univ.annee if ia.annee_univ_id else ''
    return f'{ia.etudiant.matricule}|{ip.semestre.code_semestre}|{annee}|{acro}'


def _cle_element(ie):
    """Clé naturelle d'une InscriptionElement : code LMD ou code EM planif."""
    if ie.element_id:
        return f'el:{ie.element.code}'
    if ie.em_id:
        return f'em:{ie.em.code_em}'
    return f'ie:{ie.pk}'


def _inserer_sans_collision(bucket, cle, valeur, pk, pks):
    """Insère valeur sous cle ; en cas de collision de clé naturelle (rare :
    (inscription_ped, em) n'est pas unique), TOUTES les entrées du groupe sont
    suffixées '#<pk>' — y compris la première, re-clé rétroactivement via le
    registre `pks` (dict {cle: pk_de_la_première_entrée}, un par bucket).
    Résultat indépendant de l'ordre renvoyé par le moteur (les querysets
    appelants sont en plus .order_by('pk')) ; les pk sont copiés à l'identique
    par pgloader donc stables inter-moteurs."""
    if cle in pks:
        premier_pk = pks[cle]
        if premier_pk is not None:           # 1re collision : re-clé l'entrée initiale
            bucket[f'{cle}#{premier_pk}'] = bucket.pop(cle)
            pks[cle] = None                  # groupe déjà éclaté en '#<pk>'
        bucket[f'{cle}#{pk}'] = valeur
    else:
        pks[cle] = pk
        bucket[cle] = valeur


class Command(BaseCommand):
    help = ("Extrait un snapshot canonique (invariants métier + digests de tables) "
            "pour le diff de non-régression MySQL vs PostgreSQL. READ-ONLY.")

    def add_arguments(self, parser):
        parser.add_argument('--out', required=True,
                            help='Chemin du fichier JSON de sortie.')
        parser.add_argument('--label', required=True,
                            help="Étiquette du run (ex: 'mysql' ou 'pg').")
        parser.add_argument('--skip-recalc', action='store_true', default=False,
                            help='Ne rejoue pas la chaîne de recalcul '
                                 '(digests + valeurs persistées seulement).')
        parser.add_argument('--tables', type=str, default='',
                            help='Restreint les digests ligne-à-ligne à ces tables '
                                 '(liste CSV de db_table, debug).')

    def handle(self, *args, **opts):
        from django.db import connection

        tables_filtre = {t.strip() for t in opts['tables'].split(',') if t.strip()}

        data = {
            'label': opts['label'],
            '_meta': {                              # SEULE section horodatée — ignorée par golden_diff
                'genere_le': timezone.now().astimezone(datetime.timezone.utc).isoformat(),
                'moteur': connection.vendor,
                'options': {'skip_recalc': opts['skip_recalc'],
                            'tables': sorted(tables_filtre)},
            },
        }

        # Ceinture + bretelles : TOUTE la commande dans une transaction annulée.
        # Même une écriture accidentelle (ex: get_or_create d'un singleton de
        # paramétrage pendant un recalcul) ne persiste jamais. Bonus : snapshot
        # transactionnel cohérent de la base pendant toute l'extraction.
        with transaction.atomic():
            self.stdout.write(self.style.MIGRATE_HEADING('=== GOLDEN EXTRACT (read-only) ==='))

            invariants = {}
            self.stdout.write('  [1/5] Invariants persistés (résultats)…')
            invariants['resultats_persistes'] = self._snapshot_resultats()

            if not opts['skip_recalc']:
                self.stdout.write('  [2/5] Rejeu de la chaîne canonique (transaction annulée)…')
                invariants['resultats_recalcules'] = self._resultats_recalcules()
            else:
                self.stdout.write('  [2/5] Recalcul sauté (--skip-recalc).')

            self.stdout.write('  [3/5] PV / lignes de délibération / obligations…')
            invariants['pv_deliberation'] = self._extraire_pv()

            self.stdout.write('  [4/5] Documents officiels + registre des diplômes…')
            invariants['documents_officiels'] = self._extraire_documents()
            invariants['registre_diplomes'] = self._extraire_registre_diplomes()

            data['invariants'] = invariants

            self.stdout.write('  [5/5] Digests ligne-à-ligne + signatures d\'ordre…')
            data['digests'] = self._digests_tables(tables_filtre)
            data['order_signatures'] = self._signatures_ordre()

            # READ-ONLY : rien de ce qui précède ne doit survivre à la commande.
            transaction.set_rollback(True)

        with open(opts['out'], 'w', encoding='utf-8') as fh:
            json.dump(data, fh, sort_keys=True, ensure_ascii=False, indent=1)

        # Skips NON silencieux : liste explicite des digests / signatures
        # marqués 'skipped' (table absente, erreur SQL confinée en savepoint).
        # golden_diff les traite comme écart sauf s'ils sont identiques des 2 côtés.
        skips_digests = sorted(t for t, d in data['digests'].items() if d.get('skipped'))
        skips_sigs = sorted(n for n, s in data['order_signatures'].items() if s.get('skipped'))
        if skips_digests:
            self.stderr.write(self.style.WARNING(
                f"ATTENTION : {len(skips_digests)} digest(s) de table skipped : "
                f"{', '.join(skips_digests)}"
            ))
        if skips_sigs:
            self.stderr.write(self.style.WARNING(
                f"ATTENTION : {len(skips_sigs)} signature(s) d'ordre skipped : "
                f"{', '.join(skips_sigs)}"
            ))

        nb_tables = len(data['digests'])
        self.stdout.write(self.style.SUCCESS(
            f"GOLDEN EXTRACT: OK — label='{opts['label']}', {nb_tables} table(s) digérée(s), "
            f"sortie: {opts['out']}"
        ))

    # ── (A) Invariants métier ────────────────────────────────────────────────────

    def _snapshot_resultats(self):
        """Valeurs persistées des Resultat{Semestre,Module,Element}, indexées par
        clé naturelle (matricule|code_semestre|annee|acronyme) puis clé de session.
        date_calcul (auto_now) volontairement EXCLU."""
        from apps.evaluations.models import (
            ResultatElement, ResultatModule, ResultatSemestre,
        )

        data = {}

        rs_qs = ResultatSemestre.objects.select_related(
            'inscription_ped__inscription_admin__etudiant',
            'inscription_ped__inscription_admin__annee_univ',
            'inscription_ped__inscription_admin__institution',
            'inscription_ped__semestre',
            'session__annee_univ', 'session__institution',
        )
        for rs in rs_qs.iterator(chunk_size=1000):
            cle = _cle_inscription_ped(rs.inscription_ped)
            scle = _cle_session(rs.session)
            data.setdefault(cle, {}).setdefault(scle, {})['semestre'] = {
                'moyenne':         canon_value(rs.moyenne),
                'credits_valides': canon_value(rs.credits_valides),
                'est_admis':       canon_value(rs.est_admis),
                'code_statut':     rs.code_statut,
            }

        rm_qs = ResultatModule.objects.select_related(
            'inscription_ped__inscription_admin__etudiant',
            'inscription_ped__inscription_admin__annee_univ',
            'inscription_ped__inscription_admin__institution',
            'inscription_ped__semestre', 'module',
            'session__annee_univ', 'session__institution',
        )
        for rm in rm_qs.iterator(chunk_size=1000):
            cle = _cle_inscription_ped(rm.inscription_ped)
            scle = _cle_session(rm.session)
            modules = (data.setdefault(cle, {}).setdefault(scle, {})
                           .setdefault('modules', {}))
            modules[rm.module.code] = {
                'moyenne':         canon_value(rm.moyenne),
                'code_statut':     rm.code_statut,
                'credits_valides': canon_value(rm.credits_valides),
                'est_valide':      canon_value(rm.est_valide),
                'a_eliminatoire':  canon_value(rm.a_eliminatoire),
            }

        re_qs = ResultatElement.objects.select_related(
            'inscription_element__element', 'inscription_element__em',
            'inscription_element__inscription_ped__inscription_admin__etudiant',
            'inscription_element__inscription_ped__inscription_admin__annee_univ',
            'inscription_element__inscription_ped__inscription_admin__institution',
            'inscription_element__inscription_ped__semestre',
            'session__annee_univ', 'session__institution',
        ).order_by('pk')                     # parcours déterministe inter-moteurs
        pks_elements = {}                    # (cle, scle) → registre pk du bucket 'elements'
        for re_ in re_qs.iterator(chunk_size=1000):
            ie = re_.inscription_element
            cle = _cle_inscription_ped(ie.inscription_ped)
            scle = _cle_session(re_.session)
            elements = (data.setdefault(cle, {}).setdefault(scle, {})
                            .setdefault('elements', {}))
            _inserer_sans_collision(elements, _cle_element(ie), {
                'note_finale':      canon_value(re_.note_finale),
                'code_statut':      re_.code_statut,
                'est_valide':       canon_value(re_.est_valide),
                'est_eliminatoire': canon_value(re_.est_eliminatoire),
            }, ie.pk, pks_elements.setdefault((cle, scle), {}))

        return data

    def _resultats_recalcules(self):
        """Rejoue la chaîne canonique de pv_actions._recalculer_chaine
        (éléments → purge modules → modules → semestres) pour CHAQUE session,
        relit le snapshot AVANT rollback, puis annule tout.

        Ordre des sessions : année / institution / parité / type_session — le tri
        alphabétique 'normale' < 'rattrapage' reproduit l'ordre exigé par
        l'Art. 18 (la session normale est recalculée avant sa rattrapage)."""
        from apps.evaluations.models import ResultatModule, SessionEvaluation
        from apps.evaluations.services.calcul_notes import NoteCalculService

        with transaction.atomic():
            sessions = list(
                SessionEvaluation.objects
                .select_related('annee_univ', 'institution')
                .order_by('annee_univ__annee', 'institution__acronyme',
                          'type_semestre', 'type_session', 'pk')
            )
            for session in sessions:
                svc = NoteCalculService(session)
                svc.calculer_tous_elements_session()
                ResultatModule.objects.filter(session=session).delete()
                svc.calculer_tous_modules_session()
                svc.calculer_tous_semestres_session()

            snapshot = self._snapshot_resultats()

            # Annule TOUTES les écritures du rejeu (l'audit est sur on_commit
            # → jamais déclenché ; date_calcul auto_now → jamais persisté).
            transaction.set_rollback(True)

        return snapshot

    def _extraire_pv(self):
        """PV / LigneDeliberation / ObligationRattrapage — valeurs PERSISTÉES
        telles quelles (rang inclus : jamais écrit par les services, saisie API)."""
        from apps.evaluations.models import PVDeliberation

        pvs = {}
        pks_pvs = {}                         # registre pk du bucket 'pvs'
        pv_qs = PVDeliberation.objects.select_related(
            'session__annee_univ', 'session__institution',
            'annee_univ', 'filiere', 'institution',
        ).order_by('pk')
        for pv in pv_qs.iterator(chunk_size=200):
            if pv.session_id:
                scope = f'session:{_cle_session(pv.session)}'
            elif pv.annee_univ_id:
                scope = f'annee:{pv.annee_univ.annee}'
            else:
                scope = 'scope:?'
            cle = '|'.join([
                pv.type_pv,
                pv.institution.acronyme if pv.institution_id else '',
                pv.filiere.code if pv.filiere_id else '',
                str(pv.niveau),
                pv.semestre_code or '',
                scope,
            ])

            lignes = {}
            pks_lignes = {}                  # registre pk du bucket 'lignes' de CE pv
            ligne_qs = pv.lignes.select_related(
                'inscription_admin__etudiant',
            ).prefetch_related(
                'obligations_rattrapage__inscription_element__em',
                'obligations_rattrapage__inscription_element__element',
            ).order_by('pk')                 # parcours déterministe inter-moteurs
            for ligne in ligne_qs:
                obligations = sorted(
                    '|'.join([
                        _cle_element(obl.inscription_element),
                        obl.type_obligation,
                        obl.code_statut_initial,
                    ])
                    for obl in ligne.obligations_rattrapage.all()
                )
                _inserer_sans_collision(
                    lignes,
                    ligne.inscription_admin.etudiant.matricule,
                    {
                        'decision':            ligne.decision,
                        'decision_annuelle':   ligne.decision_annuelle,
                        'credits_annuels':     canon_value(ligne.credits_annuels),
                        'moyenne_annuelle':    canon_value(ligne.moyenne_annuelle),
                        'taux_capitalisation': canon_value(ligne.taux_capitalisation),
                        'verrou_passage':      canon_value(ligne.verrou_passage),
                        'rang':                canon_value(ligne.rang),
                        'code_statut':         ligne.code_statut,
                        'obligations':         obligations,
                    },
                    ligne.pk,
                    pks_lignes,
                )

            _inserer_sans_collision(pvs, cle, {
                'est_clos': canon_value(pv.est_clos),
                'lignes':   lignes,
            }, pv.pk, pks_pvs)

        return pvs

    def _extraire_documents(self):
        """DocumentOfficiel (clé numero_serie) : métadonnées d'authenticité + les
        données DÉTERMINISTES du contexte régénéré par _build_context_releve
        (jamais d'images/base64/PDF) + l'URL QR textuelle.

        On n'appelle NI _generer_pdf NI _creer_document_officiel NI
        generer_prochain() : lecture pure."""
        from apps.documents.models import DocumentOfficiel
        from apps.documents.services import _build_context_releve

        # Clés du contexte à exclure : non déterministes ou binaires.
        EXCLUS = {'photo_b64', 'logo_b64'}

        base_url = (getattr(settings, 'DOCUMENTS_BASE_URL', '') or '').rstrip('/')

        docs = {}
        doc_qs = DocumentOfficiel.objects.select_related(
            'etudiant', 'institution', 'semestre',
        ).order_by('numero_serie')
        for doc in doc_qs.iterator(chunk_size=200):
            token = str(doc.token_verification)
            entree = {
                'numero_serie':        doc.numero_serie,
                'hash_sha256':         doc.hash_sha256,
                'token_verification':  token,
                'type_document':       doc.type_document,
                'annee_universitaire': doc.annee_universitaire,
                'matricule':           doc.etudiant.matricule,
                'est_valide':          canon_value(doc.est_valide),
                # Même construction d'URL que _get_qr_base64 (services.py:515)
                # — on extrait le TEXTE encodé dans le QR, pas l'image.
                'qr_url': (f'{base_url}/verifier/{token}' if base_url
                           else f'/verifier/{token}'),
            }
            if doc.type_document in ('releve_semestre', 'releve_complet'):
                try:
                    # Savepoint imbriqué (même pattern que _digests_tables) :
                    # sans lui, une DatabaseError levée ici empoisonnerait la
                    # transaction atomic globale et tuerait TOUT l'extract.
                    with transaction.atomic():
                        ctx = _build_context_releve(doc, doc.etudiant, doc.institution, {})
                    entree['contexte_releve'] = canon_jsonable(
                        {k: v for k, v in ctx.items() if k not in EXCLUS}
                    )
                except Exception as exc:  # robustesse : un doc corrompu ne bloque pas l'extract
                    entree['contexte_releve'] = {'erreur': type(exc).__name__}
            docs[doc.numero_serie] = entree

        return docs

    def _extraire_registre_diplomes(self):
        """RegistreDiplome : liste ordonnée par numero_diplome (unique → ordre
        déterministe, indépendant de la collation)."""
        from apps.documents.models import RegistreDiplome

        registre = []
        rd_qs = RegistreDiplome.objects.select_related('etudiant').order_by('numero_diplome')
        for rd in rd_qs.iterator(chunk_size=500):
            registre.append([
                rd.etudiant.matricule,
                rd.numero_diplome,
                rd.mention,
                canon_value(rd.moyenne_generale),
                rd.date_delivrance.isoformat() if rd.date_delivrance else NULL_SENTINEL,
            ])
        return registre

    # ── (B) Digests ligne-à-ligne ────────────────────────────────────────────────

    def _digests_tables(self, tables_filtre):
        """Digest de CHAQUE table adossée à un modèle Django (M2M auto-créées et
        managed=False incluses). Les tables absentes (ex: managed=False non créée
        en sqlite de test) sont marquées 'skipped' — l'échec SQL est confiné dans
        un savepoint pour ne pas empoisonner la transaction englobante."""
        digests = {}
        for model in apps.get_models(include_auto_created=True):
            table = model._meta.db_table
            if tables_filtre and table not in tables_filtre:
                continue
            try:
                with transaction.atomic():
                    digests[table] = digest_model(model)
            except Exception:
                digests[table] = {'count': None, 'sha256_sorted': [], 'skipped': True}
        return digests

    # ── Signatures d'ordre (collation MySQL vs PG) ──────────────────────────────

    def _signatures_ordre(self):
        """Pour chaque queryset triant sur du texte (vues emplois/suivi/absence +
        Meta.ordering, cf. rapport golden_targets §4) : sha256 de la liste
        ORDONNÉE des clés naturelles telle que le moteur la renvoie.

        Un tiebreaker unique (pk / matricule) est ajouté en fin d'order_by pour
        rendre la signature déterministe sur les ex æquo — les différences de
        collation sur les champs texte primaires restent pleinement visibles."""
        sigs = {}
        for nom, fabrique in self._specs_ordre():
            try:
                with transaction.atomic():
                    lignes = [SEP.join(canon_value(v) for v in row) for row in fabrique()]
                sigs[nom] = {
                    'count':  len(lignes),
                    'sha256': hashlib.sha256('\n'.join(lignes).encode('utf-8')).hexdigest(),
                }
            except Exception:
                sigs[nom] = {'count': None, 'sha256': '', 'skipped': True}
        return sigs

    @staticmethod
    def _specs_ordre():
        """Liste (nom, callable → values_list) des signatures d'ordre."""
        from apps.absence.models import Etudiant
        from apps.em.models import EM
        from apps.parametres.models import (
            Creneau, Jour, Niveau, Seance, Semestre, Year,
        )
        from apps.prof.models import Prof
        from apps.suivi.models import Suivie

        return [
            # emplois/views.py:670 — créneaux actifs (ordre, puis texte)
            ('creneau_actifs', lambda: Creneau.objects.filter(is_actif=True)
                .order_by('ordre', 'creneau', 'pk').values_list('creneau', 'pk')),
            # suivi/views.py:741 — état de paiement (prof.nom, semestre.semestre)
            ('suivi_prof_nom_semestre', lambda: Suivie.objects
                .order_by('prof__nom', 'semestre__semestre', 'pk')
                .values_list('prof__nom', 'semestre__semestre', 'pk')),
            # suivi/views.py:1519 — EM par code_em (aussi Meta.ordering de EM)
            ('em_code_em', lambda: EM.objects.order_by('code_em', 'pk')
                .values_list('code_em', 'pk')),
            # absence/views.py:227,383,988 — étudiants par matricule (unique)
            ('etudiant_matricule', lambda: Etudiant.objects
                .order_by('matricule').values_list('matricule')),
            # absence/views.py:1068 + Meta.ordering ['nom'] d'Etudiant
            ('etudiant_nom', lambda: Etudiant.objects
                .order_by('nom', 'matricule').values_list('nom', 'matricule')),
            # absence/views.py:954 — fiches présence (jour, créneau)
            ('suivi_jour_creneau', lambda: Suivie.objects
                .order_by('jour_fk__jour', 'creneau_fk__creneau', 'pk')
                .values_list('jour_fk__jour', 'creneau_fk__creneau', 'pk')),
            # Meta.ordering de Suivie ['-annee_universitaire', '-numero_semaine']
            ('suivi_meta_ordering', lambda: Suivie.objects
                .order_by('-annee_universitaire', '-numero_semaine', 'pk')
                .values_list('annee_universitaire', 'numero_semaine', 'pk')),
            # Meta.ordering de parametres.* (champs texte uniques)
            ('seance_type_seance', lambda: Seance.objects
                .order_by('type_seance').values_list('type_seance')),
            ('semestre_code', lambda: Semestre.objects
                .order_by('code_semestre', 'pk').values_list('code_semestre', 'pk')),
            ('year_annee_desc', lambda: Year.objects
                .order_by('-annee').values_list('annee')),
            ('niveau', lambda: Niveau.objects.order_by('niveau').values_list('niveau')),
            ('jour', lambda: Jour.objects.order_by('jour').values_list('jour')),
            # Meta.ordering de Prof ['nom'] — consommé par les vues suivi/emplois
            ('prof_nom', lambda: Prof.objects.order_by('nom', 'pk')
                .values_list('nom', 'pk')),
        ]
