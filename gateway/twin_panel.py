"""Local web panel for the digital twin: open http://localhost:8080 on the gateway.

Listens to `sagriot/+/twin` (retained, so the latest state shows at once)
and serves one page that refreshes every 2 seconds:
  - the state (😊 mutlu / 😟 susamış / ⚠️ arıza) and why,
  - soil moisture against the 70 % and 60 % field-capacity lines,
  - the soil_fc history since the panel started,
  - the two what-ifs, the 3 h forecast, and which model the twin follows.

A "🔧 Kontrol ettim" button sends `twin_ack` (clears a held fault), the
same as the Telegram button. Standard library + paho-mqtt only; the page
has no external scripts, so it works without internet.

Usage
  python gateway/twin_panel.py                       # broker on localhost, port 8080
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


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.latest = {}
        self.history = {}

    def add(self, s):
        node = s.get("node")
        if not node:
            return
        with self.lock:
            self.latest[node] = s
            h = self.history.setdefault(node, deque(maxlen=HISTORY))
            if not h or h[-1]["seq"] != s.get("seq") or h[-1]["ts"] != s.get("ts"):
                h.append({"ts": s.get("ts"), "seq": s.get("seq"), "soil_fc": s.get("soil_fc"),
                          "state": s.get("state")})

    def snapshot(self):
        with self.lock:
            return {n: {"latest": s, "history": list(self.history.get(n, []))}
                    for n, s in self.latest.items()}


PAGE = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAgrIoT Dijital İkiz</title>
<style>
:root{--bg:#f6f7f4;--card:#fff;--ink:#1d2320;--muted:#66706a;--line:#e2e6e1;
--happy:#2f8f5b;--thirsty:#c98a14;--fault:#c4453a;--water:#2a6fb0}
@media (prefers-color-scheme:dark){:root{--bg:#141816;--card:#1d2320;--ink:#e9eee9;
--muted:#9aa59e;--line:#2d3530}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:20px 24px 4px}h1{margin:0;font-size:20px}header p{margin:2px 0 0;color:var(--muted);font-size:14px}
main{display:grid;gap:16px;padding:16px 24px 32px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));max-width:1200px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
.card h2{margin:0 0 10px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.state{display:flex;gap:16px;align-items:center}.emoji{font-size:64px;line-height:1}
.label{font-size:28px;font-weight:700}.happy{color:var(--happy)}.thirsty{color:var(--thirsty)}.fault{color:var(--fault)}
.reasons{margin:10px 0 0;padding-left:18px;color:var(--fault)}
.big{font-size:34px;font-weight:700}.muted{color:var(--muted);font-size:14px}
.bar{position:relative;height:16px;border-radius:8px;background:var(--line);margin:14px 0 22px}
.fill{height:100%;border-radius:8px;background:var(--water)}
.mark{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--ink)}
.mark span{position:absolute;top:22px;font-size:12px;color:var(--muted);white-space:nowrap}
.mark.l span{right:4px}.mark.r span{left:4px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left}
.wide{grid-column:1/-1}svg{width:100%;height:220px;display:block}
.plot{position:relative;padding-left:72px}.tag{position:absolute;left:0;transform:translateY(-50%);font-size:12px;color:var(--muted)}
button{font:inherit;padding:8px 14px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--ink);cursor:pointer}
.row{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
select{font:inherit;padding:4px 8px;border-radius:8px;background:var(--card);color:var(--ink);border:1px solid var(--line)}
</style></head><body>
<header><h1>🌱 SAgrIoT Dijital İkiz</h1><p id="sub">Bağlanıyor…</p></header>
<main id="main"></main>
<script>
let node=null;
const fmt=(m)=>m==null?"5 gün içinde değil":m===0?"şu an":m<60?m+" dk sonra":
  m<1440?Math.floor(m/60)+" sa "+(m%60)+" dk sonra":"yaklaşık "+(m/1440).toFixed(1)+" gün sonra";
function chart(h){
  const pts=h.filter(p=>p.soil_fc!=null); if(pts.length<2) return '<p class="muted">Grafik için veri birikiyor…</p>';
  const vals=pts.map(p=>p.soil_fc), lo=Math.min(55,Math.min(...vals)-5), hi=Math.max(80,Math.max(...vals)+5);
  const W=1000,H=220,x=i=>i*(W/(pts.length-1)),y=v=>H-(v-lo)/(hi-lo)*H, yp=v=>(100*y(v)/H).toFixed(1);
  const col={happy:"var(--happy)",thirsty:"var(--thirsty)",fault:"var(--fault)"};
  let segs="";for(let i=1;i<pts.length;i++)segs+=`<line x1="${x(i-1)}" y1="${y(pts[i-1].soil_fc)}" x2="${x(i)}" y2="${y(pts[i].soil_fc)}" stroke="${col[pts[i].state]||'var(--water)'}" stroke-width="3" vector-effect="non-scaling-stroke"/>`;
  const line=v=>`<line x1="0" x2="${W}" y1="${y(v)}" y2="${y(v)}" stroke="var(--muted)" stroke-dasharray="6 6" vector-effect="non-scaling-stroke"/>`;
  const tag=(v,t)=>`<span class="tag" style="top:${yp(v)}%">${t}</span>`;
  return `<div class="plot"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${line(70)}${line(60)}${segs}</svg>${tag(70,"%70 uyarı")}${tag(60,"%60 kritik")}
  <span class="tag" style="top:0">%${hi.toFixed(0)}</span><span class="tag" style="top:100%">%${lo.toFixed(0)}</span></div>
  <p class="muted">Tarla kapasitesine göre nem (%), son ${pts.length} okuma. Renk o anki durumu gösterir.</p>`;
}
const when=t=>{if(!t)return"";const d=new Date(t);return isNaN(d)?t:d.toLocaleString("tr-TR",{dateStyle:"short",timeStyle:"short"})};
function render(d){
  const nodes=Object.keys(d); if(!nodes.length){document.getElementById("sub").textContent="İkizden henüz mesaj gelmedi.";return;}
  if(!node||!d[node]) node=nodes[0];
  const s=d[node].latest, wi=s.what_if, dec=s.decision||{}, m=s.models||{};
  const sel=nodes.length>1?`<select onchange="node=this.value;tick()">${nodes.map(n=>`<option ${n==node?"selected":""}>${n}</option>`).join("")}</select>`:node;
  document.getElementById("sub").innerHTML=`Düğüm ${sel} · seq ${s.seq} · ${when(s.ts)}`;
  const fc=s.soil_fc, pct=Math.max(0,Math.min(100,(fc||0)/1.2));
  const vwcTitle=s.state==="fault"?"Son geçerli toprak nemi":"Toprak nemi";
  document.getElementById("main").innerHTML=`
  <section class="card"><h2>Durum</h2><div class="state"><div class="emoji">${s.emoji}</div>
   <div><div class="label ${s.state}">${s.label}</div><div class="muted">${dec.now?"Şimdi sulanmalı":dec.warn?"3 saat içinde susayacak ("+fmt(dec.t_cross_min)+")":"Önümüzdeki 3 saat sorun yok"}</div></div></div>
   ${(s.reasons||[]).length?`<ul class="reasons">${s.reasons.map(r=>`<li>${r}</li>`).join("")}</ul><p><button onclick="ack()">🔧 Kontrol ettim</button></p>`:""}</section>
  <section class="card"><h2>${vwcTitle}</h2><div class="row"><span class="big">%${s.vwc==null?"–":s.vwc.toFixed(1)}</span><span class="muted">VWC · tarla kapasitesinin %${fc==null?"–":fc.toFixed(0)}'i</span></div>
   <div class="bar"><div class="fill" style="width:${pct}%"></div><div class="mark l" style="left:${60/1.2}%"><span>%60</span></div><div class="mark r" style="left:${70/1.2}%"><span>%70</span></div></div>
   <div class="muted">Hava ${s.air_t==null?"–":s.air_t.toFixed(1)} °C · nem %${s.rh==null?"–":s.rh.toFixed(0)} · toprak ${s.soil_t==null?"–":s.soil_t.toFixed(1)} °C · VPD ${s.vpd==null?"–":s.vpd.toFixed(2)} kPa</div></section>
  <section class="card"><h2>Ya şöyle olursa?</h2>${wi?`
   <p>🔮 <b>Sulamazsam:</b> %70 eşiğine ${fmt(wi.no_water.warn_in_min)}, %60'a ${fmt(wi.no_water.crit_in_min)}</p>
   <p>💧 <b>Şimdi sularsam:</b> nem %${wi.water_now.vwc_after}'a çıkar, eşiğe yeniden ${fmt(wi.water_now.warn_in_min)}</p>
   <p class="muted">3 saat sonra beklenen nem: %${dec.vwc_pred_3h==null?"–":dec.vwc_pred_3h.toFixed(1)}</p>`
   :`<p class="muted">Tahmin için 24 saatlik veri gerekiyor (${s.model.samples}/${s.model.needed}).</p>`}
   <p class="muted">Son sulama: ${s.last_irrigation_ts?when(s.last_irrigation_ts):"görülmedi"}</p></section>
  <section class="card wide"><h2>Nem geçmişi</h2>${chart(d[node].history)}</section>
  <section class="card wide"><h2>Modeller</h2><table><tr><th>Model</th><th>Karar</th><th>3 sa sonra</th><th>3 sa hatası (VWC)</th><th>Puanlanan</th></tr>
   ${Object.entries(m).map(([n,v])=>`<tr><td>${n===s.selected_model?"▶ <b>"+n+"</b>":n}</td><td>${v.now?"şimdi sula":v.warn?"uyar":"–"}</td><td>${v.vwc_pred_3h==null?"–":v.vwc_pred_3h.toFixed(1)}</td><td>${v.mae_3h==null?"–":v.mae_3h.toFixed(3)}</td><td>${v.scored??0}</td></tr>`).join("")}</table>
   <p class="muted">İkiz son günün en düşük hatalı modelini dinler (▶). What-if'ler fiziksel modelden gelir.</p></section>`;
}
async function tick(){try{render(await (await fetch("/api/twin")).json())}catch(e){document.getElementById("sub").textContent="Panel sunucusuna ulaşılamıyor."}}
async function ack(){await fetch("/api/ack?node="+encodeURIComponent(node),{method:"POST"});tick()}
tick();setInterval(tick,2000);
</script></body></html>"""


def main():
    import paho.mqtt.client as mqtt

    parser = argparse.ArgumentParser(description="SAgrIoT digital twin web panel")
    parser.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--user", default=os.environ.get("MQTT_USER"))
    parser.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"))
    parser.add_argument("--http", type=int, default=8080, help="web port")
    args = parser.parse_args()

    store = Store()
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
