from typing import List, Dict, Any, Optional, Tuple
import json
import logging
from fiskr.database import CLES_HORS_EMPREINTE, compute_checksum, normaliser_fiche

logger = logging.getLogger("fiskr.delta")


def flatten_dict(d: dict, prefix: str = "") -> dict:
    """Recursively flattens a nested dictionary into dot-notation keys."""
    if not isinstance(d, dict):
        return {}
    items = {}
    for k, v in d.items():
        new_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            items.update(flatten_dict(v, new_key))
        else:
            items[new_key] = v
    return items


def find_differences(old_ent: dict, new_ent: dict) -> Tuple[List[str], dict, dict]:
    """
    Compare deux fiches SOUS LEUR FORME NORMALISEE (`normaliser_fiche`, celle
    que hache l'empreinte) et rend :
        changes_detected : chemins touches (ex. ["dates_of_birth", "countries.residence"])
        before / after   : valeurs STOCKEES des cles touchees — le reviseur
                           lit la source telle qu'elle est, pas sa forme de
                           comparaison.

    Deux references inversees (« A - B » / « B - A » rangees par le lecteur),
    deux alias permutes ou un guillemet echappe autrement ne sont pas des
    modifications : la forme normalisee est la meme.
    """
    changes_detected = []
    before = {}
    after = {}
    norme_old, norme_new = normaliser_fiche(old_ent), normaliser_fiche(new_ent)

    for k in (set(norme_old) | set(norme_new)) - CLES_HORS_EMPREINTE:
        val_old, val_new = norme_old.get(k), norme_new.get(k)
        if val_old == val_new:
            continue
        sub_changes = []
        if isinstance(val_old, dict) or isinstance(val_new, dict):
            flat_old = flatten_dict(val_old or {})
            flat_new = flatten_dict(val_new or {})
            sub_changes = [fk for fk in set(flat_old) | set(flat_new)
                           if flat_old.get(fk) != flat_new.get(fk)]
        if sub_changes:
            changes_detected.extend(f"{k}.{sc}" for sc in sub_changes)
        else:
            changes_detected.append(k)
        before[k] = old_ent.get(k)
        after[k] = new_ent.get(k)

    return sorted(changes_detected), before, after


# ------------------ IDENTITE OU INFORMATION ------------------

def champs_d_identite() -> frozenset:
    """
    Champs dont la modification touche l'IDENTITE de la personne listee :
    ceux que le moteur lit REELLEMENT pour rapprocher un client d'une fiche
    (`screenpool.ENTITY_PROJECTION`, verifiee champ par champ par la
    sentinelle de tests/test_projection.py) — noms, alias, dates de naissance,
    pays, genre, type, identifiants et documents —, plus les programmes de
    sanction : le moteur ne les score pas, mais ils fondent l'obligation de
    gel. Tout le reste — motifs, adresses, informations complementaires,
    references — est de l'INFORMATION : utile a lire, sans effet sur qui est
    intercepte.

    Derive, jamais recopie : un champ que le moteur se met a lire devient
    identitaire ici sans que personne y pense.
    """
    from fiskr.screenpool import ENTITY_PROJECTION
    return (frozenset(ENTITY_PROJECTION) | {"sanction_programs"}) - CLES_HORS_EMPREINTE


NATURE_IDENTITE = "IDENTITE"
NATURE_INFORMATION = "INFORMATION"


# Issue de la comparaison de deux versions d'une meme fiche
INCHANGEE = "INCHANGEE"
FORME = "FORME"                          # forme du lecteur seule, equivalence verifiee
FORME_NON_VERIFIABLE = "FORME_NON_VERIFIABLE"  # idem, ancien texte tronque
FORME_QUALITE = "FORME_QUALITE"          # qualite PEP plus affirmee (« non precisee »)
MODIFIEE = "MODIFIEE"


def _comparer(identifiant, nom: str, avant_ent: dict, apres_ent: dict,
              identite: Optional[frozenset]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """(issue, ligne « modifiee » du rapport ou None).

    Partagee par le calcul en memoire et le calcul en base : les deux
    publient exactement les memes lignes.

    Une difference qui ne tient qu'a la correction du lecteur OpenSanctions
    (programmes de sanction passes de `pep_role` a `sanction_programs`, cf.
    `ingest.forme_corrigee_du_lecteur`) n'est pas une modification de la
    source : elle est comptee a part, et dite. Melee a une vraie modification,
    elle en est retiree : une date de naissance corrigee reste une
    modification d'identite, une note ajoutee une information — pas une
    modification de programmes de sanction parce que le lecteur les range
    ailleurs."""
    changements, avant, apres = find_differences(avant_ent, apres_ent)
    if not changements:
        return INCHANGEE, None
    # Chaque correction du lecteur est reconnue sur SES champs : celle des
    # programmes de sanction, et la qualite PEP que le lecteur n'affirme plus
    # (« non precisee » au lieu de « Personne Politiquement Exposée (PEP) »
    # sur les 709 511 fiches du lot PEP).
    from fiskr.ingest import CORRECTIONS_DU_LECTEUR
    issues_de_forme = []
    for champs, reconnaitre, issue_verifiee in CORRECTIONS_DU_LECTEUR:
        de_forme = [c for c in changements if c.split(".")[0] in champs]
        forme = reconnaitre(avant_ent, apres_ent, de_forme) if de_forme else None
        if forme is None:
            continue
        issues_de_forme.append(issue_verifiee if forme else FORME_NON_VERIFIABLE)
        changements = [c for c in changements if c not in de_forme]
        for chemin in de_forme:
            avant.pop(chemin.split(".")[0], None)
            apres.pop(chemin.split(".")[0], None)
    if issues_de_forme and not changements:
        # Une fiche que les deux corrections touchent a la fois (aucune en
        # production : le lot PEP n'a pas de programmes) se range sous la
        # premiere, et sa verification partielle l'emporte.
        if FORME_NON_VERIFIABLE in issues_de_forme:
            return FORME_NON_VERIFIABLE, None
        return issues_de_forme[0], None
    ligne = {
        "id": identifiant,
        "primary_name": nom,
        "changes_detected": changements,
        "before": avant,
        "after": apres,
    }
    if identite is not None:
        touches = [c for c in changements if c.split(".")[0] in identite]
        ligne["nature"] = NATURE_IDENTITE if touches else NATURE_INFORMATION
        ligne["identity_changes"] = touches
    return MODIFIEE, ligne


def _ordre_de_publication(lignes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Les modifications d'IDENTITE d'abord, puis par identifiant : quand le
    detail est borne, c'est ce qui change qui est intercepte qu'on doit voir."""
    return sorted(lignes, key=lambda l: (l.get("nature") == NATURE_INFORMATION, str(l["id"])))


def calculate_delta(
    old_entities: List[Dict[str, Any]],
    new_entities: List[Dict[str, Any]],
    key_column: str
) -> Dict[str, Any]:
    """
    Compares two list of entities and returns the delta report.
    Matches entity list format specified in DAT Section 8.4.
    """
    old_map = {ent.get(key_column): ent for ent in old_entities if ent.get(key_column)}
    new_map = {ent.get(key_column): ent for ent in new_entities if ent.get(key_column)}

    old_ids = set(old_map.keys())
    new_ids = set(new_map.keys())

    added_ids = new_ids - old_ids
    removed_ids = old_ids - new_ids
    common_ids = old_ids.intersection(new_ids)

    added = []
    removed = []
    modified = []
    identite = champs_d_identite() if key_column == "entity_id" else None

    # ADDED
    for i in sorted(added_ids):
        ent = new_map[i]
        name = ent.get("primary_name") or ent.get("client_company_name") or ent.get("client_last_name") or ""
        etype = ent.get("entity_type") or ent.get("client_type") or "I"
        added.append({"id": i, "primary_name": name, "type": etype})

    # REMOVED
    for i in sorted(removed_ids):
        ent = old_map[i]
        name = ent.get("primary_name") or ent.get("client_company_name") or ent.get("client_last_name") or ""
        etype = ent.get("entity_type") or ent.get("client_type") or "I"
        removed.append({"id": i, "primary_name": name, "type": etype})

    # MODIFIED : l'empreinte ne fait que designer les candidates, le champ a
    # champ normalise tranche.
    issues: Dict[str, int] = {}
    for i in sorted(common_ids):
        old_ent = old_map[i]
        new_ent = new_map[i]
        old_chk = old_ent.get("entity_checksum") or compute_checksum(old_ent)
        new_chk = new_ent.get("entity_checksum") or compute_checksum(new_ent)
        if old_chk != new_chk:
            name = new_ent.get("primary_name") or new_ent.get("client_company_name") or new_ent.get("client_last_name") or ""
            issue, ligne = _comparer(i, name, old_ent, new_ent, identite)
            issues[issue] = issues.get(issue, 0) + 1
            if ligne is not None:
                modified.append(ligne)

    rapport = _rapport(
        added, len(added), removed, len(removed), _ordre_de_publication(modified),
        len(modified), len(modified),
        nb_identite=(sum(1 for m in modified if m["nature"] == NATURE_IDENTITE)
                     if identite is not None else None),
        issues=issues)
    return rapport


# ------------------ CALCUL EN BASE, A MEMOIRE BORNEE ------------------

# Nombre de lignes de detail rendues par categorie. Le rapport n'en stocke et
# n'en affiche pas davantage (fiskr/sync.MAX_REPORT_DETAILS) : il n'y a aucune
# raison de construire les autres.
def _plafond_details() -> int:
    """Plafond partage avec le rapport de synchronisation : une seule valeur,
    sinon le calcul et le stockage ne coupent pas au meme endroit."""
    from fiskr.sync import MAX_REPORT_DETAILS
    return MAX_REPORT_DETAILS


# Ce que chaque famille d'instantane appelle « identifiant », « nom » et
# « type ». Recopie de ce que `calculate_delta` lit sur les dictionnaires, pour
# que les deux chemins publient exactement les memes lignes.
def _descripteur(cle: str):
    from fiskr.database import ClientEntity, WatchlistEntity
    if cle == "client_id":
        return (ClientEntity, "client_id",
                ("client_company_name", "client_last_name"), "client_type")
    return (WatchlistEntity, "entity_id", ("primary_name",), "entity_type")


def calculate_delta_db(db, old_snapshot_id, new_snapshot_id,
                       limite: Optional[int] = None,
                       cle: str = "entity_id") -> Dict[str, Any]:
    """
    Meme rapport que `calculate_delta`, calcule EN BASE, a memoire bornee.

    Pourquoi cette variante existe
    ------------------------------
    Le chemin d'origine chargeait les DEUX instantanes ENTIERS en memoire, sous
    forme de dictionnaires a soixante-dix colonnes, pour n'en publier que cent
    lignes par categorie. Sur la plus grosse liste de la production —
    WATCHLIST_PEP, 709 511 fiches — cela fait 1,4 million de dictionnaires a
    ~1,8 Ko : plusieurs gigaoctets, dans une requete HTTP synchrone, sur un
    hebergement mutualise. L'ecran d'examen d'un import manuel de cette liste
    ne pouvait pas s'ouvrir.

    Ce que fait cette version
    -------------------------
    - ajouts / retraits : anti-jointure SQL, un COUNT et un LIMIT ;
    - modifications : jointure sur l'identifiant avec comparaison des
      EMPREINTES, puis VERIFICATION champ a champ de chaque candidate, par
      tranches (cf. `_verifier_modifications`).

    L'empreinte DESIGNE, le champ a champ TRANCHE. Ce module affirmait que
    « empreintes differentes » et « un champ compare differe » etaient la meme
    chose ; la production l'a dementi : 15 fiches GBFCDO sur 100 « modifiees »
    sans aucun champ different, parce que l'empreinte etait calculee sur
    l'enregistrement lu et non sur ce que la base garde (designation tronquee
    a 500 caracteres). Le compteur disait 24, la liste en montrait 19. Le
    compte est desormais celui des fiches verifiees : il ne peut plus
    contredire la liste.

    Rend la structure DEJA tronquee (`{summary, details}` avec les compteurs
    `*_truncated`), celle que les rapports stockent.
    """
    from sqlalchemy import and_, func, select
    from sqlalchemy.orm import aliased

    modele, colonne_cle, colonnes_nom, colonne_type = _descripteur(cle)
    WatchlistEntity = modele  # noms locaux conserves pour la lisibilite du corps

    limite = _plafond_details() if limite is None else limite

    nouveau = aliased(WatchlistEntity)
    ancien = aliased(WatchlistEntity)

    def _sans_homologue(cote, cote_snapshot, autre, autre_snapshot):
        return _condition_sans_homologue(colonne_cle, cote, cote_snapshot,
                                         autre, autre_snapshot)

    def _nom(cote):
        colonnes = [getattr(cote, c) for c in colonnes_nom]
        return func.coalesce(*colonnes, "") if len(colonnes) > 1 else colonnes[0]

    def _lignes(condition, cote, ordre):
        return [
            {"id": eid, "primary_name": nom or "", "type": etype or "I"}
            for eid, nom, etype in db.execute(
                select(getattr(cote, colonne_cle), _nom(cote),
                       getattr(cote, colonne_type))
                .where(condition).order_by(ordre).limit(max(0, limite))).all()
        ]

    identite = champs_d_identite() if cle == "entity_id" else None

    if not old_snapshot_id:
        # Premier import : tout est un ajout, rien a comparer.
        condition = nouveau.snapshot_id == new_snapshot_id
        total = int(db.execute(select(func.count()).select_from(nouveau)
                               .where(condition)).scalar() or 0)
        ajouts = _lignes(condition, nouveau, getattr(nouveau, colonne_cle))
        return _rapport(ajouts, total, [], 0, [], 0, limite,
                        nb_identite=0 if identite is not None else None)

    cond_ajouts = _sans_homologue(nouveau, new_snapshot_id, ancien, old_snapshot_id)
    cond_retraits = _sans_homologue(ancien, old_snapshot_id, nouveau, new_snapshot_id)

    nb_ajouts = int(db.execute(select(func.count()).select_from(nouveau)
                               .where(cond_ajouts)).scalar() or 0)
    nb_retraits = int(db.execute(select(func.count()).select_from(ancien)
                                 .where(cond_retraits)).scalar() or 0)
    ajouts = _lignes(cond_ajouts, nouveau, getattr(nouveau, colonne_cle))
    retraits = _lignes(cond_retraits, ancien, getattr(ancien, colonne_cle))

    jointure = select(getattr(nouveau, colonne_cle)).select_from(nouveau).join(
        ancien, and_(ancien.snapshot_id == old_snapshot_id,
                     getattr(ancien, colonne_cle) == getattr(nouveau, colonne_cle))
    ).where(and_(nouveau.snapshot_id == new_snapshot_id,
                 func.coalesce(nouveau.entity_checksum, "")
                 != func.coalesce(ancien.entity_checksum, "")))
    candidates = [r[0] for r in db.execute(
        jointure.order_by(getattr(nouveau, colonne_cle))).all()]

    verifie = _verifier_modifications(db, old_snapshot_id, new_snapshot_id,
                                      candidates, cle, limite, identite)
    return _rapport(ajouts, nb_ajouts, retraits, nb_retraits,
                    verifie["lignes"], verifie["nombre"], limite,
                    nb_identite=verifie["identite"], issues=verifie["issues"])


def _condition_sans_homologue(colonne_cle: str, cote, cote_snapshot, autre, autre_snapshot):
    """Les fiches de `cote_snapshot` dont l'identifiant est absent de
    `autre_snapshot` : l'anti-jointure qui fait les ajouts et les retraits du
    delta. Une seule definition, que lisent le delta et les radiations."""
    from sqlalchemy import and_, exists, select
    sous_requete = select(1).where(and_(
        autre.snapshot_id == autre_snapshot,
        getattr(autre, colonne_cle) == getattr(cote, colonne_cle)))
    return and_(cote.snapshot_id == cote_snapshot, ~exists(sous_requete))


def fiches_sans_homologue(db, snapshot_id: str, autre_snapshot_id: str,
                          colonnes: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """
    TOUTES les fiches de `snapshot_id` dont l'`entity_id` est absent de
    `autre_snapshot_id`, sans plafond, reduites aux `colonnes` demandees.

    (precedent, nouveau) rend les RETRAITS du delta ; (nouveau, precedent) ses
    AJOUTS — la meme anti-jointure que `calculate_delta_db`, qui ne publie que
    cent lignes par categorie. Les radiations (fiskr/caducite.py) ont besoin de
    toutes, et d'aucune verification champ a champ : un retrait n'a pas
    d'homologue a comparer.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import aliased
    from fiskr.database import WatchlistEntity

    cote, autre = aliased(WatchlistEntity), aliased(WatchlistEntity)
    condition = _condition_sans_homologue("entity_id", cote, snapshot_id,
                                          autre, autre_snapshot_id)
    requete = select(*[getattr(cote, c) for c in colonnes]).where(condition) \
        .order_by(cote.entity_id)
    return [dict(zip(colonnes, ligne)) for ligne in db.execute(requete).all()]


# Fiches comparees par aller-retour en base : borne la memoire (deux tranches
# de lignes completes) et reste sous le plafond de parametres de SQLite.
_TRANCHE_DE_VERIFICATION = 500


def _lignes_par_id(db, modele, colonne_cle: str, snapshot_id: str,
                   ids: List[Any]) -> Dict[Any, Dict[str, Any]]:
    from sqlalchemy import select
    table = modele.__table__
    requete = select(table).where(table.c.snapshot_id == snapshot_id,
                                  table.c[colonne_cle].in_(ids))
    return {ligne[colonne_cle]: dict(ligne) for ligne in db.execute(requete).mappings()}


def _verifier_modifications(db, old_snapshot_id, new_snapshot_id, ids,
                            cle: str, limite: int,
                            identite: Optional[frozenset]) -> Dict[str, Any]:
    """
    Champ a champ normalise de CHAQUE fiche dont l'empreinte differe — pas
    seulement des cent publiees : le compte, la nature (identite /
    information) et la liste sortent du meme passage et ne peuvent pas se
    contredire.

    Une candidate sans aucun champ different n'est pas une modification : son
    empreinte etait perimee (calculee avant la normalisation, ou sur
    l'enregistrement lu plutot que sur ce que la base garde). Les deux
    fiches recoivent alors l'empreinte de leur contenu, une fois pour toutes :
    le premier passage apres le deploiement de la normalisation ne publie pas
    une vague de « modifiees », et les suivants ne paient plus cette
    verification. Cout de ce premier passage : relire les fiches communes
    (quelques minutes sur la liste PEP), une seule fois par lot.
    """
    modele, colonne_cle, colonnes_nom, _ = _descripteur(cle)
    lignes_identite: List[Dict[str, Any]] = []
    lignes_information: List[Dict[str, Any]] = []
    nombre = nb_identite = 0
    issues: Dict[str, int] = {}
    a_realigner: List[Dict[str, Any]] = []
    for debut in range(0, len(ids), _TRANCHE_DE_VERIFICATION):
        tranche = ids[debut:debut + _TRANCHE_DE_VERIFICATION]
        anciennes = _lignes_par_id(db, modele, colonne_cle, old_snapshot_id, tranche)
        nouvelles = _lignes_par_id(db, modele, colonne_cle, new_snapshot_id, tranche)
        for identifiant in tranche:
            avant_ent, apres_ent = anciennes.get(identifiant), nouvelles.get(identifiant)
            if avant_ent is None or apres_ent is None:
                continue
            nom = next((apres_ent.get(c) for c in colonnes_nom if apres_ent.get(c)), "")
            issue, ligne = _comparer(identifiant, nom, avant_ent, apres_ent, identite)
            issues[issue] = issues.get(issue, 0) + 1
            if issue == INCHANGEE:
                empreinte = compute_checksum(apres_ent)
                a_realigner.append({"pk": avant_ent["id"], "empreinte": empreinte})
                a_realigner.append({"pk": apres_ent["id"], "empreinte": empreinte})
                continue
            if ligne is None:
                # Forme du lecteur : rien a realigner, le contenu STOCKE differe
                # bel et bien — la production garde l'ancienne forme jusqu'a ce
                # qu'un lot relu soit homologue.
                continue
            nombre += 1
            if ligne.get("nature") == NATURE_IDENTITE:
                nb_identite += 1
                cible = lignes_identite
            else:
                cible = lignes_information
            # Deux reserves bornees : l'identite passe devant a la publication
            if len(cible) < max(0, limite):
                cible.append(ligne)
    _realigner_empreintes(db, modele, a_realigner)
    return {
        "lignes": _ordre_de_publication(lignes_identite + lignes_information)[:max(0, limite)],
        "nombre": nombre,
        "identite": nb_identite if identite is not None else None,
        "issues": issues,
    }


def _realigner_empreintes(db, modele, a_realigner: List[Dict[str, Any]]) -> None:
    """Ecrit l'empreinte de leur contenu sur les fiches dont elle etait
    perimee. Donnee derivee, idempotente : elle est committee tout de suite,
    sans quoi un ecran d'examen la recalculerait a chaque ouverture."""
    if not a_realigner:
        return
    from sqlalchemy import bindparam, update
    table = modele.__table__
    requete = (update(table).where(table.c.id == bindparam("pk"))
               .values(entity_checksum=bindparam("empreinte")))
    for debut in range(0, len(a_realigner), 1000):
        db.execute(requete, a_realigner[debut:debut + 1000])
    db.commit()
    logger.info(f"Delta : {len(a_realigner) // 2} fiche(s) a l'empreinte perimee "
                "(aucun champ different) realignee(s) sur leur contenu.")


# Fiches ecartees du compte des modifiees, et DITES : un compteur qui baisse
# sans raison visible ne se croit pas. Cles du detail (jamais du resume, que
# les rapports archives et les tests lisent tel quel), absentes quand nulles.
CLES_ECARTEES_DU_COMPTE = ("checksum_only_count", "reader_form_count",
                           "reader_form_unverified_count", "reader_quality_form_count")


def _rapport(ajouts, nb_ajouts, retraits, nb_retraits,
             modifiees, nb_modifiees, limite, nb_identite: Optional[int] = None,
             issues: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Structure DEJA tronquee, identique a `_truncate_delta_details`."""
    details: Dict[str, Any] = {"added": ajouts, "removed": retraits,
                               "modified": modifiees}
    for cle, total, lignes in (("added", nb_ajouts, ajouts),
                               ("removed", nb_retraits, retraits),
                               ("modified", nb_modifiees, modifiees)):
        if total > len(lignes):
            details[f"{cle}_truncated"] = total - len(lignes)
    issues = issues or {}
    ecartees = {
        "checksum_only_count": issues.get(INCHANGEE, 0),
        # Toutes les especes de forme ; les deux suivantes y sont comprises
        "reader_form_count": (issues.get(FORME, 0) + issues.get(FORME_NON_VERIFIABLE, 0)
                              + issues.get(FORME_QUALITE, 0)),
        "reader_form_unverified_count": issues.get(FORME_NON_VERIFIABLE, 0),
        # dont : la qualite PEP que le lecteur n'affirme plus (l'ecran le dit
        # a part : ce n'est pas un deplacement de programmes de sanction)
        "reader_quality_form_count": issues.get(FORME_QUALITE, 0),
    }
    details.update({cle: n for cle, n in ecartees.items() if n})
    summary = {"added_count": nb_ajouts, "removed_count": nb_retraits,
               "modified_count": nb_modifiees}
    if nb_identite is not None:
        summary["modified_identity_count"] = nb_identite
    return {"summary": summary, "details": details}
