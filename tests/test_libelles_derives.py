"""
Un code s'affiche par son libellé, et chaque vocabulaire a UNE table.

Relevé dans la modale d'alerte d'une interface en français : l'historique
montrait CREATED, PROPOSED, REDETECTED ; l'explication du score, les clés
`dob`, `gender`, `geography` sous un titre « (decision tree) » ; un toast
« Décision validée, alerte clôturée (CLOSED_FALSE_POSITIVE) » ; la liste des
comptes « ANALYSTE USER » pendant que la barre latérale disait « Analyste
Conformité » — deux tables de rôles tenues à la main, qui avaient divergé, et
qui affichaient `BLOCKING` ou `RULES` bruts pour les rôles que la seconde
ignorait. Et la fin d'un import : « Successfully imported 25 items ».

Ces tests dérivent du code les vocabulaires réellement émis (actions
d'historique écrites par le serveur, ajustements du score, rôles déclarés,
destinataires de notification) et exigent un libellé pour chacun — un code
ajouté demain ne repartira pas brut.
"""
import ast
import re
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
FISKR = RACINE / "fiskr"
APP_JS = (FISKR / "static" / "app.js").read_text(encoding="utf-8")
I18N_JS = (FISKR / "static" / "i18n.js").read_text(encoding="utf-8")


def _table_js(nom):
    bloc = re.search(rf"const {nom} = \{{(.*?)\n\}};", APP_JS, re.S)
    assert bloc, f"{nom} introuvable dans app.js"
    return set(re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"', bloc.group(1)))


def _actions_d_historique_ecrites():
    """Actions posées dans alert_events, quelle que soit la forme d'écriture."""
    trouvees = set()
    for fichier in sorted(FISKR.glob("*.py")):
        for noeud in ast.walk(ast.parse(fichier.read_text(encoding="utf-8"))):
            if not isinstance(noeud, ast.Call):
                continue
            appele = ast.unparse(noeud.func)
            # `_journal` : l'ecriture du registre des gels (fiskr/gels.py), meme
            # signature — il ecrivait quatre actions que ce releve ne voyait pas.
            if (appele.endswith("_log_alert_event") or appele == "_journal") \
                    and len(noeud.args) >= 4 and isinstance(noeud.args[3], ast.Constant):
                trouvees.add(noeud.args[3].value)
            elif appele.endswith("AlertEvent"):
                for kw in noeud.keywords:
                    if kw.arg == "action" and isinstance(kw.value, ast.Constant):
                        trouvees.add(kw.value.value)
    return trouvees


# --------------------------------------------------- historique d'alerte

def test_la_detection_trouve_bien_les_actions():
    actions = _actions_d_historique_ecrites()
    assert {"CREATED", "REDETECTED", "PROPOSED", "VALIDATED"} <= actions
    assert len(actions) >= 15


def test_chaque_action_d_historique_a_son_libelle():
    muettes = _actions_d_historique_ecrites() - _table_js("ALERT_EVENT_LABELS")
    assert not muettes, f"actions affichées en code brut dans l'historique : {sorted(muettes)}"


def test_aucun_libelle_d_historique_sans_action():
    fantomes = _table_js("ALERT_EVENT_LABELS") - _actions_d_historique_ecrites()
    assert not fantomes, f"libellés sans action écrite : {sorted(fantomes)}"


def test_l_historique_affiche_le_libelle_et_non_le_code():
    assert "escapeHtml(e.action)" not in APP_JS
    assert APP_JS.count("alertEventLabel(e.action)") >= 2, "modale d'alerte et client 360°"


# ------------------------------------------------- explication du score

def test_chaque_ajustement_du_score_a_son_libelle():
    scoring = (FISKR / "scoring.py").read_text(encoding="utf-8")
    # Les ajustements s'écrivent `"cle": {"score": ..., "description": ...}`.
    cles = set(re.findall(r'"(\w+)":\s*\{\s*"score"', scoring))
    assert {"dob", "gender", "geography"} <= cles
    assert cles <= _table_js("ADJUSTMENT_LABELS")
    assert "escapeHtml(adjustmentLabel(k))" in APP_JS, "la modale d'alerte passe par la table"
    assert "(decision tree)" not in APP_JS


# ------------------------------------------------------------------ rôles

def test_un_libelle_par_role_declare_et_une_seule_table():
    from fiskr.auth import VALID_ROLES
    assert _table_js("ROLE_LABELS") == set(VALID_ROLES)
    # La barre latérale et la liste des comptes avaient chacune leur table :
    # un rôle ne se nomme qu'à un endroit.
    assert len(re.findall(r'\bauditor: "', APP_JS)) == 1
    assert "badgeLabels" not in APP_JS


def test_chaque_destinataire_de_notification_se_nomme():
    from fiskr.events import PSEUDO_AUDIENCES
    assert set(PSEUDO_AUDIENCES) == _table_js("NOTIF_PSEUDO_AUDIENCE_LABELS")


# ------------------------------------------------------- messages serveur

def test_aucun_code_brut_dans_les_messages_du_serveur():
    """Un statut ou une priorité interpolés tels quels finissent dans un toast."""
    source = (FISKR / "api.py").read_text(encoding="utf-8")
    for motif in ("({alert.status})", "à {new_priority}", "→ {new_priority}",
                  "Successfully imported", "already uploaded"):
        assert motif not in source, f"code brut ou anglais dans un message : {motif}"


def test_les_libelles_serveur_couvrent_chaque_valeur():
    from fiskr.database import ALERT_PRIORITIES, ALERT_PRIORITY_LABELS, ALERT_CLOSED_STATUSES
    from fiskr.narrative import ALERT_STATUS_LABELS
    assert set(ALERT_PRIORITY_LABELS) == set(ALERT_PRIORITIES)
    assert set(ALERT_CLOSED_STATUSES) <= set(ALERT_STATUS_LABELS)


def test_les_messages_serveur_nouveaux_sont_traduits():
    from fiskr.apimessages import translate_message
    from fiskr.database import ALERT_PRIORITY_LABELS
    from fiskr.narrative import ALERT_STATUS_LABELS
    messages = [f"Priorité passée à {l}." for l in ALERT_PRIORITY_LABELS.values()]
    messages += [f"Décision validée, alerte {ALERT_STATUS_LABELS[s]}."
                 for s in ("CLOSED_CONFIRMED", "CLOSED_FALSE_POSITIVE")]
    # La validation d'un vrai positif sur une liste de gel annonce sa mesure
    from fiskr.api import _message_de_mesure
    from types import SimpleNamespace
    messages += [f"Décision validée, alerte {ALERT_STATUS_LABELS['CLOSED_CONFIRMED']}."
                 + _message_de_mesure(SimpleNamespace(id=12))]
    messages += ["Fichier déjà importé (même empreinte SHA-256) : rien n'a été réimporté.",
                 "Utilisateur 'marie' créé avec succès.", "Compte utilisateur mis à jour."]
    for m in messages:
        for langue in ("en", "de", "es", "zh", "ar"):
            assert translate_message(m, langue), f"[{langue}] {m}"
        assert translate_message(m, "en") != m


def test_le_toast_de_fin_d_import_se_traduit():
    """Préfixe du frontal + message du serveur : une règle composée par forme."""
    assert re.search(r"Instantané importé avec succès ! \(\\d\+\) fiches importées et mises en production",
                     I18N_JS)


def test_la_langue_de_la_page_accompagne_chaque_appel():
    """Sans en-tête, le navigateur envoie la sienne (« en-US » en Chromium) et
    une interface en français recevait ses toasts en anglais."""
    debut = APP_JS.index("async function apiFetch(")
    corps = APP_JS[debut: APP_JS.index("\n}\n", debut)]
    assert 'currentLang() !== "fr"' not in corps
    assert '"Accept-Language": fiskrI18n.currentLang()' in corps
    login = (FISKR / "static" / "login.html").read_text(encoding="utf-8")
    assert '"Accept-Language"' in login


def test_un_fichier_deja_importe_n_est_pas_annonce_importe():
    debut = APP_JS.index("async function handleIngestion(")
    corps = APP_JS[debut: APP_JS.index("\n}\n", debut)]
    assert corps.index("data.already_imported") < corps.index("Instantané importé avec succès")
