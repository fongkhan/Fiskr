"""
Une composante de blocking se calcule des deux cotes — et du cote client, sur
un champ que le profil client PORTE.

La composante IMO lisait `client_imo_number`, cle qu'aucun profil client ne
porte : le numero IMO d'un navire arrive dans `transaction_vessel_imo`, champ
que le hard match du scoring lit deja. Cote client, la composante valait donc
toujours le joker « * » ; la sonde n'atteignait que les fiches SANS numero IMO,
et le navire liste sous son numero — celui-la meme qu'on cherchait — n'etait
jamais candidat. Exactement la perte silencieuse que l'en-tete de
BLOCKING_FIELDS decrit et pretend empecher.
"""
from fiskr.api import ScreenClientRequest
from fiskr.blocking import BLOCKING_FIELDS, generate_blocking_keys, lookup_blocking_keys


class _Releve(dict):
    """Un profil vide qui note chaque cle qu'on lui demande."""
    def __init__(self):
        super().__init__()
        self.lues = set()

    def get(self, cle, defaut=None):
        self.lues.add(cle)
        return defaut


def test_chaque_composante_lit_un_champ_du_profil_client():
    champs = set(ScreenClientRequest.model_fields)
    orphelines = {}
    for item, (_libelle, cote_client, _cote_liste) in BLOCKING_FIELDS.items():
        releve = _Releve()
        try:
            cote_client(releve)
        except Exception:
            pass
        inconnues = releve.lues - champs
        if inconnues:
            orphelines[item] = sorted(inconnues)
    assert not orphelines, (
        f"composantes lisant une cle absente du profil client : {orphelines}")


def test_un_navire_liste_sous_son_imo_est_candidat():
    config = {"blocking": {"custom_key_layout": ["IMO"]}}
    navire = {"entity_id": "V-1", "entity_type": "V", "imo_number": "9123456"}
    client = {"client_id": "C-1", "client_type": "PM",
              "transaction_vessel_imo": "912 3456"}
    assert generate_blocking_keys(navire, config) & lookup_blocking_keys(client, config)
