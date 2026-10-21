"""
Territoires sous mesures restrictives de l'Union, lus dans un paiement.

POURQUOI CE MODULE EXISTE
-------------------------
Deux regimes de l'Union interdisent des operations liees a un TERRITOIRE, sans
nom de personne designee :
- reglement (UE) n° 692/2014 du Conseil (Crimee et Sebastopol, en vigueur le
  25 juin 2014 ; etendu par le reglement (UE) n° 1351/2014, art. 2 bis a 2 quinquies :
  financement d'une entite en Crimee, acquisition d'immobilier ou de
  participations, services lies au tourisme...) ;
- reglement (UE) 2022/263 du Conseil (zones des oblasts de Donetsk et de
  Louhansk non controlees par le gouvernement, en vigueur le 24 fevrier 2022),
  etendu aux zones non controlees des oblasts de Kherson et de Zaporija par le
  reglement (UE) 2022/1903 (en vigueur le 7 octobre 2022).
Le filtrage ne criblait que des noms : un virement a une societe domiciliee a
Simferopol, ou libelle « livraison Simferopol », passait PASS — aucune liste
ne porte de nom a rapprocher. Textes relus le 20 octobre 2026 (CELLAR).

CE QUE CE MODULE FAIT, ET CE QU'IL NE FAIT PAS
----------------------------------------------
Il lit les adresses des parties (ville `TwnNm`, localite, district,
subdivision `CtrySubDvsn`, lignes libres `AdrLine`), leur pays, et le texte
libre des informations de remise (`RmtInf/Ustrd`), contre une table DATEE des
territoires. Une correspondance n'est PAS un HIT de sanction : aucune personne
designee n'est en cause, et toute operation liee au territoire n'est pas
interdite (un don a un particulier ne l'est pas ; un financement d'entite,
l'achat de marchandises qui en sont originaires, un service touristique le
sont). C'est un SIGNAL a instruire, qui interdit le PASS — le verdict
`TERRITORY` de fiskr/transactions.py. Comme fiskr/country_risk.py, son modele,
il est HORS du moteur de score : il n'altere ni score, ni statut de partie, ni
`decision_tree`.

HAUTE PRECISION, MESUREE
------------------------
Un signal qui sonne sur chaque virement parisien est debranche. Or Paris
commemore la guerre de Crimee : boulevard de Sebastopol, rue de Crimee, rue
d'Eupatoria — et Sebastopol est aussi une ville de Californie, d'Australie et
du pays de Galles ; Donetsk, une ville de l'oblast de Rostov ; « antratsit »
veut dire anthracite. Quatre regles :
  1. une sequence EXACTE de mots normalises (casse, diacritiques, apostrophe
     de translitteration), jamais un fragment : « Sevastopolsky prospekt » a
     Moscou ne declenche pas ;
  2. une forme AMBIGUE (homonyme ailleurs, mot du dictionnaire) n'est lue que
     dans l'adresse d'une partie dont le pays est UA ou RU, jamais dans le
     texte libre ;
  3. un nom precede d'un mot de voie (rue, boulevard, metro...) ou suivi d'un
     type de voie (Road, Strasse) est un nom de rue ; dans une adresse, un nom
     precede d'une enseigne (hotel, restaurant) est celui de l'etablissement,
     pas un domicile — dans le texte libre, « hotel Alouchta » est un sejour a
     Alouchta ; une ville qui porte le nom de son
     oblast (Donetsk, Louhansk, Kherson) suivie ou precedee de « oblast »,
     « region »... designe l'OBLAST, que le regime ne couvre pas en entier
     (Kramatorsk est dans l'oblast de Donetsk, sous controle du gouvernement) ;
  4. la date de l'operation est confrontee a la date d'effet du lieu : un
     paiement a Louhansk de 2021 precede le reglement 2022/263, et une ville
     reprise (Kherson, 11 novembre 2022) cesse d'etre couverte.
Mesure, par ce chemin (pacs.008 -> parse_iso20022_payment -> signaux_du_message),
sur un corpus construit pour elle : 10 000 paiements ordinaires dont 1 460
portent un homonyme reel (ceux cites plus haut, Tokmok du Kirghizstan, Zaporija
et Kherson d'aujourd'hui, « sudak », « conference de Yalta »...) : 0,24 % de
declenchement, tous sur « Restaurant Jalta Berlin » en libelle de remise, aucun
sur un paiement ordinaire ; 1 000 adresses et 1 000 libelles dans les lieux
couverts, retrouves. Les cas limites sont epingles dans
tests/test_territoires_sous_embargo.py.

La table integree est une donnee de reference (pas de reseau), surchargeable
a chaud par le bloc `territoires_sous_embargo` de config.yaml — la ligne de
front bouge, et la table porte la date a laquelle elle a ete etablie.
"""
import json
import logging
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("fiskr.territoires")

# Date a laquelle la table integree a ete etablie (etat des zones non
# controlees retenu). A reviser — ici ou, sans redeploiement, dans config.yaml.
BUILTIN_AS_OF = "2026-10-20"

# Chaque lieu : son nom, ses formes lues PARTOUT (adresse et texte libre),
# ses formes AMBIGUES (lues seulement dans l'adresse d'une partie UA ou RU),
# et au besoin : sa propre date d'effet (`depuis`, quand le lieu est sorti du
# controle du gouvernement apres l'entree en vigueur du regime), sa date de
# fin (`jusqu_au`, exclue : reprise par le gouvernement), `oblast` (le nom de
# la ville est aussi celui de l'oblast) et `homonymes` (mots qui, dans la meme
# adresse, designent l'homonyme).
BUILTIN_TERRITOIRES: Dict[str, Dict[str, Any]] = {
    "CRIMEE_SEBASTOPOL": {
        "nom": "Crimée et Sébastopol",
        "regime": "Règlement (UE) n° 692/2014 du Conseil",
        "depuis": "2014-06-25",
        "lieux": [
            {"nom": "Crimée",
             "variantes": ["Crimea", "Krym", "Крым", "Крим", "Crimeia", "Qirim",
                           "Republic of Crimea", "Respublika Krym", "Autonomous Republic of Crimea",
                           "Avtonomna Respublika Krym", "République autonome de Crimée",
                           "Республика Крым", "Автономна Республіка Крим", "UA-43"],
             # Rue de Crimee et metro Crimee (Paris), « guerre de Crimee » ;
             # « krim » : roman policier en danois, norvegien, suedois.
             "ambigues": ["Crimée", "Krim"]},
            {"nom": "Sébastopol",
             "variantes": ["Sevastopol", "Sevastopil", "Sewastopol", "Sevastopol'",
                           "Севастополь", "UA-40"],
             # Boulevard de Sebastopol (Paris), Sebastopol (Californie,
             # Victoria, Torfaen), Reaumur-Sebastopol.
             "ambigues": ["Sébastopol", "Sebastopol"]},
            {"nom": "Simferopol",
             "variantes": ["Simferopol", "Simferopol'", "Simferopil", "Symferopol",
                           "Симферополь", "Сімферополь", "Aqmescit", "Akmescit"]},
            {"nom": "Kertch", "variantes": ["Kerch", "Kertch", "Kertsch", "Керчь", "Керч"]},
            # La conference de Yalta (1945) : un livre, un colloque, pas la ville
            {"nom": "Yalta", "variantes": ["Yalta", "Jalta", "Ялта"],
             "homonymes": ["CONFERENCE", "KONFERENZ", "CONFERENZA", "CONFERENCIA", "КОНФЕРЕНЦИЯ"]},
            {"nom": "Ievpatoria",
             "variantes": ["Yevpatoria", "Yevpatoriya", "Evpatoria", "Evpatoriya", "Ievpatoria",
                           "Jewpatorija", "Евпатория", "Євпаторія", "Gözleve"],
             "ambigues": ["Eupatoria", "Eupatorie"]},           # rue d'Eupatoria (Paris)
            {"nom": "Féodosie",
             "variantes": ["Feodosia", "Feodosiya", "Feodossia", "Feodossija", "Феодосия", "Феодосія"],
             "ambigues": ["Theodosia", "Théodosie"]},           # prenom
            {"nom": "Djankoï",
             "variantes": ["Dzhankoi", "Dzhankoy", "Djankoi", "Djankoy", "Dschankoj", "Джанкой"]},
            {"nom": "Alouchta", "variantes": ["Alushta", "Alouchta", "Aluschta", "Алушта"]},
            {"nom": "Bakhtchyssaraï",
             "variantes": ["Bakhchysarai", "Bakhchisaray", "Bakhchisarai", "Bakhtchyssaraï",
                           "Bachtschyssaraj", "Бахчисарай"]},
            {"nom": "Armiansk", "variantes": ["Armiansk", "Armyansk", "Армянск", "Армянськ"]},
            {"nom": "Krasnoperekopsk",
             "variantes": ["Krasnoperekopsk", "Красноперекопск", "Красноперекопськ"]},
            # Saki : ville du Nigeria, d'Azerbaidjan ; sudak : le sandre, en russe
            {"nom": "Saky", "variantes": [], "ambigues": ["Saky", "Saki", "Саки"]},
            {"nom": "Soudak", "variantes": [], "ambigues": ["Sudak", "Soudak", "Судак"]},
        ],
    },
    "DONETSK_LOUHANSK": {
        "nom": "Zones non contrôlées par le gouvernement des oblasts de Donetsk et de Louhansk",
        "regime": "Règlement (UE) 2022/263 du Conseil",
        "depuis": "2022-02-24",
        "lieux": [
            {"nom": "Donetsk", "oblast": True,
             "variantes": ["Donetsk", "Donetsk'", "Donezk", "Donieck", "Донецк", "Донецьк"],
             # Donetsk, ville de l'oblast de Rostov (Russie)
             "homonymes": ["ROSTOV", "ROSTOVSKAYA", "РОСТОВСКАЯ", "РОСТОВ"]},
            {"nom": "Louhansk", "oblast": True,
             "variantes": ["Luhansk", "Lugansk", "Louhansk", "Lougansk", "Luhans'k", "Ługańsk",
                           "Луганск", "Луганськ"]},
            {"nom": "« République populaire de Donetsk »",
             "variantes": ["Donetsk People's Republic", "Donetskaya Narodnaya Respublika",
                           "République populaire de Donetsk", "Донецкая Народная Республика"],
             "ambigues": ["DNR", "DPR", "ДНР"]},
            {"nom": "« République populaire de Louhansk »",
             "variantes": ["Luhansk People's Republic", "Lugansk People's Republic",
                           "Luganskaya Narodnaya Respublika", "République populaire de Louhansk",
                           "Луганская Народная Республика"],
             "ambigues": ["LNR", "LPR", "ЛНР"]},
            {"nom": "Makiïvka",
             "variantes": ["Makiivka", "Makiyivka", "Makeevka", "Makeyevka", "Makijiwka",
                           "Макеевка", "Макіївка"]},
            {"nom": "Horlivka", "variantes": ["Horlivka", "Gorlovka", "Gorlivka", "Горловка", "Горлівка"]},
            {"nom": "Altchevsk", "variantes": ["Alchevsk", "Alchevs'k", "Altchevsk", "Алчевск", "Алчевськ"]},
            {"nom": "Ienakiieve",
             "variantes": ["Yenakiieve", "Yenakiyeve", "Enakievo", "Yenakiyevo", "Єнакієве", "Енакиево"]},
            {"nom": "Khartsyzk", "variantes": ["Khartsyzk", "Khartsyzsk", "Харцызск", "Харцизьк"]},
            {"nom": "Kadiïvka", "variantes": ["Kadiivka", "Kadiyivka", "Кадиевка", "Кадіївка", "Стаханов"],
             "ambigues": ["Stakhanov"]},
            {"nom": "Khroustalnyï",
             "variantes": ["Krasnyi Luch", "Krasny Luch", "Khrustalnyi", "Khrustalny", "Красный Луч",
                           "Хрустальний", "Хрустальный"]},
            {"nom": "Rovenky", "variantes": ["Rovenky", "Rovenki", "Ровеньки"]},
            {"nom": "Dovjansk", "variantes": ["Dovzhansk", "Довжанськ", "Довжанск"]},
            {"nom": "Snijne", "variantes": ["Snizhne", "Snezhnoye", "Snezhnoe", "Сніжне", "Снежное"]},
            {"nom": "Tchystiakove", "variantes": ["Chystiakove", "Chistyakovo", "Торез", "Чистякове"],
             "ambigues": ["Torez"]},
            {"nom": "Chakhtarsk", "variantes": ["Shakhtarsk", "Shakhtyorsk", "Шахтарськ", "Шахтёрск", "Шахтерск"]},
            {"nom": "Debaltseve", "variantes": ["Debaltseve", "Debaltsevo", "Дебальцеве", "Дебальцево"]},
            # « antratsit » : l'anthracite, sur toute facture de charbon
            {"nom": "Antratsyt", "variantes": [], "ambigues": ["Antratsyt", "Antratsit", "Антрацит"]},
            # Sorties du controle du gouvernement apres le 24 fevrier 2022 :
            # couvertes a partir de cette date-la, pas avant.
            {"nom": "Marioupol", "depuis": "2022-05-20",
             "variantes": ["Mariupol", "Marioupol", "Mariupol'", "Mariupil", "Маріуполь", "Мариуполь"]},
            {"nom": "Sievierodonetsk", "depuis": "2022-06-25",
             "variantes": ["Sievierodonetsk", "Severodonetsk", "Siewierodonieck", "Сєвєродонецьк", "Северодонецк"]},
            {"nom": "Lyssytchansk", "depuis": "2022-07-03",
             "variantes": ["Lysychansk", "Lisichansk", "Lyssytchansk", "Лисичанськ", "Лисичанск"]},
        ],
    },
    "KHERSON_ZAPORIJIA": {
        "nom": "Zones non contrôlées par le gouvernement des oblasts de Kherson et de Zaporija",
        "regime": "Règlement (UE) 2022/263 du Conseil, étendu par le règlement (UE) 2022/1903",
        "depuis": "2022-10-07",
        # Zaporija (la ville) n'a jamais ete sous controle russe : absente.
        "lieux": [
            {"nom": "Kherson", "oblast": True, "jusqu_au": "2022-11-11",
             "variantes": ["Kherson", "Cherson", "Herson", "Херсон"]},
            {"nom": "Nova Kakhovka",
             "variantes": ["Nova Kakhovka", "Novaya Kakhovka", "Nowa Kachowka", "Нова Каховка", "Новая Каховка"]},
            {"nom": "Kakhovka", "variantes": ["Kakhovka", "Каховка"]},
            {"nom": "Henitchesk", "variantes": ["Henichesk", "Genichesk", "Henitchesk", "Генічеськ", "Геническ"]},
            {"nom": "Skadovsk", "variantes": ["Skadovsk", "Скадовськ", "Скадовск"]},
            {"nom": "Olechky", "variantes": ["Oleshky", "Aleshki", "Олешки", "Алешки"]},
            {"nom": "Hola Prystan",
             "variantes": ["Hola Prystan", "Gola Pristan", "Гола Пристань", "Голая Пристань"]},
            {"nom": "Melitopol", "variantes": ["Melitopol", "Melitopol'", "Мелітополь", "Мелитополь"]},
            {"nom": "Berdiansk", "variantes": ["Berdiansk", "Berdyansk", "Berdjansk", "Бердянськ", "Бердянск"]},
            {"nom": "Enerhodar", "variantes": ["Enerhodar", "Energodar", "Енергодар", "Энергодар"]},
            {"nom": "Polohy", "variantes": ["Polohy", "Pologi", "Пологи"]},
            # Tokmok/Tokmak : ville du Kirghizstan
            {"nom": "Tokmak", "variantes": [], "ambigues": ["Tokmak", "Токмак"]},
        ],
    },
}

# Pays dont l'adresse peut designer un lieu des territoires : l'Ukraine, et la
# Russie qui les a annexes et y attribue ses propres adresses.
PAYS_DES_TERRITOIRES = frozenset({"UA", "RU"})

# Mots de voie qui, AVANT un nom de lieu, en font un nom de rue (normalises :
# majuscules, sans diacritiques).
_AVANT_VOIE = frozenset({
    "RUE", "BD", "BOULEVARD", "BLVD", "AV", "AVE", "AVENUE", "PLACE", "PL", "QUAI", "IMPASSE",
    "ALLEE", "CHEMIN", "SQUARE", "COURS", "PASSAGE", "VILLA", "CITE", "ROUTE", "PONT",
    "STREET", "ROAD", "LANE", "STRASSE", "VIA", "VIALE", "CALLE", "PLAZA", "RUA",
    "ULITSA", "UL", "VULYTSIA", "VUL", "PROSPEKT", "PROSP", "PEREULOK", "SHOSSE",
    "УЛ", "УЛИЦА", "ВУЛ", "ВУЛИЦЯ", "ПРОСПЕКТ", "ПЕР", "METRO", "STATION", "GARE",
})
# Enseignes : dans une ADRESSE, « Hotel Sevastopol » (Moscou) ou « Restaurant
# Jalta » (Berlin) nomme l'etablissement, pas la ville. Dans le texte libre, non :
# « Hotel Alushta, sejour » paie le service touristique que le reglement
# n° 692/2014 interdit (art. 2 quinquies, ajoute par le n° 1351/2014). Mesure :
# ecartees aussi du texte libre, ces enseignes faisaient manquer chaque libelle
# « Hotel <lieu couvert> » du corpus (155 sur 1 000 libelles positifs).
_AVANT_ENSEIGNE = frozenset({"HOTEL", "RESTAURANT", "CAFE", "BAR", "BRASSERIE", "CINEMA", "THEATRE"})
# ... et APRES lui (usage anglo-saxon et germanique : « Sebastopol Road »).
_APRES_VOIE = frozenset({
    "ROAD", "RD", "STREET", "ST", "AVENUE", "AVE", "LANE", "DRIVE", "WAY", "TERRACE", "GARDENS",
    "CLOSE", "CRESCENT", "STRASSE", "STR", "WEG", "PLATZ", "GASSE", "ALLEE",
})
# Mots qui, autour d'une ville homonyme de son oblast, designent l'oblast.
_OBLAST = frozenset({"OBLAST", "OBL", "REGION", "RAION", "RAYON", "PROVINCE", "ОБЛАСТЬ", "ОБЛАСТІ", "ОБЛ"})
# Mots de liaison sautes en remontant vers le mot de voie : « rue DE Crimee ».
_LIAISONS = frozenset({"DE", "D", "DU", "DES", "LA", "LE", "L", "OF", "THE", "DEL", "DELLA", "DI",
                       "VON", "DER", "DEN"})

# Champs d'adresse lus, dans l'ordre ou l'ecran les cite. Ni StrtNm (un nom de
# rue, par definition), ni CityOfBirth (ne pas etre ne en Crimee n'est pas une
# condition du regime ; y etre ne n'y domicilie personne).
CHAMPS_ADRESSE = ("TwnNm", "TwnLctnNm", "DstrctNm", "CtrySubDvsn", "AdrLine")
CHAMP_TEXTE_LIBRE = "RmtInf/Ustrd"

_SEGMENTS = re.compile(r"[,;\n\r/|()\[\]]+")
_HORS_MOT = re.compile(r"[^\w]+")
_ELISION = re.compile(r"\b(D|L|QU|J|M|N|S|T|C)['’]")


def jetons(texte: Any) -> List[str]:
    """Mots normalises : majuscules, diacritiques retires (й -> И, é -> E),
    apostrophe de translitteration (« Simferopol' ») retiree. Une seule
    normalisation, pour la table et pour le message — independante des
    capacites du moteur de noms : couper la translitteration ne doit pas
    rendre aveugle a « Симферополь »."""
    decompose = unicodedata.normalize("NFKD", str(texte or ""))
    sans = "".join(c for c in decompose if not unicodedata.combining(c)).upper()
    # L'elision francaise separe (« d'Eupatoria » : D EUPATORIA) ; toute autre
    # apostrophe est le signe mou translittere, qui ne coupe pas le mot.
    sans = _ELISION.sub(r"\1 ", sans).replace("'", "").replace("’", "")
    return [m for m in _HORS_MOT.split(sans) if m]


def _date(valeur: Any) -> Optional[date]:
    if valeur is None or valeur == "":
        return None
    if isinstance(valeur, datetime):
        return valeur.date()
    if isinstance(valeur, date):
        return valeur
    try:
        return datetime.strptime(str(valeur).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"date illisible : {valeur!r} (AAAA-MM-JJ attendu)")


def _valider(code: str, entree: Any) -> Dict[str, Any]:
    """Une entree de la table, verifiee : un territoire mal ecrit dans
    config.yaml est REFUSE et dit (cf. `reference()["erreurs"]`), jamais lu de
    travers."""
    if not isinstance(entree, dict):
        raise ValueError("un territoire est un dictionnaire (nom, regime, depuis, lieux)")
    for cle in ("nom", "regime", "depuis"):
        if not str(entree.get(cle) or "").strip():
            raise ValueError(f"« {cle} » manquant")
    depuis = _date(entree["depuis"])
    lieux = []
    for lieu in entree.get("lieux") or []:
        if not isinstance(lieu, dict) or not str(lieu.get("nom") or "").strip():
            raise ValueError("chaque lieu porte un « nom »")
        formes = [str(v) for v in (lieu.get("variantes") or []) if str(v).strip()]
        ambigues = [str(v) for v in (lieu.get("ambigues") or []) if str(v).strip()]
        if not (formes or ambigues):
            raise ValueError(f"le lieu « {lieu['nom']} » n'a aucune forme à lire")
        lieux.append({
            "nom": str(lieu["nom"]), "variantes": formes, "ambigues": ambigues,
            "depuis": max(depuis, _date(lieu.get("depuis")) or depuis),
            "jusqu_au": _date(lieu.get("jusqu_au")),
            "oblast": bool(lieu.get("oblast")),
            "homonymes": [j for h in (lieu.get("homonymes") or []) for j in jetons(h)],
        })
    if not lieux:
        raise ValueError("aucun lieu")
    return {"code": code, "nom": str(entree["nom"]), "regime": str(entree["regime"]),
            "depuis": depuis, "lieux": lieux}


_CACHE: Dict[str, Any] = {"cle": None, "table": None}


def _table() -> Dict[str, Any]:
    """
    Table effective : integree par defaut, surchargee a chaud par le bloc
    `territoires_sous_embargo` de config.yaml. Un territoire fourni REMPLACE
    celui de meme code (les autres restent integres) ; `CODE: null` le retire.
    Memoisee sur le contenu du bloc : relue des qu'il change.
    """
    from fiskr.config import config
    bloc = config.get("territoires_sous_embargo") or {}
    cle = json.dumps(bloc, sort_keys=True, default=str, ensure_ascii=False)
    if _CACHE["cle"] == cle and _CACHE["table"] is not None:
        return _CACHE["table"]
    sources: Dict[str, Any] = dict(BUILTIN_TERRITOIRES)
    for code, entree in ((bloc.get("territoires") or {}) if isinstance(bloc, dict) else {}).items():
        if entree is None:
            sources.pop(str(code), None)
        else:
            sources[str(code)] = entree
    territoires, erreurs = [], []
    for code, entree in sources.items():
        try:
            territoires.append(_valider(code, entree))
        except ValueError as e:
            # Pas de repli sur la version integree : ce qui est applique est
            # ce qui est ecrit, et ce qui ne peut pas l'etre est nomme.
            erreurs.append(f"{code} : {e}")
            logger.warning(f"Territoire sous embargo « {code} » ignoré : {e}")
    # Index : premier mot -> formes (plus longues d'abord), pour une lecture
    # en un passage et une correspondance la plus longue (« Nova Kakhovka »
    # l'emporte sur « Kakhovka »).
    index: Dict[str, List[Tuple[Tuple[str, ...], Dict[str, Any], Dict[str, Any], bool]]] = {}
    for territoire in territoires:
        for lieu in territoire["lieux"]:
            for forme, ambigue in ([(v, False) for v in lieu["variantes"]]
                                   + [(v, True) for v in lieu["ambigues"]]):
                mots = tuple(jetons(forme))
                if mots:
                    index.setdefault(mots[0], []).append((mots, territoire, lieu, ambigue))
    for formes in index.values():
        formes.sort(key=lambda f: -len(f[0]))
    table = {
        "as_of": str((bloc.get("as_of") if isinstance(bloc, dict) else None) or BUILTIN_AS_OF),
        "territoires": territoires, "index": index, "erreurs": erreurs,
        "overridden": bool(bloc),
    }
    _CACHE.update(cle=cle, table=table)
    return table


def reference() -> Dict[str, Any]:
    """Table effective pour affichage (GET /api/embargo-territories)."""
    table = _table()
    return {
        "as_of": table["as_of"],
        "overridden": table["overridden"],
        "errors": table["erreurs"],
        "territories": [{
            "code": t["code"], "name": t["nom"], "regime": t["regime"],
            "in_force_since": t["depuis"].isoformat(),
            "places": [{
                "name": l["nom"], "forms": l["variantes"], "ambiguous_forms": l["ambigues"],
                "since": l["depuis"].isoformat(),
                "until": l["jusqu_au"].isoformat() if l["jusqu_au"] else None,
            } for l in t["lieux"]],
        } for t in table["territoires"]],
    }


def _precede_de(mots: List[str], debut: int, mots_cles: frozenset) -> bool:
    """Le mot qui precede (liaisons sautees : « rue DE Crimee ») est-il un
    mot-cle ? Un seul : « ul. Lenina Simferopol » est une adresse a
    Simferopol, pas une rue qui en porte le nom."""
    for k in range(debut - 1, -1, -1):
        if mots[k] in _LIAISONS:
            continue
        return mots[k] in mots_cles
    return False


def lire(texte: Any, *, adresse: bool, pays: str = "", contexte: Iterable[str] = (),
         date_operation: Optional[date] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Lieux des territoires ecrits dans un champ : (retenus, ecartes par la date).

    `adresse` : le champ est une adresse de partie (les formes ambigues y sont
    lues si `pays` est UA ou RU) ; sinon un texte libre (formes non ambigues
    seulement). `contexte` : tous les mots de l'adresse de la partie, pour
    reconnaitre un homonyme (Donetsk... oblast de Rostov).
    """
    table = _table()
    index = table["index"]
    jour = date_operation or date.today()
    contexte = set(contexte)
    retenus: List[Dict[str, Any]] = []
    ecartes: List[Dict[str, Any]] = []
    ambigues_permises = adresse and (pays or "").upper() in PAYS_DES_TERRITOIRES
    for segment in _SEGMENTS.split(str(texte or "")):
        mots = jetons(segment)
        i = 0
        while i < len(mots):
            trouve = None
            for forme, territoire, lieu, ambigue in index.get(mots[i], ()):
                n = len(forme)
                if tuple(mots[i:i + n]) != forme:
                    continue
                if ambigue and not ambigues_permises:
                    continue
                apres = mots[i + n] if i + n < len(mots) else ""
                if _precede_de(mots, i, _AVANT_VOIE) or apres in _APRES_VOIE:
                    continue
                if adresse and _precede_de(mots, i, _AVANT_ENSEIGNE):
                    continue
                if lieu["oblast"] and (_precede_de(mots, i, _OBLAST) or apres in _OBLAST):
                    continue
                if lieu["homonymes"] and contexte.intersection(lieu["homonymes"]):
                    continue
                trouve = (n, territoire, lieu)
                break
            if trouve is None:
                i += 1
                continue
            n, territoire, lieu = trouve
            correspondance = {
                "territory": territoire["code"], "territory_label": territoire["nom"],
                "regime": territoire["regime"], "place": lieu["nom"],
                "matched": " ".join(mots[i:i + n]),
                "in_force_since": lieu["depuis"].isoformat(),
                "until": lieu["jusqu_au"].isoformat() if lieu["jusqu_au"] else None,
            }
            couvert = lieu["depuis"] <= jour and (lieu["jusqu_au"] is None or jour < lieu["jusqu_au"])
            (retenus if couvert else ecartes).append(correspondance)
            i += n
    return retenus, ecartes


def _jour_de_l_operation(tx: Dict[str, Any], parsed: Dict[str, Any]) -> Tuple[date, str]:
    """Date confrontee a la table : celle de l'execution (IntrBkSttlmDt,
    ReqdExctnDt), a defaut la creation du message (CreDtTm), a defaut le jour
    du filtrage — et la reponse dit laquelle."""
    for valeur, source in ((tx.get("operation_date"), tx.get("operation_date_source")),
                           (parsed.get("creation_datetime"), "CreDtTm")):
        try:
            jour = _date(valeur)
        except ValueError:
            jour = None
        if jour is not None:
            return jour, source or "CreDtTm"
    return date.today(), "date du filtrage"


def signaux_du_message(parsed: Dict[str, Any], roles_texte_libre: str = "") -> Dict[str, Any]:
    """
    Signaux « territoire sous mesures restrictives » d'un message analyse
    (fiskr/transactions.parse_iso20022_payment) : adresses de toutes les
    parties (agents compris) et informations de remise, transaction par
    transaction, contre la date de chacune. Regroupes par lieu, champ et
    partie, transactions concernees jointes.
    """
    from fiskr.transactions import libelle_partie
    par_cle: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    ecartes: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

    def _ajouter(cible, correspondance, champ, role, partie, texte, ident, jour, source):
        # `text` : le champ tel qu'ecrit — l'operateur instruit sur ce que le
        # message dit, pas sur la forme normalisee (`matched`).
        cle = (correspondance["territory"], correspondance["place"], champ, role, partie, texte)
        entree = cible.setdefault(cle, dict(correspondance, field=champ, role=role, party=partie,
                                            text=texte, operation_date=jour.isoformat(),
                                            operation_date_source=source, transactions=[]))
        if ident not in entree["transactions"]:
            entree["transactions"].append(ident)

    for rang, tx in enumerate(parsed.get("transactions") or [], start=1):
        ident = tx.get("end_to_end_id") or f"#{rang}"
        jour, source = _jour_de_l_operation(tx, parsed)
        for partie in tx.get("parties") or []:
            lieux = partie.get("lieux") or []
            contexte = [j for _champ, valeur in lieux for j in jetons(valeur)]
            for champ, valeur in lieux:
                retenus, mis_de_cote = lire(valeur, adresse=True, pays=partie.get("country", ""),
                                            contexte=contexte, date_operation=jour)
                for cible, trouves in ((par_cle, retenus), (ecartes, mis_de_cote)):
                    for c in trouves:
                        _ajouter(cible, c, f"{partie['role_tag']}/PstlAdr/{champ}", partie["role"],
                                 libelle_partie(partie), valeur[:140], ident, jour, source)
        if tx.get("remittance"):
            retenus, mis_de_cote = lire(tx["remittance"], adresse=False,
                                        contexte=jetons(tx["remittance"]), date_operation=jour)
            for cible, trouves in ((par_cle, retenus), (ecartes, mis_de_cote)):
                for c in trouves:
                    _ajouter(cible, c, CHAMP_TEXTE_LIBRE, roles_texte_libre, "",
                             tx["remittance"][:140], ident, jour, source)
    return {"as_of": _table()["as_of"], "signals": list(par_cle.values()),
            "set_aside_by_date": list(ecartes.values())}
