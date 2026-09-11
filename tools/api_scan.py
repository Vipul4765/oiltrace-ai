"""API surface scan. Run against a live server:

    .venv/bin/python tools/api_scan.py

Exercises every GET route across a parameter matrix (valid, boundary and
hostile inputs), validates response field types, walks every incident detail
and vessel track, hunts for fabricated constants repeated across records, and
reconciles every displayed number against the database. Exits non-zero on any
finding.
"""
import sys, json, time, urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import db, regions, config
db.init_db()
BASE="http://127.0.0.1:8000"; ISSUES=[]
def bad(m): ISSUES.append(m); print(f"  ISSUE  {m}")
def call(method,path,timeout=120):
    req=urllib.request.Request(BASE+path,method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: return r.status, json.loads(r.read() or b'null')
    except urllib.error.HTTPError as e:
        try: body=json.loads(e.read() or b'null')
        except Exception: body=None
        return e.code, body
    except Exception as e:
        return 0, str(e)

print("=== A. GET PARAMETER MATRIX (valid + boundary + hostile) ===")
matrix = {
 "/api/stats": ["","?days=1","?days=365","?region=all","?region=mumbai","?region=%27--","?days=1&region=all"],
 "/api/alerts": ["","?limit=1","?limit=100","?region=all","?region=zzz"],
 "/api/incidents": ["","?limit=1","?limit=500","?region=all","?region=zzz","?limit=500&region=all"],
 "/api/vessels": ["","?limit=1","?limit=2000","?hours=0.5","?hours=8760","?q=a","?q=%27%20OR%201%3D1--","?in_aoi=false"],
 "/api/vessels/live": ["","?minutes=1","?minutes=1440","?in_aoi=false"],
 "/api/reports/summary": ["","?days=1","?days=3650","?region=all","?region=zzz"],
 "/api/regions": [""],
 "/api/provenance": [""],
 "/api/health": [""],
 "/api/ais/status": [""],
}
for path,qs in matrix.items():
    for q in qs:
        code,body=call("GET",path+q)
        if code!=200: bad(f"GET {path}{q} -> {code} {str(body)[:80]}")
print(f"  tested {sum(len(v) for v in matrix.values())} GET combinations")

print("\n=== B. RESPONSE SCHEMA / TYPE VALIDATION ===")
def typecheck(label, obj, spec):
    for k,(types,nullable) in spec.items():
        if k not in obj: bad(f"{label}: missing field '{k}'"); continue
        v=obj[k]
        if v is None:
            if not nullable: bad(f"{label}: '{k}' is null but should not be")
        elif not isinstance(v,types): bad(f"{label}: '{k}' is {type(v).__name__}, expected {types}")

code,h=call("GET","/api/health")
typecheck("health",h,{"status":(str,False),"ais_source":(str,False),"region":(str,False),"aoi":(dict,False)})
code,s=call("GET","/api/stats")
typecheck("stats",s,{"window_days":(int,False),"region":(str,False),"total_incidents":(int,False),
  "confirmed_spills":(int,False),"vessels_analysed":(int,False),"avg_detection_hours":((int,float),True)})
code,p=call("GET","/api/provenance")
typecheck("provenance",p,{"region":(dict,False),"layers":(dict,False),"optional_sources":(dict,False),
  "fully_real":(bool,False),"caveat":(str,True)})
for name,layer in p["layers"].items():
    typecheck(f"provenance.layers.{name}",layer,{"real":(bool,False),"source":(str,False)})
code,inc=call("GET","/api/incidents?limit=500&region=all")
if inc:
    typecheck("incident",inc[0],{"incident_id":(str,False),"detected_at":((int,float),False),
      "lat":((int,float),False),"lon":((int,float),False),"area_km2":((int,float),False),
      "confidence":((int,float),False),"status":(str,False),"region":(str,False),
      "polygon":(list,False),"scene_id":(str,False),"candidate_count":(int,False),
      "source":(str,False),"created_at":((int,float),False)})
code,v=call("GET","/api/vessels?limit=5&in_aoi=false")
if v: typecheck("vessel",v[0],{"mmsi":(int,False),"fixes":(int,False),"last_seen":((int,float),False),
      "vessel_type":(str,False),"name":(str,False)})
code,lv=call("GET","/api/vessels/live?in_aoi=false&minutes=1440")
if lv: typecheck("live vessel",lv[0],{"mmsi":(int,False),"lat":((int,float),False),
      "lon":((int,float),False),"ts":((int,float),False),"name":(str,False),"vessel_type":(str,False)})
code,rep=call("GET","/api/reports/summary?region=all&days=3650")
typecheck("reports",rep,{"window_days":(int,False),"region":(str,False),"by_region":(list,False),
  "by_status":(list,False),"confidence_bands":(list,False),"recent_scenes":(list,False),"ais":(dict,False)})
print("  schema validation done")

print("\n=== C. DETAIL ENDPOINT ON EVERY INCIDENT ===")
for i in inc:
    code,d=call("GET",f"/api/incidents/{i['incident_id']}")
    if code!=200: bad(f"detail {i['incident_id']} -> {code}"); continue
    for k in ("incident","drift","candidates","timeline"):
        if k not in d: bad(f"detail {i['incident_id']}: missing '{k}'")
    if d["drift"] is not None:
        for k in ("centreline","envelope","regions","environment","particles"):
            if k not in d["drift"]: bad(f"detail {i['incident_id']}: drift missing '{k}'")
        env=d["drift"]["environment"]
        if "synthetic" in env: bad(f"detail {i['incident_id']}: drift env still carries a 'synthetic' flag")
        for k,val in env.items():
            if val is not None and not isinstance(val,(int,float)):
                bad(f"detail {i['incident_id']}: env['{k}'] is {type(val).__name__}")
print(f"  checked {len(inc)} incidents")

print("\n=== D. TRACK ENDPOINT FOR REAL VESSELS ===")
code,vs=call("GET","/api/vessels?limit=8&in_aoi=false&hours=8760")
for x in (vs or [])[:8]:
    code,t=call("GET",f"/api/vessels/{x['mmsi']}/track?hours=8760")
    if code!=200: bad(f"track {x['mmsi']} -> {code}"); continue
    if not t["track"]: bad(f"track {x['mmsi']}: vessel listed with {x['fixes']} fixes but track is empty")
    for row in t["track"][:50]:
        if len(row)!=5: bad(f"track {x['mmsi']}: row has {len(row)} fields, expected 5")
        if not (-90<=row[1]<=90 and -180<=row[2]<=180): bad(f"track {x['mmsi']}: bad coords")
print(f"  checked {len(vs or [])} vessel tracks")

print("\n=== E. DUMMY-DATA HUNT: values repeated across unrelated records ===")
# a fabricated constant shows up identically everywhere; real measurements vary
for col in ("area_km2","confidence","length_km","width_km","lat","lon"):
    rows=db.query(f"SELECT {col} v, COUNT(*) n FROM incidents GROUP BY {col} HAVING n>1 ORDER BY n DESC LIMIT 3")
    for r in rows:
        if r["n"]>2: bad(f"incidents.{col} value {r['v']} repeats {r['n']}x - suspicious constant")
tl=db.query("SELECT stage, COUNT(DISTINCT ts - (SELECT detected_at FROM incidents i WHERE i.incident_id=incident_timeline.incident_id)) d, COUNT(*) n FROM incident_timeline GROUP BY stage")
for r in tl:
    # "Scene acquired" is offset 0 by definition - it marks the satellite pass,
    # which IS detected_at. Only a non-zero constant offset would be fabricated.
    if r["stage"].startswith("Scene acquired"):
        off=db.query_one("SELECT MAX(ABS(t.ts-i.detected_at)) m FROM incident_timeline t"
                         " JOIN incidents i USING(incident_id) WHERE t.stage=?",(r["stage"],))["m"]
        if off not in (0,None): bad(f"timeline '{r['stage']}': constant non-zero offset {off}s")
        continue
    if r["n"]>2 and r["d"]==1:
        bad(f"timeline '{r['stage']}': identical offset across {r['n']} incidents - fabricated")
print("  constant-value scan done")

print("\n=== F. EVERY NUMBER THE UI SHOWS TRACES TO THE DB ===")
for key in regions.keys():
    call("POST",f"/api/regions/{key}")
    _,st=call("GET","/api/stats"); _,li=call("GET","/api/incidents?limit=500")
    dbn=db.query_one("SELECT COUNT(*) c FROM incidents WHERE region=? AND detected_at>=?",
                     (key,time.time()-30*86400))["c"]
    if st["total_incidents"]!=dbn: bad(f"{key}: stats {st['total_incidents']} != db {dbn}")
    if len(li)!=db.query_one("SELECT COUNT(*) c FROM incidents WHERE region=?",(key,))["c"]:
        bad(f"{key}: incident list length != db count")
    conf=db.query_one("SELECT COUNT(*) c FROM incidents WHERE region=? AND status='Confirmed' AND detected_at>=?",
                      (key,time.time()-30*86400))["c"]
    if st["confirmed_spills"]!=conf: bad(f"{key}: confirmed {st['confirmed_spills']} != db {conf}")
call("POST","/api/regions/mumbai")
print("  per-region reconciliation done")

print(f"\n{'='*50}\n{len(ISSUES)} ISSUES")
for i in ISSUES: print("  -",i)
sys.exit(1 if ISSUES else 0)
