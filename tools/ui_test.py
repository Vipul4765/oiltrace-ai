"""Headless UI interaction test.

    .venv/bin/uvicorn backend.main:app --port 8000 &
    .venv/bin/python tools/ui_test.py

Drives the real page through Chrome DevTools Protocol: walks every view, clicks
Fetch Sentinel-1, watches the live job progress panel, checks the result toast,
and reports any JavaScript console error. Screenshots land in the scratch dir.
"""
import asyncio, base64, json, sys, subprocess, time, urllib.request
import websockets

PORT = 9333
proc = subprocess.Popen(["google-chrome","--headless=new","--disable-gpu","--no-sandbox",
    f"--remote-debugging-port={PORT}","--window-size=1500,950","--hide-scrollbars",
    "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

async def main():
    for _ in range(60):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
            ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
            break
        except Exception:
            await asyncio.sleep(0.4)
    else:
        print("could not attach"); return

    i = [0]
    async with websockets.connect(ws_url, max_size=60_000_000) as ws:
        async def cmd(method, params=None):
            i[0] += 1
            await ws.send(json.dumps({"id": i[0], "method": method, "params": params or {}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == i[0]:
                    return m.get("result", {})

        errors = []
        await cmd("Runtime.enable")
        await cmd("Page.enable")
        await cmd("Log.enable")

        async def pump():
            while True:
                m = json.loads(await ws.recv())
                meth = m.get("method")
                if meth == "Runtime.exceptionThrown":
                    errors.append(str(m["params"]["exceptionDetails"].get("text")))
                elif meth == "Log.entryAdded" and m["params"]["entry"]["level"] == "error":
                    errors.append(m["params"]["entry"]["text"])
        task = asyncio.create_task(pump())

        await cmd("Page.navigate", {"url": "http://127.0.0.1:8000/"})
        await asyncio.sleep(6)

        async def js(expr):
            r = await cmd("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                               "returnByValue": True})
            return r.get("result", {}).get("value")

        async def shot(name):
            r = await cmd("Page.captureScreenshot", {"format": "png"})
            path = f"/tmp/claude-1000/-home-vipul/98824a47-eee7-417e-af21-32b6f2ce6db0/scratchpad/{name}.png"
            open(path, "wb").write(base64.b64decode(r["data"]))
            return path

        print("  clicking through every view first …")
        for v in ("incidents","vessels","map","reports","settings","dashboard"):
            await js(f"document.querySelector('nav a[data-view=\"{v}\"]').click(); true")
            await asyncio.sleep(1.1)
        print(f"    console after navigation: {errors or 'none'}")

        print("  clicking Fetch Sentinel-1 …")
        await js("document.getElementById('btn-sentinel').click(); true")
        await asyncio.sleep(3.5)
        vis = await js("document.getElementById('job').classList.contains('in')")
        pct = await js("document.getElementById('job-bar').style.width")
        lbl = await js("document.getElementById('job-title').textContent + ' | ' + document.getElementById('job-sub').textContent")
        stg = await js("document.getElementById('job-stages').innerText")
        print(f"  progress panel visible: {vis}   bar={pct}")
        print(f"  {lbl}")
        for line in (stg or "").splitlines(): print(f"    {line}")
        await shot("ux-progress")

        for _ in range(40):
            if not await js("document.getElementById('btn-sentinel').disabled"): break
            await asyncio.sleep(1)
        await asyncio.sleep(1.2)
        t = await js("document.querySelectorAll('#toasts .toast').length")
        ttext = await js("(document.querySelector('#toasts .toast')||{}).innerText || ''")
        print(f"  toasts shown: {t}")
        print(f"  toast text: {(ttext or '').strip()[:150]}")
        await shot("ux-toast")

        print("  clicking Refresh …")
        await js("document.getElementById('btn-refresh').click(); true")
        await asyncio.sleep(0.4)
        rb = await js("document.getElementById('btn-refresh').innerHTML")
        print(f"  refresh button shows spinner: {'spin' in (rb or '')}")
        await asyncio.sleep(4)
        restored = await js("document.getElementById('btn-refresh').textContent.trim()")
        print(f"  refresh restored to: {restored!r}")

        task.cancel()
        print(f"  console errors: {errors or 'none'}")

try:
    asyncio.run(main())
finally:
    proc.terminate()
