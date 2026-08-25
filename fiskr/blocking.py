import re
from typing import Set, List

from fiskr import capabilities as caps
from fiskr.phonetics import double_metaphone
from fiskr.quality import strip_accents_for_matching


def _country_equivalence_values(countries: List[str]) -> Set[str]:
    """
    Classes d'equivalence des pays, a AJOUTER aux valeurs brutes.

    Sans elles, un client dont la nationalite est saisie « Allemagne » et une
    fiche listee portant « DE » ne partagent aucune cle : ils ne sont jamais
    candidats, et la canonicalisation des pays au scoring ne sert a rien.
    """
    from fiskr import resources

    ctx = resources.current_context()
    if ctx["index"] is None or resources.FIELD_COUNTRY not in ctx["fields"]:
        return set()
    values = set()
    for country in countries:
        cls = ctx["index"].canonical(country, resources.FIELD_COUNTRY)
        if cls:
            values.add(cls)
    return values


def _equivalence_keys(word: str) -> Set[str]:
    """
    Cles de blocking issues des equivalences linguistiques declarees.

    Retourne un ensemble VIDE tant qu'aucun type de champ n'est active : le
    blocking retrouve alors exactement son comportement d'origine, cle pour
    cle. Le prefixe « EQ » evite toute collision avec une cle metaphone.
    """
    if not word:
        return set()
    from fiskr import resources

    ctx = resources.current_context()
    if ctx["index"] is None or not ctx["fields"]:
        return set()
    keys: Set[str] = set()
    for field in (resources.FIELD_GIVEN_NAME, resources.FIELD_SURNAME):
        if field in ctx["fields"]:
            cls = ctx["index"].canonical(word, field)
            if cls:
                keys.add(f"EQ{cls}")
    return keys


# ------------------ CHAMPS UTILISABLES EN CLE DE BLOCKING ------------------
#
# Une composante de cle doit se calculer DES DEUX COTES — profil client et
# fiche listee — sinon la cle produite ne se rencontre jamais et le candidat
# est perdu en silence. Chaque entree porte donc les deux extracteurs.
#
# JOKER : un champ absent rend `None`, traduit en « * ». La sonde interroge
# alors AUSSI les variantes ou ce champ est joker (voir lookup_blocking_keys),
# faute de quoi ajouter un champ ferait disparaitre toute fiche listee qui ne
# le renseigne pas — c'est-a-dire l'essentiel des listes officielles.

FIELD_WILDCARD = "*"


def _premier(valeurs) -> str:
    for v in (valeurs or []):
        if v and str(v).strip():
            return str(v).strip()
    return ""


def _annee(valeur: str) -> str:
    """Annee d'une date, quel que soit le format (les listes publient des
    dates partielles : « 1960 », « 1960-00-00 », « 03/05/1960 »)."""
    trouve = re.search(r"(1[89]\d{2}|20\d{2})", str(valeur or ""))
    return trouve.group(1) if trouve else ""


def _alphanum(valeur: str) -> str:
    """Identifiants : on ne compare que les caracteres significatifs — un IBAN
    ecrit avec des espaces et le meme sans espaces doivent se rencontrer."""
    return re.sub(r"[^A-Z0-9]", "", str(valeur or "").upper())


def _mot_normalise(valeur: str) -> str:
    return strip_accents_for_matching(str(valeur or "").strip()).upper()


# nom -> (libelle, extracteur client, extracteur fiche listee)
BLOCKING_FIELDS = {
    "DOB_YEAR": (
        "Année de naissance",
        lambda e: _annee(e.get("client_dob")),
        lambda e: _annee(_premier(e.get("dates_of_birth"))),
    ),
    "GENDER": (
        "Genre",
        lambda e: (e.get("client_gender") or "").strip().upper()[:1],
        lambda e: (e.get("gender") or "").strip().upper()[:1],
    ),
    "PLACE_OF_BIRTH": (
        "Lieu de naissance",
        lambda e: _mot_normalise(e.get("client_place_of_birth")),
        lambda e: _mot_normalise(e.get("place_of_birth")),
    ),
    "CITY": (
        "Ville",
        lambda e: _mot_normalise(e.get("client_city")),
        lambda e: _mot_normalise(e.get("city")),
    ),
    "TAX_ID": (
        "Identifiant fiscal",
        lambda e: _alphanum(e.get("client_tax_id")),
        lambda e: _alphanum(e.get("tax_id")),
    ),
    "LEI": (
        "Identifiant LEI",
        lambda e: _alphanum(e.get("client_lei_number")),
        lambda e: _alphanum(e.get("lei_number")),
    ),
    "BIC": (
        "Code BIC/SWIFT",
        lambda e: _alphanum(e.get("client_bic")),
        lambda e: _alphanum(e.get("bic_swift")),
    ),
    "IBAN": (
        "IBAN",
        lambda e: _alphanum(e.get("client_iban")),
        lambda e: _alphanum(e.get("iban")),
    ),
    "IMO": (
        "Numéro IMO (navire)",
        lambda e: _alphanum(e.get("client_imo_number")),
        lambda e: _alphanum(e.get("imo_number")),
    ),
    "NATIONAL_REGISTRY": (
        "Identifiant de registre national",
        lambda e: _alphanum(_premier(e.get("client_national_registry_ids"))),
        lambda e: _alphanum(_premier(e.get("national_registry_ids"))),
    ),
}


def field_component_value(item: str, entity: dict, is_client: bool) -> str:
    """Valeur de blocking d'un champ, ou le joker si le champ est absent."""
    if entity.get(f"__joker_{item}"):
        return FIELD_WILDCARD  # variante de sonde : ce champ est jokerise
    _libelle, cote_client, cote_liste = BLOCKING_FIELDS[item]
    try:
        valeur = cote_client(entity) if is_client else cote_liste(entity)
    except Exception:
        valeur = ""
    return valeur if valeur else FIELD_WILDCARD


def generate_blocking_keys(entity: dict, config: dict) -> Set[str]:
    """
    Generates a set of blocking keys for an entity based on the configured layout.
    Supports both listed entities (I, E, V, O) and client base records (PP, PM).
    """
    blocking_config = config.get("blocking", {})
    layout = blocking_config.get("custom_key_layout", ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"])
    # Le canal voyage dans la config (cf. settings.blocking_config_for) : les
    # capacites du moteur se reglent par canal, et les gardes ci-dessous
    # doivent savoir lequel s'applique.
    channel = blocking_config.get("channel", caps.CHANNEL_SCREENING)
    
    is_client = "client_type" in entity or "client_id" in entity
    
    components_values = {}
    
    for item in layout:
        if item == "COUNTRY_ISO":
            all_countries = []
            if is_client:
                countries_dict = entity.get("client_countries", {}) or {}
                nationality = countries_dict.get("nationality", []) or []
                residence = countries_dict.get("residence", []) or []
                birth = countries_dict.get("birth_country", []) or []
                reg = countries_dict.get("registration_country", []) or []
                all_countries = list(set(nationality + residence + birth + reg))
            else:
                countries_dict = entity.get("countries", {}) or {}
                citizenship = countries_dict.get("citizenship", []) or []
                residence = countries_dict.get("residence", []) or []
                birth = countries_dict.get("birth_country", []) or []
                jurisdiction = countries_dict.get("jurisdiction_country", []) or []
                all_countries = list(set(citizenship + residence + birth + jurisdiction))
                
            all_countries = [c.upper().strip() for c in all_countries if c and str(c).strip()]
            
            if not all_countries:
                components_values[item] = ["XX"]
            else:
                # Les classes d'equivalence viennent EN PLUS des valeurs
                # brutes : aucune paire aujourd'hui candidate ne cesse de l'etre
                equivalences = (_country_equivalence_values(all_countries)
                                if caps.is_active(caps.CAP_BLOCKING_EQUIVALENCES, channel)
                                else set())
                components_values[item] = sorted(set(all_countries) | equivalences)
                
        elif item == "ENTITY_TYPE":
            if is_client:
                ctype = entity.get("client_type", "") or ""
                ctype = ctype.upper().strip()
                # Map client PP -> PP, PM -> PM
                if ctype not in ["PP", "PM"]:
                    components_values[item] = ["XX"]
                else:
                    components_values[item] = [ctype]
            else:
                etype = entity.get("entity_type", "") or ""
                etype = etype.upper().strip()
                # Map watchlist I -> PP (Individual), E/V/O -> PM (Non-Individual)
                if etype == "I":
                    components_values[item] = ["PP"]
                elif etype in ["E", "V", "O"]:
                    components_values[item] = ["PM"]
                else:
                    components_values[item] = ["XX"]
                    
        elif item in BLOCKING_FIELDS:
            # Champ libre : sa valeur, ou le joker s'il est absent. Le joker
            # est ce qui empeche un champ ajoute de faire disparaitre toutes
            # les fiches listees qui ne le renseignent pas.
            components_values[item] = [field_component_value(item, entity, is_client)]

        elif item == "PHONETIC_FIRST":
            names = []
            if is_client:
                client_type = entity.get("client_type", "")
                if client_type == "PP":
                    first = entity.get("client_first_name", "") or ""
                    last = entity.get("client_last_name", "") or ""
                    maiden = entity.get("client_maiden_name", "") or ""
                    if first.strip():
                        names.append(first)
                    if last.strip():
                        names.append(last)
                    if maiden.strip():
                        names.append(maiden)
                else:
                    company = entity.get("client_company_name", "") or ""
                    if company.strip():
                        names.append(company)
                # Alias du client : sans cle de blocking sur l'alias, la paire
                # ne serait JAMAIS candidate et le scoring ne le verrait pas.
                # Meme capacite que le scoring : couper l'une coupe l'autre, et
                # l'index reste coherent avec la sonde.
                if caps.is_active(caps.CAP_NAMES_ALIASES_CLIENT, channel):
                    for alias in (entity.get("client_aliases")
                                  or entity.get("aliases") or []):
                        if alias and str(alias).strip():
                            names.append(str(alias))
            else:
                primary_name = entity.get("primary_name", "") or ""
                if primary_name.strip():
                    names.append(primary_name)
                
                # Check parsed maiden name
                parsed = entity.get("individual_name_parsed", {}) or {}
                maiden = parsed.get("maiden_name", "") or ""
                if maiden.strip():
                    names.append(maiden)
                
                # Check aliases (Only high priority ones!)
                raw_aliases = entity.get("aliases", []) or []
                if isinstance(raw_aliases, dict):
                    aliases = raw_aliases.get("high_priority", []) or []
                else:
                    # If it's a flat list, we filter it dynamically using the qualification logic in ingest.py
                    # To avoid import loops, we implement a simple local filter:
                    aliases = []
                    for alias in raw_aliases:
                        if not alias:
                            continue
                        clean_a = re.sub(r"[\._\-]", " ", alias).strip()
                        words = clean_a.split()
                        # Low priority if single word, or len <= 4
                        if len(words) <= 1 or len(clean_a) <= 4:
                            continue
                        aliases.append(alias)
                
                for alias in aliases:
                    if alias and str(alias).strip():
                        names.append(alias)
            
            phonetics = set()
            for name in names:
                name_clean = str(name).strip()
                if not name_clean:
                    continue
                # Translitteration AVANT la cle phonetique. Le double metaphone
                # ne connait que l'alphabet latin : sur « 陈 », « 김 » ou
                # « Владимир » il retourne une cle VIDE. Une fiche ecrite dans
                # son ecriture d'origine ne produisait donc AUCUNE cle
                # phonetique et n'etait candidate de rien — quel que soit le
                # contenu des tables d'equivalences. Le scoring, lui,
                # translitterait deja des deux cotes : les deux etages se
                # contredisaient.
                latin = strip_accents_for_matching(name_clean, channel)
                words = re.split(r"[\s\-]+", latin) or [""]
                first_word = words[0] if words else ""
                # PREMIER *ET* DERNIER mot, exactement pour la raison deja
                # ecrite plus bas au sujet des equivalences : une fiche listee
                # tient son nom complet dans UNE chaine (« JOSE GARCIA
                # LOPEZ »), la ou un client a des champs separes et emet donc
                # deja une cle pour son prenom ET une pour son nom. En ne
                # regardant que le premier mot cote liste, la partition ne
                # pouvait se rejoindre que par le PRENOM.
                #
                # Mesure sur 393 fiches reelles de la production : un client
                # dont le prenom est reduit a l'initiale ne rencontrait sa
                # fiche que dans 0,8 % des cas, et un client sans prenom (nom
                # de famille seul, cas ordinaire d'un message de paiement)
                # dans 0 % — le criblage rendait « aucune correspondance »
                # sans avoir jamais compare quoi que ce soit.
                #
                # Les cles d'equivalence ci-dessous portaient deja sur le
                # dernier mot, et ouvraient donc un pont : mesure faite, il ne
                # portait que 12,7 % et 12,0 % des cas, tables de prenoms ET de
                # noms activees. Elles ne connaissent qu'une part des noms de
                # famille — « LOPEZ » oui, « GARCIA » non — la ou la cle
                # phonetique ne demande rien a personne.
                mots_cles = {first_word}
                if caps.is_active(caps.CAP_BLOCKING_PHONETIC_LAST, channel):
                    mots_cles.add(words[-1] if words else "")
                for word in mots_cles:
                    if word and caps.is_active(caps.CAP_BLOCKING_PHONETIC, channel):
                        p_key, s_key = double_metaphone(word)
                        if p_key:
                            phonetics.add(p_key)
                        if s_key:
                            phonetics.add(s_key)
                # Equivalences linguistiques : sans cette cle, « Henri » et
                # « Harry » n'atterrissent jamais dans le meme seau et ne sont
                # donc JAMAIS compares — la table de ressources serait sans
                # effet. Les cles sont ADDITIVES : les cles phonetiques
                # ci-dessus restent produites, aucune paire aujourd'hui
                # candidate ne cesse de l'etre.
                #
                # PREMIER *ET* DERNIER mot. La cle phonetique, elle, ne porte
                # que sur le premier mot : c'est le choix historique du
                # composant. Mais une fiche listee tient son nom complet dans
                # UNE seule chaine (« Muammar Gaddafi »), la ou un client a des
                # champs separes. En ne regardant que le premier mot, une
                # equivalence de NOM DE FAMILLE ne pouvait donc jamais creer de
                # pont vers une fiche listee : le client emettait EQGADDAFI, la
                # fiche n'emettait que les cles de « Muammar ». La table des
                # noms de famille etait inerte sur ce cas, qui est le cas
                # ordinaire.
                if caps.is_active(caps.CAP_BLOCKING_EQUIVALENCES, channel):
                    for word in {first_word, words[-1] if words else ""}:
                        if word:
                            for eq_key in _equivalence_keys(word):
                                phonetics.add(eq_key)
                        
            if not phonetics:
                components_values[item] = ["XX"]
            else:
                components_values[item] = list(phonetics)
        else:
            components_values[item] = ["XX"]
            
    # Compute Cartesian Product
    keys = {""}
    for item in layout:
        new_keys = set()
        values = components_values[item]
        for val in values:
            for k in keys:
                new_keys.add(f"{k}_{val}" if k else val)
        keys = new_keys
        
    return keys


# ------------------ CLES D'INTERROGATION (cote requete) ------------------
# L'index de criblage est bati sur les cles des fiches LISTEES ; le client
# interroge cet index avec les siennes. La dissymetrie compte : une fiche
# listee sans pays tombe dans la partition « XX », que ne rejoint AUCUN client
# ayant un pays. Elle est donc structurellement inatteignable — et ce n'est pas
# un cas de bord : les listes d'alerte de regulateurs ne publient presque
# jamais de pays, EUR-Lex scrape des fiches sans geographie, la CSL en compte.
#
# La correction se pose du COTE REQUETE, pas du cote index : le client
# interroge en plus la variante « pays inconnu » de ses propres cles. C'est
# strictement additif — aucune paire aujourd'hui candidate ne cesse de l'etre —
# et cela preserve le partitionnement : une fiche listee QUI PORTE un pays
# n'est toujours atteinte que par les clients de ce pays. Le surcout est borne
# par le nombre de fiches sans pays, pas par la taille de la base.


def _without_countries(entity: dict) -> dict:
    """Copie de l'entite privee de sa geographie (le reste est inchange)."""
    stripped = dict(entity)
    if "client_type" in entity or "client_id" in entity:
        stripped["client_countries"] = {}
    else:
        stripped["countries"] = {}
    return stripped


def lookup_blocking_keys(entity: dict, config: dict) -> Set[str]:
    """
    Cles a interroger dans l'index de criblage pour cette entite.

    Ce sont ses cles propres, PLUS la variante dont la composante pays vaut
    « pays inconnu » — la seule facon d'atteindre les fiches listees dont la
    source ne publie pas de geographie. Quand COUNTRY_ISO n'est pas dans le
    layout, les deux ensembles coincident et la fonction ne coute rien.
    """
    keys = generate_blocking_keys(entity, config)
    blocking_cfg = config.get("blocking", {}) or {}
    layout = blocking_cfg.get(
        "custom_key_layout", ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"])

    # --- Jokers des composantes de CHAMP ---
    # Une fiche listee qui ne renseigne pas le champ porte le joker. Sans
    # interroger aussi les variantes jokerisees, ajouter « Année de naissance »
    # ferait perdre toutes les fiches sans date — soit l'essentiel des listes
    # officielles. On interroge donc toutes les combinaisons de jokers : un
    # meme criblage doit atteindre une fiche a qui il manque n'importe quel
    # sous-ensemble de ces champs.
    champs = [c for c in layout if c in BLOCKING_FIELDS]
    if champs:
        from itertools import combinations
        for taille in range(1, len(champs) + 1):
            for jokerises in combinations(champs, taille):
                variante = dict(entity)
                for item in jokerises:
                    variante[f"__joker_{item}"] = True
                keys |= generate_blocking_keys(variante, config)

    if "COUNTRY_ISO" not in layout:
        return keys
    # Deux interrupteurs, et le plus restrictif gagne : le reglage de fichier
    # historique (config.yaml) ET la capacite reglable a chaud.
    if not blocking_cfg.get("country_wildcard", True):
        return keys
    channel = blocking_cfg.get("channel", caps.CHANNEL_SCREENING)
    if not caps.is_active(caps.CAP_BLOCKING_COUNTRY_WILDCARD, channel):
        return keys
    return keys | generate_blocking_keys(_without_countries(entity), config)
