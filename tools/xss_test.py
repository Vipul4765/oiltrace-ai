"""Cross-site-scripting regression test.

    .venv/bin/uvicorn backend.main:app --port 8000 &
    .venv/bin/python tools/xss_test.py

Vessel names, call signs and destinations arrive over AIS, which is an open
radio broadcast: anyone with a transmitter can put any string in those fields,
and AIS spoofing is documented in the wild. Those strings reach innerHTML in
about forty places.

This drives each renderer directly with a hostile payload and asserts the
browser treats it as text. Before esc() existed, a vessel named
`<img src=x onerror=...>` executed its payload in the live dashboard.

Exits non-zero if any renderer is vulnerable.
"""
import asyncio, json, subprocess, sys, urllib.request, websockets
PORT=9338
proc=subprocess.Popen(["google-chrome","--headless=new","--disable-gpu","--no-sandbox",
  f"--remote-debugging-port={PORT}","--window-size=1400,900","about:blank"],
  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
PAYLOAD = '<img src=x onerror="window.__HIT=(window.__HIT||0)+1">'
async def main():
    for _ in range(60):
        try:
            tabs=json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
            ws=next(t["webSocketDebuggerUrl"] for t in tabs if t["type"]=="page"); break
        except Exception: await asyncio.sleep(0.4)
    i=[0]
    async with websockets.connect(ws,max_size=60_000_000) as c:
        async def cmd(m,p=None):
            i[0]+=1; await c.send(json.dumps({"id":i[0],"method":m,"params":p or {}}))
            while True:
                r=json.loads(await c.recv())
                if r.get("id")==i[0]: return r.get("result",{})
        async def js(e):
            r=await cmd("Runtime.evaluate",{"expression":e,"awaitPromise":True,"returnByValue":True})
            res=r.get("result",{})
            if r.get("exceptionDetails"): return "EXCEPTION: "+str(r["exceptionDetails"].get("text"))
            return res.get("value")
        await cmd("Page.enable"); await cmd("Runtime.enable")
        await cmd("Page.navigate",{"url":"http://127.0.0.1:8000/"})
        await asyncio.sleep(6)

        P = json.dumps(PAYLOAD)
        tests = {
          "esc() itself": f"esc({P}).includes('<img')===false",
          "renderCandidates (vessel name)":
            f"(()=>{{S.candidates=[{{mmsi:1,name:{P},imo:{P},vessel_type:{P},rank:1,score:0.5,components:{{}},evidence:[{P}],track:[]}}];"
            f"renderCandidates();return document.querySelectorAll('#candidates img').length===0}})()",
          "renderWhy (evidence text)":
            f"(()=>{{S.selected={{mmsi:1,name:{P},vessel_type:{P},score:0.5,components:{{spatial:1}},evidence:[{P}]}};"
            f"renderWhy();return document.querySelectorAll('#why img').length===0}})()",
          "renderTimeline (stage/detail)":
            f"(()=>{{S.timeline=[{{ts:1700000000,stage:{P},detail:{P}}}];"
            f"renderTimeline();return document.querySelectorAll('#timeline img').length===0}})()",
          "renderAlerts (message)":
            f"(()=>{{renderAlerts([{{ts:1700000000,kind:'x',message:{P}}}]);"
            f"return document.querySelectorAll('#alerts img').length===0}})()",
          "toast (title + body)":
            f"(()=>{{toast({P},'',{P},0);return document.querySelectorAll('#toasts img').length===0}})()",
          "renderJob (stage labels)":
            f"(()=>{{renderJob({{percent:50,detail:{P},label:{P},stages:[{{key:'a',label:{P},state:'active'}}]}});"
            f"return document.querySelectorAll('#job-stages img, #job img').length===0}})()",
          "renderProvenance (source/caveat)":
            f"(()=>{{renderProvenance({{region:{{name:{P},key:'x'}},fully_real:false,caveat:{P},"
            f"layers:{{ais:{{real:false,source:{P},positions:0}},sar_imagery:{{real:true,source:{P},real_incidents:0}},"
            f"environment:{{real:true,source:{P}}},drift_model:{{real:true,source:{P}}},attribution:{{real:true,source:{P}}}}}}});"
            f"return document.querySelectorAll('#banner-slot img').length===0}})()",
          "renderIncident (scene id / region)":
            f"(()=>{{S.incident={{incident_id:{P},scene_id:{P},region_name:{P},status:'x',detected_at:1700000000,"
            f"lat:1,lon:1,area_km2:1,length_km:1,width_km:1,volume_min_m3:1,volume_max_m3:2,confidence:0.5,polygon:[]}};"
            f"renderIncident();return document.querySelectorAll('#incident-body img').length===0}})()",
        }
        allsafe=True
        for name, expr in tests.items():
            r = await js(expr)
            ok = (r is True)
            allsafe = allsafe and ok
            print(f"  {'SAFE  ' if ok else 'UNSAFE'} {name}" + ("" if ok else f"   -> {r}"))
        hits = await js("window.__HIT || 0")
        print(f"\n  payload executions across all renderers: {hits}")
        print(f"  overall: {'ALL RENDERERS ESCAPE' if allsafe and not hits else 'STILL VULNERABLE'}")
        globals()["FAILED"] = (not allsafe) or bool(hits)
asyncio.run(main())
proc.terminate()
sys.exit(1 if globals().get("FAILED") else 0)
