/* OilTrace AI - dashboard front end */
"use strict";

const S = { incident:null, drift:null, candidates:[], timeline:[], selected:null,
            aoi:null, vesselTimer:null, health:null, regions:[], prov:null,
            view:"dashboard", report:null, pendingBounds:null,
            noCandReason:null };

const $  = (id) => document.getElementById(id);
const fmt = (n, d=1) => (n===null||n===undefined||isNaN(n)) ? "--" : Number(n).toFixed(d);
const TYPE_COLOR = { Tanker:"#dc2626", Cargo:"#2563eb", Fishing:"#059669",
                     Tug:"#7c3aed", Passenger:"#0891b2" };
const typeColor = (t) => TYPE_COLOR[t] || "#64748b";

function utc(ts, withDate=true){
  if(!ts) return "--";
  const d = new Date(ts*1000);
  const p = (n)=>String(n).padStart(2,"0");
  const t = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`;
  if(!withDate) return t;
  const M = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return `${d.getUTCDate()} ${M[d.getUTCMonth()]} ${d.getUTCFullYear()}, ${t}`;
}
function ago(ts){
  const s = Date.now()/1000 - ts;
  if(s < 90) return "just now";
  if(s < 5400) return `${Math.round(s/60)} min ago`;
  if(s < 172800) return `${Math.round(s/3600)} h ago`;
  return `${Math.round(s/86400)} d ago`;
}
async function api(path, opts){
  const r = await fetch(path, opts);
  if(!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}


/* ---------------- toasts ---------------- */
function toast(msg, kind = "", title = "", ms = 6000){
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = `<span class="x">&times;</span>${title?`<b>${title}</b>`:""}${msg}`;
  $("toasts").appendChild(el);
  requestAnimationFrame(() => el.classList.add("in"));
  const kill = () => {
    el.classList.remove("in"); el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  };
  el.querySelector(".x").onclick = kill;
  if(ms) setTimeout(kill, ms);
  return kill;
}

/* ---------------- job progress ---------------- */
const MARK = { done:"&#10003;", active:"&#9679;", pending:"&#9675;" };

function showJob(title){
  $("job-title").innerHTML = title;
  $("job-sub").textContent = "";
  $("job-bar").style.width = "0%";
  $("job-stages").innerHTML = "";
  $("job").classList.add("in");
}
function renderJob(j){
  $("job-sub").textContent = j.detail || j.label || "";
  $("job-bar").style.width = j.percent + "%";
  $("job-stages").innerHTML = (j.stages || []).map(st =>
    `<div class="st ${st.state}"><span class="m">${MARK[st.state]}</span>${st.label}</div>`
  ).join("");
}
function hideJob(delay = 900){
  setTimeout(() => $("job").classList.remove("in"), delay);
}

/** Poll a real backend job. No invented progress: the bar only moves when the
 *  server reports a stage that actually started or finished. */
async function runJob(startPath, title){
  const { job_id } = await api(startPath, { method:"POST" });
  showJob(title);
  for(;;){
    await new Promise(r => setTimeout(r, 700));
    let j;
    try{ j = await api(`/api/jobs/${job_id}`); }
    catch(e){ hideJob(0); throw e; }
    renderJob(j);
    if(j.status === "done"){ hideJob(); return j.result; }
    if(j.status === "error"){ hideJob(0); throw new Error(j.error); }
  }
}

/* ---------------- button + panel helpers ---------------- */
function busy(btn, label){
  btn.dataset.label = btn.dataset.label || btn.textContent;
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span>${label}`;
}
function idle(btn){
  btn.disabled = false;
  btn.textContent = btn.dataset.label || btn.textContent;
}
function fading(id, fn){
  const el = $(id); if(!el) return fn();
  el.classList.add("swap","loading");
  return Promise.resolve(fn()).finally(() =>
    requestAnimationFrame(() => el.classList.remove("loading")));
}

/* ---------------- map ---------------- */
let map, layers = {};
function initMap(){
  map = L.map("map", { zoomControl:false, attributionControl:true })
         .setView([18.97, 72.83], 9);
  L.control.zoom({ position:"topright" }).addTo(map);
  layers.base = {
    map: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom:19, attribution:"&copy; OpenStreetMap contributors" }),
    sat: L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      { maxZoom:19, attribution:"Imagery &copy; Esri, Maxar, Earthstar Geographics" })
  };
  layers.base.map.addTo(map);
  layers.drift   = L.layerGroup().addTo(map);
  layers.spill   = L.layerGroup().addTo(map);
  layers.vessels = L.layerGroup().addTo(map);
  layers.track   = L.layerGroup().addTo(map);

  $("t-map").onclick = () => swapBase("map");
  $("t-sat").onclick = () => swapBase("sat");
}
function swapBase(which){
  Object.values(layers.base).forEach(l => map.removeLayer(l));
  layers.base[which].addTo(map);
  $("t-map").classList.toggle("on", which==="map");
  $("t-sat").classList.toggle("on", which==="sat");
}
const toLatLngs = (ring) => ring.map(p => [p[1], p[0]]);   // [lon,lat] -> [lat,lon]

function drawIncident(){
  layers.spill.clearLayers(); layers.drift.clearLayers();
  if(!S.incident) return;
  const inc = S.incident;

  if(S.drift){
    if(S.drift.envelope && S.drift.envelope.length > 2){
      L.polygon(toLatLngs(S.drift.envelope), { color:"#fb923c", weight:1.5,
        opacity:.85, fillColor:"#fb923c", fillOpacity:.13, dashArray:"5,4" })
        .bindTooltip("Back-drift uncertainty cone").addTo(layers.drift);
    }
    (S.drift.regions||[]).forEach(r => {
      if(!r.ring || r.ring.length < 3) return;
      const strong = r.hours_back >= 6;
      L.polygon(toLatLngs(r.ring), { color:"#a855f7", weight: strong?2:1,
        opacity: strong?.9:.4, fill:strong, fillColor:"#a855f7", fillOpacity:.1 })
        .bindTooltip(`Probable source region, ${r.hours_back} h before detection<br>${r.area_km2} km&sup2;`)
        .addTo(layers.drift);
    });
    if(S.drift.centreline && S.drift.centreline.length > 1){
      L.polyline(toLatLngs(S.drift.centreline), { color:"#7c3aed", weight:2.5,
        opacity:.9, dashArray:"7,5" })
        .bindTooltip("Backward trajectory (mean drift)").addTo(layers.drift);
    }
  }

  if(inc.polygon && inc.polygon.length > 2){
    L.polygon(toLatLngs(inc.polygon), { color:"#dc2626", weight:2,
      fillColor:"#f97316", fillOpacity:.55 })
      .bindTooltip(`${inc.incident_id} &mdash; ${fmt(inc.area_km2)} km&sup2;`)
      .addTo(layers.spill);
  }
  L.marker([inc.lat, inc.lon], { icon: L.divIcon({ className:"", iconSize:[26,26],
      iconAnchor:[13,13], html:
      `<div style="width:26px;height:26px;border-radius:50%;background:#dc2626;
        border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,.45);display:grid;
        place-items:center;color:#fff;font-size:13px">&#9679;</div>` }) })
    .bindPopup(`<b>${inc.incident_id}</b><br>${fmt(inc.lat,3)}&deg;N ${fmt(inc.lon,3)}&deg;E<br>
      ${fmt(inc.area_km2)} km&sup2; &middot; confidence ${Math.round(inc.confidence*100)}%`)
    .addTo(layers.spill);

  const b = L.latLngBounds(toLatLngs(inc.polygon||[]).concat([[inc.lat, inc.lon]]));
  if(S.drift && S.drift.envelope && S.drift.envelope.length) b.extend(toLatLngs(S.drift.envelope));
  moveMap(b, 0.35);
}

function vesselIcon(v, highlight){
  const c = highlight ? "#111827" : typeColor(v.vessel_type);
  const rot = v.cog || v.heading || 0;
  const size = highlight ? 20 : 15;
  return L.divIcon({ className:"vessel-ico", iconSize:[size,size],
    iconAnchor:[size/2,size/2], html:
    `<div style="transform:rotate(${rot}deg);width:${size}px;height:${size}px">
      <svg viewBox="0 0 24 24" width="${size}" height="${size}">
        <path d="M12 2 L19 21 L12 17 L5 21 Z" fill="${c}"
          stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg></div>` });
}

async function refreshVessels(){
  try{
    const list = await api("/api/vessels/live?minutes=180");
    layers.vessels.clearLayers();
    const topMmsi = S.selected ? S.selected.mmsi : null;
    list.forEach(v => {
      L.marker([v.lat, v.lon], { icon: vesselIcon(v, v.mmsi===topMmsi) })
        .bindTooltip(`<b>${v.name}</b><br>${v.vessel_type} &middot; MMSI ${v.mmsi}<br>
          ${fmt(v.sog)} kn &middot; ${fmt(v.cog,0)}&deg;<br>
          <span style="color:#94a3b8">${ago(v.ts)}</span>`)
        .addTo(layers.vessels);
    });
    $("sysais").textContent = `${list.length} vessels tracked`;
    $("nav-ves").textContent = list.length || "";
  }catch(e){ /* keep the last good render */ }
}

function drawTrack(c){
  layers.track.clearLayers();
  if(!c || !c.track || c.track.length < 2) return;
  const pts = c.track.map(p => [p[1], p[2]]);
  L.polyline(pts, { color:"#111827", weight:3, opacity:.9 }).addTo(layers.track);
  L.polyline(pts, { color:"#fbbf24", weight:1.5, opacity:.95, dashArray:"6,4" }).addTo(layers.track);
  L.circleMarker(pts[0], { radius:5, color:"#fff", weight:2, fillColor:"#10b981",
    fillOpacity:1 }).bindTooltip("Track start").addTo(layers.track);
  L.circleMarker(pts[pts.length-1], { radius:5, color:"#fff", weight:2,
    fillColor:"#111827", fillOpacity:1 }).bindTooltip("Track end").addTo(layers.track);
  const closest = c.track.reduce((best,p) =>
    Math.abs(p[0]-c.closest_ts) < Math.abs(best[0]-c.closest_ts) ? p : best, c.track[0]);
  L.circleMarker([closest[1], closest[2]], { radius:8, color:"#dc2626", weight:3,
    fillColor:"#fff", fillOpacity:.9 })
    .bindTooltip(`Closest approach &mdash; ${fmt(c.closest_km)} km, ${utc(c.closest_ts)}`)
    .addTo(layers.track);
}


/** Move the map to `bounds`.
 *
 *  Animating while the map container is hidden (a non-dashboard view) gives it
 *  zero size, and Leaflet's fly maths then produces "Invalid LatLng (NaN, NaN)".
 *  So: animate only when the map is actually on screen, otherwise set the view
 *  instantly and remember it for when the view is shown again.
 */
function moveMap(bounds, pad = 0.3){
  if(!map || !bounds || !bounds.isValid || !bounds.isValid()) return;
  const padded = bounds.pad(pad);
  const el = map.getContainer();
  const visible = el.offsetWidth > 0 && el.offsetHeight > 0;
  S.pendingBounds = padded;
  if(!visible){ try{ map.fitBounds(padded, { animate:false }); }catch(e){} return; }
  map.flyToBounds(padded, { duration:0.7, easeLinearity:0.25 });
}

/* ---------------- renderers ---------------- */
function renderProvenance(p){
  S.prov = p;
  const L = p.layers;
  const cell = (title, layer, extra) => `
    <div class="lyr">
      <div class="t"><span class="led ${layer.real?"on":"off"}"></span>${title}</div>
      <div class="s">${layer.real ? "REAL" : "NO DATA"}</div>
      <div class="n">${layer.source}${extra?" &middot; "+extra:""}</div>
    </div>`;
  const ais = L.ais;
  const aisExtra = ais.positions ? `${ais.positions.toLocaleString()} positions` : "no data";
  $("banner-slot").innerHTML = `
    <div class="prov">
      <div class="prov-head">
        <b>Data provenance &mdash; ${p.region.name}</b>
        <span class="verdict ${p.fully_real?"v-real":"v-mixed"}">
          ${p.fully_real ? "FULLY REAL PIPELINE" : "MIXED &mdash; see AIS"}</span>
      </div>
      <div class="prov-grid">
        ${cell("SAR imagery", L.sar_imagery, `${L.sar_imagery.real_incidents} real incidents`)}
        ${cell("Environment", L.environment)}
        ${cell("Drift model", L.drift_model)}
        ${cell("Attribution", L.attribution)}
        ${cell("AIS tracks", ais, aisExtra)}
      </div>
      ${p.caveat ? `<div class="n" style="margin-top:9px;color:#b45309;font-size:11.5px">
         &#9888; ${p.caveat}</div>` : ""}
    </div>`;
}

function renderRegions(data){
  S.regions = data.regions;
  const sel = $("region-select");
  sel.innerHTML = data.regions.map(r => {
    const badge = {excellent:"AIS ****", good:"AIS ***", sparse:"AIS *", none:"no AIS"}[r.ais_live];
    return `<option value="${r.key}" ${r.key===data.active?"selected":""}>${r.name} — ${badge}</option>`;
  }).join("");
  sel.onchange = async () => {
    sel.disabled = true;
    try{
      await api(`/api/regions/${sel.value}`, { method:"POST" });
      await loadAll();
    }catch(e){ toast(e.message, "err", "Region switch failed"); }
    finally{ sel.disabled = false; }
  };
}

function renderKpis(s){
  const items = [
    ["Total Incidents", s.total_incidents, `Last ${s.window_days} days`, "#eff6ff", "#2563eb",
      '<path d="M2 7c2.5 0 2.5 2 5 2s2.5-2 5-2 2.5 2 5 2 2.5-2 5-2M2 14c2.5 0 2.5 2 5 2s2.5-2 5-2 2.5 2 5 2 2.5-2 5-2"/>'],
    ["Confirmed Spills", s.confirmed_spills, `Last ${s.window_days} days`, "#ecfdf5", "#059669",
      '<path d="M20 6L9 17l-5-5"/>'],
    ["Vessels Analysed", s.vessels_analysed, `Last ${s.window_days} days`, "#eef2ff", "#4f46e5",
      '<path d="M3 17l9 4 9-4M4 12l8-9 8 9-8 4z"/>'],
    ["Acquisition to Result",
      s.avg_detection_hours === null || s.avg_detection_hours === undefined
        ? "--" : `${fmt(s.avg_detection_hours)} hrs`,
      "Satellite pass to ranked output", "#fff7ed", "#ea580c",
      '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>']
  ];
  $("kpis").innerHTML = items.map(([lbl,val,sub,bg,fg,path]) => `
    <div class="card kpi">
      <div class="ico" style="background:${bg}">
        <svg viewBox="0 0 24 24" fill="none" stroke="${fg}" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">${path}</svg></div>
      <div><div class="lbl">${lbl}</div><div class="val">${val}</div>
           <div class="sub">${sub}</div></div>
    </div>`).join("");
}

function renderIncident(){
  const inc = S.incident;
  if(!inc){ $("incident-body").innerHTML = '<div class="empty">No incident yet</div>'; return; }
  const st = $("inc-status");
  st.textContent = inc.status;
  st.className = "pill" + (inc.status === "Confirmed" ? "" : "");
  const kv = (k,v) => `<div class="kv"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  $("incident-body").innerHTML =
    kv("Incident ID", inc.incident_id) +
    kv("Detected On", utc(inc.detected_at)) +
    kv("Location", `${fmt(Math.abs(inc.lat),2)}&deg; ${inc.lat>=0?"N":"S"}, `
       + `${fmt(Math.abs(inc.lon),2)}&deg; ${inc.lon>=0?"E":"W"}`
       + `${inc.region_name ? " ("+inc.region_name+")" : ""}`) +
    `<div class="sec-title">Spill Characteristics</div>` +
    kv("Area", `${fmt(inc.area_km2)} km&sup2;`) +
    kv("Length &times; Width", `${fmt(inc.length_km)} km &times; ${fmt(inc.width_km)} km`) +
    kv("Estimated Volume", `~ ${Math.round(inc.volume_min_m3).toLocaleString()} &ndash; ${Math.round(inc.volume_max_m3).toLocaleString()} m&sup3;
       <div style="font-size:10.5px;color:#94a3b8;font-weight:450;margin-top:2px">
       Thickness is not measurable from SAR &mdash; wide range by design</div>`) +
    `<div class="kv"><div class="k">Detection Confidence</div>
      <div class="v">${Math.round(inc.confidence*100)}%</div>
      <div class="meter"><i style="width:${Math.round(inc.confidence*100)}%"></i></div></div>` +
    kv("Source Scene", `<span style="font-size:11.5px;font-family:ui-monospace,monospace">${inc.scene_id||"&mdash;"}</span>`);
}

function scoreClass(s){ return s >= .75 ? "s-hi" : s >= .5 ? "s-md" : "s-lo"; }

function renderCandidates(){
  const el = $("candidates");
  if(!S.candidates.length){
    $("cand-count").textContent = "";
    el.innerHTML = `<div class="empty" style="text-align:left;padding:18px 4px">
      <div style="font-weight:650;color:#475569;margin-bottom:7px">
        No candidate vessels</div>
      <div style="line-height:1.6">${S.noCandReason
        || "Attribution has not run for this incident yet."}</div>
      <div style="margin-top:11px;color:#cbd5e1">This box stays empty rather than
        showing a guess. A vessel appears here only when real AIS places it in the
        back-drifted source region.</div></div>`;
    return;
  }
  $("cand-count").textContent = `${S.candidates.length} scored`;
  el.innerHTML = S.candidates.map(c => `
    <div class="cand ${S.selected && S.selected.mmsi===c.mmsi ? "sel":""}" data-mmsi="${c.mmsi}">
      <div class="rank">${c.rank}</div>
      <div class="nm"><b>${c.name}</b>
        <span>${c.imo ? "IMO "+c.imo : "MMSI "+c.mmsi} &middot; ${c.vessel_type}</span></div>
      <div class="score ${scoreClass(c.score)}">${Math.round(c.score*100)}%</div>
    </div>`).join("");
  el.querySelectorAll(".cand").forEach(n => n.onclick = () => selectCandidate(+n.dataset.mmsi));
}

function selectCandidate(mmsi){
  S.selected = S.candidates.find(c => c.mmsi === mmsi) || null;
  renderCandidates(); renderWhy(); drawTrack(S.selected); refreshVessels();
}

function renderWhy(){
  const c = S.selected;
  $("why-name").textContent = c ? c.name : "";
  if(!c){ $("why").innerHTML = '<div class="empty">Select a candidate vessel</div>'; return; }
  const w = c.components || {};
  const bars = Object.keys(w).map(k => `
    <div class="bar"><div class="t">${k.replace("_"," ")}</div>
      <div class="g"><i style="width:${Math.round(w[k]*100)}%"></i></div>
      <div class="n">${Math.round(w[k]*100)}</div></div>`).join("");
  $("why").innerHTML =
    `<div style="font-size:12px;color:#64748b;margin:2px 0 9px">
       Overall score <b style="color:#0f172a;font-size:14px">${Math.round(c.score*100)}%</b>
       &middot; ${c.vessel_type}${c.length_m ? " &middot; "+Math.round(c.length_m)+" m LOA" : ""}</div>
     <div class="bars">${bars}</div>
     <div style="margin-top:12px">${(c.evidence||[]).map(e => `<div class="ev">${e}</div>`).join("")}</div>`;
}

function renderTimeline(){
  const el = $("timeline");
  if(!S.timeline.length){ el.innerHTML = '<div class="empty">&mdash;</div>'; return; }
  el.innerHTML = S.timeline.map(t => `
    <div class="step"><b>${t.stage}</b>
      <span>${utc(t.ts)}${t.detail ? " &middot; "+t.detail : ""}</span></div>`).join("");
}

function renderAlerts(list){
  const color = { detection:"#dc2626", ranking:"#2563eb", status:"#10b981" };
  $("alerts").innerHTML = list.length ? list.map(a => `
    <div class="alert"><div class="bullet" style="background:${color[a.kind]||"#f59e0b"}"></div>
      <div style="flex:1"><div>${a.message}</div><time>${utc(a.ts)}</time></div></div>`).join("")
    : '<div class="empty">No alerts</div>';
}

function renderEnv(){
  const e = S.drift && S.drift.environment;
  if(!e){ $("env").innerHTML = '<div class="empty">&mdash;</div>'; return; }
  const compass = (d) => ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW",
    "WSW","W","WNW","NW","NNW"][Math.round(d/22.5)%16];
  const has = (v) => v !== null && v !== undefined && !isNaN(v);
  const rows = [
    ["Wind Speed", has(e.wind_speed_ms)
      ? `${fmt(e.wind_speed_ms*1.94384)} knots (${compass(e.wind_from_deg)})` : "--",
      '<path d="M3 8h11a3 3 0 10-3-3M3 13h15a3 3 0 11-3 3"/>'],
    ["Ocean Current", has(e.current_speed_ms)
      ? `${fmt(e.current_speed_ms,2)} m/s (${compass(e.current_to_deg)})` : "--",
      '<path d="M2 8c3 0 3 2 6 2s3-2 6-2 3 2 6 2M2 15c3 0 3 2 6 2s3-2 6-2 3 2 6 2"/>'],
    ["Wave Height", has(e.wave_height_m) ? `${fmt(e.wave_height_m)} m` : "--",
      '<path d="M2 12c3 0 3 3 6 3s3-3 6-3 3 3 6 3M2 6c3 0 3 3 6 3s3-3 6-3 3 3 6 3"/>'],
    ["Sea Surface Temp.", has(e.sst_c) ? `${fmt(e.sst_c)} &deg;C` : "--",
      '<path d="M14 14V5a2 2 0 10-4 0v9a4 4 0 104 0z"/>']
  ];
  $("env").innerHTML = rows.map(([k,v,p]) => `
    <div class="env"><div class="ico"><svg viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="2" stroke-linecap="round">${p}</svg></div>
      <div><div class="k">${k}</div><div class="v">${v}</div></div></div>`).join("")
    + `<div class="note" style="padding:9px 0 0">Live Open-Meteo marine &amp;
        weather at the spill location. A dash means Open-Meteo had no value &mdash;
        never a substituted one.</div>`;
}

function renderMiniDrift(){
  const svg = $("mini-drift");
  svg.innerHTML = "";
  if(!S.drift || !S.incident){ return; }
  const pts = [];
  (S.drift.envelope||[]).forEach(p => pts.push(p));
  (S.drift.centreline||[]).forEach(p => pts.push(p));
  (S.incident.polygon||[]).forEach(p => pts.push(p));
  if(pts.length < 3) return;

  const W = svg.clientWidth || 300, H = 150, pad = 12;
  const lons = pts.map(p=>p[0]), lats = pts.map(p=>p[1]);
  const lo0 = Math.min(...lons), lo1 = Math.max(...lons);
  const la0 = Math.min(...lats), la1 = Math.max(...lats);
  const sx = (lo1-lo0) || 1e-6, sy = (la1-la0) || 1e-6;
  const k = Math.min((W-2*pad)/sx, (H-2*pad)/sy);
  const ox = (W - sx*k)/2, oy = (H - sy*k)/2;
  const X = (lon) => ox + (lon-lo0)*k;
  const Y = (lat) => H - (oy + (lat-la0)*k);
  const path = (ring, close) => ring.map((p,i) =>
    `${i?"L":"M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("") + (close?"Z":"");
  const ns = "http://www.w3.org/2000/svg";
  const add = (tag, attrs) => { const e = document.createElementNS(ns, tag);
    for(const a in attrs) e.setAttribute(a, attrs[a]); svg.appendChild(e); return e; };

  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if(S.drift.envelope && S.drift.envelope.length>2)
    add("path", { d:path(S.drift.envelope,true), fill:"#dbeafe", stroke:"#93c5fd",
                  "stroke-width":1, "stroke-dasharray":"4,3" });
  (S.drift.regions||[]).filter(r=>r.hours_back>=6).forEach(r =>
    add("path", { d:path(r.ring,true), fill:"#ede9fe", stroke:"#a855f7",
                  "stroke-width":1, opacity:.85 }));
  if(S.drift.centreline && S.drift.centreline.length>1)
    add("path", { d:path(S.drift.centreline,false), fill:"none", stroke:"#7c3aed",
                  "stroke-width":2, "stroke-dasharray":"5,4" });
  if(S.incident.polygon && S.incident.polygon.length>2)
    add("path", { d:path(S.incident.polygon,true), fill:"#fca5a5", stroke:"#dc2626",
                  "stroke-width":1.5, opacity:.95 });
  const c = S.drift.centreline || [];
  if(c.length) add("circle", { cx:X(c[c.length-1][0]), cy:Y(c[c.length-1][1]), r:4,
                               fill:"#7c3aed", stroke:"#fff", "stroke-width":1.5 });
  $("drift-note").innerHTML = `Slick back-drifted ${fmt(S.drift.hours_back,0)} h using
    ${S.drift.particles} particles. Purple = probable source region;
    blue dashed = uncertainty cone.`;
}


/* ---------------- view router ---------------- */
const VIEWS = ["dashboard","incidents","vessels","map","reports","settings"];

function showView(name){
  if(!VIEWS.includes(name)) name = "dashboard";
  S.view = name;
  VIEWS.forEach(v => { const el = $("view-"+v); if(el) el.hidden = (v !== name); });
  document.querySelectorAll("nav a[data-view]").forEach(a =>
    a.classList.toggle("active", a.dataset.view === name));

  // One Leaflet instance, moved between the dashboard panel and the full-page
  // map view. Cheaper and less error-prone than keeping two maps in sync.
  const wrap = document.querySelector(".map-wrap");
  const host = name === "map" ? $("map-host") : $("map-slot");
  if(wrap && host && wrap.parentElement !== host) host.appendChild(wrap);
  if(map) setTimeout(() => {
    map.invalidateSize();
    const el = map.getContainer();
    if(S.pendingBounds && el.offsetWidth > 0 && el.offsetHeight > 0){
      try{ map.fitBounds(S.pendingBounds, { animate:false }); }catch(e){}
    }
  }, 60);

  if(name === "incidents") renderIncidentsTable();
  if(name === "vessels")   renderVesselsTable();
  if(name === "reports")   renderReports();
  if(name === "settings")  renderSettings();
  if(location.hash.slice(1) !== name) history.replaceState(null,"","#"+name);
}

/* ---------------- incidents view ---------------- */
async function renderIncidentsTable(){
  const all = $("inc-all").checked;
  const el = $("incidents-table");
  el.innerHTML = '<div class="empty">Loading…</div>';
  let rows;
  try{ rows = await api(`/api/incidents?limit=200${all?"&region=all":""}`); }
  catch(e){ el.innerHTML = `<div class="empty">Failed: ${e.message}</div>`; return; }
  if(!rows.length){
    el.innerHTML = `<div class="empty">No incidents${all?"":" in this region"}.
      Press <b>Fetch Sentinel-1</b> to analyse a real satellite scene.</div>`;
    return;
  }
  el.innerHTML = `<table class="dt"><thead><tr>
      <th>Incident</th><th>Acquired (UTC)</th><th>Region</th><th>Position</th>
      <th class="num">Area km²</th><th class="num">Conf.</th>
      <th class="num">Vessels</th><th>Top candidate</th><th>Status</th>
    </tr></thead><tbody>${rows.map(r => `
      <tr data-id="${r.incident_id}">
        <td><b>${r.incident_id}</b><div class="mono" style="color:#94a3b8">${(r.scene_id||"").slice(0,30)}</div></td>
        <td>${utc(r.detected_at)}</td>
        <td><span class="btag">${r.region || "—"}</span></td>
        <td class="mono">${fmt(Math.abs(r.lat),3)}${r.lat>=0?"N":"S"} ${fmt(Math.abs(r.lon),3)}${r.lon>=0?"E":"W"}</td>
        <td class="num">${fmt(r.area_km2)}</td>
        <td class="num">${Math.round((r.confidence||0)*100)}%</td>
        <td class="num">${r.candidate_count||0}</td>
        <td>${r.top_vessel ? `${r.top_vessel} <b>${Math.round((r.top_score||0)*100)}%</b>` : "—"}</td>
        <td><span class="btag">${r.status}</span></td>
      </tr>`).join("")}</tbody></table>`;
  el.querySelectorAll("tbody tr").forEach(tr => tr.onclick = async () => {
    await loadIncident(tr.dataset.id); showView("dashboard");
  });
}

/* ---------------- vessels view ---------------- */
async function renderVesselsTable(){
  const el = $("vessels-table");
  el.innerHTML = '<div class="empty">Loading…</div>';
  const q = ($("ves-q").value || "").trim();
  const inAoi = !$("ves-global").checked;
  let rows;
  try{
    rows = await api(`/api/vessels?limit=500&hours=168&in_aoi=${inAoi}`
                     + (q ? `&q=${encodeURIComponent(q)}` : ""));
  }catch(e){ el.innerHTML = `<div class="empty">Failed: ${e.message}</div>`; return; }
  if(!rows.length){
    el.innerHTML = `<div class="empty">No vessels with real AIS positions here.
      ${S.prov && !S.prov.layers.ais.real ? "This region has no live AIS coverage — try Fetch GFW in Settings, or switch region." : ""}</div>`;
    return;
  }
  el.innerHTML = `<div style="font-size:11.5px;color:#94a3b8;padding:0 2px 9px">
      ${rows.length} vessels · last 7 days · every row from a real AIS transmission</div>
    <table class="dt"><thead><tr>
      <th>Vessel</th><th>MMSI</th><th>IMO</th><th>Type</th>
      <th class="num">Fixes</th><th class="num">Avg kn</th><th>Last seen</th>
    </tr></thead><tbody>${rows.map(v => `
      <tr data-mmsi="${v.mmsi}">
        <td><b>${v.name}</b>${v.callsign?` <span style="color:#94a3b8">${v.callsign}</span>`:""}</td>
        <td class="mono">${v.mmsi}</td>
        <td class="mono">${v.imo || "—"}</td>
        <td><span class="btag" style="background:${typeColor(v.vessel_type)}1a;color:${typeColor(v.vessel_type)}">${v.vessel_type}</span></td>
        <td class="num">${v.fixes}</td>
        <td class="num">${v.avg_sog!=null?fmt(v.avg_sog):"—"}</td>
        <td>${ago(v.last_seen)}</td>
      </tr>`).join("")}</tbody></table>`;
  el.querySelectorAll("tbody tr").forEach(tr => tr.onclick = () => showVesselTrack(+tr.dataset.mmsi));
}

async function showVesselTrack(mmsi){
  try{
    const d = await api(`/api/vessels/${mmsi}/track?hours=168`);
    if(!d.track.length){
      toast("No stored AIS positions for this vessel.", "warn"); return; }
    layers.track.clearLayers();
    const pts = d.track.map(p => [p[1], p[2]]);
    L.polyline(pts, { color:"#111827", weight:3, opacity:.9 }).addTo(layers.track);
    L.circleMarker(pts[pts.length-1], { radius:6, color:"#fff", weight:2,
      fillColor:"#dc2626", fillOpacity:1 })
      .bindTooltip(`<b>${d.vessel.name || mmsi}</b><br>${d.track.length} real fixes`)
      .addTo(layers.track);
    showView("map");
    moveMap(L.latLngBounds(pts), 0.2);
  }catch(e){ toast(e.message, "err", "Could not load track"); }
}

/* ---------------- reports view ---------------- */
async function renderReports(){
  const el = $("reports-body");
  el.innerHTML = '<div class="empty">Loading…</div>';
  let r;
  try{ r = await api("/api/reports/summary?days=365"
                     + ($("rep-all").checked ? "&region=all" : "")); }
  catch(e){ el.innerHTML = `<div class="empty">Failed: ${e.message}</div>`; return; }
  S.report = r;
  const rows = (list, k, v) => list.length
    ? list.map(x => `<div class="srow"><span>${x[k] ?? "—"}</span><b>${x[v]}</b></div>`).join("")
    : '<div class="srow"><span style="color:#94a3b8">no data</span></div>';
  const ais = r.ais || {};
  el.innerHTML = `<div class="rep-grid">
      <div class="rep-card"><h4>Incidents by region</h4>
        ${(r.by_region||[]).length ? r.by_region.map(x => `<div class="srow">
          <span>${x.region}</span><b>${x.n} · avg ${x.avg_area??"—"} km² · conf ${Math.round((x.avg_conf||0)*100)}%</b>
        </div>`).join("") : '<div class="srow"><span style="color:#94a3b8">no incidents yet</span></div>'}</div>
      <div class="rep-card"><h4>By status</h4>${rows(r.by_status||[], "status", "n")}</div>
      <div class="rep-card"><h4>Detection confidence</h4>${rows(r.confidence_bands||[], "band", "n")}</div>
      <div class="rep-card"><h4>AIS holdings</h4>
        <div class="srow"><span>Positions</span><b>${(ais.positions||0).toLocaleString()}</b></div>
        <div class="srow"><span>Distinct vessels</span><b>${(ais.vessels||0).toLocaleString()}</b></div>
        <div class="srow"><span>Oldest fix</span><b>${ais.oldest?utc(ais.oldest):"—"}</b></div>
        <div class="srow"><span>Newest fix</span><b>${ais.newest?utc(ais.newest):"—"}</b></div></div>
    </div>
    <div class="rep-card" style="margin-top:14px"><h4>Satellite scenes analysed</h4>
      ${(r.recent_scenes||[]).length ? `<table class="dt"><thead><tr>
        <th>Scene</th><th>Acquired (UTC)</th><th class="num">Detections</th></tr></thead>
        <tbody>${r.recent_scenes.map(x => `<tr><td class="mono">${x.scene_id}</td>
          <td>${utc(x.acquired)}</td><td class="num">${x.detections}</td></tr>`).join("")}
        </tbody></table>` : '<div class="srow"><span style="color:#94a3b8">no scenes analysed yet</span></div>'}
    </div>
    <div class="note" style="padding:12px 2px 0">Scope:
      <b>${r.region === "all" ? "all regions" : r.region}</b>. Every figure above
      is a COUNT or AVG over stored records. Nothing here is estimated or
      filled in.</div>`;
}

function download(name, text, type){
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name; a.click(); URL.revokeObjectURL(a.href);
}

/* ---------------- settings view ---------------- */
async function renderSettings(){
  const el = $("settings-body");
  el.innerHTML = '<div class="empty">Loading…</div>';
  const [health, ais, prov] = await Promise.all([
    api("/api/health"), api("/api/ais/status"), api("/api/provenance")
  ]);
  const c = ais.collector || {};
  const opt = prov.optional_sources || {};
  const yn = (b) => b ? '<b style="color:#047857">yes</b>' : '<b style="color:#b45309">no</b>';
  el.innerHTML = `<div class="rep-grid">
      <div class="rep-card"><h4>Area of interest</h4>
        <div class="srow"><span>Region</span><b>${prov.region.name}</b></div>
        <div class="srow"><span>Key</span><b class="mono">${prov.region.key}</b></div>
        <div class="srow"><span>Latitude</span><b class="mono">${prov.region.lat_min} … ${prov.region.lat_max}</b></div>
        <div class="srow"><span>Longitude</span><b class="mono">${prov.region.lon_min} … ${prov.region.lon_max}</b></div>
        <div class="srow"><span>Live AIS coverage</span><b>${prov.region.ais_live}</b></div>
        <div class="note" style="padding:8px 0 0">${prov.region.ais_note}</div></div>

      <div class="rep-card"><h4>AIS collector</h4>
        <div class="srow"><span>Mode</span><b>${ais.source}</b></div>
        <div class="srow"><span>Connected</span>${yn(c.connected)}</div>
        <div class="srow"><span>Messages received</span><b>${(c.messages_received||0).toLocaleString()}</b></div>
        <div class="srow"><span>Positions stored</span><b>${(c.positions_stored||0).toLocaleString()}</b></div>
        <div class="srow"><span>Throttled (rate limit)</span><b>${(c.throttled||0).toLocaleString()}</b></div>
        <div class="srow"><span>Reconnects</span><b>${c.reconnects||0}</b></div>
        ${c.last_error?`<div class="note" style="padding:8px 0 0;color:#b45309">${c.last_error}</div>`:""}</div>

      <div class="rep-card"><h4>Extra data sources</h4>
        <div class="srow"><span>Global Fishing Watch token</span>${yn(opt.global_fishing_watch?.configured)}</div>
        <div class="srow"><span>NOAA MarineCadastre</span>${yn(true)}</div>
        <div class="srow"><span>Vessel fixes in this AOI</span><b>${(opt.gfw_positions||0).toLocaleString()}</b></div>
        <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn" id="set-gfw">Fetch GFW vessels</button>
          <button class="btn" id="set-scenes">List Sentinel-1 scenes</button>
        </div></div>
    </div>
    <div id="set-out" class="note" style="padding:12px 2px 0"></div>`;

  $("set-gfw").onclick = async (e) => {
    const b = e.currentTarget; busy(b, "Requesting");
    $("set-out").textContent = "Querying Global Fishing Watch…";
    try{
      const r = await api("/api/ingest/gfw?days=60", { method:"POST" });
      $("set-out").innerHTML = `Stored <b>${r.positions_stored}</b> real fixes from
        <b>${r.vessels}</b> vessels (${r.rows_returned} rows).<br>${r.limitation}`;
      toast(`${r.positions_stored} fixes from ${r.vessels} vessels.`,
            "ok", "Global Fishing Watch");
      await loadAll();
    }catch(err){
      $("set-out").textContent = "";
      toast(err.message, "err", "GFW request failed");
    }
    finally{ idle(b); }
  };
  $("set-scenes").onclick = async (e) => {
    const b = e.currentTarget; busy(b, "Searching");
    $("set-out").textContent = "Searching Sentinel-1…";
    try{
      const list = await api("/api/sentinel1/scenes?days=45");
      $("set-out").innerHTML = list.length
        ? `<b>${list.length}</b> real scenes over this AOI:<br>` + list.slice(0,8).map(x =>
            `<span class="mono">${x.datetime.slice(0,16)} · ${x.orbit_state} · ${x.id.slice(0,44)}</span>`).join("<br>")
        : "No Sentinel-1 IW scenes here in the last 45 days.";
    }catch(err){
      $("set-out").textContent = "";
      toast(err.message, "err", "Scene search failed");
    }
    finally{ idle(b); }
  };
}

/* ---------------- data flow ---------------- */
async function loadIncident(id){
  const d = await api(`/api/incidents/${id}`);
  S.incident = d.incident; S.drift = d.drift;
  S.candidates = d.candidates || []; S.timeline = d.timeline || [];
  S.noCandReason = d.no_candidates_reason || null;
  S.selected = S.candidates[0] || null;
  renderIncident(); renderCandidates(); renderTimeline(); renderEnv();
  renderWhy(); renderMiniDrift(); drawIncident(); drawTrack(S.selected);
}

async function loadAll(){
  const [health, stats, incidents, alerts, ais, regions, prov] = await Promise.all([
    api("/api/health"), api("/api/stats"), api("/api/incidents?limit=20"),
    api("/api/alerts?limit=10"), api("/api/ais/status"),
    api("/api/regions"), api("/api/provenance")
  ]);
  S.health = health;
  renderRegions(regions); renderProvenance(prov);
  $("build-region").textContent = prov.region.key;
  renderKpis(stats); renderAlerts(alerts);

  const aisReal = prov.layers.ais.real;
  $("sysdot").className = "dot" + (aisReal ? "" : " sim");
  $("systext").textContent = aisReal
    ? `Live AIS — ${prov.region.name}`
    : `No live AIS coverage — ${prov.region.name}`;

  $("nav-inc").textContent = incidents.length || "";
  if(incidents.length) await loadIncident(incidents[0].incident_id);
  else {
    S.incident=null; S.drift=null; S.candidates=[]; S.timeline=[]; S.selected=null;
    renderIncident(); renderCandidates(); renderTimeline(); renderEnv();
    renderWhy(); renderMiniDrift();
    layers.spill.clearLayers(); layers.drift.clearLayers(); layers.track.clearLayers();
    const r = prov.region;
    moveMap(L.latLngBounds([[r.lat_min, r.lon_min], [r.lat_max, r.lon_max]]), 0.05);
  }
  await refreshVessels();
}

$("btn-refresh").onclick = async (e) => {
  const b = e.currentTarget;
  busy(b, "Refreshing");
  try{ await loadAll(); }
  catch(err){ toast(err.message, "err", "Refresh failed"); }
  finally{ idle(b); }
};
$("btn-sentinel").onclick = async (e) => {
  const b = e.currentTarget;
  busy(b, "Fetching");
  try{
    const r = await runJob(
      "/api/ingest/sentinel1?days=45&min_area_km2=2&background=true",
      "Fetching Sentinel-1");
    await loadAll();
    const n = (r.detections || []).length;
    const when = `${r.acquired.slice(0,16).replace("T"," ")} UTC · ${r.age_days} days old`;
    if(n){
      toast(`${n} dark feature${n>1?"s":""} detected in scene acquired ${when}.` +
            ` Coastline masked: ${r.land_masked_pct ?? "n/a"}%.`,
            "ok", `${n} detection${n>1?"s":""}`);
    }else{
      toast(`Scene acquired ${when}. No dark features above threshold —` +
            ` for a clean sea that is the correct result.`, "", "No detections");
    }
  }catch(err){ toast(err.message, "err", "Sentinel-1 fetch failed"); }
  finally{ idle(b); }
};

document.querySelectorAll("nav a[data-view]").forEach(a =>
  a.onclick = (e) => { e.preventDefault(); showView(a.dataset.view); });
$("inc-all").onchange = renderIncidentsTable;
$("ves-global").onchange = renderVesselsTable;
$("rep-all").onchange = renderReports;
let vq; $("ves-q").oninput = () => { clearTimeout(vq); vq = setTimeout(renderVesselsTable, 300); };
$("rep-json").onclick = () => {
  download("oiltrace-report.json", JSON.stringify(S.report||{}, null, 2), "application/json");
  toast("Report exported as JSON.", "ok", "Downloaded");
};
$("rep-csv").onclick = async () => {
  const rows = await api("/api/incidents?limit=500&region=all");
  const cols = ["incident_id","region","detected_at","lat","lon","area_km2",
                "length_km","width_km","confidence","status","scene_id",
                "candidate_count","top_vessel","top_score"];
  const csv = [cols.join(",")].concat(rows.map(r =>
    cols.map(c => {
      let v = r[c];
      if(c === "detected_at" && v) v = new Date(v*1000).toISOString();
      return `"${String(v ?? "").replace(/"/g,'""')}"`;
    }).join(","))).join("\n");
  download("oiltrace-incidents.csv", csv, "text/csv");
  toast(`${rows.length} incidents exported.`, "ok", "CSV downloaded");
};

initMap();
showView(location.hash.slice(1) || "dashboard");
window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
loadAll().catch(e => console.error(e));
S.vesselTimer = setInterval(refreshVessels, 12000);
window.addEventListener("resize", () => renderMiniDrift());
