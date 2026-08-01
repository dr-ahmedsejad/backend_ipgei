from apps.inscriptions.models import InscriptionElement
from apps.evaluations.models import Note, ResultatElement, ResultatModule, ResultatSemestre
from decimal import Decimal, ROUND_HALF_UP

ip_id = 129  # InscriptionPed S1 de 255001
session_id = 7

ies = list(InscriptionElement.objects.filter(
    inscription_ped_id=ip_id
).select_related('em__module_lmd'))

# Charger toutes les notes en une seule requête
notes_index = {}
for n in Note.objects.filter(inscription_element__inscription_ped_id=ip_id, session_id=session_id):
    notes_index.setdefault(n.inscription_element_id, {})[n.type_note] = n.valeur

modules_dict = {}
for ie in ies:
    em = ie.em
    if not em:
        continue
    mod     = em.module_lmd
    mod_key = mod.pk if mod else 'SANS'
    label   = str(mod) if mod else 'SANS MODULE'
    coeff_em = Decimal(str(em.coefficient)) if em.coefficient else Decimal('1')

    try:
        me = ResultatElement.objects.get(inscription_element=ie).note_finale
    except ResultatElement.DoesNotExist:
        me = None

    notes_em = notes_index.get(ie.pk, {})

    if mod_key not in modules_dict:
        coeff_mod = Decimal(str(mod.coefficient)) if mod and mod.coefficient else None
        modules_dict[mod_key] = {'label': label, 'coeff_module': coeff_mod, 'elements': []}

    modules_dict[mod_key]['elements'].append({
        'code':     getattr(em, 'code_em', '?'),
        'coeff_em': coeff_em,
        'cc':       notes_em.get('CC'),
        'tp':       notes_em.get('TP'),
        'exam':     notes_em.get('EXAM'),
        'me':       me,
    })

print('=' * 75)
total_num = Decimal('0')
total_den = Decimal('0')

for mk, mod in modules_dict.items():
    print(f"\nModule : {mod['label']}  (coeff_module={mod['coeff_module']})")
    print(f"  {'EM':<14} {'coeff':>5}  {'CC':>6}  {'TP':>6}  {'Exam':>6}  {'ME':>6}  {'ME×coeff':>10}")
    print(f"  {'-'*65}")
    mod_num = Decimal('0')
    mod_den = Decimal('0')
    for e in mod['elements']:
        me_val  = e['me'] if e['me'] is not None else Decimal('0')
        c       = e['coeff_em']
        contrib = me_val * c
        mod_num   += contrib
        mod_den   += c
        total_num += contrib
        total_den += c
        cc   = f"{e['cc']:.2f}"   if e['cc']   is not None else '—'
        tp   = f"{e['tp']:.2f}"   if e['tp']   is not None else '—'
        exam = f"{e['exam']:.2f}" if e['exam'] is not None else '—'
        me_s = f"{me_val:.2f}"   if e['me']   is not None else '—'
        print(f"  {e['code']:<14} {str(c):>5}  {cc:>6}  {tp:>6}  {exam:>6}  {me_s:>6}  {str(contrib):>10}")
    if mod_den:
        mm = (mod_num / mod_den).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        print(f"  {'':14} {'':>5}  {'':>6}  {'':>6}  {'':>6}  Moy = {mod_num} / {mod_den} = {mm}")

mgs = (total_num / total_den).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if total_den else None
print(f"\n{'='*75}")
print(f"MGS = {total_num} / {total_den} = {mgs}")
print(f"{'='*75}")

try:
    rs = ResultatSemestre.objects.get(inscription_ped_id=ip_id, session_id=session_id)
    print(f"ResultatSemestre en base : {rs.moyenne}  admis={rs.est_admis}  code={rs.code_statut}")
except ResultatSemestre.DoesNotExist:
    print("Pas de ResultatSemestre (session 7)")
