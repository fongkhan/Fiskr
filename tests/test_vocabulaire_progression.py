"""
Le vocabulaire de progression est déclaré à trois endroits : ils doivent
s'accorder.

Une opération longue annonce une **phase** (`QUEUED`, `PARSE`, `PERSIST`…) et
une **nature** (`import`, `sync`, `backtest`…). Trois déclarations en dépendent :

1. ce que le code **émet** réellement ;
2. `fiskr/progress.py` — `PHASES` et `KINDS`, la référence serveur ;
3. `app.js` — `PROGRESS_PHASE_LABELS` (le libellé français affiché) et
   `OPERATION_KIND_ICONS` (l'icône du panneau des travaux).

Elles avaient divergé. **Quatre phases émises n'étaient déclarées nulle part**,
dont `QUEUED` — la phase que porte *tout* job en attente, donc celle que
l'opérateur voyait le plus souvent : un code brut en majuscules au milieu d'une
interface en français. Et **neuf des quatorze natures** soumises ne figuraient
pas dans la table d'icônes, donc s'affichaient sans icône.

Ces tests dérivent la liste de ce qui est réellement émis depuis le code, plutôt
que de la recopier : c'est la seule façon qu'un ajout futur ne reparte pas en
silence.
"""
import re
from pathlib import Path

import pytest

from fiskr.progress import KINDS, PHASES

RACINE = Path(__file__).resolve().parent.parent
SOURCES = sorted((RACINE / "fiskr").glob("*.py"))
APP_JS = (RACINE / "fiskr" / "static" / "app.js").read_text(encoding="utf-8")


def _phases_emises():
    """Phases posées par le code, quelle que soit la forme d'appel."""
    motifs = (
        r'phase\s*=\s*"([A-Z_]+)"',           # phase="PARSE" / job.phase = "QUEUED"
        r'\.phase\(\s*"([A-Z_]+)"',           # tracker.phase("DELTA")
        r'progress\(\s*"([A-Z_]+)"',          # progress("SCREEN_SHARED", …)
        r'_phase_progress\(\s*"([A-Z_]+)"',   # _phase_progress("SCREEN_ADDED")
    )
    trouvees = set()
    for fichier in SOURCES:
        if fichier.name == "progress.py":
            continue
        texte = fichier.read_text(encoding="utf-8")
        for motif in motifs:
            trouvees.update(re.findall(motif, texte))
    return trouvees


def _kinds_emis():
    motifs = (
        r'_submit_job\(\s*"([a-z_]+)"',
        r'jobs\.task\(\s*"([a-z_]+)"',
        r'kind\s*=\s*"([a-z_]+)"',
        r'"kind"\s*:\s*"([a-z_]+)"',        # charge utile de /api/progress/active
    )
    trouves = set()
    for fichier in SOURCES:
        if fichier.name == "progress.py":
            continue
        texte = fichier.read_text(encoding="utf-8")
        for motif in motifs:
            trouves.update(re.findall(motif, texte))
    return trouves


def _libelles_front():
    bloc = re.search(r"const PROGRESS_PHASE_LABELS = \{(.*?)\n\};", APP_JS, re.S)
    assert bloc, "PROGRESS_PHASE_LABELS introuvable dans app.js"
    # Plusieurs libelles par ligne : la cle est ce qui precede un `: "`
    return set(re.findall(r'([A-Z_]+)\s*:\s*"', bloc.group(1)))


def _icones_front():
    bloc = re.search(r"const OPERATION_KIND_ICONS = \{(.*?)\n\};", APP_JS, re.S)
    assert bloc, "OPERATION_KIND_ICONS introuvable dans app.js"
    return set(re.findall(r"([a-z_]+)\s*:\s*uiIcon", bloc.group(1)))


# ------------------ LA DÉTECTION FONCTIONNE ------------------

def test_la_detection_trouve_bien_des_phases_et_des_natures():
    """Sans cette garde, une regex cassée rendrait tous les tests verts."""
    assert len(_phases_emises()) >= 12
    assert len(_kinds_emis()) >= 10


# ------------------ ACCORD DES TROIS DÉCLARATIONS ------------------

def test_toute_phase_emise_est_declaree():
    manquantes = _phases_emises() - set(PHASES)
    assert not manquantes, (
        f"phases émises mais absentes de progress.PHASES : {sorted(manquantes)}")


def test_toute_phase_declaree_a_un_libelle_francais():
    muettes = set(PHASES) - _libelles_front()
    assert not muettes, (
        f"phases sans libellé dans app.js — elles s'afficheront en code brut : "
        f"{sorted(muettes)}")


def test_queued_a_un_libelle():
    """Le cas qui a motivé ces tests : la phase de tout job en attente."""
    assert "QUEUED" in PHASES
    assert "QUEUED" in _libelles_front()


def test_toute_nature_emise_est_declaree():
    manquantes = _kinds_emis() - set(KINDS)
    assert not manquantes, (
        f"natures soumises mais absentes de progress.KINDS : {sorted(manquantes)}")


def test_toute_nature_declaree_a_une_icone():
    sans_icone = set(KINDS) - _icones_front()
    assert not sans_icone, (
        f"natures sans icône dans app.js : {sorted(sans_icone)}")


def test_toute_nature_declaree_a_un_lien_profond():
    """La ligne du panneau des travaux doit emmener quelque part : neuf des
    quatorze natures n'avaient pas d'entrée, donc pas de lien."""
    from fiskr.api import _OPERATION_LINKS
    sans_lien = set(KINDS) - set(_OPERATION_LINKS)
    assert not sans_lien, f"natures sans lien profond : {sorted(sans_lien)}"


def test_les_liens_profonds_pointent_des_ecrans_qui_existent():
    """Un lien vers un sous-onglet inexistant est pire que pas de lien : il
    donne l'illusion d'une destination."""
    from fiskr.api import _OPERATION_LINKS
    index = (RACINE / "fiskr" / "static" / "index.html").read_text(encoding="utf-8")
    inconnus = []
    for nature, lien in _OPERATION_LINKS.items():
        parties = lien.lstrip("#").split("/")
        onglet = parties[0]
        # `applyHashRoute` cherche `sec-<onglet>` : sans lui, le clic ne fait
        # RIEN. C'était le cas de `#batch`, qui n'a jamais eu de `sec-batch`.
        if f'id="sec-{onglet}"' not in index and onglet != "alerts":
            inconnus.append((nature, lien, "onglet inexistant"))
        elif len(parties) > 1 and f'id="sub-sec-{parties[1]}"' not in index:
            inconnus.append((nature, lien, "sous-onglet inexistant"))
    assert not inconnus, f"liens profonds vers un écran inexistant : {inconnus}"


# ------------------ ET RIEN DE MORT ------------------

def test_aucune_phase_declaree_n_est_inatteignable():
    """Une phase déclarée que rien n'émet est un libellé mort : soit le code
    qui l'émettait a disparu, soit elle n'a jamais existé."""
    emises = _phases_emises()
    # DONE est posée par le registre lui-même (progress.finish), hors du scan
    inertes = set(PHASES) - emises - {"DONE"}
    assert not inertes, f"phases déclarées que rien n'émet : {sorted(inertes)}"


def test_aucune_nature_declaree_n_est_inatteignable():
    inertes = set(KINDS) - _kinds_emis()
    assert not inertes, f"natures déclarées que rien ne soumet : {sorted(inertes)}"
