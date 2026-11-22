"""Memecoin-Bot (nur Spielgeld) – Webserver + Hintergrund-Schleife.

Start lokal:   python app.py
Railway:       gunicorn app:app (siehe railway.json) – die Schleife startet beim Import.
"""
import atexit
import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import signal
import threading
import time
from collections import defaultdict, deque

from flask import Flask, Response, jsonify, render_template, request

from memebot import config, market, notify, params, sources
from memebot.engine import Engine
from memebot.store import Store
from memebot.version import VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("memebot")
app = Flask(__name__)

# ---------------------------------------------------------------- Zugangsschutz (wie bei den anderen Bots)
FREIE_PFADE = {"/health", "/favicon.ico"}


def _abdruck(wert):
    return hashlib.sha256(wert.encode("utf-8")).digest()


_USER = os.environ.get("DASHBOARD_USER", "")
_PASS = os.environ.get("DASHBOARD_PASSWORD", "")
AUTH_AKTIV = bool(_USER and _PASS)
_USER_H, _PASS_H = _abdruck(_USER), _abdruck(_PASS)
if not AUTH_AKTIV:
    log.warning("UNGESCHUETZT: DASHBOARD_USER und DASHBOARD_PASSWORD sind nicht beide gesetzt.")
_FAILS = defaultdict(deque)


def _berechtigt(header):
    if not header:
        return False
    art, _, wert = header.partition(" ")
    if art.lower() != "basic" or not wert:
        return False
    try:
        klar = base64.b64decode(wert, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    benutzer, trenner, passwort = klar.partition(":")
    if not trenner:
        return False
    ok_u = secrets.compare_digest(_abdruck(benutzer), _USER_H)
    ok_p = secrets.compare_digest(_abdruck(passwort), _PASS_H)
    return ok_u & ok_p


@app.before_request
def _zugangsschutz():
    if request.path in FREIE_PFADE:
        return None
    if AUTH_AKTIV:
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")[:64]
        now, fails = time.time(), _FAILS[ip]
        while fails and now - fails[0] > 600:
            fails.popleft()
        if len(fails) >= 10:
            return Response("Zu viele Fehlversuche. Bitte in 10 Minuten erneut versuchen.", 429, mimetype="text/plain")
        if not _berechtigt(request.headers.get("Authorization", "")):
            if request.headers.get("Authorization"):
                fails.append(now)
            if len(_FAILS) > 5000:
                _FAILS.clear()
            return Response("Zugang nur mit Anmeldung.", 401, {"WWW-Authenticate": 'Basic realm="Memecoin-Bot", charset="UTF-8"'},
                            mimetype="text/plain")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Requested-With") != "memebot":
        return jsonify(error="Aufruf nur aus dem Dashboard möglich"), 403
    return None


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


@app.after_request
def _kopf(resp):
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/api/") or request.path == "/":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- Bot
store = Store(os.path.join(config.DATA_DIR, "memebot.sqlite"))
engine = Engine(store)
_stop = threading.Event()
_beat = {"thread": None, "restarts": 0}
_started = {"v": False}
_lock_file = {"f": None}


def _loop():
    time.sleep(2)
    store.decide("system", "INFO", f"Bot gestartet (Spielgeld, v{VERSION})")
    while not _stop.is_set():
        try:
            engine.cycle()
        except Exception as e:  # noqa: BLE001 – der Takt darf nie sterben
            log.exception("Takt abgebrochen")
            engine.state["last_error"] = sources.redact(f"{type(e).__name__}: {e}")[:300]
            store.event("hoch", "Takt mit Fehler abgebrochen", engine.state["last_error"])
            notify.send("hoch", "Takt mit Fehler abgebrochen", notify.esc(engine.state["last_error"]), key="takt-fehler")
        _stop.wait(config.TICK_SEC)


def _start_loop():
    t = threading.Thread(target=_loop, name="memebot-loop", daemon=True)
    _beat["thread"] = t
    t.start()


def _watchdog():
    alerted = False
    while not _stop.is_set():
        _stop.wait(60)
        t = _beat["thread"]
        if t is not None and not t.is_alive() and not _stop.is_set():
            _beat["restarts"] += 1
            store.event("kritisch", "Schleife war beendet – neu gestartet")
            _start_loop()
        last = engine.state.get("last_cycle") or engine.state["started"]
        age = time.time() - last
        if age > config.WATCHDOG_MIN * 60 and not alerted:
            alerted = True
            notify.send("kritisch", "Memecoin-Bot hängt", f"Seit {age / 60:.0f} min kein fertiger Takt.")
        elif age < 120 and alerted:
            alerted = False
            notify.send("hoch", "Memecoin-Bot läuft wieder")
        if age > 30 * 60:
            store.event("kritisch", "Takt hängt seit 30 min – Neustart")
            time.sleep(2)
            os.kill(os.getpid(), signal.SIGTERM)


def _single():
    try:
        import fcntl
        f = open(os.path.join(config.DATA_DIR, "loop.lock"), "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file["f"] = f
        return True
    except BlockingIOError:
        return False
    except Exception:  # noqa: BLE001
        return True


def _auto_start():
    if _started["v"] or not config.AUTOSTART:
        return
    _started["v"] = True
    if not _single():
        log.warning("Ein anderer Prozess fährt die Schleife bereits.")
        return
    atexit.register(_stop.set)
    _start_loop()
    threading.Thread(target=_watchdog, name="memebot-watchdog", daemon=True).start()


_auto_start()   # muss beim Import laufen, damit es auch unter Gunicorn startet


# ---------------------------------------------------------------- Hilfen
def _acc_view(a, with_params=False):
    st = engine.trader.account_stats(a)
    out = {"id": a["id"], "name": a["name"], "strategy": a["strategy"], "kind": a["kind"], "note": a.get("note"),
           "created": a["created"], "active": a["active"], **st}
    if with_params:
        out["params"] = a["params"] if isinstance(a["params"], dict) else json.loads(a["params"])
    return out


def _pos_view(p):
    pnl = (p["proceeds"] or 0) + (p["value"] or 0) - p["cost"] if p["status"] != "cancelled" else 0
    return {"id": p["id"], "account_id": p["account_id"], "symbol": p["symbol"], "mint": p["mint"], "pool": p["pool"],
            "dex": p["dex"], "status": p["status"], "created": p["created"], "opened_at": p["opened_at"], "closed_at": p["closed_at"],
            "cost": round(p["cost"], 2), "value": round(p["value"] or 0, 2), "proceeds": round(p["proceeds"] or 0, 2),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl / p["cost"] * 100, 1) if p["cost"] else 0,
            "entry": p["market_entry"], "last": p["last_price"], "peak": p["peak_price"], "score": p["score"],
            "reason_in": p["reason_in"], "reason_out": p["reason_out"], "tp1_done": p["tp1_done"]}


def _hours():
    try:
        return max(1, min(24 * 60, int(request.args.get("hours", 48))))
    except ValueError:
        return 48


# ---------------------------------------------------------------- Seiten + API
@app.get("/health")
def health():
    last = engine.state.get("last_cycle")
    return jsonify(ok=True, version=VERSION, last_cycle_age=round(time.time() - last) if last else None)


@app.get("/")
def index():
    return render_template("index.html", version=VERSION)


@app.get("/api/overview")
def api_overview():
    accs = engine.trader.accounts()
    names = {a["id"]: a["name"] for a in accs}
    open_pos = [dict(_pos_view(p), konto=names.get(p["account_id"], "?"))
                for p in store.q("SELECT * FROM positions WHERE status IN ('pending','open') ORDER BY created DESC")]
    activity = store.q("""SELECT d.*, a.name konto, a.kind FROM decisions d LEFT JOIN accounts a ON a.id=d.account_id
                          WHERE d.action IN ('ENTER','EXIT','PARTIAL','CANCEL') AND a.kind='main' ORDER BY d.ts DESC LIMIT 20""")
    last_closed = store.one("""SELECT p.*, a.name konto FROM positions p JOIN accounts a ON a.id=p.account_id
                               WHERE p.status='closed' AND a.kind='main' ORDER BY p.closed_at DESC LIMIT 1""")
    why = None
    if last_closed:
        why = dict(_pos_view(last_closed), konto=last_closed["konto"], signal=json.loads(last_closed["signal"] or "null"),
                   fills=store.q("SELECT * FROM fills WHERE position_id=? ORDER BY ts", (last_closed["id"],)))
    hot = store.q("""SELECT symbol, mint, metrics FROM tokens WHERE status='watch' AND hot=1 ORDER BY updated DESC LIMIT 40""")
    ticker = []
    for h in hot:
        m = json.loads(h["metrics"] or "{}")
        ticker.append({"symbol": h["symbol"], "mint": h["mint"], "ch_m5": m.get("ch_m5", 0), "mcap": m.get("mcap", 0)})
    ticker.sort(key=lambda x: -abs(x["ch_m5"] or 0))
    return jsonify(
        version=VERSION, now=time.time(), state=engine.state, sol_usd=market.sol_usd(),
        accounts=[_acc_view(a) for a in accs], agents=engine.agents_status(), skips=engine.skips.most_common(8),
        open=open_pos, activity=activity, why=why, ticker=ticker[:20],
        proposals=store.val("SELECT COUNT(*) FROM proposals WHERE status='offen'", default=0),
        telegram=notify.state["status"], auth=AUTH_AKTIV)


@app.get("/api/equity")
def api_equity():
    since = time.time() - _hours() * 3600
    out = {}
    for a in engine.trader.accounts():
        rows = store.q("SELECT ts, equity FROM equity WHERE account_id=? AND ts>=? ORDER BY ts", (a["id"], since))
        pts = [[r["ts"], r["equity"]] for r in rows]
        pts.append([time.time(), round(engine.trader.equity(a), 2)])
        out[a["id"]] = {"name": a["name"], "kind": a["kind"], "strategy": a["strategy"], "start": a["start_balance"], "points": pts}
    return jsonify(out)


@app.get("/api/trades")
def api_trades():
    acc = request.args.get("account", type=int)
    status = request.args.get("status", "")
    sql = "SELECT p.*, a.name konto, a.kind FROM positions p JOIN accounts a ON a.id=p.account_id WHERE 1=1"
    args = []
    if acc:
        sql += " AND p.account_id=?"
        args.append(acc)
    if status in ("open", "closed", "cancelled", "pending"):
        sql += " AND p.status=?"
        args.append(status)
    rows = store.q(sql + " ORDER BY p.created DESC LIMIT 300", args)
    return jsonify([dict(_pos_view(r), konto=r["konto"], kind=r["kind"]) for r in rows])


_chart_cache = {}


@app.get("/api/trade/<int:pid>")
def api_trade(pid):
    p = store.one("SELECT p.*, a.name konto FROM positions p JOIN accounts a ON a.id=p.account_id WHERE p.id=?", (pid,))
    if not p:
        return jsonify(error="nicht gefunden"), 404
    fills = store.q("SELECT * FROM fills WHERE position_id=? ORDER BY ts", (pid,))
    decisions = store.q("SELECT * FROM decisions WHERE mint=? AND ts BETWEEN ? AND ? ORDER BY ts",
                        (p["mint"], p["created"] - 600, (p["closed_at"] or time.time()) + 60))
    return jsonify(dict(_pos_view(p), konto=p["konto"], signal=json.loads(p["signal"] or "null"), fills=fills, decisions=decisions))


@app.get("/api/chart/<int:pid>")
def api_chart(pid):
    """Minuten-Kerzen rund um den Trade (GeckoTerminal), 2 min zwischengespeichert."""
    p = store.one("SELECT pool, created, closed_at FROM positions WHERE id=?", (pid,))
    if not p:
        return jsonify(error="nicht gefunden"), 404
    c = _chart_cache.get(pid)
    if c and time.time() - c[0] < 120:
        return jsonify(c[1])
    try:
        candles = market.ohlcv(p["pool"], 600)
    except sources.SourceError as e:
        return jsonify(error=str(e), candles=[])
    data = {"candles": candles}
    _chart_cache[pid] = (time.time(), data)
    if len(_chart_cache) > 200:
        _chart_cache.clear()
    return jsonify(data)


@app.get("/api/log")
def api_log():
    sql = "SELECT d.*, a.name konto FROM decisions d LEFT JOIN accounts a ON a.id=d.account_id WHERE 1=1"
    args = []
    for key, col in (("agent", "d.agent"), ("action", "d.action")):
        v = request.args.get(key)
        if v:
            sql += f" AND {col}=?"
            args.append(v)
    acc = request.args.get("account", type=int)
    if acc:
        sql += " AND d.account_id=?"
        args.append(acc)
    q = (request.args.get("q") or "").strip()
    if q:
        sql += " AND (d.symbol LIKE ? OR d.reason LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    return jsonify(store.q(sql + " ORDER BY d.ts DESC LIMIT 400", args))


@app.get("/api/wallets")
def api_wallets():
    rows = store.q("SELECT * FROM wallets ORDER BY tracked DESC, score DESC, pnl_usd DESC LIMIT 200")
    recent = store.q("""SELECT t.*, k.symbol FROM wallet_trades t LEFT JOIN tokens k ON k.mint=t.mint
                        WHERE t.src='rpc' ORDER BY t.ts DESC LIMIT 60""")
    return jsonify(wallets=rows, recent=recent, scout=engine.scout.stats, feed=engine.copy.stats,
                   min_tokens=config.WALLET_MIN_TOKENS, min_score=config.WALLET_MIN_SCORE)


@app.get("/api/learning")
def api_learning():
    accs = store.q("SELECT * FROM accounts ORDER BY strategy DESC, kind='main' DESC, active DESC, id")
    out = []
    for a in accs:
        a["params"] = json.loads(a["params"])
        out.append(_acc_view(a, with_params=True) | {"parent": a["parent"], "generation": a["generation"]})
    props = store.q("SELECT * FROM proposals ORDER BY ts DESC LIMIT 50")
    for p in props:
        p["changes"] = json.loads(p["changes"])
        p["stats"] = json.loads(p["stats"])
    return jsonify(accounts=out, proposals=props, labels=params.LABELS, learner=engine.learner.stats,
                   min_trades=config.LEARN_MIN_TRADES, min_edge=config.LEARN_MIN_EDGE_PCT)


@app.post("/api/proposal/<int:pid>")
def api_proposal(pid):
    body = request.get_json(silent=True) or {}
    ok, msg = engine.learner.decide_proposal(pid, bool(body.get("accept")))
    return jsonify(ok=ok, message=msg), (200 if ok else 400)


@app.get("/api/system")
def api_system():
    return jsonify(sources=sources.status(), state=engine.state, telegram=notify.state, auth=AUTH_AKTIV,
                   events=store.q("SELECT * FROM events ORDER BY ts DESC LIMIT 30"),
                   watch=store.val("SELECT COUNT(*) FROM tokens WHERE status='watch'", default=0),
                   hot=store.val("SELECT COUNT(*) FROM tokens WHERE status='watch' AND hot=1", default=0),
                   sim={"gebuehr_pct": config.FEE_PCT, "tx_kosten": config.TX_COST_USD, "slip_zuschlag_pct": config.EXTRA_SLIP_PCT,
                        "verzoegerung_s": config.FILL_DELAY_SEC}, version=VERSION)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
