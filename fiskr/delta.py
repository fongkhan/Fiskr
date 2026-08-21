from typing import List, Dict, Any, Optional, Tuple
import json
from fiskr.database import compute_checksum

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
    Compares two entity dicts.
    Returns:
        changes_detected: list of strings (e.g. ["dates_of_birth", "countries.residence"])
        before: dict of changed keys
        after: dict of changed keys
    """
    changes_detected = []
    before = {}
    after = {}
    
    exclude_keys = {"id", "snapshot_id", "entity_checksum"}
    
    # 1. First compare root keys (non-dict)
    root_keys = (set(old_ent.keys()) | set(new_ent.keys())) - exclude_keys
    
    for k in root_keys:
        val_old = old_ent.get(k)
        val_new = new_ent.get(k)
        
        if isinstance(val_old, dict) or isinstance(val_new, dict):
            # Nested comparison
            flat_old = flatten_dict(val_old or {})
            flat_new = flatten_dict(val_new or {})
            all_flat_keys = set(flat_old.keys()) | set(flat_new.keys())
            
            sub_changes = []
            for fk in all_flat_keys:
                if flat_old.get(fk) != flat_new.get(fk):
                    sub_changes.append(fk)
                    
            if sub_changes:
                # Add nested paths to changes_detected
                for sc in sub_changes:
                    changes_detected.append(f"{k}.{sc}")
                before[k] = val_old
                after[k] = val_new
        else:
            # Flat comparison
            if val_old != val_new:
                changes_detected.append(k)
                before[k] = val_old
                after[k] = val_new
                
    return sorted(changes_detected), before, after

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
        
    # MODIFIED (Using checksum comparisons)
    for i in sorted(common_ids):
        old_ent = old_map[i]
        new_ent = new_map[i]
        
        # Determine checksums
        old_chk = old_ent.get("entity_checksum")
        new_chk = new_ent.get("entity_checksum")
        
        if not old_chk:
            old_chk = compute_checksum(old_ent)
        if not new_chk:
            new_chk = compute_checksum(new_ent)
            
        if old_chk != new_chk:
            changes, before, after = find_differences(old_ent, new_ent)
            if changes:
                name = new_ent.get("primary_name") or new_ent.get("client_company_name") or new_ent.get("client_last_name") or ""
                modified.append({
                    "id": i,
                    "primary_name": name,
                    "changes_detected": changes,
                    "before": before,
                    "after": after
                })
                
    return {
        "summary": {
            "added_count": len(added),
            "removed_count": len(removed),
            "modified_count": len(modified)
        },
        "details": {
            "added": added,
            "removed": removed,
            "modified": modified
        }
    }


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
      EMPREINTES, un COUNT et un LIMIT.

    L'equivalence des empreintes n'est pas une approximation :
    `compute_checksum` et `find_differences` excluent EXACTEMENT les memes trois
    cles (`id`, `snapshot_id`, `entity_checksum`), donc « empreintes
    differentes » et « au moins un champ compare differe » sont la meme chose.
    Seules les fiches effectivement publiees en detail sont ensuite chargees en
    entier, pour en tirer le champ-a-champ.

    Rend la structure DEJA tronquee (`{summary, details}` avec les compteurs
    `*_truncated`), celle que les rapports stockent.
    """
    from sqlalchemy import and_, exists, func, select
    from sqlalchemy.orm import aliased

    modele, colonne_cle, colonnes_nom, colonne_type = _descripteur(cle)
    WatchlistEntity = modele  # noms locaux conserves pour la lisibilite du corps

    limite = _plafond_details() if limite is None else limite

    nouveau = aliased(WatchlistEntity)
    ancien = aliased(WatchlistEntity)

    def _sans_homologue(cote, cote_snapshot, autre, autre_snapshot):
        sous_requete = select(1).where(and_(
            autre.snapshot_id == autre_snapshot,
            getattr(autre, colonne_cle) == getattr(cote, colonne_cle)))
        return and_(cote.snapshot_id == cote_snapshot, ~exists(sous_requete))

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

    def _compte(condition, cote):
        return int(db.execute(
            select(func.count()).select_from(cote.__table__ if hasattr(cote, "__table__")
                                             else cote).where(condition)).scalar() or 0)

    if not old_snapshot_id:
        # Premier import : tout est un ajout, rien a comparer.
        condition = nouveau.snapshot_id == new_snapshot_id
        total = int(db.execute(select(func.count()).select_from(nouveau)
                               .where(condition)).scalar() or 0)
        ajouts = _lignes(condition, nouveau, getattr(nouveau, colonne_cle))
        return _rapport(ajouts, total, [], 0, [], 0, limite)

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

    nb_modifiees = int(db.execute(
        select(func.count()).select_from(jointure.subquery())).scalar() or 0)
    ids_modifiees = [r[0] for r in db.execute(
        jointure.order_by(getattr(nouveau, colonne_cle)).limit(max(0, limite))).all()]

    modifiees = _details_modifiees(db, old_snapshot_id, new_snapshot_id,
                                   ids_modifiees, cle)
    return _rapport(ajouts, nb_ajouts, retraits, nb_retraits,
                    modifiees, nb_modifiees, limite)


def _details_modifiees(db, old_snapshot_id, new_snapshot_id, ids,
                       cle: str = "entity_id") -> List[Dict[str, Any]]:
    """Champ-a-champ des SEULES fiches publiees en detail."""
    if not ids:
        return []
    modele, colonne_cle, colonnes_nom, _ = _descripteur(cle)

    def _par_id(snapshot_id):
        rows = db.query(modele).filter(
            modele.snapshot_id == snapshot_id,
            getattr(modele, colonne_cle).in_(ids)).all()
        return {getattr(r, colonne_cle): {c.name: getattr(r, c.name)
                                          for c in modele.__table__.columns}
                for r in rows}

    anciennes, nouvelles = _par_id(old_snapshot_id), _par_id(new_snapshot_id)
    sortie = []
    for identifiant in ids:
        avant_ent, apres_ent = anciennes.get(identifiant), nouvelles.get(identifiant)
        if avant_ent is None or apres_ent is None:
            continue
        changements, avant, apres = find_differences(avant_ent, apres_ent)
        if not changements:
            continue
        sortie.append({
            "id": identifiant,
            "primary_name": next((apres_ent.get(c) for c in colonnes_nom
                                  if apres_ent.get(c)), ""),
            "changes_detected": changements,
            "before": avant,
            "after": apres,
        })
    return sortie


def _rapport(ajouts, nb_ajouts, retraits, nb_retraits,
             modifiees, nb_modifiees, limite) -> Dict[str, Any]:
    """Structure DEJA tronquee, identique a `_truncate_delta_details`."""
    details: Dict[str, Any] = {"added": ajouts, "removed": retraits,
                               "modified": modifiees}
    for cle, total, lignes in (("added", nb_ajouts, ajouts),
                               ("removed", nb_retraits, retraits),
                               ("modified", nb_modifiees, modifiees)):
        if total > len(lignes):
            details[f"{cle}_truncated"] = total - len(lignes)
    return {
        "summary": {"added_count": nb_ajouts, "removed_count": nb_retraits,
                    "modified_count": nb_modifiees},
        "details": details,
    }
