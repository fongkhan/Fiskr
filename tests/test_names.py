from fiskr.names import parse_individual_name, ensure_parsed_name


def test_family_name_in_capitals_with_multiple_given_names():
    # Convention des listes officielles (EUR-Lex, ONU) : NOM en capitales
    parsed = parse_individual_name("Aleksandr Vladimirovich GUTSAN")
    assert parsed["first_name"] == "Aleksandr Vladimirovich"
    assert parsed["last_name"] == "GUTSAN"

    parsed = parse_individual_name("Algony Hamdan DAGALO MUSA")
    assert parsed["first_name"] == "Algony Hamdan"
    assert parsed["last_name"] == "DAGALO MUSA"


def test_family_name_first_in_capitals():
    # L'ordre des blocs n'importe pas
    parsed = parse_individual_name("BABKIN Igor Yuryevich")
    assert parsed["first_name"] == "Igor Yuryevich"
    assert parsed["last_name"] == "BABKIN"


def test_comma_separated_format():
    parsed = parse_individual_name("Dupont, Jean Marc")
    assert parsed["first_name"] == "Jean Marc"
    assert parsed["last_name"] == "Dupont"


def test_family_particles_attached_to_capitals():
    parsed = parse_individual_name("Usama bin LADIN")
    assert parsed["first_name"] == "Usama"
    assert parsed["last_name"] == "bin LADIN"

    parsed = parse_individual_name("Jean-Marie Le PEN")
    assert parsed["first_name"] == "Jean-Marie"
    assert parsed["last_name"] == "Le PEN"


def test_initials_are_given_names():
    parsed = parse_individual_name("J. K. ROWLING")
    assert parsed["first_name"] == "J. K."
    assert parsed["last_name"] == "ROWLING"


def test_fallback_without_case_signal():
    # Casse uniforme : repli premier token = prenom
    parsed = parse_individual_name("Vladimir Putin")
    assert parsed["first_name"] == "Vladimir"
    assert parsed["last_name"] == "Putin"

    parsed = parse_individual_name("VLADIMIR PUTIN")
    assert parsed["first_name"] == "VLADIMIR"
    assert parsed["last_name"] == "PUTIN"


def test_single_token_is_family_name():
    parsed = parse_individual_name("DAGALO")
    assert parsed["first_name"] == ""
    assert parsed["last_name"] == "DAGALO"


def test_ensure_parsed_name_uses_explicit_columns():
    item = {"entity_type": "PP", "primary_name": "Jean Marc Dupont",
            "first_name": "Jean Marc", "last_name": "Dupont"}
    item = ensure_parsed_name(item)
    assert item["individual_name_parsed"] == {"first_name": "Jean Marc", "last_name": "Dupont", "maiden_name": ""}


def test_ensure_parsed_name_parses_primary_name():
    item = {"entity_type": "I", "primary_name": "Irina Dmitriyevna DEREVYAGINA"}
    item = ensure_parsed_name(item)
    assert item["individual_name_parsed"]["first_name"] == "Irina Dmitriyevna"
    assert item["individual_name_parsed"]["last_name"] == "DEREVYAGINA"


def test_ensure_parsed_name_preserves_existing_and_skips_entities():
    # Un decoupage fourni par la source (ex: OFAC XML) n'est jamais ecrase
    item = {"entity_type": "I", "primary_name": "Igor Yuryevich BABKIN",
            "individual_name_parsed": {"first_name": "Igor", "last_name": "Babkin", "maiden_name": ""}}
    assert ensure_parsed_name(item)["individual_name_parsed"]["first_name"] == "Igor"

    entity = {"entity_type": "E", "primary_name": "ZARYA HOLDING"}
    assert "individual_name_parsed" not in ensure_parsed_name(entity)
