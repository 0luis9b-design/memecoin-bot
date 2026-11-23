"""Marktdaten lesen und in ein einheitliches Format bringen."""
import math
import time
from datetime import datetime

from . import config
from .sources import SourceError, get_json

GT = "https://api.geckoterminal.com/api/v2/networks/solana"
DS = "https://api.dexscreener.com"
RC = "https://api.rugcheck.xyz/v1/tokens"
SOL_MINT = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
_sol = {"usd": None, "ts": 0}


def f(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def iso_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def sol_usd():
    return _sol["usd"] or config.SOL_FALLBACK_USD


def set_sol(v):
    if v and 5 < v < 5000:
        _sol["usd"], _sol["ts"] = v, time.time()


# ---------------------------------------------------------------- GeckoTerminal
def parse_gt_pools(data):
    """GeckoTerminal-Poolliste -> Liste von Dicts (nur Paare gegen SOL)."""
    out = []
    for p in (data or {}).get("data") or []:
        a = p.get("attributes") or {}
        rel = p.get("relationships") or {}
        base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
        quote = ((rel.get("quote_token") or {}).get("data") or {}).get("id", "")
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
        base, quote = base.replace("solana_", ""), quote.replace("solana_", "")
        if quote != SOL_MINT or not base or base in STABLES:
            continue
        set_sol(f(a.get("quote_token_price_usd")))
        tx5 = (a.get("transactions") or {}).get("m5") or {}
        name = a.get("name") or ""
        out.append({
            "pool": a.get("address"), "mint": base, "symbol": name.split(" / ")[0].strip()[:24] or "?", "dex": dex,
            "created_at": iso_ts(a.get("pool_created_at")), "price": f(a.get("base_token_price_usd")),
            "reserve": f(a.get("reserve_in_usd")), "fdv": f(a.get("fdv_usd")),
            "vol_m5": f((a.get("volume_usd") or {}).get("m5")), "buys_m5": int(f(tx5.get("buys"))),
            "sells_m5": int(f(tx5.get("sells"))), "buyers_m5": int(f(tx5.get("buyers"))),
        })
    return out


def new_pools(page=1):
    return parse_gt_pools(get_json(f"{GT}/new_pools", {"page": page}))


def trending_pools():
    return parse_gt_pools(get_json(f"{GT}/trending_pools", {"duration": "5m"}))


def pool_trades(pool):
    """Letzte Trades eines Pools (bis 300, 24 h) – Grundlage der Wallet-Suche."""
    data = get_json(f"{GT}/pools/{pool}/trades", max_wait=5)
    out = []
    for t in (data or {}).get("data") or []:
        a = t.get("attributes") or {}
        kind = a.get("kind")
        if kind not in ("buy", "sell") or a.get("is_anomalous"):
            continue
        token_price = f(a.get("price_to_in_usd") if kind == "buy" else a.get("price_from_in_usd"))
        out.append({"wallet": a.get("tx_from_address"), "kind": kind, "usd": f(a.get("volume_in_usd")),
                    "price": token_price, "ts": iso_ts(a.get("block_timestamp")), "tx": a.get("tx_hash")})
    return out


def ohlcv(pool, minutes=180):
    data = get_json(f"{GT}/pools/{pool}/ohlcv/minute", {"aggregate": 1, "limit": min(1000, minutes), "currency": "usd"},
                    max_wait=5)
    rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted([[int(r[0]), f(r[1]), f(r[2]), f(r[3]), f(r[4]), f(r[5])] for r in rows if len(r) >= 6])


# ---------------------------------------------------------------- DexScreener
def quote_reserve_usd(pair):
    """Wie viel $ steckt auf der SOL-Seite des Pools? Daraus ergibt sich, wie stark ein Kauf den Kurs bewegt.

    Pump.fun-Kurve: virtuelle Reserven, k = 30 SOL * 1,073 Mrd. Token  ->  SOL-Reserve = sqrt(k * Preis in SOL).
    Andere Pools: die Hälfte der Liquidität.
    """
    dex = pair.get("dex") or ""
    if dex in ("pumpfun", "pump-fun") and pair.get("price_native"):
        return math.sqrt(30 * 1.073e9 * pair["price_native"]) * sol_usd()
    return max(0.0, pair.get("liq", 0) / 2)


def parse_ds_pair(p):
    base, quote = p.get("baseToken") or {}, p.get("quoteToken") or {}
    tx = p.get("txns") or {}
    vol, ch = p.get("volume") or {}, p.get("priceChange") or {}
    price, native = f(p.get("priceUsd")), f(p.get("priceNative"))
    if quote.get("address") == SOL_MINT and price and native:
        set_sol(price / native)
    out = {
        "pool": p.get("pairAddress"), "mint": base.get("address"), "symbol": (base.get("symbol") or "?")[:24],
        "name": (base.get("name") or "")[:60], "dex": p.get("dexId") or "", "quote": quote.get("address"),
        "price": price, "price_native": native, "liq": f((p.get("liquidity") or {}).get("usd")),
        "mcap": f(p.get("marketCap") or p.get("fdv")),
        "created_at": (f(p.get("pairCreatedAt")) / 1000) or None,
        "vol_m5": f(vol.get("m5")), "vol_h1": f(vol.get("h1")), "vol_h6": f(vol.get("h6")),
        "buys_m5": int(f((tx.get("m5") or {}).get("buys"))), "sells_m5": int(f((tx.get("m5") or {}).get("sells"))),
        "buys_h1": int(f((tx.get("h1") or {}).get("buys"))), "sells_h1": int(f((tx.get("h1") or {}).get("sells"))),
        "ch_m5": f(ch.get("m5")), "ch_h1": f(ch.get("h1")), "ch_h6": f(ch.get("h6")),
        "boosts": int(f((p.get("boosts") or {}).get("active"))),
        "socials": len(((p.get("info") or {}).get("socials") or [])) + len(((p.get("info") or {}).get("websites") or [])),
    }
    out["reserve"] = quote_reserve_usd(out)
    return out


def pairs(pools):
    """Aktuelle Daten zu bis zu 30 Pools je Abruf."""
    out = {}
    pools = [p for p in pools if p]
    for i in range(0, len(pools), 30):
        data = get_json(f"{DS}/latest/dex/pairs/solana/{','.join(pools[i:i + 30])}")
        for p in (data or {}).get("pairs") or []:
            if p.get("chainId") == "solana":
                d = parse_ds_pair(p)
                out[d["pool"]] = d
    return out


def best_pair_for_mint(mints):
    """Zu Coin-Adressen den liquidesten SOL-Pool finden (für Copy-Signale und DexScreener-Listen)."""
    out = {}
    mints = [m for m in mints if m]
    for i in range(0, len(mints), 30):
        data = get_json(f"{DS}/tokens/v1/solana/{','.join(mints[i:i + 30])}")
        for p in data if isinstance(data, list) else []:
            d = parse_ds_pair(p)
            if d["quote"] != SOL_MINT or not d["mint"]:
                continue
            cur = out.get(d["mint"])
            if not cur or d["reserve"] > cur["reserve"]:
                out[d["mint"]] = d
    return out


def ds_latest_lists():
    """Coins, die gerade ein DexScreener-Profil oder einen Boost bekommen haben (oft frischer Hype)."""
    mints = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        try:
            data = get_json(DS + path, max_wait=5)
        except SourceError:
            continue
        for t in data if isinstance(data, list) else []:
            if t.get("chainId") == "solana" and t.get("tokenAddress"):
                mints.append(t["tokenAddress"])
    return list(dict.fromkeys(mints))


# ---------------------------------------------------------------- RugCheck
def parse_rugcheck(r, pools=()):
    """Bericht -> die Werte, die der Rug-Check-Agent braucht."""
    r = r or {}
    known = r.get("knownAccounts") or {}
    skip = {a for a, v in known.items() if (v or {}).get("type") in ("AMM", "LOCKER")}
    skip |= {m.get("pubkey") for m in r.get("markets") or []} | set(pools)
    holders = [h for h in r.get("topHolders") or [] if h.get("owner") not in skip and h.get("address") not in skip]
    top10 = sum(f(h.get("pct")) for h in holders[:10])
    insiders = sum(1 for h in holders if h.get("insider"))
    supply = f((r.get("token") or {}).get("supply"))
    creator_pct = (f(r.get("creatorBalance")) / supply * 100) if supply else 0.0
    # Liquiditäts-Sperre: zählt der Pool, in dem wir handeln. Bonding-Kurven haben keine LP-Token,
    # und Staub-Pools (< 100 $) von Trittbrettfahrern sagen nichts über den Coin.
    markets = r.get("markets") or []
    ours = [m for m in markets if m.get("pubkey") in set(pools)]
    if not ours:
        ours = [m for m in markets if f((m.get("lp") or {}).get("lpLockedUSD")) + f((m.get("lp") or {}).get("quoteUSD")) >= 100]
    lp_locked = [f((m.get("lp") or {}).get("lpLockedPct")) for m in ours if not is_curve(m.get("marketType"))]
    # „Low Liquidity“ prüft der Bot selbst mit DexScreener-Daten (RugCheck sieht bei frischen Pools oft nur Staub-Pools)
    risks = [{"name": x.get("name"), "level": x.get("level"), "text": x.get("description") or x.get("value") or ""}
             for x in r.get("risks") or [] if (x.get("name") or "") != "Low Liquidity"]
    return {
        "mint_auth": bool(r.get("mintAuthority")), "freeze_auth": bool(r.get("freezeAuthority")),
        "transfer_fee": f((r.get("transferFee") or {}).get("pct")), "rugged": bool(r.get("rugged")),
        "top10_pct": round(top10, 1), "holders": int(f(r.get("totalHolders"))), "insiders": insiders,
        "insider_networks": int(f(r.get("graphInsidersDetected"))), "creator_pct": round(creator_pct, 2),
        "creator_tokens": len(r.get("creatorTokens") or []), "lp_locked_pct": min(lp_locked) if lp_locked else 100.0,
        "score_norm": f(r.get("score_normalised"), 0), "risks": risks,
        "launchpad": ((r.get("launchpad") or {}).get("name") or ""), "creator": r.get("creator") or "",
        # die größten echten Holder (ohne Pools/Locker) – der Rug-Wächter vergleicht sie mit dem nächsten Bericht
        "top_holders": [[h.get("owner") or h.get("address"), round(f(h.get("pct")), 2)] for h in holders[:10]],
    }


def is_curve(market_type):
    """Bonding-Kurve (Pump.fun, Meteora DBC, Raydium LaunchLab …) statt normalem Pool?"""
    t = (market_type or "").lower()
    return t == "pump_fun" or any(k in t for k in ("dbc", "launchlab", "launchpad", "curve", "moonshot", "boop"))


def rugcheck(mint, pools=()):
    return parse_rugcheck(get_json(f"{RC}/{mint}/report", max_wait=8), pools)
