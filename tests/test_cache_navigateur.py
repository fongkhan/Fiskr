"""
Cache navigateur : trois allers-retours par chargement de page, pour rien.

Les pages référencent leurs ressources par l'**empreinte de leur contenu**
(`app.js?v=<hash>`) : l'URL change dès que le fichier change, et jamais
autrement. Mais aucune réponse ne portait d'en-tête de cache, alors le
navigateur les revalidait à **chaque** chargement. Mesuré sur la production :

    app.js       304, 0 octet, 1,01 s
    i18n.js      304, 0 octet, 0,66 s
    styles.css   304, 0 octet, 0,70 s

Le corps était déjà épargné — correction d'une lecture trop rapide de ma
part : le frontal comprime bien en brotli (476 Ko d'`app.js` arrivent en
167 Ko). Ce qui ne l'était pas, ce sont les allers-retours eux-mêmes.

Une URL versionnée est désormais **immuable** (un an), donc plus redemandée du
tout. Une URL SANS version garde la revalidation : rien ne garantit alors que
l'URL suive le contenu.

La page HTML, elle, ne peut pas être mise en cache longuement — c'est elle qui
porte la version des ressources, et servie périmée elle référencerait les
anciennes, exactement le défaut que l'empreinte corrige. Elle porte donc
`no-cache` plus un ETag : redemandée à chaque fois, mais rendue en 304 vide
tant que rien n'a bougé.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app, _version_demandee, _StaticVersionne
from fiskr.auth import get_current_user
from fiskr import buildinfo

INDEX = Path("fiskr/static/index.html").read_text(encoding="utf-8")


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ------------------------- ressources versionnées -------------------------

def test_une_ressource_versionnee_est_immuable(client):
    reponse = client.get(f"/static/app.js?v={buildinfo.STATIC_VERSION}")
    assert reponse.status_code == 200
    cache = reponse.headers.get("cache-control", "")
    assert "immutable" in cache and "max-age=31536000" in cache, cache


def test_une_ressource_sans_version_reste_revalidee(client):
    """Rien ne garantit qu'une URL nue suive le contenu : la figer un an
    servirait un vieux fichier après un déploiement."""
    reponse = client.get("/static/app.js")
    assert reponse.status_code == 200
    assert "immutable" not in reponse.headers.get("cache-control", "")


@pytest.mark.parametrize("query, attendu", [
    (b"v=abc123", True),
    (b"v=abc123&autre=1", True),
    (b"autre=1&v=abc", True),
    (b"", False),
    (b"autre=1", False),
    (b"v=", False),            # parametre vide : pas une URL generee
    (b"v=%20", False),         # blanc seul non plus
    (b"version=abc", False),   # ne pas confondre avec un autre parametre
    (b"nv=abc", False),        # ni avec un suffixe
])
def test_la_detection_de_version_ne_se_laisse_pas_abuser(query, attendu):
    assert _version_demandee(query) is attendu


def test_la_detection_survit_a_une_chaine_invalide():
    """Une requête malformée ne doit pas faire tomber le service de fichiers."""
    assert _version_demandee(b"\xff\xfe%%%") in (True, False)


def test_l_entete_est_pose_par_le_service_de_fichiers():
    assert _StaticVersionne.IMMUABLE.startswith("public,")
    assert "immutable" in _StaticVersionne.IMMUABLE


# --------------------------- la page HTML ---------------------------

def _connecte(client):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "cn", "role": "admin", "roles": ["admin"]}
    return client


def test_la_page_de_connexion_revalide_et_rend_un_304(client):
    premiere = client.get("/login")
    assert premiere.status_code == 200
    assert premiere.headers["cache-control"] == "no-cache"
    etag = premiere.headers["etag"]
    assert etag

    seconde = client.get("/login", headers={"If-None-Match": etag})
    assert seconde.status_code == 304
    assert seconde.content == b""
    assert seconde.headers["etag"] == etag


def test_un_etag_perime_rend_de_nouveau_la_page(client):
    reponse = client.get("/login", headers={"If-None-Match": 'W/"perime"'})
    assert reponse.status_code == 200
    assert reponse.content


def test_la_page_ne_peut_pas_etre_mise_en_cache_longuement(client):
    """C'est elle qui porte la version des ressources : servie depuis un cache
    périmé, elle référencerait les anciennes — le défaut même que l'empreinte
    de contenu corrige."""
    cache = client.get("/login").headers.get("cache-control", "")
    assert "no-cache" in cache
    assert "max-age" not in cache
    assert "immutable" not in cache


def test_la_page_porte_bien_la_version_du_contenu(client):
    """Le lien entre les deux moitiés : c'est parce que la page réécrit la
    version que la ressource peut être déclarée immuable."""
    html = client.get("/login").text
    versions = set(re.findall(r'\.(?:js|css)\?v=([^"\']+)', html))
    assert versions, "aucune ressource versionnée dans la page"
    assert versions == {buildinfo.STATIC_VERSION}


def test_l_empreinte_suit_le_contenu_et_pas_l_horloge():
    """Une version qui bougerait à chaque démarrage annulerait tout le cache."""
    assert buildinfo.static_fingerprint() == buildinfo.static_fingerprint()
    assert re.fullmatch(r"[0-9a-f]{12}", buildinfo.STATIC_VERSION)


def test_les_ressources_de_la_page_sont_toutes_versionnees():
    """Une ressource oubliée resterait revalidée à chaque chargement."""
    nues = re.findall(r'(?:src|href)="(/static/[^"?]+\.(?:js|css))"', INDEX)
    assert not nues, f"ressources sans version : {nues}"
