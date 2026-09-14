"""
Les codes de type de séance qui ont une SIGNIFICATION MÉTIER.

UN SEUL ENDROIT où ces listes sont écrites. Le triplet des épreuves vivait en
cinq exemplaires — deux constantes nommées et trois listes en clair au milieu
d'une condition. Chacune est une occasion de diverger le jour où un quatrième
type apparaît : celui qui ajoute le code ne peut pas deviner qu'il existe
quatre autres endroits à corriger.

Le référentiel `parametres.Seance` reste administrable et peut contenir
n'importe quoi ; ce module ne nomme que les codes dont le CODE dépend — ceux
sur lesquels il prend une décision.
"""

#: Les ÉPREUVES. Une absence relevée sur l'une d'elles est une absence à la
#: copie ; celle d'un cours ne l'est pas. « DS » vaut devoir surveillé, « EF »
#: examen final, « ER » épreuve de rattrapage.
#:
#: Ce qu'elles ont en commun, et qui justifie de les traiter ensemble : elles
#: n'ont ni salle ni enseignant — la répartition en salles se décide sur les
#: fiches, et le surveillant relève de la vacation —, et elles n'avancent pas
#: le programme.
#:
#: `ipgei.EpreuveIPGEI.TYPE_CHOICES` porte les mêmes codes avec leurs libellés.
#: Un test vérifie que les deux ne se séparent pas.
TYPES_EPREUVE = ('DS', 'EF', 'ER')

#: Ce qui se paie en SURVEILLANCE : les épreuves, plus le type « Surveillance »
#: lui-même, saisi en vacation pour un surveillant qui n'assurait aucune
#: épreuve inscrite à l'emploi du temps.
#:
#: Dérivé du triplet, et non réécrit : ajouter un type d'épreuve doit le rendre
#: payable en surveillance sans qu'on ait à y penser.
TYPES_SURVEILLANCE = TYPES_EPREUVE + ('SURVEILLANCE',)
