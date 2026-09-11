"""Deep integrity audit. Run against a live server:

    .venv/bin/uvicorn backend.main:app --port 8000 &
    .venv/bin/python tools/audit.py

Checks database integrity, that every incident sits inside its stamped region,
that the API is self-consistent across all regions, that incident details hold
together, and that the reports page agrees with the raw tables. Exits non-zero
if anything is wrong.
"""
import sys, json, math, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import db, geo, config, regions
db.init_db()
BASE="http://127.0.0.1:8000"
ISSUES=[]
def bad(sec,msg): ISSUES.append(f"[{sec}] {msg}"); print(f"  ISSUE  {msg}")
def ok(msg): print(f"  ok     {msg}")
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=90) as r: return json.load(r)

print("=== 2. DATABASE INTEGRITY ===")
q=db.query_one
n=q("SELECT COUNT(*) c FROM incidents")["c"]
for tbl,col in [("candidates","incident_id"),("incident_timeline","incident_id"),
                ("drift_runs","incident_id")]:
    orph=q(f"SELECT COUNT(*) c FROM {tbl} WHERE {col} NOT IN (SELECT incident_id FROM incidents)")["c"]
    (ok if orph==0 else bad)(f"{tbl}: {orph} orphan rows") if orph==0 else bad("db",f"{tbl} has {orph} orphan rows")
orph=q("SELECT COUNT(*) c FROM alerts WHERE incident_id IS NOT NULL AND incident_id NOT IN (SELECT incident_id FROM incidents)")["c"]
ok("alerts: no orphans") if orph==0 else bad("db",f"alerts has {orph} orphan rows")
bad_c=q("SELECT COUNT(*) c FROM incidents WHERE lat NOT BETWEEN -90 AND 90 OR lon NOT BETWEEN -180 AND 180")["c"]
ok("incident coordinates in range") if bad_c==0 else bad("db",f"{bad_c} incidents with impossible coords")
bad_p=q("SELECT COUNT(*) c FROM ais_positions WHERE lat NOT BETWEEN -90 AND 90 OR lon NOT BETWEEN -180 AND 180")["c"]
ok("AIS coordinates in range") if bad_p==0 else bad("db",f"{bad_p} AIS rows with impossible coords")
nulls=q("SELECT COUNT(*) c FROM incidents WHERE region IS NULL OR scene_id IS NULL OR created_at IS NULL")["c"]
ok("incidents fully populated") if nulls==0 else bad("db",f"{nulls} incidents missing region/scene/created_at")
fut=q("SELECT COUNT(*) c FROM incidents WHERE detected_at > ?",(time.time()+3600,))["c"]
ok("no incidents dated in the future") if fut==0 else bad("db",f"{fut} incidents in the future")
futa=q("SELECT COUNT(*) c FROM ais_positions WHERE ts > ?",(time.time()+3600,))["c"]
ok("no AIS fixes in the future") if futa==0 else bad("db",f"{futa} AIS fixes in the future")
conf=q("SELECT COUNT(*) c FROM incidents WHERE confidence NOT BETWEEN 0 AND 1")["c"]
ok("confidences within 0..1") if conf==0 else bad("db",f"{conf} incidents with confidence outside 0..1")
sc=q("SELECT COUNT(*) c FROM candidates WHERE score NOT BETWEEN 0 AND 1")["c"]
ok("candidate scores within 0..1") if sc==0 else bad("db",f"{sc} candidates with score outside 0..1")
rk=q("SELECT COUNT(*) c FROM (SELECT incident_id, rank, COUNT(*) n FROM candidates GROUP BY incident_id, rank HAVING n>1)")["c"]
ok("candidate ranks unique per incident") if rk==0 else bad("db",f"{rk} duplicated ranks")
vol=q("SELECT COUNT(*) c FROM incidents WHERE volume_min_m3 > volume_max_m3")["c"]
ok("volume min <= max") if vol==0 else bad("db",f"{vol} incidents with inverted volume range")
poly=0
for r in db.query("SELECT incident_id, polygon FROM incidents"):
    try:
        ring=json.loads(r["polygon"])
        if not isinstance(ring,list) or len(ring)<3: poly+=1
    except Exception: poly+=1
ok("all incident polygons valid") if poly==0 else bad("db",f"{poly} invalid polygons")
src=q("SELECT COUNT(*) c FROM ais_positions WHERE source IS NULL")["c"]
ok("all AIS rows tagged with a source") if src==0 else bad("db",f"{src} AIS rows with NULL source")

print("\n=== 3. INCIDENT LOCATION vs ITS REGION ===")
mis=0
for r in db.query("SELECT incident_id, region, lat, lon FROM incidents"):
    reg=regions.get(r["region"])
    if not (reg.lat_min<=r["lat"]<=reg.lat_max and reg.lon_min<=r["lon"]<=reg.lon_max):
        mis+=1; bad("region",f"{r['incident_id']} at {r['lat']:.2f},{r['lon']:.2f} is outside {r['region']}")
if mis==0: ok("every incident lies inside its stamped region")

print("\n=== 4. API CROSS-CONSISTENCY (per region) ===")
for key in regions.keys():
    urllib.request.urlopen(urllib.request.Request(BASE+f"/api/regions/{key}",method="POST"),timeout=60).read()
    st=get("/api/stats"); inc=get("/api/incidents?limit=500")
    ves=get("/api/vessels/live?minutes=1440"); prov=get("/api/provenance")
    al=get("/api/alerts?limit=100")
    if st["total_incidents"]!=len(inc):
        bad("api",f"{key}: stats={st['total_incidents']} but list={len(inc)}")
    if st["region"]!=key: bad("api",f"{key}: stats reports region {st['region']}")
    reg=regions.get(key)
    outside=[v for v in ves if not (reg.lat_min<=v["lat"]<=reg.lat_max and reg.lon_min<=v["lon"]<=reg.lon_max)]
    if outside: bad("api",f"{key}: {len(outside)} live vessels outside the AOI")
    wrong=[i for i in inc if i.get("region") not in (key,None)]
    if wrong: bad("api",f"{key}: {len(wrong)} incidents from another region in the list")
    badal=[a for a in al if a.get("incident_id") and a["incident_id"] not in {i["incident_id"] for i in inc}]
    if badal: bad("api",f"{key}: {len(badal)} alerts referencing incidents outside this region")
    if prov["region"]["key"]!=key: bad("api",f"{key}: provenance region mismatch")
    print(f"  {key:<16} incidents={len(inc):<3} vessels={len(ves):<5} alerts={len(al):<3} fully_real={prov['fully_real']}")

print("\n=== 5. INCIDENT DETAIL INTEGRITY ===")
urllib.request.urlopen(urllib.request.Request(BASE+"/api/regions/mumbai",method="POST"),timeout=60).read()
allinc=get("/api/incidents?limit=500&region=all")
checked=0
for i in allinc[:12]:
    d=get(f"/api/incidents/{i['incident_id']}")
    checked+=1
    if d["incident"]["region"] and d["incident"].get("region_name") is None:
        bad("detail",f"{i['incident_id']}: region set but region_name missing")
    ring=d["incident"]["polygon"]
    if len(ring)<3: bad("detail",f"{i['incident_id']}: polygon has {len(ring)} points")
    else:
        la,lo=geo.ring_centroid(ring)
        if geo.haversine_km(la,lo,d["incident"]["lat"],d["incident"]["lon"])>10:
            bad("detail",f"{i['incident_id']}: polygon centroid far from stored lat/lon")
    for c in d["candidates"]:
        s=sum(config.SCORE_WEIGHTS[k]*v for k,v in c["components"].items())
        if abs(s-c["score"])>1e-3:
            bad("detail",f"{i['incident_id']}/{c['mmsi']}: score {c['score']} != weighted sum {s:.4f}")
    ranks=[c["rank"] for c in d["candidates"]]
    if ranks!=sorted(ranks): bad("detail",f"{i['incident_id']}: candidates not rank-ordered")
    ts=[t["ts"] for t in d["timeline"]]
    if ts!=sorted(ts): bad("detail",f"{i['incident_id']}: timeline out of order")
    for t in d["timeline"]:
        if t["ts"]>time.time()+3600: bad("detail",f"{i['incident_id']}: timeline stage in the future")
ok(f"checked {checked} incident details")

print("\n=== 6. REPORTS vs RAW DB (global scope) ===")
rep=get("/api/reports/summary?days=3650&region=all")  # compare like with like
tot_rep=sum(x["n"] for x in rep["by_region"])
tot_db=q("SELECT COUNT(*) c FROM incidents")["c"]
ok(f"reports total {tot_rep} == db {tot_db}") if tot_rep==tot_db else bad("reports",f"reports {tot_rep} != db {tot_db}")
st_rep=sum(x["n"] for x in rep["by_status"])
ok("status buckets sum to total") if st_rep==tot_db else bad("reports",f"status sum {st_rep} != {tot_db}")
cb=sum(x["n"] for x in rep["confidence_bands"])
ok("confidence bands sum to total") if cb==tot_db else bad("reports",f"confidence sum {cb} != {tot_db}")
ais_db=q("SELECT COUNT(*) c FROM ais_positions")["c"]
ok("AIS holdings match") if rep["ais"]["positions"]==ais_db else bad("reports",f"reports AIS {rep['ais']['positions']} != db {ais_db}")

print("\n=== 6b. REGION-SCOPED REPORT vs REGION-SCOPED LIST ===")
for key in regions.keys():
    urllib.request.urlopen(urllib.request.Request(BASE+f"/api/regions/{key}",method="POST"),timeout=60).read()
    rr=get("/api/reports/summary?days=3650"); ii=get("/api/incidents?limit=500")
    n=sum(x["n"] for x in rr["by_region"])
    if n!=len(ii): bad("reports",f"{key}: report {n} != incident list {len(ii)}")
    if rr["region"]!=key: bad("reports",f"{key}: report scope says {rr['region']}")
    stray=[x["region"] for x in rr["by_region"] if x["region"]!=key]
    if stray: bad("reports",f"{key}: report contains other regions {stray}")
else: ok("every region-scoped report matches its incident list")

print(f"\n{'='*52}\n{len(ISSUES)} ISSUES FOUND")
for i in ISSUES: print("  -",i)

sys.exit(1 if ISSUES else 0)
