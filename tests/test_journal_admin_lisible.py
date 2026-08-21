"""
Le journal d'administration doit se lire, pas se déchiffrer.

C'est l'écran qu'un contrôleur ACPR ou FED ouvre en premier : qui a fait quoi,
quand. Le frontal traduit chaque action en français depuis `ADMIN_ACTION_LABELS`
et retombe sur le code brut quand elle manque.

**Vingt-huit des trente-cinq actions journalisées n'avaient pas de libellé.**
Un contrôleur lisait `RETENTION_PURGE`, `ACCOUNT_LOCKED`, `APIKEY_REVOKED`,
`MFA_RESET`, `LOGIN_FAILED` — en majuscules, en anglais, au milieu d'une
interface française.

Ce test dérive du code la liste des actions **réellement** journalisées, plutôt
que de la recopier : c'est la seule façon qu'une action ajoutée demain ne
reparte pas muette.
"""
import ast
import re
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
APP_JS = (RACINE / "fiskr" / "static" / "app.js").read_text(encoding="utf-8")


def _actions_journalisees():
    """Actions posées au journal d'administration, quelle que soit la forme."""
    trouvees = set()
    for fichier in sorted((RACINE / "fiskr").glob("*.py")):
        arbre = ast.parse(fichier.read_text(encoding="utf-8"))
        for noeud in ast.walk(arbre):
            if not isinstance(noeud, ast.Call):
                continue
            appele = ast.unparse(noeud.func)
            if appele.endswith("log_admin_action"):
                # 3e positionnel, ou mot-cle `action=`
                if len(noeud.args) >= 3 and isinstance(noeud.args[2], ast.Constant):
                    trouvees.add(noeud.args[2].value)
                for kw in noeud.keywords:
                    if kw.arg == "action" and isinstance(kw.value, ast.Constant):
                        trouvees.add(kw.value.value)
            elif appele.endswith("AdminAuditLog"):
                for kw in noeud.keywords:
                    if kw.arg == "action" and isinstance(kw.value, ast.Constant):
                        trouvees.add(kw.value.value)
    return {a for a in trouvees if isinstance(a, str)}


def _libelles():
    bloc = re.search(r"const ADMIN_ACTION_LABELS = \{(.*?)\n\};", APP_JS, re.S)
    assert bloc, "ADMIN_ACTION_LABELS introuvable dans app.js"
    # Plusieurs libelles par ligne : la cle est ce qui precede un `: "`
    return set(re.findall(r'([A-Z][A-Z0-9_]*)\s*:\s*"', bloc.group(1)))


def test_la_detection_trouve_bien_les_actions():
    """Sans cette garde, une détection cassée rendrait le test vert à vide."""
    actions = _actions_journalisees()
    assert len(actions) >= 30
    assert "RETENTION_PURGE" in actions and "USER_CREATED" in actions


def test_toute_action_journalisee_a_un_libelle_francais():
    muettes = _actions_journalisees() - _libelles()
    assert not muettes, (
        "actions sans libellé — elles s'affichent en code brut dans le journal "
        f"que lit un contrôleur : {sorted(muettes)}")


def test_aucun_libelle_ne_designe_une_action_inexistante():
    """Un libellé sans action est une trace de code disparu : il fait croire
    que quelque chose est tracé alors que rien ne l'écrit plus."""
    fantomes = _libelles() - _actions_journalisees()
    assert not fantomes, f"libellés sans action correspondante : {sorted(fantomes)}"


def test_les_actions_sensibles_sont_nommees_sans_ambiguite():
    """Trois actions qu'un contrôleur cherche explicitement : la purge de
    rétention, la révocation de clé et le verrouillage de compte."""
    bloc = re.search(r"const ADMIN_ACTION_LABELS = \{(.*?)\n\};", APP_JS, re.S).group(1)
    for action in ("RETENTION_PURGE", "APIKEY_REVOKED", "ACCOUNT_LOCKED"):
        libelle = re.search(rf"{action}:\s*\"([^\"]+)\"", bloc)
        assert libelle, f"{action} sans libellé"
        assert len(libelle.group(1)) > 5, f"{action} : libellé trop court"
