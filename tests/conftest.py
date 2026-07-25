"""
Outillage partage des tests.

Les operations longues (cahier de tests, mise en production) repondent 202
avec un jeton et travaillent en tache de fond : les tests doivent attendre la
fin du job avant d'observer son resultat.
"""
import time


def wait_for_job(client, token, timeout=60.0):
    """
    Attend la fin d'une operation de fond et retourne son etat final.

    `token` peut etre le `job_token` renvoye par un 202 (ou None : la fonction
    ne fait alors rien et retourne None, pour les chemins ou aucun job n'est
    lance). Leve AssertionError si le job n'a pas fini dans le delai imparti :
    un test ne doit jamais se poursuivre sur un resultat incomplet.
    """
    if not token:
        return None
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/progress?id={token}")
        if response.status_code == 200:
            last = response.json()
            if last.get("status") in ("DONE", "ERROR"):
                return last
        time.sleep(0.05)
    raise AssertionError(f"Job {token} toujours en cours après {timeout} s : {last}")


def post_and_wait(client, url, timeout=60.0, **kwargs):
    """POST sur un endpoint asynchrone puis attente du job. Retourne la
    reponse HTTP : les refus synchrones (400/403/404) sont rendus tels quels,
    aucun job n'ayant demarre."""
    response = client.post(url, **kwargs)
    if response.status_code in (200, 202):
        wait_for_job(client, (response.json() or {}).get("job_token"), timeout=timeout)
    return response
