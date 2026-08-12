"""
Banc d'essai du re-criblage post-delta : sequentiel vs parallele.

Le re-criblage repasse toute la base clients apres chaque mise en production
de liste. Ce banc compare la phase de CALCUL (celle qui est parallelisee) dans
les deux modes, sur un univers synthetique, et VERIFIE que les deux rendent
exactement les memes correspondances (indices, scores, fiches listees).

    python tools/bench_rescreen.py

Le gain plafonne au nombre de processus que `resolve_processes` accorde
(coeurs disponibles moins deux, borne par un budget memoire). Aucune base
n'est touchee.
"""
import os, sys, time, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fiskr import screenpool
from fiskr.settings import blocking_config_for
from fiskr.blocking import generate_blocking_keys, lookup_blocking_keys
from fiskr.scoring import match_entities
from fiskr.config import config

rng = random.Random(11)
FIRST=["Vladimir","Ivan","Sergei","Dmitri","Boris","Mohammed","Ahmed","Jean","Pierre","Louis"]
LAST=["Ivanov","Petrov","Sokolov","Volkov","Rahman","Hussein","Dupont","Martin","Bernard","Moreau"]
CO=["RU","IR","SY","FR","DE","US","GB","AE"]
cfg = blocking_config_for(["COUNTRY_ISO","ENTITY_TYPE","PHONETIC_FIRST"])

# Delta typique : quelques centaines d'entités changées
ents=[]
for i in range(400):
    n=f"{rng.choice(FIRST)} {rng.choice(LAST)}"; p=n.split()
    ents.append({"entity_id":f"D{i}","primary_name":n,"entity_type":"I",
        "individual_name_parsed":{"first_name":p[0],"last_name":p[1]},
        "aliases":{"high_priority":[],"low_priority":[]},
        "dates_of_birth":[f"19{rng.randint(50,99)}-01-01"],"gender":"M",
        "countries":{"citizenship":[rng.choice(CO)]},"_list_type":"WATCHLIST_DGT"})
index={}
for e in ents:
    for k in generate_blocking_keys(e,cfg): index.setdefault(k,[]).append(e)

N=4000
clients=[]
for i in range(N):
    if i%4==0:
        e=rng.choice(ents); p=e["primary_name"].split()
        clients.append({"client_id":f"C{i}","client_type":"PP","client_first_name":p[0],
            "client_last_name":p[1],"client_dob":e["dates_of_birth"][0],"client_gender":"M",
            "client_countries":{"nationality":e["countries"]["citizenship"]}})
    else:
        n=f"{rng.choice(FIRST)} {rng.choice(LAST)}"; p=n.split()
        clients.append({"client_id":f"C{i}","client_type":"PP","client_first_name":p[0],
            "client_last_name":p[1],"client_dob":"1970-01-01","client_gender":"M",
            "client_countries":{"nationality":[rng.choice(CO)]}})

def sequential():
    hits=[]
    for i,c in enumerate(clients):
        cand={}
        for k in lookup_blocking_keys(c,cfg):
            for e in index.get(k,[]): cand[e["entity_id"]]=e
        if not cand: continue
        best=None
        for e in cand.values():
            s=match_entities(c,e,config); s["watchlist_entity"]=e
            if best is None or s["final_score"]>best["final_score"]: best=s
        if best and best.get("status")=="ALERT": hits.append((i,best))
    return hits

t=time.perf_counter(); hs=sequential(); tseq=time.perf_counter()-t
p=screenpool.resolve_processes(len(clients)+len(ents))
t=time.perf_counter(); hp=screenpool.parallel_match(clients,index,cfg,processes=p); tpar=time.perf_counter()-t

print(f"clients={N}  entités changées={len(ents)}  processus={p}")
print(f"séquentiel : {tseq:.2f}s   parallèle : {tpar:.2f}s   gain = {tseq/tpar:.2f}x")
print(f"hits identiques : {[h[0] for h in hs]==[h[0] for h in hp]}  ({len(hs)} alertes)")
scores_ok = all(abs(a[1]['final_score']-b[1]['final_score'])<1e-9 for a,b in zip(hs,hp))
ents_ok = all(a[1]['watchlist_entity']['entity_id']==b[1]['watchlist_entity']['entity_id'] for a,b in zip(hs,hp))
print(f"scores identiques : {scores_ok} | fiches listées identiques : {ents_ok}")
