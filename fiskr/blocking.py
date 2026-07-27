import re
from typing import Set, List
from fiskr.phonetics import double_metaphone
from fiskr.quality import strip_accents


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


def generate_blocking_keys(entity: dict, config: dict) -> Set[str]:
    """
    Generates a set of blocking keys for an entity based on the configured layout.
    Supports both listed entities (I, E, V, O) and client base records (PP, PM).
    """
    blocking_config = config.get("blocking", {})
    layout = blocking_config.get("custom_key_layout", ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"])
    
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
                components_values[item] = sorted(
                    set(all_countries) | _country_equivalence_values(all_countries))
                
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
                latin = strip_accents(name_clean)
                words = re.split(r"[\s\-]+", latin) or [""]
                first_word = words[0] if words else ""
                if first_word:
                    p_key, s_key = double_metaphone(first_word)
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
    if "COUNTRY_ISO" not in layout:
        return keys
    if not blocking_cfg.get("country_wildcard", True):
        return keys
    return keys | generate_blocking_keys(_without_countries(entity), config)
