"""
Les relevés de notes d'un ou de plusieurs étudiants, tous leurs semestres,
en un seul PDF — même adresse et même réponse qu'à l'ISS
(`documents/officiels/releves-etudiant/`), contenu de l'IPGEI.

La génération en lot (`ipgei/documents.py`, `editer_en_lot`) fait un relevé
par élève pour UN semestre. Ici c'est l'inverse : un élève, chacun de ses
semestres (toutes ses années à l'IPGEI), dans l'ordre (année puis semestre),
fusionnés en un document à imprimer d'un coup.

Chaque relevé est un document officiel IPGEI (`generer_releve_semestre` :
numéro de série, QR, registre). Règle de l'IPGEI : un document est RÉÉMIS à
chaque tirage, avec son propre numéro — on ne réutilise pas un relevé déjà émis.

Un semestre sans note retenue n'est pas imprimé : sa moyenne serait vide, et
le relevé passerait pour un échec. L'aperçu le dit.
"""
from io import BytesIO

TYPE = 'ipgei_releve_semestre'

# Au-delà, un seul PDF devient lourd à produire et à imprimer : on scinde.
MAX_ETUDIANTS = 60


def _inscriptions(etudiant):
    from apps.ipgei.models import InscriptionIPGEI
    return list(InscriptionIPGEI.objects.filter(etudiant=etudiant, actif=True)
                .select_related('classe__institution', 'etudiant')
                .order_by('annee_universitaire'))


def _semestres(inscription):
    """Les semestres de l'année où l'élève a au moins une ligne de notes."""
    from apps.ipgei.models import Note, SemestreIPGEI
    ids = set(Note.objects.filter(inscription=inscription).values_list('semestre_id', flat=True))
    return list(SemestreIPGEI.objects.filter(pk__in=ids).order_by('code'))


def a_des_notes(inscription, semestre):
    """Au moins une note RETENUE sur ce semestre."""
    from apps.ipgei.models import Note
    return Note.objects.filter(inscription=inscription, semestre=semestre,
                               note_retenue__isnull=False).exists()


def semestres_de(etudiant):
    """Ses semestres, du plus ancien au plus récent, avec la moyenne que
    porterait le relevé. Lecture seule."""
    from apps.ipgei import services
    from apps.ipgei.models import Deliberation, LigneDeliberation

    lignes = []
    for inscription in _inscriptions(etudiant):
        for semestre in _semestres(inscription):
            resultats = a_des_notes(inscription, semestre)
            moyenne = None
            if resultats:
                moyenne, _, _ = services.moyenne_semestrielle(inscription, semestre)
            jury = (LigneDeliberation.objects
                    .filter(inscription=inscription, deliberation__semestre=semestre,
                            deliberation__statut=Deliberation.STATUT_VALIDEE)
                    .order_by('-deliberation__pk').first())
            lignes.append({
                'annee_universitaire': inscription.annee_universitaire,
                'semestre': semestre.pk,
                'semestre_code': semestre.code,
                'filiere': inscription.classe.nom,
                'inscription': inscription.pk,
                'moyenne': float(moyenne) if moyenne is not None else None,
                'decision': (jury.get_decision_display() or None) if jury and resultats else None,
                'a_des_resultats': resultats and moyenne is not None,
            })
    lignes.sort(key=lambda l: (l['annee_universitaire'], l['semestre_code']))
    return lignes


def _ajouter_releves(writer, etudiant, user):
    """Ajoute au PDF les relevés de l'élève qui ont des résultats ; rend ses
    lignes (avec `imprime` et `numero_serie`) et le nombre ajouté."""
    from django.conf import settings
    from django.core.exceptions import ValidationError

    from apps.ipgei import documents as docs_ipgei
    from apps.ipgei.models import InscriptionIPGEI, SemestreIPGEI

    base_url = getattr(settings, 'DOCUMENTS_BASE_URL', '').rstrip('/')
    lignes, nb = semestres_de(etudiant), 0
    for l in lignes:
        if not l['a_des_resultats']:
            continue
        inscription = InscriptionIPGEI.objects.select_related('classe__institution', 'etudiant').get(
            pk=l['inscription'])
        semestre = SemestreIPGEI.objects.get(pk=l['semestre'])
        try:
            doc, pdf = docs_ipgei.generer_releve_semestre(
                inscription, semestre, utilisateur=user, base_url=base_url)
        except ValidationError as exc:
            l['imprime'] = False
            l['erreur'] = (getattr(exc, 'messages', None) or [str(exc)])[0]
            continue
        l['imprime'] = bool(pdf)
        if pdf:
            writer.append(BytesIO(pdf))
            l['numero_serie'] = doc.numero_serie
            nb += 1
    return lignes, nb


def generer_releves_etudiants(etudiants, user):
    """(pdf_bytes, bilan) : les relevés de PLUSIEURS élèves, l'un après
    l'autre dans l'ordre donné, en un seul PDF. Un élève sans aucun résultat
    est sauté et signalé dans le bilan, sans bloquer les autres.
    Lève ValueError si rien n'est imprimable."""
    from pypdf import PdfWriter

    if len(etudiants) > MAX_ETUDIANTS:
        raise ValueError(f'{len(etudiants)} étudiants : {MAX_ETUDIANTS} au plus par impression.')
    writer, bilan, total = PdfWriter(), [], 0
    for etudiant in etudiants:
        lignes, nb = _ajouter_releves(writer, etudiant, user)
        bilan.append({'etudiant': etudiant.pk, 'matricule': etudiant.matricule,
                      'releves': nb, 'semestres': lignes})
        total += nb
    if total == 0:
        noms = ', '.join(e.matricule for e in etudiants)
        raise ValueError(f'Aucun semestre avec des résultats ({noms}) : aucun relevé à imprimer.')
    out = BytesIO()
    writer.write(out)
    writer.close()
    return out.getvalue(), bilan


def generer_releves_etudiant(etudiant, user):
    """(pdf_bytes, lignes) pour un seul élève."""
    pdf, bilan = generer_releves_etudiants([etudiant], user)
    return pdf, bilan[0]['semestres']
