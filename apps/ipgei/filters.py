"""
Filtres d'API IPGEI.

Depuis que `Classe` hérite de `departement.Departement`, le niveau est une clé
étrangère vers `parametres.Niveau` et non plus une chaîne. Ces FilterSet
maintiennent le contrat d'origine — `?niveau=MPSI`, `?classe__niveau=MP` — pour
que les écrans continuent d'interroger l'API avec des codes de niveau plutôt
qu'avec des identifiants numériques dénués de sens côté client.
"""
import django_filters

from .models import Classe, InscriptionIPGEI


class ClasseFilter(django_filters.FilterSet):
    """`?niveau=MPSI` continue de fonctionner malgré la clé étrangère."""
    niveau = django_filters.CharFilter(field_name='niveau__niveau', lookup_expr='iexact')

    class Meta:
        model  = Classe
        fields = ['niveau', 'annee_universitaire', 'actif']


class InscriptionIPGEIFilter(django_filters.FilterSet):
    """Idem pour le filtre traversant `?classe__niveau=MPSI`."""
    classe__niveau = django_filters.CharFilter(
        field_name='classe__niveau__niveau', lookup_expr='iexact',
    )

    class Meta:
        model  = InscriptionIPGEI
        fields = ['classe', 'annee_universitaire', 'statut', 'actif', 'sous_groupe']
