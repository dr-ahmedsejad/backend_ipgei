"""
Le portail étudiant, branché sur le moteur IPGEI.

CE QUE CE MODULE CORRIGE. Le portail lisait la couche LMD du socle —
`InscriptionPedagogique`, `InscriptionElement`, `ResultatSemestre` — que l'IPGEI
a remplacée. Ces tables sont VIDES : 0 inscription pédagogique, 0 résultat, face
à 441 inscriptions IPGEI et 5 580 notes. Un étudiant qui se connectait voyait
donc ses notes vides, ses résultats vides, son emploi du temps vide — sans la
moindre erreur pour l'en avertir.

Personne ne s'en était aperçu parce qu'aucun compte étudiant n'existe : 298
étudiants, 1 compte, et celui-là n'est rattaché à personne.

CE QUE CE MODULE NE FAIT PAS. Il ne réécrit pas les écrans : les formes rendues
restent celles que le frontend attend déjà. Seule la SOURCE change.

Deux tables du socle restent lues telles quelles, et c'est volontaire : `Suivie`
et `EM` sont alimentées par la projection IPGEI (`apps/ipgei/miroir.py`) et
contiennent de vraies données — 270 et 34 lignes. Les rebrancher n'aurait rien
apporté qu'un risque.
"""
from decimal import Decimal

from apps.ipgei.models import (
    Deliberation, InscriptionIPGEI, LigneDeliberation, Note, SemestreIPGEI,
)


def inscriptions_de(etudiant, annee: str = None):
    """
    Les inscriptions IPGEI d'un étudiant, la plus récente d'abord.

    Un étudiant en a une par année — le portail affiche donc plusieurs années,
    et l'écran choisit laquelle. Filtrer sur `annee` rend l'inscription unique.
    """
    qs = (InscriptionIPGEI.objects
          .filter(etudiant=etudiant, actif=True)
          .select_related('classe__institution', 'classe__niveau', 'etudiant')
          .order_by('-annee_universitaire'))
    return qs.filter(annee_universitaire=annee) if annee else qs


def inscription_courante(etudiant, annee: str = None):
    """L'inscription visée, ou la plus récente. `None` si l'étudiant n'en a pas."""
    return inscriptions_de(etudiant, annee).first()


def jury_a_statue(inscription, semestre=None) -> bool:
    """
    Un jury VALIDÉ a-t-il statué sur ce dossier ?

    C'est la règle de visibilité que portait l'ancien portail — « visible
    uniquement après clôture du PV ». Elle compte : un étudiant qui lit ses
    moyennes avant le jury, c'est un guichet qui se remplit de réclamations sur
    des chiffres qui ne sont pas encore des décisions.

    Un jury CALCULÉ mais non signé n'a rien décidé : seul `STATUT_VALIDEE` fait
    foi, comme pour `services.seuil_applicable`.
    """
    lignes = LigneDeliberation.objects.filter(
        inscription=inscription,
        deliberation__statut=Deliberation.STATUT_VALIDEE,
    )
    if semestre is not None:
        lignes = lignes.filter(deliberation__semestre=semestre)
    return lignes.exists()


def notes_publiables(inscription):
    """
    Les notes que l'étudiant peut voir : celles des semestres dont le jury a
    statué.

    On filtre par SEMESTRE et non en bloc : le S1 peut être délibéré quand le S2
    ne l'est pas, et masquer les deux pour cette raison priverait l'étudiant de
    ce qui est pourtant arrêté.
    """
    ouverts = set(
        LigneDeliberation.objects
        .filter(inscription=inscription,
                deliberation__statut=Deliberation.STATUT_VALIDEE,
                deliberation__semestre__isnull=False)
        .values_list('deliberation__semestre_id', flat=True)
    )
    # Une délibération ANNUELLE ouvre les deux semestres de son année : elle
    # statue sur la moyenne annuelle, qui les résume tous deux.
    annuelles = LigneDeliberation.objects.filter(
        inscription=inscription,
        deliberation__statut=Deliberation.STATUT_VALIDEE,
        deliberation__portee=Deliberation.PORTEE_ANNUELLE,
    ).exists()
    if annuelles:
        ouverts |= set(
            SemestreIPGEI.objects
            .filter(annee_universitaire=inscription.annee_universitaire)
            .values_list('id', flat=True))

    return (Note.objects
            .filter(inscription=inscription, semestre_id__in=ouverts)
            .select_related('matiere', 'semestre')
            .order_by('semestre__code', 'matiere__ordre', 'matiere__code'))


def _nombre(valeur):
    """`Decimal` → `float` pour le JSON, `None` conservé."""
    return float(valeur) if valeur is not None else None


def note_en_ligne(note, seuil: Decimal) -> dict:
    """
    Une note IPGEI dans la forme que l'écran du portail attend déjà.

    Le vocabulaire diffère — le socle dit « CC », l'IPGEI dit « DS » ; le socle
    compte des crédits, l'IPGEI des coefficients. On projette sans renommer les
    clés : changer le contrat obligerait à retoucher des écrans qui marchent.
    """
    return {
        'id':          note.pk,
        'em':          note.matiere_id,
        'em_libelle':  f'{note.matiere.code} — {note.matiere.intitule}',
        'note_cc':     _nombre(note.moy_ds),
        # Le TP ne se montre que s'il a COMPTÉ : l'instantané le dit, et une
        # classe peut déroger (cf. `PonderationClasse`).
        'note_tp':     _nombre(note.note_tp) if note.pct_tp else None,
        'note_exam':   _nombre(note.moy_exam),
        'note_finale': _nombre(note.note_retenue),
        'valide':      (note.note_retenue is not None
                        and Decimal(note.note_retenue) >= seuil),
        # Pas de crédits à l'IPGEI : le coefficient est ce qui pèse.
        'credits':     float(note.coefficient or note.matiere.coefficient or 0),
        'semestre':    note.semestre.code,
        'annee_univ':  note.semestre.annee_universitaire,
    }


def document_deja_delivre(etudiant, type_document: str, annee: str,
                          semestre_code: str = ''):
    """
    Le document déjà au registre pour ce couple, PDF compris — s'il existe.

    L'IPGEI enregistre CHAQUE délivrance : `creer_document` fait un `create`
    inconditionnel, et c'est voulu — une pièce officielle remise deux fois
    laisse deux traces, la seconde marquée duplicata. Au guichet, où l'agent
    clique une fois, c'est la bonne règle.

    Au PORTAIL elle ne tient pas : l'étudiant rafraîchit, revient, reclique.
    Un étudiant a déjà cinq exemplaires du même relevé annuel au registre ; à
    trois cents, la table et les numéros de série s'useraient en quelques
    semaines. On ressert donc le dernier PDF produit.

    `semestre_code` est INDISPENSABLE pour un relevé de semestre. Sans lui, rien
    au registre ne séparait un relevé de S3 d'un de S4, et la réutilisation
    devait être refusée pour ces pièces-là : `DocumentOfficiel.semestre` pointe
    vers le `Semestre` du socle, que l'IPGEI n'utilise pas, et
    `creer_document` recevait un semestre sans jamais le stocker. C'est ce que
    `semestre_code` répare.

    Ne rend rien si le fichier a disparu du disque : mieux vaut régénérer que
    servir une entrée de registre sans pièce.
    """
    import os

    from apps.documents.models import DocumentOfficiel

    qs = (DocumentOfficiel.objects
          .filter(etudiant=etudiant, type_document=type_document,
                  annee_universitaire=annee, semestre_code=semestre_code,
                  est_valide=True)
          .order_by('-date_generation'))

    for doc in qs[:3]:
        try:
            chemin = doc.fichier_pdf.path if doc.fichier_pdf else None
        except ValueError:
            chemin = None
        if chemin and os.path.exists(chemin):
            return doc
    return None
