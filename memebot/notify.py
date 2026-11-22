"""Telegram-Meldungen. Token und Chat-ID stehen nur in Umgebungsvariablen und werden nie geloggt.

Stufen: info < normal < hoch < kritisch
  normal   Käufe/Verkäufe der Hauptkonten, Tagesbericht – nicht in der Ruhezeit, max. NOTIFY_DAILY_CAP am Tag
  hoch     Vorschläge des Lern-Agenten, Quellen-Ausfälle – sofort, gleiches Thema max. alle 6 h
  kritisch Bot hängt – immer sofort
"""
import html
import logging
import queue
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config
from .sources import SESSION, redact

log = logging.getLogger("memebot.notify")
BERLIN = ZoneInfo("Europe/Berlin")
LEVELS = {"info": 0, "normal": 1, "hoch": 2, "kritisch": 3}
_lock = threading.Lock()
_keys = {}
_day = {"day": "", "n": 0}
_q = queue.Queue(maxsize=200)
state = {"status": "aus – TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID nicht gesetzt", "gesendet": 0, "unterdrueckt": 0}
_worker = {"t": None}


def enabled():
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def esc(s):
    return html.escape(str(s), quote=False)


def _quiet(now):
    a, b = config.QUIET_FROM, config.QUIET_TO
    if a == b:
        return False
    return (a <= now.hour < b) if a < b else (now.hour >= a or now.hour < b)


def decide(level, key=None, now=None):
    now = now or datetime.now(BERLIN)
    lv = LEVELS.get(level, 1)
    if lv < LEVELS.get(config.NOTIFY_MIN_LEVEL, 1):
        return False, "unter Mindeststufe"
    with _lock:
        if key and lv < 3 and time.time() - _keys.get(key, 0) < 6 * 3600:
            return False, "schon gemeldet"
        if lv == 1:
            if _quiet(now):
                return False, "Ruhezeit"
            d = now.strftime("%Y-%m-%d")
            if _day["day"] != d:
                _day.update(day=d, n=0)
            if _day["n"] >= config.NOTIFY_DAILY_CAP:
                return False, "Tageslimit"
            _day["n"] += 1
        if key:
            _keys[key] = time.time()
    return True, ""


def _send(text):
    try:
        r = SESSION.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                         json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:3900], "parse_mode": "HTML",
                               "disable_web_page_preview": True}, timeout=config.HTTP_TIMEOUT)
        if r.status_code >= 400:
            state["status"] = f"Fehler HTTP {r.status_code}"
            log.warning("Telegram: HTTP %s", r.status_code)
            return False
        state["status"] = "OK"
        state["gesendet"] += 1
        return True
    except Exception as e:  # noqa: BLE001
        state["status"] = redact(f"Fehler: {type(e).__name__}")
        return False


def _run():
    while True:
        text = _q.get()
        _send(text)
        time.sleep(1.1)  # Telegram mag keine Salven


def send(level, title, body="", key=None):
    """Meldung in die Warteschlange (blockiert die Schleife nie)."""
    if not enabled():
        return False
    ok, why = decide(level, key)
    if not ok:
        state["unterdrueckt"] += 1
        return False
    if _worker["t"] is None:
        _worker["t"] = threading.Thread(target=_run, name="telegram", daemon=True)
        _worker["t"].start()
    text = f"{config.NOTIFY_PREFIX}<b>{esc(title)}</b>" + (f"\n{body}" if body else "")
    try:
        _q.put_nowait(text)
    except queue.Full:
        state["unterdrueckt"] += 1
        return False
    return True
