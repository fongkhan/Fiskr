"""
Client et personne listee cote a cote, dans l'alerte.

L'alerte n'affichait que deux noms et l'explication du score. Pour cocher
« Identite verifiee (nom, date de naissance, pays) », l'analyste ouvrait
« Client 360° » — troisieme modale empilee — pour la date de naissance du
client, puis la palette (Ctrl+K, la reference, Entree) pour celle de la
personne listee : environ neuf gestes par alerte, mesures dans un vrai
navigateur, et deux fenetres qu'aucune ne montrait ensemble.

Les lignes du tableau sont les comparaisons DU MOTEUR, relevees en le faisant
tourner (`identification.comparaisons_du_moteur`) : noms, naissance, genre,
pays, identifiants — avec, pour chacune, les champs client et les champs fiche
qu'il met en regard. Aucune correspondance de champs n'est ecrite ici : le jour
ou le moteur compare un champ de plus, la colonne le montre.

Les marques (concordance, ecart) sont le verdict du moteur sur les DEUX
colonnes affichees, recalcule ici — pas celui du criblage, qui a pu porter sur
une autre identification du client — et avec toutes ses capacites : une
comparaison coupee dans le reglage du score ne rend pas egales deux dates de
naissance differentes. Une donnee absente d'un cote n'est jamais un ecart :
le moteur peut la penaliser (pays manquant), l'analyste n'a rien a confronter.
"""
from typing import Any, Dict, List, Optional, Tuple

VERDICT_CONCORDANCE = "CONCORDANCE"
VERDICT_ECART = "ECART"
VERDICT_NEUTRE = "NEUTRE"
VERDICT_ABSENT = "ABSENT"
VERDICT_VIDE = "VIDE"

# Libelles rendus tels quels par l'API et traduits par fiskr/static/i18n.js.
# Une ligne par libelle : le controle des traductions orphelines les cherche
# entiers.
VERDICTS = {
    VERDICT_CONCORDANCE: "Concordance",
    VERDICT_ECART: "Écart",
    VERDICT_NEUTRE: "Sans effet sur le score",
    VERDICT_ABSENT: "Absent d'un côté",
    VERDICT_VIDE: "Renseigné d'aucun côté",
}

FAMILLES = {
    "names": "Noms et alias",
    "dob": "Naissance",
    "gender": "Genre",
    "geography": "Nationalités et pays",
    "identifiers": "Identifiants",
}

# Le type du client choisit le chemin du moteur (personne physique ou morale) ;
# il n'est compare a rien.
_HORS_COMPARAISON = frozenset({"client_type"})

LIBELLES = {
    # noms
    "client_first_name": "Prénom",
    "client_last_name": "Nom",
    "client_maiden_name": "Nom de naissance",
    "client_company_name": "Raison sociale",
    "client_aliases": "Alias",
    "primary_name": "Nom principal",
    "aliases": "Alias",
    "aliases.high_priority": "Alias",
    "aliases.low_priority": "Alias de faible qualité (non comparés)",
    "individual_name_parsed.first_name": "Prénom",
    "individual_name_parsed.last_name": "Nom",
    "individual_name_parsed.maiden_name": "Nom de naissance",
    # naissance, genre
    "client_dob": "Date de naissance",
    "dates_of_birth": "Dates de naissance",
    "client_gender": "Genre",
    "gender": "Genre",
    "genders": "Genre",
    # pays
    "client_countries.nationality": "Nationalité",
    "client_countries.residence": "Résidence",
    "client_countries.birth_country": "Pays de naissance",
    "client_countries.registration_country": "Pays d'immatriculation",
    "countries.citizenship": "Nationalité",
    "countries.residence": "Résidence",
    "countries.birth_country": "Pays de naissance",
    "countries.jurisdiction_country": "Juridiction",
    "client_country": "Pays de l'adresse",
    "country": "Pays",
    "jurisdiction_country": "Juridiction",
    # identifiants
    "client_lei_number": "LEI",
    "lei_number": "LEI",
    "client_bic": "BIC / SWIFT",
    "bic_swift": "BIC / SWIFT",
    "client_tax_id": "Identifiant fiscal",
    "tax_id": "Identifiant fiscal",
    "client_crypto_wallets": "Adresses crypto",
    "crypto_wallets": "Adresses crypto",
    "client_passport_documents": "Passeport",
    "passport_documents": "Passeport",
    "client_national_registry_ids": "Registre national",
    "national_registry_ids": "Registre national",
    "client_national_id_documents": "Pièce d'identité nationale",
    "national_id_documents": "Pièce d'identité nationale",
    "transaction_vessel_imo": "Numéro IMO",
    "imo_number": "Numéro IMO",
    "transaction_aircraft_registration": "Immatriculation d'aéronef",
    "aircraft_tail_number": "Immatriculation d'aéronef",
    "transaction_vessel_mmsi": "MMSI",
    "vessel_mmsi": "MMSI",
    "transaction_vessel_call_sign": "Indicatif d'appel",
    "vessel_call_sign": "Indicatif d'appel",
    "client_other_id_documents": "Autres documents",
    "other_id_documents": "Autres documents",
    "client_other_registration_ids": "Autres immatriculations",
    "other_registration_ids": "Autres immatriculations",
    # contexte de la fiche
    "entity_id": "Référence de la fiche",
    "official_reference": "Référence officielle",
    "sanction_programs": "Programmes de sanction",
    "designation_reasons": "Motifs de la désignation",
    "designation": "Fonction / Désignation",
    "listed_on": "Inscrite le",
}

# Ce que la fiche dit de la mesure — motif, programme, reference. Le moteur ne
# le compare pas (il n'a rien en face cote client) : informatif, affiche sous
# le tableau, jamais marque.
CONTEXTE_DE_LA_FICHE = ("entity_id", "official_reference", "sanction_programs",
                        "designation_reasons", "designation", "listed_on")


def _texte_d_un_element(element: Any) -> Optional[str]:
    """Un element de liste lisible : un document « numero (pays, type) »."""
    if element is None or isinstance(element, bool):
        return None
    if isinstance(element, dict):
        valeurs = [str(v).strip() for v in element.values()
                   if v is not None and not isinstance(v, (dict, list, bool)) and str(v).strip()]
        if not valeurs:
            return None
        principal = str(element.get("number") or element.get("address") or valeurs[0]).strip()
        reste = [v for v in valeurs if v != principal]
        return f"{principal} ({', '.join(reste)})" if reste else principal
    texte = str(element).strip()
    return texte or None


def _textes(valeur: Any) -> List[str]:
    if valeur is None or isinstance(valeur, bool):
        return []
    if isinstance(valeur, (list, tuple, set)):
        return [t for t in (_texte_d_un_element(e) for e in valeur) if t]
    texte = _texte_d_un_element(valeur)
    return [texte] if texte else []


def _entrees(champ: str, valeur: Any) -> List[Dict[str, Any]]:
    """Les valeurs d'un champ, libellees ; un conteneur (pays, alias, nom
    decompose) s'ouvre en autant de lignes que de rubriques renseignees."""
    if isinstance(valeur, dict) and not ("number" in valeur or "address" in valeur):
        entrees = []
        for rubrique, contenu in valeur.items():
            textes = _textes(contenu)
            if textes:
                entrees.append({"champ": f"{champ}.{rubrique}",
                                "libelle": LIBELLES.get(f"{champ}.{rubrique}", rubrique),
                                "valeurs": textes})
        return entrees
    textes = _textes(valeur)
    return [{"champ": champ, "libelle": LIBELLES.get(champ, champ), "valeurs": textes}] if textes else []


def _cote(profil: Dict[str, Any], champs, famille: str) -> List[Dict[str, Any]]:
    entrees = []
    for champ in champs:
        if champ in _HORS_COMPARAISON:
            continue
        for entree in _entrees(champ, profil.get(champ)):
            if famille == "gender":
                # Regle du moteur : seuls M et F disent un genre.
                entree["valeurs"] = [v for v in entree["valeurs"] if v.upper() in ("M", "F")]
            if entree["valeurs"]:
                entrees.append(entree)
    return entrees


def _verdict(score: float, client, fiche) -> str:
    if not client and not fiche:
        return VERDICT_VIDE
    if not client or not fiche:
        return VERDICT_ABSENT
    if score > 0:
        return VERDICT_CONCORDANCE
    if score < 0:
        return VERDICT_ECART
    return VERDICT_NEUTRE


def _ligne(famille: str, libelle: str, verdict: Optional[str], detail: Optional[str],
           client, fiche, **autres) -> Dict[str, Any]:
    return {"famille": famille, "libelle": libelle, "verdict": verdict,
            "verdict_libelle": VERDICTS.get(verdict) if verdict else None,
            "detail": detail, "client": client, "fiche": fiche, **autres}


def comparer(profil: Dict[str, Any], fiche: Dict[str, Any], config: Dict[str, Any],
             channel: str) -> Dict[str, Any]:
    """
    Le tableau de comparaison d'un client et d'une fiche : une ligne par
    famille de comparaison du moteur (une par identifiant renseigne), puis le
    contexte de la fiche.
    """
    from fiskr import capabilities as caps
    from fiskr.identification import comparaisons_du_moteur
    from fiskr.scoring import check_hard_matches, match_entities

    with caps.use_context(channel, caps.CAPABILITY_CATALOG):
        arbre = match_entities(dict(profil), dict(fiche), config)
        ajustements = arbre.get("adjustments") or {}
        par_famille: Dict[str, List[Dict[str, Any]]] = {}
        for comparaison in comparaisons_du_moteur():
            famille = comparaison.famille
            if famille == "identifiers":
                par_famille[famille] = _identifiants(comparaison, profil, fiche, channel,
                                                     check_hard_matches)
                continue
            client = _cote(profil, comparaison.client, famille)
            cote_fiche = _cote(fiche, comparaison.fiche, famille)
            if famille == "names":
                par_famille[famille] = [_ligne(
                    famille, FAMILLES[famille], None, None, client, cote_fiche,
                    score_de_nom=arbre.get("base_score"),
                    noms_rapproches=[arbre.get("best_client_name") or "",
                                     arbre.get("best_watchlist_name") or ""])]
                continue
            ajustement = ajustements.get(famille) or {}
            par_famille[famille] = [_ligne(
                famille, FAMILLES.get(famille, famille),
                _verdict(float(ajustement.get("score") or 0.0), client, cote_fiche),
                ajustement.get("description"), client, cote_fiche,
                impact=ajustement.get("score"))]

    # Ordre de lecture de l'analyste (noms, naissance, genre, pays, puis
    # identifiants) plutot que l'ordre du moteur, qui commence par le hard
    # match. Une famille que le moteur apprendrait vient a la suite.
    ordre = list(FAMILLES) + [f for f in par_famille if f not in FAMILLES]
    lignes = [ligne for famille in ordre for ligne in par_famille.get(famille, [])]

    contexte = []
    for champ in CONTEXTE_DE_LA_FICHE:
        contexte.extend(_entrees(champ, fiche.get(champ)))
    return {"lignes": lignes, "contexte": contexte}


def _identifiants(comparaison, profil, fiche, channel, check_hard_matches) -> List[Dict[str, Any]]:
    """Une ligne par identifiant renseigne d'un cote au moins. Le verdict est
    celui du hard match du moteur, restreint a CET identifiant : la meme
    normalisation (blancs, tirets, casse), les memes exigences (un LEI a vingt
    caracteres) que lors du criblage."""
    lignes = []
    for champs_client, champs_fiche in comparaison.paires:
        client = _cote(profil, champs_client, "identifiers")
        cote_fiche = _cote(fiche, champs_fiche, "identifiers")
        if not client and not cote_fiche:
            continue
        verdict, detail = VERDICT_ABSENT, None
        if client and cote_fiche:
            identique, raison = check_hard_matches(
                {c: profil.get(c) for c in champs_client},
                {f: fiche.get(f) for f in champs_fiche}, channel)
            verdict = VERDICT_CONCORDANCE if identique else VERDICT_ECART
            detail = raison or None
        libelle = LIBELLES.get(champs_fiche[0], champs_fiche[0])
        lignes.append(_ligne("identifiers", libelle, verdict, detail, client, cote_fiche))
    if not lignes:
        lignes.append(_ligne("identifiers", FAMILLES["identifiers"], VERDICT_VIDE, None, [], []))
    return lignes


def champs_affiches() -> Tuple[str, ...]:
    """Tous les champs que le tableau peut afficher (controle des libelles)."""
    from fiskr.identification import comparaisons_du_moteur
    champs = []
    for comparaison in comparaisons_du_moteur():
        champs.extend(c for c in comparaison.client if c not in _HORS_COMPARAISON)
        champs.extend(comparaison.fiche)
    return tuple(dict.fromkeys(champs + list(CONTEXTE_DE_LA_FICHE)))
