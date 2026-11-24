"""Local web panel for the digital twin: open http://localhost:8080 on the gateway.

Laid out like the concept design ("SAgrIoT Dijital İkiz Konsept", 3 Nov):
  - left: the pot as a character (😊 mutlu / 😟 susamış / ⚠️ arıza), one
    sentence in its voice, and six numbers (VWC, threshold, VPD, drying
    rate, last watering, field capacity);
  - right: soil moisture from -24 h to +24 h with the measurement, the
    twin's 3 h forecast, the chosen what-if and the 70 % FC line;
    "Ya şöyle olursa?" with the two scenarios; "Son olaylar".

Listens to `sagriot/+/twin` (retained, so the latest state shows at once).
History and events are kept in memory from the moment the panel starts.
A "Kontrol ettim" button (in a fault) sends `twin_ack`, like Telegram.
For about a day after a watering the drying rate is not shown: the
watering jump is still inside the model's 24 h window and pulls the
fitted rate toward zero (same on the node and in the reference).
Standard library + paho-mqtt; no external scripts, works offline.

Usage
  python gateway/twin_panel.py                          # localhost broker, port 8080
  python gateway/twin_panel.py --title "Ev saksısı"
  python gateway/twin_panel.py --host 20.91.194.65 --user ... --password ...
"""
import argparse
import json
import os
import re
import threading
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HISTORY = 2000          # points kept per node (in memory)
EVENTS = 12


class Store:
    def __init__(self, title):
        self.title = title
        self.lock = threading.Lock()
        self.latest = {}
        self.history = {}
        self.events = {}

    def _event(self, node, ts, text):
        self.events.setdefault(node, deque(maxlen=EVENTS)).appendleft({"ts": ts, "text": text})

    def add(self, s):
        node = s.get("node")
        if not node:
            return
        with self.lock:
            prev = self.latest.get(node)
            h = self.history.setdefault(node, deque(maxlen=HISTORY))
            ts = s.get("ts")
            if prev is None:
                self._event(node, ts, "Panel bağlandı, ikizin son durumu alındı")
            else:
                if s.get("last_irrigation_ts") and s["last_irrigation_ts"] != prev.get("last_irrigation_ts"):
                    before = h[-1]["vwc"] if h and h[-1]["vwc"] is not None else None
                    after = s.get("vwc")
                    self._event(node, ts, "Sulama algılandı" + (
                        f": {before:.1f} → {after:.1f} %VWC" if before is not None and after is not None else ""))
                if s.get("state") != prev.get("state"):
                    d = s.get("decision") or {}
                    if s["state"] == "fault":
                        self._event(node, ts, "Arıza şüphesi: " + "; ".join(s.get("reasons") or []))
                    elif s["state"] == "thirsty":
                        self._event(node, ts, "Uyarı: nem eşiğin altında" if d.get("now")
                                    else "Uyarı: eşiğe 3 saatten az kaldı")
                    elif prev.get("state") == "fault":
                        self._event(node, ts, "Ölçüm normale döndü, ikiz ile uyumlu")
                    else:
                        self._event(node, ts, "Durum: mutlu")
                if (s.get("model") or {}).get("fitted") and not (prev.get("model") or {}).get("fitted"):
                    self._event(node, ts, "İkiz 24 saatlik veriyle tahmine başladı")
                if s.get("selected_model") != prev.get("selected_model"):
                    self._event(node, ts, f"Karar modeli değişti: {s.get('selected_model')}")
            self.latest[node] = s
            if not h or h[-1]["seq"] != s.get("seq") or h[-1]["ts"] != ts:
                h.append({"ts": ts, "seq": s.get("seq"), "vwc": s.get("vwc"),
                          "state": s.get("state")})

    def snapshot(self):
        with self.lock:
            return {"title": self.title,
                    "nodes": {n: {"latest": s, "history": list(self.history.get(n, [])),
                                  "events": list(self.events.get(n, []))}
                              for n, s in self.latest.items()}}


PAGE = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAgrIoT Dijital İkiz</title>
<style>
:root{--bg:#f2f3ee;--card:#fff;--tile:#f3f4f0;--ink:#1f2421;--muted:#6b726d;--line:#e3e6df;
--happy:#2e7d4f;--thirsty:#a85d0c;--fault:#5a4bd1;--meas:#1f2421;--twin:#3f8f62;--whatif:#b06a12;--thr:#b33a2e;--fc:#3f8f62;
--pot:#c8714a;--rim:#a95a39;--potoff:#c7aea2;--rimoff:#a8948a;--leaf:#4f9a64;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151816;--card:#1e2320;--tile:#262c28;--ink:#e8ece8;
--muted:#9ba49e;--line:#2f3732;--meas:#e8ece8;--happy:#58b07d;--thirsty:#d9923c;--fault:#8f84f0}}
*{box-sizing:border-box}html,body{margin:0}body{background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:18px 16px 28px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.kicker{font-size:11px;letter-spacing:.12em;font-weight:700;color:var(--muted)}
h1{margin:2px 0 0;font-size:24px}.sub{font-size:13px;color:var(--muted)}
.pills{display:flex;gap:6px;align-items:center;font-size:12px;color:var(--muted)}
.pill{padding:6px 14px;border-radius:999px;border:1.5px solid;font-weight:600;font-size:13px;background:var(--card)}
.pill.happy{color:var(--happy);border-color:var(--happy)}.pill.thirsty{color:var(--thirsty);border-color:var(--thirsty)}
.pill.fault{color:var(--fault);border-color:var(--fault)}.pill.on{color:#fff}
.pill.happy.on{background:var(--happy)}.pill.thirsty.on{background:var(--thirsty)}.pill.fault.on{background:var(--fault)}
.grid{display:grid;grid-template-columns:minmax(260px,330px) 1fr;gap:14px;margin-top:14px}
@media (max-width:820px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.right{display:grid;gap:14px;align-content:start}
.hero{text-align:center}.hero svg{width:150px;height:auto}
.badge{display:inline-block;color:#fff;font-weight:700;font-size:12px;padding:3px 10px;border-radius:999px;margin-top:4px}
.say{font-size:17px;margin:8px 0 4px}.why{font-size:12.5px;color:var(--muted);margin:0 0 14px}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:8px;text-align:left}
.tile{background:var(--tile);border-radius:8px;padding:7px 10px}.tile small{display:block;font-size:11px;color:var(--muted)}
.tile b{font:600 14px var(--mono);white-space:nowrap}
.chead{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.chead h2,.card h2{margin:0;font-size:16px}.legend{display:flex;gap:12px;font-size:11.5px;color:var(--muted);flex-wrap:wrap}
.legend i{display:inline-block;width:18px;height:0;border-top:2px solid;vertical-align:middle;margin-right:4px}
#chart{margin-top:8px}#chart svg{display:block;width:100%}
.btns{display:flex;gap:8px;margin:10px 0;flex-wrap:wrap}
.btns button{font:600 13px system-ui,sans-serif;padding:8px 14px;border-radius:9px;border:1.5px solid var(--whatif);
color:var(--whatif);background:var(--card);cursor:pointer}.btns button.on{background:var(--whatif);color:#fff}
.btns button:disabled{border-color:var(--line);color:var(--muted);background:var(--card);cursor:default}
.note{background:var(--tile);border-radius:8px;padding:10px 12px;font-size:14px}
.ev{display:grid;grid-template-columns:110px 1fr;gap:4px 10px;font-size:13.5px;margin-top:8px}
.ev span{font:12px var(--mono);color:var(--muted)}
.ack{margin-top:10px;font:600 13px system-ui,sans-serif;padding:7px 12px;border-radius:9px;border:1.5px solid var(--fault);
background:var(--card);color:var(--fault);cursor:pointer}
footer{margin-top:14px;font-size:12px;color:var(--muted)}
select{font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px}
</style></head><body><div class="wrap">
<div class="top"><div><div class="kicker">SAGRIOT · DİJİTAL İKİZ</div><h1 id="title">…</h1><div class="sub" id="sub">Bağlanıyor…</div></div>
<div class="pills">Durum <span class="pill happy" id="p-happy">Mutlu</span><span class="pill thirsty" id="p-thirsty">Susamış</span><span class="pill fault" id="p-fault">Arıza</span></div></div>
<div class="grid"><section class="card hero" id="hero"></section>
<div class="right">
<section class="card"><div class="chead"><h2>Toprak nemi (%VWC)</h2><div class="legend">
<span><i style="border-color:var(--meas)"></i>Ölçüm</span><span><i style="border-color:var(--twin);border-top-style:dashed"></i>İkizin tahmini</span>
<span><i style="border-color:var(--whatif);border-top-style:dotted"></i>What-if</span><span><i style="border-color:var(--thr)"></i>Eşik (%70 FC)</span></div></div>
<div id="chart"></div></section>
<section class="card"><h2>Ya şöyle olursa?</h2><div class="btns">
<button id="b-no" onclick="pick('no_water')">☀ Sulamazsam?</button><button id="b-wet" onclick="pick('water_now')">💧 Şimdi sularsam?</button></div>
<div class="note" id="whatif"></div></section>
<section class="card"><h2>Son olaylar</h2><div class="ev" id="events"></div></section>
</div></div>
<footer id="foot"></footer></div>
<script>
let node=null, scenario={};
const $=id=>document.getElementById(id);
const num=(v,d=1)=>v==null?"–":v.toFixed(d).replace(".",",");
const COL=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function hm(t){const d=new Date(t);return isNaN(d)?"":d.toLocaleTimeString("tr-TR",{hour:"2-digit",minute:"2-digit"})}
function rel(t,ref){const d=new Date(t),r=new Date(ref);if(isNaN(d)||isNaN(r))return t||"–";
 const day=x=>new Date(x.getFullYear(),x.getMonth(),x.getDate()).getTime(),k=Math.round((day(r)-day(d))/864e5);
 return k<=0?"bugün "+hm(t):k===1?"dün "+hm(t):k+" gün önce"}
function later(ts,min){if(min==null)return null;const d=new Date(new Date(ts).getTime()+min*6e4);
 return isNaN(d)?null:d.toLocaleString("tr-TR",{weekday:"long",hour:"2-digit",minute:"2-digit"})}
function dur(m){if(m==null)return"5 günden fazla";if(m===0)return"şu an";const h=Math.floor(m/60),mm=m%60;
 return h>=24?"yaklaşık "+(m/1440).toFixed(1).replace(".",",")+" gün":h?h+" sa "+mm+" dk":mm+" dk"}
function pot(state){
 const off=state==="fault",pc=off?"var(--potoff)":"var(--pot)",rc=off?"var(--rimoff)":"var(--rim)";
 const face=state==="happy"?'<path d="M66 118 q14 12 28 0" stroke="#3a2a22" stroke-width="3" fill="none" stroke-linecap="round"/>'
  :state==="thirsty"?'<path d="M68 124 q12 -10 24 0" stroke="#3a2a22" stroke-width="3" fill="none" stroke-linecap="round"/>'
  :'<path d="M68 120 h24" stroke="#3a2a22" stroke-width="3" stroke-linecap="round"/>';
 const plant=state==="thirsty"
  ?'<path d="M80 80 q2 -22 14 -34" stroke="var(--leaf)" stroke-width="4" fill="none"/><path d="M93 47 q-18 -2 -26 -14 q18 -2 26 14z" fill="var(--leaf)"/><path d="M84 66 q14 6 22 20 q-16 -2 -22 -20z" fill="var(--leaf)"/><path d="M112 40 q4 8 0 12 q-4 -4 0 -12z" fill="#69a8d6"/>'
  :'<path d="M80 80 v-40" stroke="var(--leaf)" stroke-width="4"/><path d="M80 52 q-22 -2 -30 -18 q22 0 30 18z" fill="var(--leaf)"/><path d="M80 44 q20 -6 32 -22 q-22 2 -32 22z" fill="var(--leaf)"/><path d="M80 64 q18 0 26 12 q-18 2 -26 -12z" fill="var(--leaf)"/>';
 const badge=off?'<circle cx="122" cy="40" r="15" fill="var(--fault)"/><text x="122" y="47" text-anchor="middle" font-size="20" font-weight="700" fill="#fff">!</text>':"";
 return `<svg viewBox="0 0 160 160" aria-hidden="true">${plant}<rect x="38" y="78" width="84" height="12" rx="3" fill="${rc}"/>
  <path d="M44 90 h72 l-9 58 h-54z" fill="${pc}"/><circle cx="70" cy="108" r="3.5" fill="#3a2a22"/><circle cx="90" cy="108" r="3.5" fill="#3a2a22"/>${face}${badge}</svg>`}
function chart(s,hist){
 const el=$("chart"),W=Math.max(300,el.clientWidth),H=250,L=40,R=78,T=22,B=26;
 const p=s.params||{},step=p.step_minutes||5,wi=s.what_if,now=s.vwc;
 const H24=1440,meas=[];hist.forEach((h,i)=>{const t=(i-(hist.length-1))*step;if(t>=-H24&&h.vwc!=null)meas.push([t,h.vwc])});
 const fc=s.state==="fault"?[]:(s.forecast||[]).map((v,k)=>[(k+1)*step,v]);if(fc.length&&now!=null)fc.unshift([0,now]);
 let wif=[],sc=scenario[node];
 if(wi&&s.state!=="fault"&&now!=null){const c=wi[sc].curve||[],cs=wi.curve_step_min||30;
  wif=sc==="water_now"?[[0,now],[0,wi.water_now.vwc_after]]:[[0,now]];c.forEach((v,j)=>wif.push([(j+1)*cs,v]))}
 const all=[...meas,...fc,...wif].map(q=>q[1]).concat([p.warn_vwc,p.field_capacity].filter(v=>v!=null));
 if(!all.length){el.innerHTML='<p class="sub">Veri bekleniyor…</p>';return}
 const lo=Math.floor(Math.min(...all)-3),hi=Math.ceil(Math.max(...all)+2);
 const X=t=>L+(t+H24)/(2*H24)*(W-L-R),Y=v=>T+(hi-v)/(hi-lo)*(H-T-B);
 const path=pts=>pts.map((q,i)=>(i?"L":"M")+X(q[0]).toFixed(1)+" "+Y(q[1]).toFixed(1)).join(" ");
 let g="";const ticks=5;for(let i=0;i<=ticks;i++){const v=lo+(hi-lo)*i/ticks;g+=`<line x1="${L}" x2="${W-R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${L-6}" y="${Y(v)+4}" text-anchor="end" font-size="11" fill="var(--muted)">${Math.round(v)}</text>`}
 [-24,-12,0,12,24].forEach(h=>g+=`<text x="${X(h*60)}" y="${H-6}" text-anchor="middle" font-size="11" fill="var(--muted)">${h>0?"+":""}${h} sa</text>`);
 g+=`<line x1="${X(0)}" x2="${X(0)}" y1="${T-4}" y2="${H-B}" stroke="var(--muted)" stroke-dasharray="3 3"/><text x="${X(0)}" y="${T-8}" text-anchor="middle" font-size="11" font-weight="700" fill="var(--ink)">şimdi</text>`;
 if(p.field_capacity!=null)g+=`<line x1="${L}" x2="${W-R}" y1="${Y(p.field_capacity)}" y2="${Y(p.field_capacity)}" stroke="var(--fc)" stroke-dasharray="2 3"/><text x="${W-R+6}" y="${Y(p.field_capacity)+4}" font-size="11" fill="var(--fc)">FC ${num(p.field_capacity)}</text>`;
 if(p.warn_vwc!=null)g+=`<line x1="${L}" x2="${W-R}" y1="${Y(p.warn_vwc)}" y2="${Y(p.warn_vwc)}" stroke="var(--thr)"/><text x="${W-R+6}" y="${Y(p.warn_vwc)+4}" font-size="11" fill="var(--thr)">Eşik ${num(p.warn_vwc)}</text>`;
 if(meas.length>1)g+=`<path d="${path(meas)}" fill="none" stroke="var(--meas)" stroke-width="2"/>`;
 if(wif.length>1)g+=`<path d="${path(wif)}" fill="none" stroke="var(--whatif)" stroke-width="2" stroke-dasharray="2 3"/>`;
 if(fc.length>1)g+=`<path d="${path(fc)}" fill="none" stroke="var(--twin)" stroke-width="2.5" stroke-dasharray="6 4"/>`;
 if(s.state==="fault"&&meas.length){const q=meas[meas.length-1],r=(s.reasons||[])[0]||"";g+=`<circle cx="${X(q[0])}" cy="${Y(q[1])}" r="6" fill="none" stroke="var(--fault)" stroke-width="2"/><text x="${X(q[0])-10}" y="${Y(q[1])+4}" text-anchor="end" font-size="11.5" font-weight="700" fill="var(--fault)">${r.split(":")[0]}</text>`}
 el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" height="${H}">${g}</svg>`}
function pick(sc){scenario[node]=sc;tick()}
function render(d){
 $("title").textContent=d.title||"Saksı";const nodes=Object.keys(d.nodes);
 if(!nodes.length){$("sub").textContent="İkizden henüz mesaj gelmedi. Bulut servisi açık mı?";return}
 if(!node||!d.nodes[node])node=nodes[0];
 const N=d.nodes[node],s=N.latest,p=s.params||{},wi=s.what_if,dec=s.decision||{};
 if(!scenario[node])scenario[node]=s.state==="thirsty"?"no_water":"water_now";
 const sc=scenario[node],off=s.state==="fault"||!wi;
 const sel=nodes.length>1?`<select onchange="node=this.value;tick()">${nodes.map(n=>`<option ${n==node?"selected":""}>${n}</option>`).join("")}</select>`:node;
 $("sub").innerHTML=`${sel} · son okuma ${hm(s.ts)} · Durum: ${s.label} · ${off?"senaryolar kapalı":'senaryo: "'+(sc==="water_now"?"Şimdi sularsam?":"Sulamazsam?")+'"'}`;
 ["happy","thirsty","fault"].forEach(k=>$("p-"+k).classList.toggle("on",s.state===k));
 let say,why;
 if(s.state==="happy"){say="Her şey yolunda, sulamaya gerek yok.";why=wi?`Bugünkü koşullarla eşiğe ${dur(wi.no_water.warn_in_min)} var.`:`Tahmin için veri birikiyor (${s.model.samples}/${s.model.needed}).`}
 else if(s.state==="thirsty"){say=dec.now?"Susadım! Şimdi sulanmam gerek.":`Susadım! ${dur(dec.t_cross_min)} içinde eşiğin altına düşeceğim.`;
  why=s.drying_per_day!=null?`Toprak saatte ${num(s.drying_per_day/24,2)} puan kaybediyor (VPD ${num(s.vpd,2)} kPa).`:"Nem eşiğin altında."}
 else{say="Sensörüm bozuk olabilir.";why=(s.reasons||[]).join(". ")+". Sensör kontrol edilene kadar sulama önerisi verilmiyor."}
 const color={happy:"var(--happy)",thirsty:"var(--thirsty)",fault:"var(--fault)"}[s.state];
 $("hero").innerHTML=`${pot(s.state)}<div><span class="badge" style="background:${color}">${s.label[0].toUpperCase()+s.label.slice(1)}</span></div>
  <p class="say">${say}</p><p class="why">${why}</p>
  <div class="tiles"><div class="tile"><small>${s.state==="fault"&&!(s.reasons||[]).every(r=>r.startsWith("ani düşüş"))?"Son geçerli nem":"Toprak nemi"}</small><b>${num(s.vwc)} %VWC</b></div>
  <div class="tile"><small>Eşik (%70 FC)</small><b>${num(p.warn_vwc)} %VWC</b></div>
  <div class="tile"><small>VPD</small><b>${num(s.vpd,2)} kPa</b></div>
  <div class="tile"><small>Kuruma hızı</small><b>${s.state==="fault"||s.drying_per_day==null?"–":s.drying_per_day<0.05&&s.last_irrigation_ts?"– (sulama sonrası)":num(s.drying_per_day)+" puan/gün"}</b></div>
  <div class="tile"><small>Son sulama</small><b>${s.last_irrigation_ts?rel(s.last_irrigation_ts,s.ts):"görülmedi"}</b></div>
  <div class="tile"><small>Tarla kapasitesi</small><b>${num(p.field_capacity)} %VWC</b></div></div>
  ${s.state==="fault"?'<button class="ack" onclick="ack()">🔧 Kontrol ettim</button>':""}`;
 $("b-no").disabled=$("b-wet").disabled=off;$("b-no").classList.toggle("on",!off&&sc==="no_water");$("b-wet").classList.toggle("on",!off&&sc==="water_now");
 let note;
 if(s.state==="fault")note="Sensöre şu an güvenilmediği için senaryolar kapalı.";
 else if(!wi)note=`Senaryolar için 24 saatlik veri gerekiyor (${s.model.samples}/${s.model.needed}).`;
 else if(sc==="water_now"){const t=later(s.ts,wi.water_now.warn_in_min);note=`Şimdi sularsan nem FC'ye (${num(wi.water_now.vwc_after)}) çıkar ve `+(t?`${t}'e kadar yeter.`:"5 günden uzun süre yeter.")}
 else{const w=wi.no_water;note=w.warn_in_min===0?`Sulamazsan: zaten susamış`+(w.crit_in_min===0?", kritik eşiğin de altında.":`, kritik eşiğe (%60 FC) ${later(s.ts,w.crit_in_min)||dur(w.crit_in_min)} iner.`)
  :`Sulamazsan nem ${later(s.ts,w.warn_in_min)||"5 gün içinde değil"}'de eşiğin altına iner.`}
 $("whatif").textContent=note;
 $("events").innerHTML=(N.events||[]).slice(0,6).map(e=>`<span>${rel(e.ts,s.ts)}</span><div>${e.text}</div>`).join("")||'<div class="sub">Henüz olay yok.</div>';
 const m=(s.models||{})[s.selected_model]||{};
 $("foot").textContent=`Tek yönlü dijital ikiz (digital shadow) · karar modeli: ${s.selected_model||"baseline"}`+(m.mae_3h!=null?` (3 sa hatası ${num(m.mae_3h,3)} VWC)`:"")+` · FC ${num(p.field_capacity)} %VWC, eşik %70 FC · çiftçi uyarıları Telegram'dan.`;
 chart(s,N.history)}
async function tick(){try{render(await (await fetch("/api/twin")).json())}catch(e){$("sub").textContent="Panel sunucusuna ulaşılamıyor."}}
async function ack(){await fetch("/api/ack?node="+encodeURIComponent(node),{method:"POST"});tick()}
tick();setInterval(tick,2000);addEventListener("resize",tick);
</script></body></html>"""


def main():
    import paho.mqtt.client as mqtt

    parser = argparse.ArgumentParser(description="SAgrIoT digital twin web panel")
    parser.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--user", default=os.environ.get("MQTT_USER"))
    parser.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"))
    parser.add_argument("--http", type=int, default=8080, help="web port")
    parser.add_argument("--title", default="Saksı 1", help="name shown at the top")
    args = parser.parse_args()

    store = Store(args.title)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sagriot-panel")
    if args.user:
        client.username_pw_set(args.user, args.password)
    client.on_connect = lambda c, u, f, rc, p: (print(f"MQTT connected ({rc})"),
                                                c.subscribe("sagriot/+/twin", qos=1))

    def on_message(c, u, m):
        try:
            store.add(json.loads(m.payload))
        except ValueError:
            pass
    client.on_message = on_message
    client.connect(args.host, args.port)
    client.loop_start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif self.path == "/api/twin":
                self._send(200, json.dumps(store.snapshot(), ensure_ascii=False),
                           "application/json; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path.startswith("/api/ack?node="):
                node = urllib.parse.unquote(self.path.split("=", 1)[1])
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", node):
                    return self._send(400, "bad node", "text/plain")
                client.publish(f"sagriot/{node}/twin_ack", "{}", qos=1)
                self._send(200, "{}", "application/json")
            else:
                self._send(404, "not found", "text/plain")

    server = ThreadingHTTPServer(("0.0.0.0", args.http), Handler)
    print(f"panel: http://localhost:{args.http}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
