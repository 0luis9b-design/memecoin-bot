"""Abrufe der kostenlosen Datenquellen – jede mit eigener Ratenbegrenzung und Statistik fürs Dashboard.

GeckoTerminal  neue Pools, Trending, Trades (Wallet-Suche), Minuten-Kerzen     30 Abrufe/min
DexScreener    Kurse, Volumen, Käufe/Verkäufe, Liquidität (bis 30 Paare je Abruf)  300/min
RugCheck       Sicherheitsbericht je Coin                                        sparsam
Solana-RPC     Transaktionen der beobachteten Wallets (Copy-Trading)             öffentlicher Zugang
"""
import re
import threading
import time
from collections import deque

import requests

from . import config

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "memecoin-paper-bot/1.0", "Accept": "application/json"})

LIMITS = {  # Abrufe pro Minute (mit Sicherheitsabstand zur offiziellen Grenze)
    "geckoterminal": 8,    # offiziell 30, auf Railway-IPs gab es aber schon ab ~10 Abrufen/min 429
    "dexscreener": 240,
    "rugcheck": config.RUGCHECK_PER_MIN,
    "solana": 50,          # öffentlicher Zugang: bei 90/min gab es ~16 % 429
}
_lock = threading.Lock()
_calls = {k: deque() for k in LIMITS}
_paused = {k: 0.0 for k in LIMITS}
STATS = {k: {"ok": 0, "fehler": 0, "letzter_fehler": "", "letzter_ok": None, "ms": 0.0} for k in LIMITS}


class SourceError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(redact(msg))
        self.status = status


def redact(text):
    return re.sub(r"(api[-_]?key=|bot)[A-Za-z0-9:_\-]{8,}", r"\1***", str(text))


def source_for(url):
    if "geckoterminal" in url:
        return "geckoterminal"
    if "dexscreener" in url:
        return "dexscreener"
    if "rugcheck" in url:
        return "rugcheck"
    return "solana"


def available(src):
    """Wie viele Abrufe sind in dieser Minute noch frei?"""
    with _lock:
        now = time.time()
        q = _calls[src]
        while q and now - q[0] > 60:
            q.popleft()
        if now < _paused[src]:
            return 0
        return max(0, LIMITS[src] - len(q))


def _wait_slot(src, max_wait=20):
    t0 = time.time()
    while True:
        with _lock:
            now = time.time()
            q = _calls[src]
            while q and now - q[0] > 60:
                q.popleft()
            if now >= _paused[src] and len(q) < LIMITS[src]:
                q.append(now)
                return
            wait = max(_paused[src] - now, (60 - (now - q[0])) if q else 1, 0.2)
        if time.time() - t0 + wait > max_wait:
            raise SourceError(f"{src}: Ratenlimit erreicht – Abruf übersprungen")
        time.sleep(min(wait, 2))


def _done(src, ok, ms, err=""):
    s = STATS[src]
    s["ms"] = round(0.8 * s["ms"] + 0.2 * ms, 1) if s["ms"] else round(ms, 1)
    if ok:
        s["ok"] += 1
        s["letzter_ok"] = time.time()
    else:
        s["fehler"] += 1
        s["letzter_fehler"] = redact(err)[:200]


def get_json(url, params=None, retries=1, max_wait=20):
    src = source_for(url)
    last = None
    for attempt in range(retries + 1):
        _wait_slot(src, max_wait)
        t0 = time.time()
        try:
            r = SESSION.get(url, params=params, timeout=config.HTTP_TIMEOUT)
            ms = (time.time() - t0) * 1000
            if r.status_code == 429:
                with _lock:
                    _paused[src] = time.time() + 60
                _done(src, False, ms, "HTTP 429 – Quelle 60 s pausiert")
                raise SourceError(f"{src}: zu viele Abrufe (429)", 429)
            if r.status_code >= 400:
                _done(src, False, ms, f"HTTP {r.status_code}")
                last = SourceError(f"{src}: HTTP {r.status_code}", r.status_code)
                if r.status_code < 500:
                    raise last
            else:
                data = r.json()
                _done(src, True, ms)
                return data
        except SourceError:
            raise
        except (requests.RequestException, ValueError) as e:
            _done(src, False, (time.time() - t0) * 1000, type(e).__name__)
            last = SourceError(f"{src}: {type(e).__name__}")
        if attempt < retries:
            time.sleep(1.5)
    raise last


def rpc(method, params, max_wait=10):
    src = "solana"
    _wait_slot(src, max_wait)
    t0 = time.time()
    try:
        r = SESSION.post(config.SOLANA_RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                         timeout=config.HTTP_TIMEOUT)
        ms = (time.time() - t0) * 1000
        if r.status_code == 429:
            with _lock:
                _paused[src] = time.time() + 30
            _done(src, False, ms, "HTTP 429 – RPC 30 s pausiert")
            raise SourceError("solana: zu viele Abrufe (429)", 429)
        if r.status_code >= 400:
            _done(src, False, ms, f"HTTP {r.status_code}")
            raise SourceError(f"solana: HTTP {r.status_code}", r.status_code)
        data = r.json()
        if data.get("error"):
            _done(src, False, ms, str(data["error"])[:150])
            raise SourceError(f"solana: {data['error']}")
        _done(src, True, ms)
        return data.get("result")
    except (requests.RequestException, ValueError) as e:
        _done(src, False, (time.time() - t0) * 1000, type(e).__name__)
        raise SourceError(f"solana: {type(e).__name__}") from None


def status():
    out = {}
    for k in LIMITS:
        s = dict(STATS[k])
        s["frei_pro_min"] = available(k)
        s["limit_pro_min"] = LIMITS[k]
        out[k] = s
    return out
