"""
Garde-fous sur les serializers IPGEI.

Deux classes de bugs se sont révélées en recette et ne se voyaient QU'AVEC des
données en base — d'où ces tests :

1. Un `SerializerMethodField` déclaré mais absent de `Meta.fields` fait lever
   une AssertionError par DRF. Comme la construction des champs n'a lieu qu'au
   premier élément sérialisé, l'endpoint répond 200 tant que le queryset est
   vide, puis 500 dès qu'une ligne existe. C'est ce qui est arrivé à
   `SeanceReelleSerializer.nb_absents` : l'écran EDT tombait en 500 exactement
   au moment où une grille venait d'être dupliquée.

2. DRF dérive un `UniqueTogetherValidator` des contraintes d'unicité du modèle,
   et ce validateur rend obligatoire TOUT champ qu'il couvre — même déclaré
   `null=True, blank=True`. Sans valeur par défaut, `institution` et
   `sous_groupe` devenaient donc requis dans chaque POST, ce qui rendait
   impossibles la création d'une classe et celle d'une séance de classe entière.
"""
import inspect

import pytest
from rest_framework import serializers

from apps.ipgei import serializers as ipgei_serializers
from apps.ipgei import serializers_documents


def _classes_de_serializers():
    """Tous les serializers déclarés par l'app IPGEI."""
    for module in (ipgei_serializers, serializers_documents):
        for nom, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, serializers.BaseSerializer) and cls.__module__ == module.__name__:
                yield nom, cls


@pytest.mark.unit
@pytest.mark.parametrize(
    'nom,cls', list(_classes_de_serializers()), ids=lambda v: v if isinstance(v, str) else '',
)
def test_champs_declares_coherents(nom, cls):
    """
    Construire les champs d'un serializer ne doit jamais lever.

    Accéder à `.fields` exécute `get_field_names()`, c'est-à-dire précisément la
    vérification que DRF effectue au premier rendu — le moment où le bug se
    manifestait en production plutôt qu'au démarrage.
    """
    champs = cls().fields
    assert len(champs) > 0, f'{nom} ne déclare aucun champ.'


@pytest.mark.unit
@pytest.mark.parametrize('cls,champ', [
    (ipgei_serializers.ClasseSerializer,        'institution'),
    (ipgei_serializers.MatiereSerializer,       'institution'),
    (ipgei_serializers.SemestreIPGEISerializer, 'institution'),
    (ipgei_serializers.DeliberationSerializer,  'institution'),
    (ipgei_serializers.SeanceTypeSerializer,    'sous_groupe'),
    (ipgei_serializers.SeanceReelleSerializer,  'sous_groupe'),
])
def test_champ_optionnel_malgre_contrainte_unicite(cls, champ):
    """
    Ces champs entrent dans une contrainte d'unicité du modèle : sans valeur par
    défaut, le UniqueTogetherValidator de DRF les rendrait obligatoires et le
    client ne pourrait plus les omettre.
    """
    field = cls().fields[champ]
    assert field.required is False, (
        f'{cls.__name__}.{champ} est requis : le client devra le renseigner à chaque POST.'
    )
    assert field.default is not serializers.empty, (
        f'{cls.__name__}.{champ} n\'a pas de valeur par défaut — `required=False` seul '
        'ne suffit pas face au UniqueTogetherValidator.'
    )
