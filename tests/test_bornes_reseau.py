"""
Bornes de taille des lectures RESEAU.

Une precedente passe a plafonne tout ce qui entre par televersement. Le meme
artefact entre aussi par telechargement, et ce cote-la n'etait borne par rien :
`download_to_file` ecrivait sur le disque tant que l'hote envoyait, et
`http_get_text` mettait en memoire tout ce qu'on lui servait. Une porte fermee
et une porte ouverte, pour la meme piece.

Ces tests tiennent les deux bornes ET leur coherence : le plafond de
telechargement est DERIVE du plafond de televersement des listes, il n'en est
pas une copie qu'on maintiendrait a la main.
"""

import pytest

import fiskr.sync as sync_mod
from fiskr.limites import (TAILLE_MAX_TELEVERSEMENT, TAILLE_MAX_TELECHARGEMENT,
                           TAILLE_MAX_PAGE)
from fiskr.sync import http_get_text, download_to_file, ReponseTropVolumineuse


def _sans_attente(monkeypatch):
    """Reprises immediates : ces tests mesurent des bornes, pas des delais."""
    cfg = sync_mod.get_sync_config()
    reseau = dict(cfg["network"])
    reseau["backoff_seconds"] = 0
    reseau["retries"] = 3
    monkeypatch.setattr(sync_mod, "network_for_url", lambda url: reseau)


class _FluxCompte:
    """Reponse en streaming qui COMPTE ce qui lui a reellement ete demande."""

    def __init__(self, total_octets, status_code=200, bloc=256 * 1024):
        self.status_code = status_code
        self.charset_encoding = "utf-8"
        self.headers = {}
        self._total = total_octets
        self._bloc = bloc
        self.octets_servis = 0

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self, chunk_size=65536):
        restant = self._total
        while restant > 0:
            n = min(self._bloc, restant)
            self.octets_servis += n
            restant -= n
            yield b"x" * n


# ---------------------------------------------------------------- page texte

def test_page_au_dela_du_plafond_est_refusee(monkeypatch):
    _sans_attente(monkeypatch)
    flux = _FluxCompte(TAILLE_MAX_PAGE + 1024 * 1024)

    class Client:
        def stream(self, method, url, timeout=None, headers=None):
            return flux

    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: Client())
    with pytest.raises(ReponseTropVolumineuse) as exc:
        http_get_text("https://exemple.test/flux.rss")
    assert "32 Mo" in str(exc.value)
    assert "fiskr/limites.py" in str(exc.value)  # l'exploitant sait quoi regler


def test_la_lecture_s_arrete_au_premier_bloc_de_trop(monkeypatch):
    """
    La borne doit etre REELLE, pas cosmetique : refuser apres avoir tout mis en
    memoire ne protege de rien. On verifie donc que le flux n'a pas ete servi
    en entier — c'est la seule preuve que la memoire est bornee.
    """
    _sans_attente(monkeypatch)
    flux = _FluxCompte(TAILLE_MAX_PAGE * 4)

    class Client:
        def stream(self, method, url, timeout=None, headers=None):
            return flux

    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: Client())
    with pytest.raises(ReponseTropVolumineuse):
        http_get_text("https://exemple.test/flux.rss")
    # Un seul bloc au-dela du plafond a ete lu, pas les quatre fois le plafond
    assert flux.octets_servis <= TAILLE_MAX_PAGE + 256 * 1024
    assert flux.octets_servis < TAILLE_MAX_PAGE * 2


def test_un_depassement_n_est_jamais_retente(monkeypatch):
    """Rejouer ne ferait pas retrecir la reponse : ce serait payer deux fois le
    telechargement qu'on vient de refuser."""
    _sans_attente(monkeypatch)
    appels = {"n": 0}

    class Client:
        def stream(self, method, url, timeout=None, headers=None):
            appels["n"] += 1
            return _FluxCompte(TAILLE_MAX_PAGE + 1)

    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: Client())
    with pytest.raises(ReponseTropVolumineuse):
        http_get_text("https://exemple.test/flux.rss")
    assert appels["n"] == 1


def test_page_sous_le_plafond_passe_normalement(monkeypatch):
    _sans_attente(monkeypatch)

    class Flux(_FluxCompte):
        def iter_bytes(self, chunk_size=65536):
            yield b"<html>Journal Officiel</html>"

    class Client:
        def stream(self, method, url, timeout=None, headers=None):
            return Flux(0)

    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: Client())
    assert http_get_text("https://eur-lex.europa.eu/oj") == "<html>Journal Officiel</html>"


def test_jeu_de_caracteres_declare_par_l_hote_est_respecte(monkeypatch):
    """Le passage au streaming ne doit pas perdre le decodage que faisait
    `response.text` : un acte du JO en latin-1 doit rester lisible."""
    _sans_attente(monkeypatch)

    class Flux(_FluxCompte):
        def iter_bytes(self, chunk_size=65536):
            yield "Décision du Conseil".encode("iso-8859-1")

    class Client:
        def stream(self, method, url, timeout=None, headers=None):
            f = Flux(0)
            f.charset_encoding = "iso-8859-1"
            return f

    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: Client())
    assert http_get_text("https://eur-lex.europa.eu/oj") == "Décision du Conseil"


def test_octet_invalide_ne_fait_pas_echouer_la_synchronisation(monkeypatch):
    """Un accent casse dans une page vaut mieux qu'une source entiere perdue."""
    _sans_attente(monkeypatch)

    class Flux(_FluxCompte):
        def iter_bytes(self, chunk_size=65536):
            yield b"Reglement \xff 269/2014"

    class Client:
        def stream(self, method, url, timeout=None, headers=None):
            return Flux(0)

    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: Client())
    texte = http_get_text("https://eur-lex.europa.eu/oj")
    assert "269/2014" in texte


# ------------------------------------------------------------- telechargement

def _flux_httpx(monkeypatch, total_octets):
    import httpx
    flux = _FluxCompte(total_octets)
    flux.headers = {"content-length": str(total_octets)}
    monkeypatch.setattr(
        httpx, "stream",
        lambda method, url, timeout=None, follow_redirects=None, headers=None: flux)
    return flux


def test_telechargement_au_dela_du_plafond_est_refuse(monkeypatch, tmp_path):
    _sans_attente(monkeypatch)
    _flux_httpx(monkeypatch, TAILLE_MAX_TELECHARGEMENT + 1024 * 1024)
    dest = tmp_path / "liste.xml"
    with pytest.raises(ReponseTropVolumineuse):
        download_to_file("https://exemple.test/liste.xml", dest, retries=0)


def test_un_telechargement_refuse_ne_laisse_rien_sur_le_disque(monkeypatch, tmp_path):
    """Refuser 512 Mo puis en laisser 512 Mo sur le disque n'aurait aucun sens :
    c'est exactement la place qu'on refusait d'accorder."""
    _sans_attente(monkeypatch)
    _flux_httpx(monkeypatch, TAILLE_MAX_TELECHARGEMENT + 1024 * 1024)
    dest = tmp_path / "liste.xml"
    with pytest.raises(ReponseTropVolumineuse):
        download_to_file("https://exemple.test/liste.xml", dest, retries=0)
    assert not dest.exists()


def test_telechargement_sous_le_plafond_est_ecrit(monkeypatch, tmp_path):
    _sans_attente(monkeypatch)
    _flux_httpx(monkeypatch, 3 * 1024 * 1024)
    dest = tmp_path / "liste.xml"
    download_to_file("https://exemple.test/liste.xml", dest, retries=0)
    assert dest.stat().st_size == 3 * 1024 * 1024


# ------------------------------------------------------- coherence des bornes

def test_le_plafond_de_telechargement_est_derive_de_celui_du_televersement():
    """
    Une liste officielle est la MEME piece qu'elle arrive par l'ecran d'import
    ou par l'URL d'une source. Deux plafonds tenus a la main de part et d'autre
    finiraient par diverger — et la porte la plus haute annulerait l'autre.
    """
    assert TAILLE_MAX_TELECHARGEMENT == TAILLE_MAX_TELEVERSEMENT["liste"]


def test_une_page_est_plafonnee_plus_bas_qu_un_fichier():
    """Une page HTML ou un flux RSS n'est pas un fichier de donnees : lui
    accorder le plafond d'une liste officielle reviendrait a ne rien borner."""
    assert TAILLE_MAX_PAGE < TAILLE_MAX_TELECHARGEMENT


def test_la_table_des_plafonds_a_un_seul_proprietaire():
    """`fiskr.api` reexporte la table, il n'en tient pas une seconde copie."""
    from fiskr.api import TAILLE_MAX_TELEVERSEMENT as depuis_api
    assert depuis_api is TAILLE_MAX_TELEVERSEMENT


def test_http_get_text_ne_bufferise_pas_le_corps_avant_de_le_mesurer():
    """
    Garde de source : mesurer `response.text` APRES coup ne borne rien, le
    corps est deja en memoire a ce moment-la. Cette voie doit lire par blocs.
    """
    import inspect
    brut = inspect.getsource(sync_mod.http_get_text)
    # Le commentaire du decodage cite `response.text` pour expliquer ce qu'il
    # reproduit : on ne lit que le CODE, pas ce qu'on dit du code.
    code = "\n".join(l.split("#", 1)[0] for l in brut.splitlines())
    assert ".stream(" in code
    assert "iter_bytes" in code
    assert "TAILLE_MAX_PAGE" in code
    assert "response.text" not in code
