"""
L'outil parle au bon moment : notifications du navigateur, visite guidée.

**Les notifications** posent une question qu'un produit de conformité ne peut
pas traiter à la légère : une notification du système s'affiche par-dessus
n'importe quelle application, sur un poste parfois partagé ou projeté en
réunion. « Ivan Ivanov correspond à la liste OFAC » sur cet écran-là est une
fuite, pas un service. Le message dit donc un NOMBRE ; l'application dit le
reste. C'est le premier test de ce fichier, et le plus important.

Deux autres décisions comptent : le sondage ne tourne que quand l'onglet est
masqué (l'inverse exact du sondage d'écran, qui s'arrête à ce moment-là) et
seulement si ce poste l'a demandé — rien n'est ajouté à la charge du serveur
pour les autres ; et les notifications sont groupées, jamais une par alerte,
puisqu'un homonyme d'un nom courant peut en ouvrir des centaines d'un coup.

**La visite guidée** désigne des éléments réels par leur identifiant, et saute
ceux qui ne sont pas là : montrer du doigt une porte qui n'existe pas est
pire que se taire.
"""
import os
import re

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


def _bloc(marqueur, source=None):
    src = source if source is not None else _lire("app.js")
    debut = src.index(marqueur)
    return src[debut:src.index("\n}", debut)]


# ------------------------------------------------- ce que le message contient

def test_la_notification_ne_porte_aucun_nom():
    """LE point du lot. Le corps du message se construit à partir d'un compte,
    et de rien d'autre : aucune donnée de dossier n'a le droit d'y entrer."""
    fn = _bloc("function _prevenirLePoste")
    assert "nouvelles === 1" in fn and "nouvelles} nouvelles alertes" in fn
    for interdit in ("client_name", "watchlist_name", "client_id", "primary_name"):
        assert interdit not in fn, f"« {interdit} » n'a rien à faire dans une notification système"


def test_les_notifications_sont_groupees_et_ne_s_empilent_pas():
    """Un homonyme d'un nom très courant peut ouvrir des centaines d'alertes
    d'un coup : les notifier une par une serait un incident en soi."""
    fn = _bloc("function _prevenirLePoste")
    assert 'tag: "fiskr-alertes"' in fn, "sans tag, chaque signal empile une bannière de plus"
    src = _lire("app.js")
    veille = _bloc("async function _veilleNotifications", src)
    assert "nouvelles > 0" in veille and "_prevenirLePoste(nouvelles)" in veille, \
        "un seul signal par tour de veille, portant le nombre"


def test_la_veille_ne_tourne_que_l_onglet_masque_et_sur_demande():
    veille = _bloc("async function _veilleNotifications")
    assert "if (!ongletMasque())" in veille, \
        "une notification pour un écran qu'on regarde est du bruit"
    assert "_notifNavVoulue()" in veille and "_notifNavAutorisee()" in veille, \
        "rien ne doit sonder pour les postes qui n'ont rien demandé"
    demarrage = _bloc("function _demarrerVeilleNotifications")
    assert "_notifNavVoulue() || !_notifNavAutorisee()" in demarrage


def test_le_premier_tour_ne_previent_de_rien():
    """Au premier relevé il n'y a pas de « avant » : annoncer les alertes
    déjà là comme des nouveautés réveillerait pour du travail connu."""
    veille = _bloc("async function _veilleNotifications")
    assert "_notifNavDernierCompte === null" in veille
    assert re.search(r"_notifNavDernierCompte = ouvertes;\s*return;", veille)


def test_la_permission_ne_se_demande_qu_au_geste_de_l_utilisateur():
    """Demandée au chargement, elle est refusée d'office par les navigateurs
    — et par les utilisateurs."""
    src = _lire("app.js")
    init = _bloc("function initNotificationsNavigateur", src)
    assert "requestPermission" not in init, "l'init ne doit rien demander"
    bascule = _bloc("async function basculerNotificationsNavigateur", src)
    assert "requestPermission" in bascule


def test_un_refus_du_navigateur_est_dit_et_non_masque():
    """Une case cochée qui ne produira jamais rien est pire qu'une case
    décochée : le refus se dit, et le réglage revient à l'arrêt."""
    bascule = _bloc("async function basculerNotificationsNavigateur")
    assert 'permission !== "granted"' in bascule
    assert '_NOTIF_NAV_CLE, "0"' in bascule
    assert "Refusées par le navigateur" in bascule


def test_le_reglage_est_declare_dans_le_balisage():
    page = _lire("index.html")
    assert re.search(r'id="notif-navigateur"[^>]*onchange="basculerNotificationsNavigateur\(this\.checked\)"', page)
    assert 'id="notif-navigateur-etat"' in page
    assert re.search(r"^ initNotificationsNavigateur\(\);", _lire("app.js"), re.M)


def test_l_absence_de_l_api_ne_casse_rien():
    """Un navigateur sans notifications système ne doit pas jeter — il doit
    le dire."""
    src = _lire("app.js")
    for nom in ("function initNotificationsNavigateur", "async function basculerNotificationsNavigateur"):
        fn = _bloc(nom, src)
        assert 'typeof Notification === "undefined"' in fn, nom


# --------------------------------------------------------------- la visite

def test_chaque_etape_designe_un_element_reel_du_balisage():
    """Une étape qui pointe un identifiant inexistant serait sautée pour
    toujours, sans que rien ne le signale."""
    src = _lire("app.js")
    m = re.search(r"const VISITE_ETAPES = \[(.*?)\n\];", src, re.S)
    assert m, "étapes de visite introuvables"
    cibles = re.findall(r'cible: "([\w-]+)"', m.group(1))
    assert len(cibles) >= 4, cibles
    page = _lire("index.html")
    for cible in cibles:
        assert f'id="{cible}"' in page, f"étape pointant un élément absent : {cible}"


def test_une_etape_dont_l_element_manque_est_sautee():
    """Les écrans dépendent du rôle : montrer du doigt une porte que ce
    compte ne voit pas est pire que se taire."""
    src = _lire("app.js")
    filtre = _bloc("function _elementDeVisite", src)
    assert "getElementById(etape.cible)" in filtre
    assert "offsetParent === null" in filtre, "un élément masqué ne se montre pas"
    demarrage = _bloc("function demarrerVisiteGuidee", src)
    assert "VISITE_ETAPES.filter(_elementDeVisite)" in demarrage


def test_la_visite_ne_s_impose_qu_une_fois_et_reste_revisitable():
    src = _lire("app.js")
    init = _bloc("function initVisiteGuidee", src)
    assert '_VISITE_CLE) === "1"' in init and "if (vue) return;" in init
    fin = _bloc("function _fermerVisiteGuidee", src)
    assert '_VISITE_CLE, "1"' in fin
    page = _lire("index.html")
    assert "demarrerVisiteGuidee(true)" in page, \
        "une visite qu'on ne peut plus revoir est une visite qu'on subit"


def test_le_contenu_de_la_visite_est_neutralise_et_traduit():
    src = _lire("app.js")
    affichage = _bloc("function _afficherEtapeDeVisite", src)
    assert "escapeHtml(etape.titre)" in affichage and "escapeHtml(etape.texte)" in affichage
    dico = _lire("i18n.js")
    m = re.search(r"const VISITE_ETAPES = \[(.*?)\n\];", src, re.S)
    absents = []
    for cle in re.findall(r'(?:titre|texte): "((?:[^"\\]|\\.)+)"', m.group(1)):
        if f'"{cle}"' not in dico:
            absents.append(cle[:60])
    assert not absents, "étapes sans traduction :\n  " + "\n  ".join(absents)


def test_la_visite_a_son_habillage():
    css = _lire("styles.css")
    for classe in (".visite-voile", ".visite-halo", ".visite-bulle", ".visite-compte"):
        assert classe in css, classe
