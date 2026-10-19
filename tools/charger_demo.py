#!/usr/bin/env python3
"""
Charge le jeu de DEMONSTRATION (watchlist.json, 5 fiches) comme une liste en
production — par un geste explicite, et seulement par lui.

Pourquoi cet outil existe
-------------------------
Ce jeu entrait tout seul : quand aucune liste n'etait en production, le
chargement du cache moteur le versait dans la base, sous le type
WATCHLIST_OFAC et au statut READY. Reproduit sur une base vide : le premier
criblage rendait 200 « aucune correspondance » avec sa ligne d'audit, la mise
en service affichait « Listes : OK — 1 liste en production » et « Fraicheur :
OK », et le refus de cribler sans univers ne mordait jamais sur une
installation neuve — la seule ou il compte vraiment.

Cinq noms connus ne sont pas une liste de sanctions. Une installation de
demonstration les charge en le demandant ; une production ne les voit jamais.

Usage
-----
    python tools/charger_demo.py                      # liste WATCHLIST_OFAC
    python tools/charger_demo.py --type WATCHLIST_EU
    python tools/charger_demo.py --retirer            # retire les lots de demonstration

Le lot est reconnaissable partout : identifiant `demo-watchlist-…`, fichier
« DÉMONSTRATION — watchlist.json ». L'outil refuse de s'ajouter a des listes
deja en production (--force pour passer outre) : melanger cinq fiches de
demonstration a un vrai referentiel fausserait chaque decision qui suivrait.
"""
import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Prefixe des lots de demonstration : c'est par lui qu'on les retrouve, et
# qu'on les retire.
PREFIXE = "demo-watchlist-"
NOM_DE_FICHIER = "DÉMONSTRATION — watchlist.json"
TYPE_PAR_DEFAUT = "WATCHLIST_OFAC"


def _chemin_par_defaut():
    from fiskr.config import PROJECT_ROOT
    return PROJECT_ROOT / "watchlist.json"


def _fiche(snapshot_id: str, index: int, item: dict):
    """Une fiche du jeu, sous la forme d'une fiche listee en base."""
    from fiskr.database import WatchlistEntity, compute_checksum
    from fiskr.ingest import categorize_aliases, parse_multi_value

    aliases = item.get("aliases") or []
    if not isinstance(aliases, dict):
        aliases = categorize_aliases([{"name": a, "type": "Strong"} for a in aliases if a])
    brut = item.get("entity_type", "I")
    genres = item.get("genders") or []
    return WatchlistEntity(
        snapshot_id=snapshot_id,
        entity_id=item.get("entity_id") or f"WL-DEMO-{index}",
        entity_type="I" if brut == "PP" else ("E" if brut == "PM" else brut),
        primary_name=item.get("primary_name", ""),
        individual_name_parsed=item.get("individual_name_parsed") or {},
        aliases=aliases,
        dates_of_birth=item.get("dates_of_birth", []),
        date_of_death=item.get("date_of_death"),
        is_deceased=str(item.get("is_deceased", "False")).lower() == "true",
        gender=item.get("gender") or (genres[0] if genres else "U"),
        countries=item.get("countries") or {},
        alternative_addresses=parse_multi_value(
            item, "alternative_addresses", "client_alternative_addresses"),
        entity_checksum=compute_checksum(item),
    )


def charger(db, file_type: str = TYPE_PAR_DEFAUT, chemin=None, force: bool = False):
    """
    Pose le jeu de demonstration en production (lot READY) et incremente
    l'epoque : les processus en service rechargent leur cache. Rend le lot.

    Leve ValueError si des listes sont deja en production et que `force`
    n'est pas demande, ou si le type n'est pas un type de liste.
    """
    from fiskr.database import Snapshot, WATCHLIST_FILE_TYPES
    from fiskr.settings import bump_watchlist_epoch
    from fiskr import univers

    if file_type not in WATCHLIST_FILE_TYPES:
        raise ValueError(f"Type de liste inconnu : {file_type}.")
    if not force:
        deja = univers.lots_en_production(db)
        if deja:
            raise ValueError(
                f"{len(deja)} lot(s) déjà en production : le jeu de démonstration "
                f"s'y mêlerait à un vrai référentiel. Relancez avec --force si "
                f"c'est voulu.")
    chemin = chemin or _chemin_par_defaut()
    with open(chemin, "rb") as f:
        contenu = f.read()
    items = json.loads(contenu)

    snapshot_id = f"{PREFIXE}{uuid.uuid4().hex[:8]}"
    lot = Snapshot(
        snapshot_id=snapshot_id, file_type=file_type, file_name=NOM_DE_FICHIER,
        file_hash=hashlib.sha256(contenu).hexdigest(), record_count=len(items),
        uploaded_at=datetime.utcnow(), status="READY",
    )
    db.add(lot)
    db.flush()
    for index, item in enumerate(items):
        db.add(_fiche(snapshot_id, index, item))
    db.commit()
    bump_watchlist_epoch(db)
    db.refresh(lot)
    return lot


def retirer(db) -> int:
    """Retire tous les lots de demonstration et leurs fiches. Rend leur nombre."""
    from fiskr.database import Snapshot, WatchlistEntity
    from fiskr.settings import bump_watchlist_epoch

    ids = [s.snapshot_id for s in db.query(Snapshot.snapshot_id).filter(
        Snapshot.snapshot_id.like(f"{PREFIXE}%")).all()]
    if not ids:
        return 0
    db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id.in_(ids)).delete(synchronize_session=False)
    db.query(Snapshot).filter(
        Snapshot.snapshot_id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    bump_watchlist_epoch(db)
    return len(ids)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--type", default=TYPE_PAR_DEFAUT,
                        help=f"type de liste du lot (défaut : {TYPE_PAR_DEFAUT})")
    parser.add_argument("--fichier", default=None, help="jeu JSON (défaut : watchlist.json)")
    parser.add_argument("--force", action="store_true",
                        help="charger même si des listes sont déjà en production")
    parser.add_argument("--retirer", action="store_true",
                        help="retirer les lots de démonstration")
    args = parser.parse_args(argv)

    from fiskr import database
    database.init_db()
    db = database.SessionLocal()
    try:
        if args.retirer:
            n = retirer(db)
            print(f"{n} lot(s) de démonstration retiré(s).")
            return 0
        try:
            lot = charger(db, args.type.strip().upper(), args.fichier, args.force)
        except ValueError as refus:
            print(f"Refusé : {refus}")
            return 1
        print(f"Jeu de démonstration chargé : lot {lot.snapshot_id} ({lot.file_type}, "
              f"{lot.record_count} fiches). Retrait : python tools/charger_demo.py --retirer")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
