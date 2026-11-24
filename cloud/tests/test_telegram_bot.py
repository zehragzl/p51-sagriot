"""Tests for the Telegram bot logic (no network)."""
import json
import sys
from pathlib import Path

CLOUD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLOUD))

from telegram_bot import Notifier, SNOOZE_S, state_text   # noqa: E402


def twin(state, seq=0, node="node01", what_if=None, reasons=()):
    labels = {"happy": ("😊", "mutlu"), "thirsty": ("😟", "susamış"), "fault": ("⚠️", "arıza")}
    emoji, label = labels[state]
    return {"node": node, "seq": seq, "state": state, "emoji": emoji, "label": label,
            "reasons": list(reasons), "vwc": 40.0, "soil_fc": 61.6, "air_t": 24.0, "rh": 55.0,
            "decision": {"now": state == "thirsty", "warn": False},
            "what_if": what_if, "model": {"samples": 300, "needed": 288}}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_messages_only_on_state_changes():
    n = Notifier()
    assert n.on_twin(twin("happy", 0)) is None                   # start: no message
    text, actions = n.on_twin(twin("thirsty", 1))
    assert "susadı" in text and actions == ["watered", "snooze", "not_needed"]
    assert n.on_twin(twin("thirsty", 2)) is None                 # same state: quiet
    text, actions = n.on_twin(twin("happy", 3))
    assert "toparlandı" in text and actions == []


def test_fault_message_has_the_check_button_and_reasons():
    n = Notifier()
    n.on_twin(twin("happy"))
    text, actions = n.on_twin(twin("fault", reasons=["VWC ≈ 0: prob toprakta değil"]))
    assert actions == ["checked", "snooze"] and "prob toprakta değil" in text
    assert n.on_twin(twin("happy")) is None                      # fault -> happy: quiet


def test_snooze_reminds_after_an_hour_if_still_thirsty():
    clock = Clock()
    n = Notifier(clock)
    n.on_twin(twin("happy"))
    n.on_twin(twin("thirsty"))
    reply, publish, record = n.on_button("node01", "snooze")
    assert record["action"] == "snooze" and publish[0][0] == "sagriot/node01/feedback"
    clock.t += SNOOZE_S - 1
    assert n.on_twin(twin("thirsty")) is None
    clock.t += 2
    text, _ = n.on_twin(twin("thirsty"))
    assert text.startswith("⏰ Hatırlatma")
    assert n.on_twin(twin("thirsty")) is None                    # only once


def test_snooze_is_cancelled_when_the_pot_recovers():
    clock = Clock()
    n = Notifier(clock)
    n.on_twin(twin("thirsty"))
    n.on_button("node01", "snooze")
    n.on_twin(twin("happy"))
    clock.t += 2 * SNOOZE_S
    text, _ = n.on_twin(twin("thirsty"))
    assert not text.startswith("⏰")                              # a new alert, not a reminder


def test_checked_button_clears_the_twin_fault():
    n = Notifier()
    n.on_twin(twin("fault", seq=9))
    reply, publish, record = n.on_button("node01", "checked")
    topics = [t for t, _ in publish]
    assert topics == ["sagriot/node01/feedback", "sagriot/node01/twin_ack"]
    assert record["state"] == "fault" and record["seq"] == 9
    json.loads(publish[0][1])                                    # valid JSON


def test_feedback_records_what_the_farmer_saw():
    n = Notifier()
    n.on_twin(twin("thirsty", seq=42))
    _, _, record = n.on_button("node01", "not_needed")
    assert record["action"] == "not_needed" and record["decision"]["now"] is True
    assert record["vwc"] == 40.0 and "time" in record


def test_unknown_button_does_nothing():
    reply, publish, record = Notifier().on_button("node01", "dance")
    assert publish == [] and record is None


def test_state_text_with_and_without_what_if():
    text = state_text(twin("thirsty"))
    assert "Tahmin için veri birikiyor (300/288)" in text
    wi = {"no_water": {"warn_in_min": 0, "crit_in_min": 90},
          "water_now": {"vwc_after": 64.9, "warn_in_min": 1990}}
    text = state_text(twin("thirsty", what_if=wi))
    assert "Sulamazsam: %70 eşiğine şu an" in text
    assert "Şimdi sularsam: eşiğe yeniden yaklaşık 1.4 gün sonra" in text
    wi["no_water"]["warn_in_min"] = 125
    assert "2 sa 5 dk sonra" in state_text(twin("happy", what_if=wi))


def test_watered_cancels_the_reminder_and_confirmation_is_announced():
    clock = Clock()
    n = Notifier(clock)
    n.on_twin(twin("happy"))
    n.on_twin(twin("thirsty"))
    n.on_button("node01", "snooze")
    reply, _, _ = n.on_button("node01", "watered")
    assert "ikize bildirdim" in reply and "node01" not in n.remind_at
    confirmed = {**twin("happy"), "watering": {"status": "confirmed"}}
    text, _ = n.on_twin(confirmed)
    assert text.startswith("✅ Sulama doğrulandı")
