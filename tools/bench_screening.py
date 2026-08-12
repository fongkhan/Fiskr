"""
Banc d'essai du noyau de criblage (blocking + scoring), hors base.

Construit un univers synthetique de fiches listees et un panel de clients,
puis chronometre et PROFILE `screen_one` — le code commun aux chemins temps
reel, batch et cahier de tests. Sert a mesurer l'effet d'une optimisation du
moteur sur SA machine (les coeurs et la version de Python comptent) :

    python tools/bench_screening.py

Ajuster N_ENT / N_CLI ci-dessous pour se rapprocher de sa volumetrie. Aucune
base n'est touchee (config lue en lecture seule, repli SQLite ignore).
"""
import os, sys, time, random, cProfile, pstats, io
os.environ.setdefault("FISKR_JOBS_MODE", "eager")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fiskr.config import config as base_config
from fiskr.blocking import generate_blocking_keys
from fiskr import screenpool

rng = random.Random(42)
FIRST = ["Vladimir","Ivan","Sergei","Dmitri","Boris","Alexei","Nikolai","Mikhail","Yuri","Andrei",
         "Mohammed","Ahmed","Ali","Hassan","Omar","Khalid","Youssef","Ibrahim","Mahmoud","Tariq",
         "Jean","Pierre","Louis","Michel","Henri","Paul","Jacques","Claude","Andre","Marcel"]
LAST = ["Ivanov","Petrov","Sidorov","Volkov","Kuznetsov","Popov","Sokolov","Lebedev","Novikov","Morozov",
        "Al-Amin","Rahman","Hussein","Nasser","Farah","Aziz","Mansour","Haddad","Saleh","Karim",
        "Dupont","Martin","Bernard","Durand","Petit","Robert","Richard","Moreau","Laurent","Simon"]
COUNTRIES = ["RU","IR","SY","FR","DE","US","GB","AE","SA","CN"]

def rand_name():
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"

def make_entity(i):
    name = rand_name()
    parts = name.split()
    return {
        "entity_id": f"E{i}", "primary_name": name, "entity_type": "I",
        "individual_name_parsed": {"first_name": parts[0], "last_name": parts[1]},
        "aliases": {"high_priority": [], "low_priority": []},
        "dates_of_birth": [f"19{rng.randint(50,99)}-0{rng.randint(1,9)}-1{rng.randint(0,9)}"],
        "gender": rng.choice(["M","F"]),
        "countries": {"citizenship": [rng.choice(COUNTRIES)]},
        "_list_type": "WATCHLIST_EU",
    }

N_ENT = 15000
N_CLI = 800
entities = [make_entity(i) for i in range(N_ENT)]

cfg = dict(base_config)
cfg["engine_channel"] = "SCREENING"
cfg.setdefault("scoring", {})
cfg["scoring"].setdefault("weights", {"jaro_winkler":0.4,"damerau_levenshtein":0.4,"token_sort":0.2})
bl = dict(cfg.get("blocking", {}) or {})
bl.setdefault("custom_key_layout", ["COUNTRY_ISO","ENTITY_TYPE","PHONETIC_FIRST"])
bl["channel"] = "SCREENING"
cfg["blocking"] = bl

# Build index
index = {}
for ent in entities:
    for k in generate_blocking_keys(ent, cfg):
        index.setdefault(k, []).append(ent)

# Clients : moitié dérivés d'entités (candidats garantis), moitié aléatoires
clients = []
for i in range(N_CLI):
    if i % 2 == 0:
        ent = rng.choice(entities)
        parts = ent["primary_name"].split()
        clients.append({"client_id": f"C{i}", "client_type": "PP",
                        "client_first_name": parts[0], "client_last_name": parts[1],
                        "client_countries": {"nationality": ent["countries"]["citizenship"]},
                        "client_dob": ent["dates_of_birth"][0], "client_gender": ent["gender"]})
    else:
        name = rand_name(); parts = name.split()
        clients.append({"client_id": f"C{i}", "client_type": "PP",
                        "client_first_name": parts[0], "client_last_name": parts[1],
                        "client_countries": {"nationality": [rng.choice(COUNTRIES)]},
                        "client_dob": f"1970-01-01", "client_gender": "M"})

def run():
    cand_total = 0
    for c in clients:
        screenpool.screen_one(c, index, cfg, set(), [])
    return cand_total

# warmup + candidate stats
import statistics
from fiskr.blocking import lookup_blocking_keys
cc = [sum(len(index.get(k,[])) for k in lookup_blocking_keys(c, cfg)) for c in clients]
print(f"index buckets={len(index)}  clients={N_CLI}  entities={N_ENT}")
print(f"candidats/client: moy={statistics.mean(cc):.1f} max={max(cc)}")

t0 = time.perf_counter(); run(); dt = time.perf_counter()-t0
print(f"screen_one x{N_CLI} : {dt*1000:.0f} ms  ({dt/N_CLI*1000:.2f} ms/client)")

pr = cProfile.Profile(); pr.enable(); run(); pr.disable()
s = io.StringIO(); ps = pstats.Stats(pr, stream=s).sort_stats("tottime"); ps.print_stats(14)
print(s.getvalue())
