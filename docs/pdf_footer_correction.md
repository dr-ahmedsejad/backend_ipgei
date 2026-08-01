# Coller un pied de page en bas d'un PDF (wkhtmltopdf) — piège & solution

**Date :** 2026-06-17
**Contexte :** relevé de notes officiel (`apps/documents/templates/documents/releve_notes.html`),
ligne « NB : Relevé valable après signature, délivré en un seul exemplaire » qui devait
être **collée en bas de page**, mais apparaissait au milieu / loin du bord.

---

## 1. Le symptôme

La ligne NB se retrouvait à ~281 mm du haut (≈ **15,7 mm du bas**) au lieu d'être collée
au pied. Pire : **toutes les tentatives de correction côté CSS restaient sans effet**
(« je vois pas de changement »).

## 2. La cause racine (prouvée par mesure pixel)

Le rendu PDF passe par **pdfkit / wkhtmltopdf** (`core/pdf_renderer.py`). Ce moteur
(vieux WebKit) a deux comportements qui cassent les techniques CSS habituelles :

1. **Il IGNORE une `height` fixe sur `html, body`.**
   → Un *spacer* `<td style="height:100%">` dans une table `height:100%` ne pousse
   **rien** vers le bas. Changer `body { height: 336/339/350mm }` n'avait
   **strictement aucun effet** (NB toujours à 281 mm). D'où l'impression que « rien
   ne change ».

2. **Il réduit (smart-shrink) le corps de 240 mm sur une page A4 de 210 mm à une
   échelle IMPRÉVISIBLE** (~0,83, et non 210/240 = 0,875).
   → Tout calcul de hauteur « à l'aveugle » pour compenser est faux.

3. **`position: fixed; bottom: …` est mal géré** (le bloc tombe au milieu de page).

### Comment ça a été diagnostiqué
Génération du PDF → rastérisation `pdftoppm -png -r 200` → analyse des lignes sombres
avec PIL pour mesurer en mm la position réelle du contenu. C'est la **seule** façon
fiable de savoir où atterrit un élément (l'œil sur une miniature trompe).

## 3. La solution : le pied de page NATIF de wkhtmltopdf

On n'essaie plus de positionner le NB **dans** le corps. On utilise le mécanisme
`footer-html` de wkhtmltopdf : un document HTML **séparé**, rendu indépendamment
(à la largeur RÉELLE de la page, sans le scaling du corps), placé dans la marge basse.

### Fichiers

**`apps/documents/templates/documents/_releve_footer.html`** (nouveau) — le pied :
```html
<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<style>
  body { margin: 0; font-family: Arial, sans-serif; }
  .nb { border-top: 1px solid #ccc; margin: 0 8mm; padding-top: 1.2mm;
        font-size: 8pt; color: #444; line-height: 1.2; overflow: hidden; }
  .nb .fr { float: left; }
  .nb .ar { float: right; direction: rtl; }
</style></head>
<body>
  <div class="nb">
    <span class="fr">NB : Relevé valable après signature, délivré en un seul exemplaire</span>
    <span class="ar" lang="ar">ملاحظة: كشف الدرجات صالح بعد التوقيع ويسلّم في نسخة واحدة</span>
  </div>
</body></html>
```

**`apps/documents/services.py` → `_render_pdf()`** — options, **pour le relevé uniquement** :
```python
if template_name == 'documents/releve_notes.html':
    footer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'templates', 'documents', '_releve_footer.html')
    footer_url = 'file:///' + footer_path.replace('\\', '/')   # ⚠ URL file://, pas un chemin Windows
    options['margin-bottom'] = '8mm'     # réserve la zone du pied
    options['footer-html']   = footer_url
    options['footer-spacing'] = '0'
```

**`releve_notes.html`** : on a **retiré** le `<tr class="sec-nb">`, le `<tr class="sec-spacer">`
et tous les `height: …` du body / `.page-table` (devenus inutiles, gérés par le footer).

## 4. Points de calibrage (mesurés)

| `margin-bottom` | Résultat |
|---|---|
| `14mm` | NB plus haut **+ débordement sur une 2e page vide** ❌ |
| **`8mm`** | **NB à 4,6 mm du bas, 1 seule page** ✅ |

- Le footer en **flux normal** se cale en haut de la zone `margin-bottom` ; **réduire**
  `margin-bottom` rapproche le NB du bord bas.
- N'appliquer le footer **qu'au relevé** (`if template_name == ...`), pas à l'attestation.

## 5. Pièges annexes à connaître

- **`footer-html` veut une URL `file:///` avec slashes.** Un chemin Windows à backslash
  échoue **silencieusement** (le footer ne s'affiche pas, aucune erreur).
- **`enable-local-file-access`** doit être actif (déjà le cas dans `_render_pdf`).
- **`pdftoppm` ne supprime pas les anciens PNG** : un vieux `_m-2.png` qui traîne fait
  croire à « 2 pages ». Toujours `rm -f _m-*.png` **avant** de compter les pages.
- **Cache PDF** : `fichier_pdf` est un cache régénérable. Après toute modif de gabarit,
  purger pour voir le changement :
  ```bash
  python manage.py purger_pdf_cache --apply           # local
  sudo docker compose exec backend python manage.py purger_pdf_cache --apply   # prod
  ```
  En complément, les téléchargements envoient `Cache-Control: no-store` (sinon le
  navigateur ressert l'ancien PDF).

## 6. Procédure de mesure (réutilisable)

```python
# manage.py shell
from apps.documents.services import _generer_pdf
from apps.documents.models import DocumentOfficiel
d = DocumentOfficiel.objects.get(pk=<id_releve>)
b = _generer_pdf(d, d.etudiant,
                 {'annee_universitaire': d.annee_universitaire, 'semestre': d.semestre_id},
                 is_duplicata=True)
open('_m.pdf', 'wb').write(b)
```
```bash
rm -f _m-*.png                       # IMPORTANT : purger les vieux PNG
pdftoppm -png -r 200 _m.pdf _m
python - <<'PY'
from PIL import Image; im = Image.open('_m-1.png').convert('L'); W,H = im.size; px = im.load(); mm = 25.4/200
def row(y): return sum(1 for x in range(0,W,3) if px[x,y] < 110)
last = next(y for y in range(H-1,-1,-1) if row(y) > 3)
print('marge bas (mm) =', round((H-1-last)*mm, 1))
PY
```

---
**Commits :** `45c5398` (footer natif), `50bfcf4` (en-têtes anti-cache).
