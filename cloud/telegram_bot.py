"""Telegram bot: tells the farmer when the pot needs attention.

Listens to the twin (`sagriot/<node>/twin`, retained) and sends a Telegram
message when the state changes:

  😊 -> 😟  "Saksı susadı"        buttons: ✅ Suladım  ⏰ 1 saat ertele  ❌ Gerek yok
  any -> ⚠️ "Sensörde sorun var"   buttons: 🔧 Kontrol ettim  ⏰ 1 saat ertele
  😟 -> 😊  "Toprak toparlandı"    (no buttons)

Every button press is
  - saved to FEEDBACK_FILE (one JSON line: time, node, action, the state
    the farmer saw). This is the "close the loop" data: what the system
    advised and what the person did;
  - published to `sagriot/<node>/feedback`;
  - for 🔧 Kontrol ettim, also sent to `sagriot/<node>/twin_ack`, which
    clears a held fault in the twin.
⏰ asks for a reminder: if the pot is still thirsty (or faulty) an hour
later, the message comes again. "✅ Suladım" goes to the twin through
`feedback`: the twin marks the watering as reported and confirms it only
when the VWC rise shows up (or raises a fault after 30 min).

Commands: /durum (current state and what-ifs), /start (shows the chat id).

Setup
  1. Telegram: talk to @BotFather, /newbot, copy the token.
  2. TELEGRAM_TOKEN=... python cloud/telegram_bot.py
  3. Send /start to the bot. The first chat that does is saved as the
     farmer's chat (or set TELEGRAM_CHAT_ID).

Only the standard library is used for Telegram (HTTPS long polling), so
the bot needs nothing beyond paho-mqtt.
"""
import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SNOOZE_S = 3600

ACTIONS = {
    "watered":  "✅ Suladım",
    "snooze":   "⏰ 1 saat ertele",
    "not_needed": "❌ Gerek yok",
    "checked":  "🔧 Kontrol ettim",
}


def _minutes_text(minutes):
    if minutes is None:
        return "5 gün içinde değil"
    if minutes == 0:
        return "şu an"
    hours, mins = divmod(int(minutes), 60)
    if hours >= 24:
        return f"yaklaşık {hours / 24:.1f} gün sonra"
    return f"{hours} sa {mins} dk sonra" if hours else f"{mins} dk sonra"


def state_text(s):
    """Short Turkish summary of a twin state, used by messages and /durum."""
    lines = [f"{s['emoji']} {s['node']}: {s['label']}"]
    if s.get("vwc") is not None:
        # in a fault the twin shows the last reading it trusted, not the bad one
        what = "Son geçerli toprak nemi" if s["state"] == "fault" else "Toprak nemi"
        lines.append(f"{what}: %{s['vwc']:.1f} VWC (tarla kapasitesinin %{s['soil_fc']:.0f}'i)")
    if s.get("air_t") is not None and s.get("rh") is not None:
        lines.append(f"Hava: {s['air_t']:.1f} °C, %{s['rh']:.0f} nem")
    for reason in s.get("reasons") or []:
        lines.append(f"• {reason}")
    if (s.get("watering") or {}).get("status") == "pending":
        lines.append("💧 Sulama bildirildi, nem artışı bekleniyor")
    models = s.get("models") or {}
    if len(models) > 1:
        chosen = s.get("selected_model", "baseline")
        mae = models.get(chosen, {}).get("mae_3h")
        lines.append(f"Karar modeli: {chosen}" + (f" (3 sa hatası {mae:.2f} VWC)" if mae is not None else ""))
    wi = s.get("what_if")
    if wi:
        lines.append(f"🔮 Sulamazsam: %70 eşiğine {_minutes_text(wi['no_water']['warn_in_min'])}")
        lines.append(f"💧 Şimdi sularsam: eşiğe yeniden {_minutes_text(wi['water_now']['warn_in_min'])}")
    elif s.get("model"):
        lines.append(f"Tahmin için veri birikiyor ({s['model']['samples']}/{s['model']['needed']})")
    return "\n".join(lines)


class Notifier:
    """Telegram-free core: decides what to send and what a button does."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.last_state = {}        # node -> state string
        self.latest = {}            # node -> last twin dict
        self.remind_at = {}         # node -> epoch seconds (set by the snooze button)

    def on_twin(self, s):
        """Returns a message (text, [action, ...]) or None."""
        node, state = s["node"], s["state"]
        prev = self.last_state.get(node)
        self.last_state[node] = state
        self.latest[node] = s
        due = self.remind_at.get(node)
        if state in ("thirsty", "fault") and due is not None and self.clock() >= due:
            del self.remind_at[node]
            return self._alert(s, reminder=True)
        if prev == state:
            return None
        if state == "happy":
            self.remind_at.pop(node, None)
            if prev == "thirsty" or (prev == "fault" and (s.get("watering") or {}).get("status") == "confirmed"):
                head = ("✅ Sulama doğrulandı, toprak toparlandı."
                        if (s.get("watering") or {}).get("status") == "confirmed"
                        else "Toprak toparlandı, teşekkürler.")
                return (f"{head}\n\n{state_text(s)}", [])
            return None             # first message after start, or fault -> happy
        return self._alert(s)

    def _alert(self, s, reminder=False):
        head = "⏰ Hatırlatma: " if reminder else ""
        if s["state"] == "thirsty":
            return (f"{head}Saksı susadı!\n\n{state_text(s)}", ["watered", "snooze", "not_needed"])
        return (f"{head}Sensörde sorun var.\n\n{state_text(s)}", ["checked", "snooze"])

    def on_button(self, node, action):
        """Returns (reply text, publish list [(topic, payload)], feedback record)."""
        now = self.clock()
        s = self.latest.get(node, {})
        record = {"time": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                  "node": node, "action": action, "state": s.get("state"),
                  "seq": s.get("seq"), "vwc": s.get("vwc"), "soil_fc": s.get("soil_fc"),
                  "decision": s.get("decision")}
        publish = [(f"sagriot/{node}/feedback", json.dumps(record, ensure_ascii=False))]
        if action == "snooze":
            self.remind_at[node] = now + SNOOZE_S
            reply = "Tamam, 1 saat sonra durum düzelmediyse tekrar hatırlatacağım."
        elif action == "checked":
            publish.append((f"sagriot/{node}/twin_ack", "{}"))
            reply = "Teşekkürler, arıza uyarısını kapattım."
        elif action == "watered":
            self.remind_at.pop(node, None)
            reply = ("Teşekkürler! Sulamayı ikize bildirdim. Nemin arttığını görünce doğrulayacağım; "
                     "30 dakika içinde artmazsa haber veririm.")
        elif action == "not_needed":
            reply = "Not aldım. Bu geri bildirim eşikleri iyileştirmek için kaydedildi."
        else:
            return "Bilinmeyen buton.", [], None
        return reply, publish, record


class Telegram:
    def __init__(self, token, timeout=30, api=None):
        api = api or os.environ.get("TELEGRAM_API", "https://api.telegram.org")
        self.base = f"{api}/bot{token}/"
        self.timeout = timeout

    def call(self, method, **params):
        data = urllib.parse.urlencode(
            {k: json.dumps(v) if isinstance(v, (dict, list)) else v
             for k, v in params.items()}).encode()
        req = urllib.request.Request(self.base + method, data=data)
        with urllib.request.urlopen(req, timeout=self.timeout + 10) as resp:
            body = json.loads(resp.read().decode())
        if not body.get("ok"):
            raise RuntimeError(f"{method}: {body}")
        return body["result"]

    def send(self, chat_id, text, node=None, actions=()):
        params = {"chat_id": chat_id, "text": text}
        if actions:
            params["reply_markup"] = {"inline_keyboard": [[
                {"text": ACTIONS[a], "callback_data": f"{node}|{a}"} for a in actions]]}
        return self.call("sendMessage", **params)


def main():
    import paho.mqtt.client as mqtt

    parser = argparse.ArgumentParser(description="SAgrIoT Telegram bot")
    parser.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--user", default=os.environ.get("MQTT_USER"))
    parser.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"))
    parser.add_argument("--state", default=os.environ.get("BOT_STATE", "data/bot_state.json"))
    parser.add_argument("--feedback", default=os.environ.get("FEEDBACK_FILE", "data/feedback.jsonl"))
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_TOKEN is not set (get one from @BotFather)")
    tg = Telegram(token)
    state_path, feedback_path = Path(args.state), Path(args.feedback)
    saved = json.loads(state_path.read_text()) if state_path.exists() else {}
    chat = {"id": os.environ.get("TELEGRAM_CHAT_ID") or saved.get("chat_id")}
    notifier = Notifier()
    lock = threading.Lock()

    def save_chat(chat_id):
        chat["id"] = chat_id
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"chat_id": chat_id}))

    # -- MQTT side ---------------------------------------------------------
    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"MQTT connected ({reason_code})")
        client.subscribe("sagriot/+/twin", qos=1)

    def on_message(client, userdata, message):
        try:
            s = json.loads(message.payload)
        except ValueError:
            return
        with lock:
            msg = notifier.on_twin(s)
        if msg and chat["id"]:
            text, actions = msg
            try:
                tg.send(chat["id"], text, s["node"], actions)
                print(f"sent: {s['node']} {s['state']}")
            except (urllib.error.URLError, RuntimeError) as e:
                print(f"Telegram send failed: {e}")
        elif msg:
            print("state changed but no chat yet: send /start to the bot")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sagriot-telegram")
    if args.user:
        client.username_pw_set(args.user, args.password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port)
    client.loop_start()

    # -- Telegram side (long polling) ----------------------------------------
    print("bot running; send /start to it in Telegram")
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            updates = tg.call("getUpdates", **params)
        except (urllib.error.URLError, RuntimeError, TimeoutError) as e:
            print(f"getUpdates failed ({e}), retrying in 5 s")
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            if "message" in u:
                m = u["message"]
                text = (m.get("text") or "").strip()
                chat_id = m["chat"]["id"]
                if text.startswith("/start"):
                    if not chat["id"]:
                        save_chat(chat_id)
                    tg.send(chat_id, f"Merhaba! Bu sohbet saksı uyarılarını alacak.\nchat id: {chat_id}\n/durum ile son durumu görebilirsin.")
                elif text.startswith("/durum"):
                    with lock:
                        states = list(notifier.latest.values())
                    tg.send(chat_id, "\n\n".join(state_text(s) for s in states)
                            or "Henüz ikizden mesaj gelmedi.")
            elif "callback_query" in u:
                q = u["callback_query"]
                node, _, action = q.get("data", "").partition("|")
                with lock:
                    reply, publish, record = notifier.on_button(node, action)
                for topic, payload in publish:
                    client.publish(topic, payload, qos=1)
                if record:
                    feedback_path.parent.mkdir(parents=True, exist_ok=True)
                    with feedback_path.open("a") as f:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                tg.call("answerCallbackQuery", callback_query_id=q["id"])
                tg.send(q["message"]["chat"]["id"], reply)
                print(f"button: {node} {action}")


if __name__ == "__main__":
    main()
