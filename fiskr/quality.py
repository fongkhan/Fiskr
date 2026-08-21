import re
import unicodedata
from datetime import datetime
from functools import lru_cache

from fiskr import capabilities as caps

# Translitteration multi-ecritures (cyrillique, arabe, CJK, grec, hebreu...)
# vers le latin : indispensable pour que les alias non latins des listes
# officielles (OFAC, ONU) matchent les noms latins du referentiel clients.
# Repli silencieux sur l'aplatissement de diacritiques si absent.
try:
    from anyascii import anyascii as _transliterate
    TRANSLIT_AVAILABLE = True
except ImportError:
    _transliterate = None
    TRANSLIT_AVAILABLE = False

# Fin du bloc « Latin Extended-B ». Au-dela, on n'est plus dans une ecriture
# latine : c'est le critere, et non une liste d'ecritures connues.
_LAST_LATIN_CODEPOINT = 0x024F

# Plages de points de code par ECRITURE. Elles ne servent pas a decider SI on
# translittere — c'est le role de `_is_non_latin` ci-dessous, inchange — mais a
# NOMMER l'ecriture rencontree, ce qu'aucun code du depot ne savait faire.
# `has_non_latin_chars` etait binaire : « latin / non latin ». Il etait donc
# impossible de traiter le cyrillique autrement que le chinois, alors que ce
# sont deux decisions de conformite distinctes — un etablissement expose a la
# Russie et pas a la Chine n'a aucune raison de payer le cout de l'un pour
# l'autre.
#
# Les plages suivent l'attribution Unicode par bloc. Ce qui n'entre dans aucune
# n'est pas ignore : il tombe dans « other », qui a sa propre bascule. Aucune
# ecriture ne peut donc echapper au reglage par oubli de plage.
_SCRIPT_RANGES = (
    ("cyrillic", ((0x0400, 0x04FF), (0x0500, 0x052F), (0x2DE0, 0x2DFF),
                  (0xA640, 0xA69F))),
    ("greek", ((0x0370, 0x03FF), (0x1F00, 0x1FFF))),
    ("hebrew", ((0x0590, 0x05FF), (0xFB1D, 0xFB4F))),
    ("arabic", ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
                (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
    ("devanagari", ((0x0900, 0x097F), (0xA8E0, 0xA8FF))),
    ("thai", ((0x0E00, 0x0E7F),)),
    # Hangul avant han : les jamo et les syllabes coreennes ont leurs propres
    # blocs, mais un texte coreen ancien peut melanger hanja (han) et hangul.
    ("hangul", ((0x1100, 0x11FF), (0x3130, 0x318F), (0xA960, 0xA97F),
                (0xAC00, 0xD7AF), (0xD7B0, 0xD7FF))),
    ("kana", ((0x3040, 0x309F), (0x30A0, 0x30FF), (0x31F0, 0x31FF),
              (0xFF66, 0xFF9D))),
    ("han", ((0x2E80, 0x2FDF), (0x3005, 0x3007), (0x3400, 0x4DBF),
             (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2A6DF),
             (0x2A700, 0x2EBEF))),
)

SCRIPT_OTHER = "other"


def _is_non_latin(char: str) -> bool:
    """
    Critere HISTORIQUE, inchange : au-dela du latin etendu B, et ni
    ponctuation, ni separateur, ni caractere de controle.

    Il reste le seul juge de « faut-il translitterer ce caractere ». Le
    nommage d'ecriture vient APRES et ne peut donc pas elargir ni restreindre
    le perimetre de translitteration existant.
    """
    if ord(char) <= _LAST_LATIN_CODEPOINT or char.isspace():
        return False
    # Ponctuation et symboles generaux ne justifient pas une translitteration
    # du nom entier (tiret cadratin, guillemets...)
    return unicodedata.category(char)[0] not in ("P", "Z", "C")


def script_of(char: str) -> str:
    """Ecriture d'un caractere non latin. « other » si aucune plage connue."""
    code = ord(char)
    for name, ranges in _SCRIPT_RANGES:
        for start, end in ranges:
            if start <= code <= end:
                return name
    return SCRIPT_OTHER


def detect_scripts(text: str) -> frozenset:
    """
    Ecritures non latines presentes dans le texte.

    Vide pour un texte purement latin — accents et diacritiques compris, qui
    restent latins. Un nom peut en contenir plusieurs : « 陈 Quanguo »,
    « ООО Ромашка Ltd ».
    """
    return frozenset(script_of(c) for c in (text or "") if _is_non_latin(c))


def has_non_latin_chars(text: str) -> bool:
    """
    Vrai si le texte sort de l'ecriture latine (accents et diacritiques
    compris, qui restent latins).

    Le test porte sur le POINT DE CODE, pas sur le nom Unicode du caractere.
    La version precedente cherchait les mots CYRILLIC / ARABIC / CJK / HEBREW /
    THAI / GREEK dans le nom du caractere : une liste blanche d'ecritures, donc
    fausse par construction pour toutes les autres. « 김 » se nomme HANGUL
    SYLLABLE GIM — aucun de ces mots — et n'etait donc JAMAIS translittere ;
    idem pour les kana japonais (HIRAGANA LETTER A, KATAKANA LETTER A), le
    devanagari, l'armenien, le georgien, l'ethiopien. Ces noms traversaient
    tout le criblage dans leur ecriture d'origine, ou aucune metrique de chaine
    ni aucune cle phonetique ne pouvait rien en faire.

    Le repli `except ValueError` ne rattrapait pas le cas : le hangul et les
    kana ONT un nom Unicode.
    """
    return any(_is_non_latin(c) for c in text or "")

def _strip_combining(text: str) -> str:
    nfkd_form = unicodedata.normalize('NFKD', text)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

def strip_accents(text: str) -> str:
    """
    Normalise un nom vers le latin ASCII : translittere d'abord les ecritures
    non latines (cyrillique Владимир -> Vladimir, arabe, CJK...) quand le
    texte en contient, puis retire accents et diacritiques (Müller -> Muller).
    Utilise partout (nettoyage a l'ingestion, scoring des deux cotes).

    INCONDITIONNEL, et cela n'est pas un oubli. `resources.normalize_term` bat
    l'index des equivalences avec cette fonction, et la recherche d'API s'en
    sert aussi : si la normalisation devenait reglable ICI, l'index cesserait
    de retrouver ses propres entrees des qu'un reglage changerait. La variante
    reglable est `strip_accents_for_matching`, reservee a la COMPARAISON.
    """
    # Meme voie rapide que `strip_accents_for_matching`, meme demonstration :
    # ASCII n'a ni ecriture non latine ni caractere combinant.
    if text.isascii():
        return text
    if TRANSLIT_AVAILABLE and text and has_non_latin_chars(text):
        text = _transliterate(text)
    return _strip_combining(text)


def _transliterate_selected(text: str, scripts) -> str:
    """
    Translittere les seuls caracteres des ecritures citees, caractere par
    caractere. Chemin emprunte UNIQUEMENT quand un texte melange des ecritures
    dont certaines sont coupees : quand elles sont toutes actives, on repasse
    par `_transliterate` sur la chaine entiere, donc le rendu d'aujourd'hui
    est preserve au caractere pres.
    """
    out = []
    for char in text:
        if _is_non_latin(char) and script_of(char) in scripts:
            out.append(_transliterate(char))
        else:
            out.append(char)
    return "".join(out)


def strip_accents_for_matching(text: str, channel: str = caps.CHANNEL_SCREENING) -> str:
    """
    Variante REGLABLE de `strip_accents`, reservee a la comparaison (blocking,
    scoring, nettoyage du client au criblage).

    Deux reglages y jouent :
    - la translitteration, globalement puis ecriture par ecriture. Une ecriture
      coupee traverse le moteur telle quelle : aucune metrique de chaine ne
      peut rien en faire et le double metaphone rend une cle vide. C'est
      exactement la perte annoncee par le catalogue.
    - l'aplatissement des diacritiques : coupe, « Müller » cesse de rapprocher
      « MULLER ».

    Toutes capacites actives, le resultat est celui de `strip_accents`.

    Memoise : au criblage d'un univers, les memes noms (cote client comme cote
    liste) traversent la normalisation des dizaines de fois — chaque nom client
    est compare a chaque candidat, chaque nom liste a chaque client. La cle du
    cache inclut le CONTEXTE effectif des capacites : un changement de reglage
    (translitteration, ecritures, diacritiques) produit un contexte different,
    donc une nouvelle entree, sans invalidation explicite.
    """
    # Voie rapide : un texte purement ASCII traverse cette fonction INCHANGE,
    # quelles que soient les capacites. `detect_scripts` n'y trouve aucune
    # ecriture non latine (donc aucune translitteration) et la decomposition
    # NFKD d'ASCII est ASCII sans caractere combinant (donc aucun diacritique
    # a retirer). Mesure sur un echantillon reel de la production : 98,3 % des
    # noms listes sont ASCII purs, et le detour coutait 1,01 us par appel avec
    # cache chaud contre 0,23 us par cette voie (x4,5) — sans cache, 5,39 us
    # contre 0,34 (x16). C'est le chemin le plus chaud du moteur : il est
    # emprunte deux fois par comparaison, sur un univers entier de candidats.
    if text.isascii():
        return text
    return _strip_accents_for_matching_cached(
        text, channel, caps.current_context(channel))


@lru_cache(maxsize=200_000)
def _strip_accents_for_matching_cached(text, channel, _active):
    # `_active` (frozenset du contexte effectif) ne sert QUE de cle de cache :
    # le corps relit caps.is_active, qui rend la meme valeur pour ce contexte.
    if TRANSLIT_AVAILABLE and text and caps.is_active(caps.CAP_TRANSLIT, channel):
        scripts = detect_scripts(text)
        if scripts:
            actives = {s for s in scripts
                       if caps.is_active(caps.script_capability(s), channel)}
            if actives == scripts:
                text = _transliterate(text)
            elif actives:
                text = _transliterate_selected(text, actives)
    if not caps.is_active(caps.CAP_DIACRITICS, channel):
        return text
    return _strip_combining(text)

def clean_noise_words(text: str, channel: str = None) -> str:
    """Removes corporate noise suffixes (SA, SARL, LLC, LTD, GMBH, SOCIETE) for PMs.

    `channel` absent = normalisation INCONDITIONNELLE. C'est le cas de
    l'ingestion : ce qui est stocke ne doit pas dependre d'un reglage a chaud,
    sinon deux fiches de la meme liste porteraient des formes differentes
    selon l'heure de leur import. Le reglage n'agit que sur le chemin de
    COMPARAISON, ou un canal est fourni.
    """
    if channel is not None and not caps.is_active(caps.CAP_NOISE_WORDS, channel):
        return text
    pattern = r"\b(SA|SARL|LLC|LTD|GMBH|SOCIETE)\b"
    cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()

def validate_date(date_str: str) -> bool:
    """Validates if date is in strict YYYY-MM-DD format."""
    if not date_str:
        return False
    try:
        datetime.strptime(date_str.strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False

def validate_lei(lei: str) -> bool:
    """Checks if LEI is 20-character alphanumeric."""
    if not lei:
        return False
    lei_clean = lei.strip()
    return len(lei_clean) == 20 and lei_clean.isalnum()

def evaluate_and_clean(entity: dict, channel: str = None) -> dict:
    """
    Evaluates upgraded data quality rules on Watchlist or Client entries.
    Identifies Level 1 REJECT, Level 2 WARNING/DEGRADED, and Level 3 AUTO-CLEAN.

    `channel` absent = INGESTION : la normalisation est inconditionnelle, et
    c'est voulu. Ce qui est stocke est verse au dossier reglementaire avec son
    instantane de liste ; le faire dependre d'un reglage a chaud rendrait deux
    fiches de la meme liste normalisees differemment selon l'heure de leur
    import, et effacerait retroactivement la forme d'origine.

    `channel` fourni = COMPARAISON : c'est le nettoyage de la sonde client au
    criblage, ou les capacites du moteur s'appliquent.

    CONSEQUENCE A CONNAITRE, et elle est asymetrique : couper une ecriture agit
    immediatement sur le cote CLIENT ; les fiches deja ingerees restent
    normalisees telles qu'elles ont ete stockees, jusqu'au prochain
    rechargement complet de leur liste.
    """
    errors = []
    warnings = []
    
    # 1. Determine if this is a Client base record or a Watchlist entry
    is_client = "client_type" in entity or "client_id" in entity
    
    if is_client:
        # Client Referentials
        client_id = entity.get("client_id", "")
        client_type = entity.get("client_type", "")
        first_name = entity.get("client_first_name", "") or ""
        last_name = entity.get("client_last_name", "") or ""
        company_name = entity.get("client_company_name", "") or ""
        maiden_name = entity.get("client_maiden_name", "") or ""
        dob = entity.get("client_dob", "") or ""
        gender = entity.get("client_gender", "U") or "U"
        is_deceased = str(entity.get("client_is_deceased", "False")).lower() == "true"
        
        # Countries
        countries_dict = entity.get("client_countries", {}) or {}
        nationality = countries_dict.get("nationality", []) or []
        residence = countries_dict.get("residence", []) or []
        birth = countries_dict.get("birth_country", []) or []
        reg = countries_dict.get("registration_country", []) or []
        all_countries = list(set(nationality + residence + birth + reg))
        
        # LEI
        lei = entity.get("client_lei_number", "") or ""
        
        # Primary Name resolution
        if client_type == "PP":
            primary_name = f"{first_name} {last_name}".strip()
        else:
            primary_name = company_name.strip()
            
    else:
        # Watchlist Entities
        entity_id = entity.get("entity_id", "")
        entity_type = entity.get("entity_type", "")
        primary_name = entity.get("primary_name", "") or ""
        
        parsed_name = entity.get("individual_name_parsed", {}) or {}
        first_name = parsed_name.get("first_name", "") or ""
        last_name = parsed_name.get("last_name", "") or ""
        maiden_name = parsed_name.get("maiden_name", "") or ""
        
        dob_list = entity.get("dates_of_birth", []) or []
        dob = dob_list[0] if dob_list else ""
        
        gender = entity.get("gender", "U") or "U"
        is_deceased = str(entity.get("is_deceased", "False")).lower() == "true"
        date_of_death = entity.get("date_of_death", "") or ""
        
        # Countries
        countries_dict = entity.get("countries", {}) or {}
        citizenship = countries_dict.get("citizenship", []) or []
        residence = countries_dict.get("residence", []) or []
        birth = countries_dict.get("birth_country", []) or []
        jurisdiction = countries_dict.get("jurisdiction_country", []) or []
        all_countries = list(set(citizenship + residence + birth + jurisdiction))
        
        # LEI
        lei = entity.get("lei_number", "") or ""

    # ------------------ LEVEL 1: CRITICAL / REJECT ------------------
    # Rule_B01: Champ Nom Principal Vide
    if is_client:
        if client_type == "PP" and not last_name.strip():
            errors.append("Rule_B01: Champ Nom Principal Vide (client_last_name manquant)")
        elif client_type == "PM" and not company_name.strip():
            errors.append("Rule_B01: Champ Nom Principal Vide (client_company_name manquant)")
        elif not client_type and not primary_name.strip():
            errors.append("Rule_B01: Champ Nom Principal Vide")
    else:
        if not primary_name.strip():
            errors.append("Rule_B01: Champ Nom Principal Vide")

    # Rule_B02: Type d'Entité Invalide ou Incohérent
    if is_client:
        if client_type not in ["PP", "PM"]:
            errors.append(f"Rule_B02: Type d'Entité Invalide côté Client ({client_type} - doit être PP ou PM)")
    else:
        if entity_type not in ["I", "E", "V", "O"]:
            errors.append(f"Rule_B02: Type d'Entité Invalide côté Watchlist ({entity_type} - doit être I, E, V, ou O)")

    # Rule_B04: Incohérence Nom/Structure Individu (PP/I must have first and last names)
    current_type = client_type if is_client else entity_type
    if current_type in ["PP", "I"]:
        if not first_name.strip() and not last_name.strip():
            errors.append("Rule_B04: Incohérence Nom/Structure Individu (Prénom et Nom de famille absents)")

    # Rule_B05: Longueur Nom Insuffisante (Moins de 2 caractères alphanumériques nettoyés)
    alphanumeric_chars = "".join([c for c in primary_name if c.isalnum()])
    if primary_name.strip() and len(alphanumeric_chars) < 2:
        errors.append("Rule_B05: Longueur Nom Insuffisante (Moins de 2 caractères alphanumériques de base)")

    if errors:
        return {
            "is_valid": False,
            "status": "REJECT",
            "errors": errors,
            "warnings": warnings,
            "cleansed_name": primary_name,
            "cleansed_aliases": entity.get("aliases", []) or []
        }

    # ------------------ LEVEL 2: WARNING / DEGRADED ------------------
    # Rule_M01: Absence totale de géographie
    if not all_countries:
        warnings.append("Rule_M01: Absence totale de Géographie")

    # Rule_M02: Absence d'identifiant d'âge/existence
    if current_type in ["PP", "I"] and not is_deceased:
        if is_client and not dob:
            warnings.append("Rule_M02: Absence d'identifiant d'âge/existence (DOB manquante)")
        elif not is_client and not entity.get("dates_of_birth", []):
            warnings.append("Rule_M02: Absence d'identifiant d'âge/existence (DOB manquante)")

    # Rule_M03: Caractères Non Translittérés (Hors ASCII/Latin)
    if has_non_latin_chars(primary_name):
        warnings.append("Rule_M03: Présence de caractères hors blocs ASCII étendu/Latin")

    # Rule_M04: Contradiction Statut Vital Prémédité
    if not is_client and entity.get("date_of_death") and not is_deceased:
        warnings.append("Rule_M04: Contradiction Statut Vital Prémédité (date de décès fournie mais is_deceased est False)")
        # Force is_deceased for scoring downstream
        entity["is_deceased"] = True

    # Rule_M05: Format Date Invalide
    dates_to_test = []
    if is_client and dob:
        dates_to_test.append(dob)
    elif not is_client:
        dates_to_test.extend(entity.get("dates_of_birth", []))
        if entity.get("date_of_death"):
            dates_to_test.append(entity.get("date_of_death"))
            
    for d in dates_to_test:
        if d and not validate_date(d):
            warnings.append(f"Rule_M05: Format Date Invalide ({d} - doit respecter YYYY-MM-DD)")

    # Rule_M06: Format Numéro Passeport Suspect
    passports = []
    if is_client:
        passports = entity.get("client_passport_documents", []) or []
    else:
        passports = entity.get("passport_documents", []) or []
    for p in passports:
        pnum = p.get("number", "") if isinstance(p, dict) else ""
        if pnum:
            if len(pnum.strip()) < 4 or any(c in pnum for c in [" ", "-", "_", "@", "#"]):
                warnings.append(f"Rule_M06: Format Numéro Passeport Suspect ({pnum})")

    # Rule_M07: Structure LEI Invalide
    if lei:
        if not validate_lei(lei):
            warnings.append(f"Rule_M07: Structure LEI Invalide ({lei} - doit faire 20 caractères alphanumériques)")

    # Rule_M08: Échec d'Extraction PDF (si score de confiance < 85%)
    confidence = entity.get("extraction_confidence")
    if confidence is not None and float(confidence) < 85.0:
        warnings.append(f"Rule_M08: Échec d'Extraction de Confiance PDF (score de {confidence}%)")

    status = "DEGRADED" if warnings else "OK"

    # ------------------ LEVEL 3: AUTO-CLEAN & Cleansing ------------------
    # Rule_I03: Incohérence de Genre Multi-valué (Fallback to U)
    resolved_gender = "U"
    raw_genders = entity.get("genders", []) or []
    if not is_client and not raw_genders and entity.get("gender"):
        raw_genders = [entity.get("gender")]
    elif is_client and entity.get("client_gender"):
        raw_genders = [entity.get("client_gender")]
        
    g_clean = list(set(str(g).upper().strip() for g in raw_genders if g))
    if len(g_clean) == 1 and g_clean[0] in ["M", "F"]:
        resolved_gender = g_clean[0]
    elif len(g_clean) > 1:
        warnings.append("Rule_I03: Incohérence de Genre Multi-valué (Force le repli sur U)")
        resolved_gender = "U"

    def cleanse_text(t: str, is_pm: bool) -> str:
        # Rule_I01: Espaces multiples
        t = re.sub(r"\s+", " ", t).strip()
        
        # Rule_I02: Caractères Spéciaux de Saisie (Replace points, underscores with spaces)
        t = re.sub(r"[\._\-]", " ", t)
        t = re.sub(r"[@#\$%\^&\*\(\)\+=\{\}\[\]\|\\:;\"<>\?,]", "", t)
        
        # Standard casing
        t = t.upper()
        
        # Accent stripping. Au criblage (canal fourni) la normalisation obeit
        # aux capacites ; a l'ingestion elle reste inconditionnelle.
        t = strip_accents(t) if channel is None else strip_accents_for_matching(t, channel)

        # PM Noise Suffixes
        if is_pm:
            t = clean_noise_words(t, channel)

        return re.sub(r"\s+", " ", t).strip()

    # Clean primary name
    is_pm_type = current_type in ["PM", "E", "V"]
    cleansed_primary_name = cleanse_text(primary_name, is_pm_type)
    
    # Clean aliases
    raw_aliases = entity.get("aliases", []) or []
    if isinstance(raw_aliases, dict):
        # Already structured as {"high_priority": [], "low_priority": []}
        high = [cleanse_text(a, is_pm_type) for a in raw_aliases.get("high_priority", []) if a]
        low = [cleanse_text(a, is_pm_type) for a in raw_aliases.get("low_priority", []) if a]
        cleansed_aliases = {"high_priority": high, "low_priority": low}
    else:
        # List of strings, clean all
        cleansed_aliases = [cleanse_text(a, is_pm_type) for a in raw_aliases if a]

    # Clean maiden name
    cleansed_maiden_name = cleanse_text(maiden_name, False) if maiden_name else ""

    return {
        "is_valid": True,
        "status": status,
        "errors": [],
        "warnings": warnings,
        "cleansed_name": cleansed_primary_name,
        "cleansed_maiden_name": cleansed_maiden_name,
        "cleansed_aliases": cleansed_aliases,
        "resolved_gender": resolved_gender
    }
