"""Smart-Wallet-Suche (Scout) und Copy-Feed.

Scout     sammelt die Trades frischer Coins (GeckoTerminal) und rechnet für jede Wallet aus, wie gut ihre Käufe
          gelaufen sind. Wallets, die oft früh in Coins einsteigen, die danach steigen, werden beobachtet.
          Bots (sehr viele Trades in einem Coin), Kleinstbeträge und Wallets, die überall dabei sind, fliegen raus.
Copy-Feed fragt die beobachteten Wallets über den öffentlichen Solana-Zugang ab und meldet neue Käufe/Verkäufe.
"""
import logging
import time
from collections import defaultdict

from .. import config, market
from ..sources import SourceError, available, rpc

log = logging.getLogger("memebot.wallets")
IGNORE_MINTS = {market.SOL_MINT} | market.STABLES


class Scout:
    name = "scout"

    def __init__(self, store):
        self.s = store
        self.stats = {"coins_gesammelt": 0, "trades": 0, "wallets_bewertet": 0, "beobachtet": 0, "fehler": ""}

    def harvest(self, max_pools=3):
        """Trades frischer, aktiver Coins einsammeln (alle 15 min erneut, solange der Coin jung ist)."""
        now = time.time()
        rows = self.s.q("""SELECT mint, pool FROM tokens WHERE status='watch' AND (? - COALESCE(created_at, first_seen)) BETWEEN 180 AND 7200
                           AND (harvested_at IS NULL OR harvested_at < ?) ORDER BY hot DESC, updated DESC LIMIT ?""",
                        (now, now - 900, max_pools))
        n = 0
        for r in rows:
            if available("geckoterminal") < 4:   # Platz für Scanner und Charts lassen
                break
            try:
                trades = market.pool_trades(r["pool"])
            except SourceError as e:
                self.stats["fehler"] = str(e)
                continue
            self.s.x("UPDATE tokens SET harvested_at=? WHERE mint=?", (now, r["mint"]))
            rows_in = [(t["wallet"], r["mint"], t["ts"], t["kind"], t["usd"], t["price"], t["tx"], "gt")
                       for t in trades if t["wallet"] and t["price"] > 0 and t["ts"]]
            self.s.many("INSERT OR IGNORE INTO wallet_trades VALUES(?,?,?,?,?,?,?,?)", rows_in)
            self.stats["coins_gesammelt"] += 1
            self.stats["trades"] += len(rows_in)
            n += 1
        return n

    def score_wallets(self):
        """Ergebnis je Wallet und Coin -> Treffer/Fehlgriff -> Score. Die besten werden beobachtet."""
        now = time.time()
        trades = self.s.q("SELECT wallet, mint, ts, kind, usd, price FROM wallet_trades WHERE ts>?", (now - 7 * 86400,))
        prices = {r["mint"]: r["last_price"] for r in self.s.q("SELECT mint, last_price FROM tokens")}
        per = defaultdict(lambda: {"buy_usd": 0.0, "buy_qty": 0.0, "sell_usd": 0.0, "sell_qty": 0.0, "n": 0, "first": None})
        for t in trades:
            k = (t["wallet"], t["mint"])
            x = per[k]
            x["n"] += 1
            q = t["usd"] / t["price"] if t["price"] else 0
            if t["kind"] == "buy":
                x["buy_usd"] += t["usd"]
                x["buy_qty"] += q
                x["first"] = min(x["first"] or t["ts"], t["ts"])
            else:
                x["sell_usd"] += t["usd"]
                x["sell_qty"] += q
        mints_total = len({m for _, m in per})
        wallets = defaultdict(lambda: {"tokens": 0, "hits": 0, "misses": 0, "pnl": 0.0, "bot": False})
        presence = defaultdict(int)
        for (w, m), x in per.items():
            presence[w] += 1
            if x["n"] > 40:
                wallets[w]["bot"] = True
            if x["buy_usd"] < 10 or not x["first"] or now - x["first"] < 1800:
                continue  # Kleinstbetrag oder Ergebnis noch nicht reif
            unsold = max(0.0, x["buy_qty"] - x["sell_qty"])
            mark = prices.get(m) or 0
            value = x["sell_usd"] + unsold * mark
            mult = value / x["buy_usd"]
            w_ = wallets[w]
            w_["tokens"] += 1
            w_["pnl"] += value - x["buy_usd"]
            if mult >= config.WALLET_HIT_MULT:
                w_["hits"] += 1
            elif mult < 1:
                w_["misses"] += 1
        rows = []
        for w, x in wallets.items():
            everywhere = mints_total >= 10 and presence[w] / mints_total > 0.5
            if x["tokens"] == 0:
                continue
            sc = (x["hits"] + 1) / (x["tokens"] + 3)
            if x["pnl"] < 0:
                sc *= 0.7
            if x["bot"] or everywhere:
                sc = 0.0
            rows.append((w, x["tokens"], x["hits"], x["misses"], round(x["pnl"], 2), round(sc, 3), now, now,
                         "Bot/überall dabei" if (x["bot"] or everywhere) else None))
        self.s.many("""INSERT INTO wallets(address,tokens,hits,misses,pnl_usd,score,added_at,updated,note) VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(address) DO UPDATE SET tokens=excluded.tokens, hits=excluded.hits, misses=excluded.misses,
                       pnl_usd=excluded.pnl_usd, score=excluded.score, updated=excluded.updated, note=excluded.note""", rows)
        # Beobachtungsliste: die besten Wallets mit genug Coins
        best = self.s.q("SELECT address FROM wallets WHERE tokens>=? AND score>=? ORDER BY score DESC, pnl_usd DESC LIMIT ?",
                        (config.WALLET_MIN_TOKENS, config.WALLET_MIN_SCORE, config.WALLETS_TRACK_MAX))
        keep = [b["address"] for b in best]
        newly = self.s.q("SELECT address FROM wallets WHERE tracked=0 AND address IN (%s)" % (",".join("?" * len(keep)) or "NULL"), keep)
        self.s.x("UPDATE wallets SET tracked=0 WHERE tracked=1 AND address NOT IN (%s)" % (",".join("?" * len(keep)) or "NULL"), keep)
        if keep:
            self.s.x("UPDATE wallets SET tracked=1 WHERE address IN (%s)" % ",".join("?" * len(keep)), keep)
        for n in newly:
            self.s.decide("scout", "WALLET", "neue Smart Wallet wird beobachtet", data={"wallet": n["address"]})
        self.stats["wallets_bewertet"] = len(rows)
        self.stats["beobachtet"] = len(keep)
        return len(keep)


# ---------------------------------------------------------------- Transaktion lesen
def parse_swaps(tx, wallet):
    """Käufe/Verkäufe einer Wallet in einer Transaktion (jsonParsed). -> [{mint, kind, qty, sol}]"""
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return []
    meta = tx.get("meta") or {}
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
    try:
        idx = keys.index(wallet)
        sol = ((meta.get("postBalances") or [])[idx] - (meta.get("preBalances") or [])[idx]) / 1e9
    except (ValueError, IndexError, TypeError):
        sol = 0.0
    deltas = defaultdict(float)
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != wallet:
                continue
            amt = (b.get("uiTokenAmount") or {}).get("uiAmount") or 0
            deltas[b.get("mint")] += sign * float(amt)
    sol += deltas.pop(market.SOL_MINT, 0.0)   # Wrapped SOL zählt wie SOL
    out = []
    for mint, q in deltas.items():
        if mint in IGNORE_MINTS or abs(q) <= 0:
            continue
        if q > 0 and sol < 0:
            out.append({"mint": mint, "kind": "buy", "qty": q, "sol": -sol})
        elif q < 0 and sol > 0:
            out.append({"mint": mint, "kind": "sell", "qty": -q, "sol": sol})
    return out


class CopyFeed:
    name = "copyfeed"

    def __init__(self, store):
        self.s = store
        self.stats = {"abgefragt": 0, "kaeufe": 0, "verkaeufe": 0, "fehler": "", "letzte_runde": None}

    def poll(self):
        """-> Liste neuer Signale {wallet, mint, kind, price_usd, ts, sig, wallet_score}"""
        wallets = self.s.q("SELECT address, last_sig, score FROM wallets WHERE tracked=1 ORDER BY COALESCE(last_check,0) LIMIT ?",
                           (config.COPY_WALLETS_PER_ROUND,))
        signals = []
        now = time.time()
        for w in wallets:
            if available("solana") < 5:
                break
            try:
                sigs = rpc("getSignaturesForAddress", [w["address"], {"limit": 10}]) or []
            except SourceError as e:
                self.stats["fehler"] = str(e)
                break
            self.stats["abgefragt"] += 1
            newest = sigs[0]["signature"] if sigs else w["last_sig"]
            self.s.x("UPDATE wallets SET last_check=?, last_sig=? WHERE address=?", (now, newest, w["address"]))
            if not w["last_sig"]:
                continue  # erste Abfrage: nur merken, nichts Altes nachkaufen
            fresh = []
            for sg in sigs:
                if sg["signature"] == w["last_sig"]:
                    break
                if sg.get("err") is None:
                    fresh.append(sg)
            for sg in fresh[:5]:
                try:
                    tx = rpc("getTransaction", [sg["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
                except SourceError as e:
                    self.stats["fehler"] = str(e)
                    continue
                ts = sg.get("blockTime") or now
                for sw in parse_swaps(tx, w["address"]):
                    price_usd = sw["sol"] / sw["qty"] * market.sol_usd() if sw["qty"] else 0
                    self.s.x("INSERT OR IGNORE INTO wallet_trades VALUES(?,?,?,?,?,?,?,?)",
                             (w["address"], sw["mint"], ts, sw["kind"], sw["sol"] * market.sol_usd(), price_usd, sg["signature"], "rpc"))
                    self.stats["kaeufe" if sw["kind"] == "buy" else "verkaeufe"] += 1
                    signals.append({"wallet": w["address"], "mint": sw["mint"], "kind": sw["kind"], "price_usd": price_usd,
                                    "usd": sw["sol"] * market.sol_usd(), "ts": ts, "sig": sg["signature"],
                                    "wallet_score": w["score"] or 0})
        self.stats["letzte_runde"] = now
        return signals

    def cluster(self, mint, minutes=10):
        """Wie viele beobachtete Wallets haben diesen Coin in den letzten Minuten gekauft?"""
        return self.s.val("""SELECT COUNT(DISTINCT t.wallet) FROM wallet_trades t JOIN wallets w ON w.address=t.wallet
                             WHERE t.mint=? AND t.kind='buy' AND t.src='rpc' AND t.ts>? AND w.tracked=1""",
                          (mint, time.time() - minutes * 60), default=0)
