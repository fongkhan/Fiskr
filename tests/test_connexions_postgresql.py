"""
Des connexions PostgreSQL qui se voient mourir, et un pilote qu'on ne laisse
plus au hasard.

Relevé sur l'installation de production le 19 octobre 2026 :

* la synchronisation DFAT finit chaque passage sur « server closed the
  connection unexpectedly » — le serveur ferme les connexions restées
  inactives pendant les neuf minutes où le portail australien ne répond pas,
  et l'erreur de base MASQUE la vraie ;
* une synchronisation PEP est « en cours » depuis 26 jours : le profil d'un
  appel bloqué sur une connexion coupée sans que le client en soit prévenu.

Et, constaté dans un environnement neuf : `postgresql://…` laissait SQLAlchemy
choisir le pilote, SQLAlchemy 2.1 a basculé de psycopg2 vers psycopg (v3), et
l'échec d'import était rattrapé par un repli sur SQLite. Une production dont
on aurait réinstallé les dépendances aurait tourné sur une base locale vide.
"""
import pytest

from fiskr import database


# --------------------------------------------------------------------------
# Le moteur
# --------------------------------------------------------------------------

def test_une_connexion_reprise_du_pool_est_verifiee_avant_usage():
    options = database.options_postgresql()
    assert options["pool_pre_ping"] is True
    assert 0 < options["pool_recycle"] <= 3600


def test_une_connexion_morte_est_detectee_en_minutes_et_non_jamais():
    """
    Sans keepalives explicites, une connexion à demi ouverte n'est détectée
    qu'au bout de deux heures par défaut du système — ou jamais si le système
    les a coupés. Ici : ~ idle + interval × count.
    """
    args = database.options_postgresql()["connect_args"]
    assert args["keepalives"] == 1
    delai = args["keepalives_idle"] + args["keepalives_interval"] * args["keepalives_count"]
    assert delai <= 300, f"{delai} s avant de voir une connexion morte"


def test_les_options_sont_des_parametres_que_libpq_comprend():
    """Une option inconnue de libpq ferait échouer TOUTE connexion : on le demande au pilote."""
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.extensions import parse_dsn
    args = database.options_postgresql()["connect_args"]
    dsn = "host=127.0.0.1 dbname=x " + " ".join(f"{k}={v}" for k, v in args.items())
    assert set(args) <= set(parse_dsn(dsn))


def test_le_moteur_de_production_utilise_ces_options():
    import inspect
    source = inspect.getsource(database.init_db)
    assert "create_engine(pg_url, **options_postgresql())" in source


# --------------------------------------------------------------------------
# Le pilote
# --------------------------------------------------------------------------

@pytest.mark.parametrize("brute,attendue", [
    ("postgresql://u:p@h:5432/b", "postgresql+psycopg2://u:p@h:5432/b"),
    ("postgres://u:p@h/b", "postgresql+psycopg2://u:p@h/b"),
    ("postgresql+psycopg2://u:p@h/b", "postgresql+psycopg2://u:p@h/b"),
    ("postgresql+psycopg://u:p@h/b", "postgresql+psycopg://u:p@h/b"),
    ("sqlite:///x.db", "sqlite:///x.db"),
])
def test_le_pilote_est_nomme_dans_l_url(brute, attendue):
    assert database.url_postgresql(brute) == attendue


def test_le_pilote_nomme_est_celui_que_le_projet_declare():
    """L'URL pointe vers psycopg2 ; requirements.txt doit donc le déclarer."""
    with open("requirements.txt", encoding="utf-8") as f:
        assert "psycopg2" in f.read()
    assert database.pg_url.startswith("postgresql+psycopg2://") or not database.pg_url.startswith("postgres")


def test_un_pilote_absent_ne_bascule_plus_sur_sqlite(monkeypatch):
    """
    Un pilote manquant est une installation incomplète, pas une base
    injoignable : basculer en silence sur une base locale vide est la pire
    réponse possible pour un produit de conformité.
    """
    def _sans_pilote(*a, **k):
        raise ModuleNotFoundError("No module named 'psycopg'")
    monkeypatch.setattr(database, "pg_url", "postgresql+psycopg://u:p@h/b")
    monkeypatch.setattr(database, "create_engine", _sans_pilote)
    with pytest.raises(RuntimeError, match="Repli sur SQLite refusé"):
        database.init_db()


# --------------------------------------------------------------------------
# La synchronisation ne garde pas de transaction ouverte pendant un téléchargement
# --------------------------------------------------------------------------

def test_aucune_transaction_n_est_ouverte_pendant_le_telechargement():
    """
    Le cas DFAT : neuf minutes de portail muet, une transaction ouverte, une
    connexion fermée par le serveur — et l'erreur de base qui masque la vraie.
    """
    from pathlib import Path
    from fiskr import sync
    from fiskr.database import get_db

    db = next(get_db())
    vu = {}

    def _fetcher(url, dest):
        vu["transaction"] = db.in_transaction()
        Path(dest).write_text("", encoding="utf-8")

    try:
        sync._run_list_replacement_sync(
            db, source="TXTEST", file_type="WATCHLIST_MAS", url="https://exemple.test/x.csv",
            parser=lambda chemin: iter(()), file_label="tx", temp_suffix=".csv",
            fetcher=_fetcher)
    finally:
        db.close()
    assert vu.get("transaction") is False, "une transaction restait ouverte pendant le téléchargement"
