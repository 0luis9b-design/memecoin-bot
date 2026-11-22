"""SQLite-Speicher. Liegt auf Railway im Volume (DATA_DIR), damit nach einem Neustart nichts verloren geht."""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY, name TEXT, strategy TEXT, kind TEXT, params TEXT, start_balance REAL, cash REAL,
  created REAL, active INTEGER DEFAULT 1, parent INTEGER, note TEXT, generation INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens (
  mint TEXT PRIMARY KEY, pool TEXT, symbol TEXT, name TEXT, dex TEXT, created_at REAL, first_seen REAL,
  source TEXT, status TEXT DEFAULT 'watch', metrics TEXT, updated REAL, first_price REAL, max_price REAL,
  min_price REAL, last_price REAL, rug TEXT, rug_at REAL, harvested_at REAL, missing_since REAL, hot INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS tokens_status ON tokens(status);
CREATE TABLE IF NOT EXISTS snapshots (mint TEXT, ts REAL, price REAL, mcap REAL, liq REAL, vol_m5 REAL, buys_m5 INTEGER, sells_m5 INTEGER);
CREATE INDEX IF NOT EXISTS snap_mint_ts ON snapshots(mint, ts);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY, account_id INTEGER, mint TEXT, pool TEXT, symbol TEXT, dex TEXT, status TEXT,
  created REAL, opened_at REAL, closed_at REAL, budget REAL, cost REAL, qty REAL, qty_left REAL, entry_price REAL,
  market_entry REAL, peak_price REAL, last_price REAL, value REAL, proceeds REAL DEFAULT 0, tp1_done INTEGER DEFAULT 0,
  score REAL, reason_in TEXT, reason_out TEXT, signal TEXT, entry_liq REAL);
CREATE INDEX IF NOT EXISTS pos_acc ON positions(account_id, status);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY, position_id INTEGER, ts REAL, side TEXT, market_price REAL, eff_price REAL, qty REAL,
  usd REAL, fee REAL, slip_pct REAL, note TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, ts REAL, agent TEXT, account_id INTEGER, action TEXT, mint TEXT, symbol TEXT,
  reason TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS dec_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS wallet_trades (
  wallet TEXT, mint TEXT, ts REAL, kind TEXT, usd REAL, price REAL, tx TEXT, src TEXT, UNIQUE(tx, wallet, kind, mint));
CREATE INDEX IF NOT EXISTS wt_wallet ON wallet_trades(wallet);
CREATE INDEX IF NOT EXISTS wt_mint ON wallet_trades(mint);
CREATE TABLE IF NOT EXISTS wallets (
  address TEXT PRIMARY KEY, tokens INTEGER, hits INTEGER, misses INTEGER, pnl_usd REAL, score REAL,
  tracked INTEGER DEFAULT 0, last_sig TEXT, last_check REAL, added_at REAL, updated REAL, copied INTEGER DEFAULT 0, note TEXT);
CREATE TABLE IF NOT EXISTS equity (account_id INTEGER, ts REAL, equity REAL);
CREATE INDEX IF NOT EXISTS eq_acc ON equity(account_id, ts);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, ts REAL, account_id INTEGER, variant_id INTEGER, changes TEXT, stats TEXT,
  status TEXT DEFAULT 'offen', decided REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, level TEXT, title TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS launches (
  mint TEXT PRIMARY KEY, curve TEXT, creator TEXT, ts REAL, symbol TEXT, name TEXT, initial_sol REAL,
  mcap_sol REAL, checks INTEGER DEFAULT 0, checked_at REAL, status TEXT DEFAULT 'neu');
CREATE INDEX IF NOT EXISTS launch_creator ON launches(creator, ts);
CREATE INDEX IF NOT EXISTS launch_status ON launches(status, ts);
CREATE TABLE IF NOT EXISTS shadow (
  id INTEGER PRIMARY KEY, mint TEXT, symbol TEXT, strategy TEXT, rule TEXT, reason TEXT, ts REAL, price REAL,
  liq REAL, max_price REAL, min_price REAL, last_price REAL, last_liq REAL, updated REAL, parts TEXT,
  done INTEGER DEFAULT 0, max_gain REAL, final REAL, outcome TEXT);
CREATE INDEX IF NOT EXISTS shadow_open ON shadow(done, mint);
CREATE INDEX IF NOT EXISTS shadow_rule ON shadow(rule, done);
"""


class Store:
    def __init__(self, path):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
            self.db.commit()

    # --- Grundfunktionen
    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def many(self, sql, rows):
        with self.lock:
            self.db.executemany(sql, rows)
            self.db.commit()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            r = self.db.execute(sql, args).fetchone()
            return dict(r) if r else None

    def val(self, sql, args=(), default=None):
        with self.lock:
            r = self.db.execute(sql, args).fetchone()
            return r[0] if r and r[0] is not None else default

    # --- Schlüssel/Wert
    def kv_get(self, k, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        if not r:
            return default
        try:
            return json.loads(r["v"])
        except ValueError:
            return r["v"]

    def kv_set(self, k, v):
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    # --- Entscheidungs-Log („log first“): jede Entscheidung mit Begründung
    def decide(self, agent, action, reason, account_id=None, mint=None, symbol=None, data=None):
        self.x("INSERT INTO decisions(ts,agent,account_id,action,mint,symbol,reason,data) VALUES(?,?,?,?,?,?,?,?)",
               (time.time(), agent, account_id, action, mint, symbol, reason[:500],
                json.dumps(data, default=str)[:4000] if data is not None else None))

    def event(self, level, title, body=""):
        self.x("INSERT INTO events(ts,level,title,body) VALUES(?,?,?,?)", (time.time(), level, title[:200], body[:2000]))

    def cleanup(self, snapshot_keep_h):
        cut = time.time() - snapshot_keep_h * 3600
        self.x("DELETE FROM snapshots WHERE ts<?", (cut,))
        self.x("DELETE FROM decisions WHERE ts<? AND action IN ('SKIP','BLOCK')", (time.time() - 3 * 86400,))
        self.x("DELETE FROM decisions WHERE ts<?", (time.time() - 30 * 86400,))
        self.x("DELETE FROM tokens WHERE status='dead' AND updated<? AND mint NOT IN (SELECT mint FROM positions)",
               (time.time() - 3 * 86400,))
        self.x("DELETE FROM wallet_trades WHERE ts<?", (time.time() - 14 * 86400,))
        self.x("DELETE FROM events WHERE ts<?", (time.time() - 14 * 86400,))
        self.x("DELETE FROM launches WHERE ts<?", (time.time() - 3 * 86400,))
        self.x("DELETE FROM shadow WHERE ts<?", (time.time() - 30 * 86400,))
