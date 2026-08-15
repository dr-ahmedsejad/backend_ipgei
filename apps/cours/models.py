"""
Module « Cours » — fil pédagogique par EM. EN LIGNE UNIQUEMENT (boîte de réception).

Un enseignant publie un cours (texte + pièces jointes : image / vidéo / lien / fichier)
rattaché à un EM et à une année. Seuls les enseignants de cet EM (via SuiviePointage)
peuvent publier ; les étudiants inscrits à cet EM pour l'année le voient et peuvent
répondre. Le rattachement (qui enseigne / qui est inscrit) est DÉRIVÉ, jamais stocké ici.

INBOX (voir portail-inbox) : ces tables sont EXCLUES de la sync (settings.SYNC_EXCLUDE_TABLE)
→ les cours vivent uniquement sur le VPS et survivent aux restaurations. Contrainte : AUCUNE
FK vers une table miroir (em, institution, user) → on stocke des `*_id` bruts + snapshot.
Les pièces jointes / réponses gardent leur FK vers Cours (inbox → inbox = autorisé).
"""
from django.db import models


TYPE_PIECE_CHOICES = [
    ('image',   'Image'),
    ('video',   'Vidéo'),
    ('lien',    'Lien'),
    ('fichier', 'Fichier'),
]


class Cours(models.Model):
    """Un post de cours publié par un enseignant pour un EM / une année (inbox, FK-free)."""
    # Cible (clé naturelle vers le miroir — pas de FK)
    em_id       = models.IntegerField(db_index=True)
    em_code     = models.CharField(max_length=50,  blank=True, default='')   # snapshot
    em_intitule = models.CharField(max_length=255, blank=True, default='')   # snapshot
    annee_universitaire = models.CharField(max_length=20, help_text="Ex. '2025-2026'.")
    institution_id = models.IntegerField(null=True, blank=True)
    # Auteur (enseignant) — id + snapshot
    auteur_id   = models.IntegerField(null=True, blank=True, db_index=True)
    auteur_nom  = models.CharField(max_length=255, blank=True, default='')
    auteur_role = models.CharField(max_length=20,  blank=True, default='')

    titre   = models.CharField(max_length=255, blank=True, default='')
    contenu = models.TextField(blank=True, default='')

    date_creation     = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'cours'
        ordering = ['-date_creation']
        verbose_name = 'Cours'
        verbose_name_plural = 'Cours'

    def __str__(self):
        return f'{self.titre or "Cours"} — EM {self.em_id} ({self.annee_universitaire})'


class CoursPieceJointe(models.Model):
    """Pièce jointe d'un cours : fichier uploadé (image/vidéo/PDF…) OU lien externe."""
    cours   = models.ForeignKey(Cours, on_delete=models.CASCADE, related_name='pieces')
    type    = models.CharField(max_length=10, choices=TYPE_PIECE_CHOICES, default='fichier')
    url     = models.URLField(max_length=1000, blank=True, default='')       # lien / vidéo externe
    fichier = models.FileField(upload_to='cours/%Y/%m/', null=True, blank=True)
    libelle = models.CharField(max_length=255, blank=True, default='')

    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'cours_piece_jointe'
        ordering = ['id']
        verbose_name = 'Pièce jointe de cours'
        verbose_name_plural = 'Pièces jointes de cours'

    def __str__(self):
        return f'{self.type} — cours {self.cours_id}'


class CoursReponse(models.Model):
    """Réponse (fil public par EM) : étudiant inscrit ou enseignant de l'EM.

    `parent` permet de répondre à une réponse précise (fil imbriqué) — ex. l'enseignant
    répond à un étudiant donné. Nul = réponse de premier niveau sur le cours."""
    cours   = models.ForeignKey(Cours, on_delete=models.CASCADE, related_name='reponses')
    parent  = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='enfants')
    # Auteur — id + snapshot (pas de FK vers user : table inbox exclue de la sync)
    auteur_id   = models.IntegerField(null=True, blank=True, db_index=True)
    auteur_nom  = models.CharField(max_length=255, blank=True, default='')
    auteur_role = models.CharField(max_length=20,  blank=True, default='')
    contenu = models.TextField()

    date_creation     = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(null=True, blank=True)   # nul = jamais modifié

    class Meta:
        db_table = 'cours_reponse'
        ordering = ['date_creation', 'id']
        verbose_name = 'Réponse de cours'
        verbose_name_plural = 'Réponses de cours'

    def __str__(self):
        return f'Réponse {self.id} — cours {self.cours_id}'
