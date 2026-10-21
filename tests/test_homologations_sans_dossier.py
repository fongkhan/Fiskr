"""
L'historique des homologations dit qu'il ne remonte pas à l'origine.

Relevé en production le 20 octobre 2026 : 560 notifications « liste
approuvée » pour 251 dossiers d'homologation. Ce n'était pas un doublon : sur
chaque jour où les deux existent, une notification par dossier. Le dossier
d'homologation n'existe que depuis le 17 août 2026 ; les approbations
antérieures n'en ont pas, et l'historique, qui annonçait 251 homologations,
ne disait pas qu'il commençait là. Le lot, lui, garde son réviseur et sa
date depuis toujours : l'historique les compte et les date, sans fabriquer
de dossier.
"""
import uuid
from datetime import datetime, timedelta

from fiskr.database import ReviewRecord, Snapshot, get_db


def _lot(db, statut, decide=True, dossier=False, jours=30):
    sid = f"sans-dossier-{uuid.uuid4().hex[:8]}"
    quand = datetime.utcnow() - timedelta(days=jours)
    db.add(Snapshot(snapshot_id=sid, file_type="WATCHLIST_DGT", file_name=f"{sid}.json",
                    file_hash=uuid.uuid4().hex, record_count=1, status=statut,
                    uploaded_at=quand, reviewed_by="ancien" if decide else None,
                    reviewed_at=quand if decide else None))
    if dossier:
        db.add(ReviewRecord(snapshot_id=sid, file_type="WATCHLIST_DGT", file_name=f"{sid}.json",
                            decision="APPROVED" if statut != "REJECTED" else "REJECTED",
                            decided_by="ancien", decided_at=quand))
    db.commit()
    return sid


def test_les_decisions_anterieures_au_dossier_sont_comptees_et_datees():
    from fiskr.api import _decisions_sans_dossier
    db = next(get_db())
    crees = []
    try:
        avant = _decisions_sans_dossier(db, "WATCHLIST_DGT", None)["count"]
        crees += [_lot(db, "READY", jours=70), _lot(db, "SUPERSEDED", jours=60),
                  _lot(db, "REJECTED", jours=50)]
        # Avec dossier, ou jamais décidé (version dépassée retirée de la file) : pas compté
        crees += [_lot(db, "READY", dossier=True), _lot(db, "SUPERSEDED", decide=False)]
        mesure = _decisions_sans_dossier(db, "WATCHLIST_DGT", None)
        assert mesure["count"] == avant + 3
        assert mesure["du"] is not None and mesure["au"] is not None
        assert _decisions_sans_dossier(db, "WATCHLIST_DGT", "REJECTED")["count"] >= 1
        approuvees = _decisions_sans_dossier(db, "WATCHLIST_DGT", "APPROVED")["count"]
        assert approuvees == _decisions_sans_dossier(db, "WATCHLIST_DGT", None)["count"] \
            - _decisions_sans_dossier(db, "WATCHLIST_DGT", "REJECTED")["count"]
    finally:
        db.query(ReviewRecord).filter(ReviewRecord.snapshot_id.in_(crees)).delete(synchronize_session=False)
        db.query(Snapshot).filter(Snapshot.snapshot_id.in_(crees)).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_l_historique_porte_le_compte():
    """L'endpoint rend le compte à côté de l'historique, et l'écran le lit."""
    from pathlib import Path
    import inspect
    from fiskr import api
    assert "decisions_sans_dossier" in inspect.getsource(api.list_review_history)
    app_js = (Path(api.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "data.decisions_sans_dossier" in app_js
