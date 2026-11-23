"""Der Takt: ruft die Agenten in der richtigen Reihenfolge auf.

scanner -> rugcheck -> analyst -> sizer -> executor -> risk -> logger -> equity
scout (Wallet-Suche) -> copyfeed -> (dieselbe Kette für das Copy-Konto)
learning (alle 6 h)
"""
import json
import logging
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, market, notify, params, rules
from .agents.regime import Regime
from .backtest import Backtester
from .agents import analysis
from .agents.analysis import RugGuard
from .agents.launches import LaunchFeed
from .agents.learning import Learner
from .agents.scanner import Scanner
from .agents.shadow import Shadow
from .agents.trader import Trader
from .agents.wallets import CopyFeed, Scout
from .notify import esc
from .sources import SourceError

log = logging.getLogger("memebot.engine")
BERLIN = ZoneInfo("Europe/Berlin")
NAMES = {"scanner": "Scanner", "copy": "Copy-Trading"}


class Engine:
    def __init__(self, store):
        self.s = store
        self.scanner = Scanner(store)
        self.rug = RugGuard(store)
        self.trader = Trader(store)
        self.scout = Scout(store)
        self.copy = CopyFeed(store)
        self.shadow = Shadow(store)
        self.launches = LaunchFeed(store)
        self.learner = Learner(store, self.trader, self.shadow)
        self.regime = Regime(store)
        self.backtester = Backtester(store, self.trader)
        self.due = {}
        self.skips = Counter()          # Gründe, warum Coins nicht gekauft wurden (für die Pipeline-Anzeige)
        self.block_reasons = Counter()  # welche Rug-Check-Regel fällt wie oft durch (je Coin und 10 min einmal)
        self.analyst = {"bewertet": 0, "bestanden": 0, "letzte": None}
        self.last_logged = {}           # mint -> Zeit (Analyst-Log höchstens alle 10 min je Coin)
        self.state = {"cycles": 0, "last_cycle": None, "last_error": "", "started": time.time()}
        self.ensure_accounts()
        self.ensure_variants()
        self.due["backtest"] = time.time()   # erster Replay-Lauf nach BACKTEST_EVERY_H (vorher fehlen die Aufnahmen)

    # ------------------------------------------------------------ Konten anlegen
    def ensure_variants(self):
        """Neue Startvarianten aus späteren Versionen in bestehenden Datenbanken nachträglich anlegen."""
        now = time.time()
        if not self.s.kv_get("v13_aufgeraeumt"):
            for vname in params.RETIRED_V13:
                acc = self.s.one("SELECT * FROM accounts WHERE name=? AND active=1", (f"Scanner · {vname}",))
                if not acc:
                    continue
                self.s.x("UPDATE accounts SET active=0, note=? WHERE id=?",
                         ("abgeschaltet in v1.3: Abwandlung von „nach großem Anstieg kaufen“ – diese Idee hat live und im Backtest klar verloren",
                          acc["id"]))
                self.s.x("UPDATE positions SET status='cancelled', reason_out='Testkonto abgeschaltet' WHERE account_id=? AND status IN ('pending','open')",
                         (acc["id"],))
                self.s.decide("learning", "INFO", f"Testkonto {acc['name']} abgeschaltet (v1.3: Platz für neue Strategie-Familien)", acc["id"])
            self.s.kv_set("v13_aufgeraeumt", True)
        names = {r["name"] for r in self.s.q("SELECT name FROM accounts")}
        for strategy in ("scanner", "copy"):
            base = params.defaults(strategy)
            for vname, changes in params.VARIANTS[strategy].items():
                name = f"{NAMES[strategy]} · {vname}"
                if name in names:
                    continue
                self.s.x("INSERT INTO accounts(name,strategy,kind,params,start_balance,cash,created,note,generation) VALUES(?,?,?,?,?,?,?,?,0)",
                         (name, strategy, "test", json.dumps(dict(base, **changes)), config.START_BALANCE, config.START_BALANCE, now,
                          "Startvariante: " + ", ".join(f"{params.LABELS.get(k, k)} {v}" for k, v in changes.items())))
                self.s.decide("system", "INFO", f"neues Testkonto {name} angelegt")
    def ensure_accounts(self):
        if self.s.val("SELECT COUNT(*) FROM accounts", default=0):
            return
        now = time.time()
        for strategy in ("scanner", "copy"):
            base = params.defaults(strategy)
            self.s.x("INSERT INTO accounts(name,strategy,kind,params,start_balance,cash,created,note) VALUES(?,?,?,?,?,?,?,?)",
                     (NAMES[strategy], strategy, "main", json.dumps(base), config.START_BALANCE, config.START_BALANCE, now,
                      "Hauptkonto – Regeln ändern sich nur nach Freigabe"))
            for vname, changes in params.VARIANTS[strategy].items():
                p = dict(base, **changes)
                self.s.x("INSERT INTO accounts(name,strategy,kind,params,start_balance,cash,created,note,generation) VALUES(?,?,?,?,?,?,?,?,0)",
                         (f"{NAMES[strategy]} · {vname}", strategy, "test", json.dumps(p), config.START_BALANCE,
                          config.START_BALANCE, now, "Startvariante: " + ", ".join(f"{params.LABELS.get(k, k)} {v}" for k, v in changes.items())))
        self.s.decide("system", "INFO", "Konten angelegt: je ein Hauptkonto für Scanner und Copy-Trading plus Testkonten")

    def _due(self, key, every):
        now = time.time()
        if now - self.due.get(key, 0) >= every:
            self.due[key] = now
            return True
        return False

    # ------------------------------------------------------------ Takt
    def cycle(self):
        t0 = time.time()
        if self._due("scan", config.SCAN_SEC):
            self.scanner.discover()
        if self._due("launches", 30):
            self.launches.check(self.scanner)
            self.launches.handle_migrations(self.scanner)
        if self._due("lost_pools", 60):
            self.find_lost_pools()
        updated = self.scanner.refresh()
        self.trader.process(updated)
        if updated:
            self.shadow.update(updated)
            self.evaluate_scanner(updated)
        if self._due("shadow", 300):
            self.shadow.finalize()
        if self._due("regime", 300):
            self.regime.update()
        if self._due("backtest", config.BACKTEST_EVERY_H * 3600):
            self.backtester.start("geplant")
        if self._due("copy", config.COPY_SEC):
            try:
                self.handle_copy(self.copy.poll())
            except SourceError as e:
                self.copy.stats["fehler"] = str(e)
        if self._due("harvest", config.HARVEST_SEC):
            self.scout.harvest()
        if self._due("wallets", config.WALLET_SCORE_SEC):
            self.scout.score_wallets()
        if self._due("equity", config.EQUITY_SEC):
            self.trader.record_equity()
        if self._due("learn", config.LEARN_SEC) and self.state["cycles"] > 0:
            self.learner.run()
        if self._due("cleanup", 3600):
            self.s.cleanup(config.SNAPSHOT_KEEP_H)
        if self._due("statuslog", 300):
            self.log_status()
        self.daily_report()
        self.state["cycles"] += 1
        self.state["last_cycle"] = time.time()
        self.state["cycle_ms"] = round((time.time() - t0) * 1000)

    # ------------------------------------------------------------ Scanner-Strategie
    def evaluate_scanner(self, updated):
        accs = self.trader.accounts("scanner")
        if not accs:
            return
        for mint, d in updated.items():
            if d.get("quote") and d["quote"] != market.SOL_MINT:
                continue
            passing = []
            for a in accs:
                ok, key, why = analysis.check_pre(d, a["params"])
                if ok:
                    passing.append(a)
                elif a["kind"] == "main":
                    self.skips[analysis.rule_name(key)] += 1
                    self.shadow.record("scanner", mint, d["symbol"], key, why, d)
            if not passing:
                continue
            self._evaluate(mint, d, passing, strategy="scanner")

    def _evaluate(self, mint, d, accs, strategy, extra=None):
        status, _, rug = self.rug.check(mint, d["pool"], accs[0]["params"])
        if status == "warten":
            return
        hist = self.scanner.history(mint, 30)
        comps = analysis.components(d, hist, rug)
        swing = rules.swing_pct([h["price"] for h in hist])
        self.analyst["bewertet"] += 1
        log_now = time.time() - self.last_logged.get((strategy, mint), 0) > 600
        for a in accs:
            p = a["params"]
            sc, parts = analysis.total(comps, p.get("weights"))   # jedes Konto rechnet mit seinen eigenen Gewichten
            if extra:
                sc = extra["score_fn"](sc)
            if a["kind"] == "main":
                self.analyst["letzte"] = {"symbol": d["symbol"], "punkte": sc, "zeit": time.time()}
            checks = RugGuard.checks(rug, p)
            failed = [c for c in checks if not c[1]]
            if failed:
                if a["kind"] == "main":
                    key = failed[0][3] if len(failed) == 1 else "rug:mehrere"
                    self.shadow.record(strategy, mint, d["symbol"], key, ", ".join(c[0] for c in failed), d)
                if log_now and a["kind"] == "main":
                    self.block_reasons.update(c[0] for c in failed)
                    self.s.decide("rugcheck", "BLOCK", f"{a['name']}: durchgefallen – {', '.join(c[0] for c in failed)}", a["id"], mint,
                                  d["symbol"], {"checks": checks})
                    self.skips["Rug-Check"] += 1
                continue
            if sc < p["min_score"]:
                if a["kind"] == "main":
                    self.shadow.record(strategy, mint, d["symbol"], "punktzahl", f"Punktzahl {sc:.0f} < {p['min_score']}", d, parts)
                if log_now and a["kind"] == "main":
                    self.s.decide("analyst", "SKIP", f"{a['name']}: Punktzahl {sc:.0f} < {p['min_score']}", a["id"], mint, d["symbol"],
                                  {"punkte": parts})
                    self.skips["Punktzahl"] += 1
                continue
            if extra and not extra["account_ok"](a):
                continue
            self.analyst["bestanden"] += 1
            reason = extra["reason"] if extra else (
                f"Vol 5 min {d['vol_m5']:,.0f} $ · {d['buys_m5']} Käufe/{d['sells_m5']} Verk. · {d['ch_m5']:+.0f} % · "
                f"{rug['holders']} Holder · Top 10 {rug['top10_pct']:.0f} %")
            sig = {"mint": mint, "pool": d["pool"], "symbol": d["symbol"], "dex": d["dex"], "score": sc, "price": d["price"],
                   "reserve": d["reserve"], "reason": reason, "heat": self.regime.heat, "swing": swing,
                   "data": {"strategie": strategy, "werte": {k: d.get(k) for k in ("price", "mcap", "liq", "reserve", "vol_m5", "vol_h1",
                                                                                      "buys_m5", "sells_m5", "ch_m5", "ch_h1", "age_min", "dex")},
                            "checks": checks, "punkte": parts, "marktlage": self.regime.heat, "schwankung": swing,
                            **({"copy": extra["info"]} if extra else {})}}
            if self.trader.try_enter(a, sig, "analyst" if strategy == "scanner" else "copyfeed"):
                self.shadow.record(strategy, mint, d["symbol"], "gekauft", reason, d, parts)
        if log_now:
            self.last_logged[(strategy, mint)] = time.time()
            if len(self.last_logged) > 5000:
                self.last_logged.clear()

    # ------------------------------------------------------------ Copy-Strategie
    def handle_copy(self, signals):
        if not signals:
            return
        accs = self.trader.accounts("copy")
        now = time.time()
        for sg in signals:
            if sg["kind"] == "sell":
                self._follow_sell(sg, accs)
                continue
            if now - sg["ts"] > config.COPY_MAX_SIGNAL_AGE_SEC:
                self.s.decide("copyfeed", "SKIP", f"Kauf der Wallet zu spät bemerkt ({(now - sg['ts']) / 60:.0f} min)", mint=sg["mint"],
                              data={"wallet": sg["wallet"]})
                continue
            d = self._token_now(sg["mint"])
            if not d:
                self.s.decide("copyfeed", "SKIP", "kein handelbarer SOL-Pool gefunden", mint=sg["mint"], data={"wallet": sg["wallet"]})
                continue
            d["age_min"] = (now - (d.get("created_at") or now)) / 60
            chase = (d["price"] / sg["price_usd"] - 1) * 100 if sg["price_usd"] else 999
            n_wallets = max(1, self.copy.cluster(sg["mint"]))   # die meldende Wallet zählt immer mit
            passing = []
            for a in accs:
                ok, key, why = analysis.check_pre(d, a["params"])
                if not ok:
                    if a["kind"] == "main":
                        self.shadow.record("copy", sg["mint"], d["symbol"], key, why, d)
                        self.s.decide("copyfeed", "SKIP", f"{a['name']}: {why}", a["id"], sg["mint"], d["symbol"], {"wallet": sg["wallet"]})
                    continue
                passing.append(a)
            if not passing:
                continue
            info = {"wallet": sg["wallet"], "wallet_score": sg["wallet_score"], "wallet_preis": sg["price_usd"],
                    "aufpreis_pct": round(chase, 1), "wallets_im_cluster": n_wallets, "wallet_usd": round(sg["usd"], 2)}

            def account_ok(a, chase=chase, sg=sg, d=d, n_wallets=n_wallets):
                p = a["params"]
                if chase > p["max_chase_pct"]:
                    if a["kind"] == "main":
                        self.shadow.record("copy", sg["mint"], d["symbol"], "aufpreis", f"{chase:.0f} % Aufpreis", d)
                        self.s.decide("copyfeed", "SKIP", f"{a['name']}: {chase:.0f} % teurer als die Wallet (max. {p['max_chase_pct']} %)",
                                      a["id"], sg["mint"], d["symbol"])
                    return False
                if n_wallets < p.get("min_wallets", 1):
                    return False
                return sg["wallet_score"] >= p["min_wallet_score"]

            self._evaluate(sg["mint"], d, passing, "copy", extra={
                "score_fn": lambda an, sg=sg, n=n_wallets: min(100.0, 0.5 * an + 50 * sg["wallet_score"] + 10 * max(0, n - 1)),
                "account_ok": account_ok, "info": info,
                "reason": f"Smart Wallet {sg['wallet'][:4]}…{sg['wallet'][-4:]} (Score {sg['wallet_score']:.2f}) kauft · "
                          f"{n_wallets} Wallet(s) · Aufpreis {chase:+.0f} %"})

    def _token_now(self, mint):
        row = self.s.one("SELECT metrics, updated FROM tokens WHERE mint=?", (mint,))
        if row and row["metrics"] and time.time() - (row["updated"] or 0) < 60:
            return json.loads(row["metrics"])
        try:
            best = market.best_pair_for_mint([mint]).get(mint)
        except SourceError:
            return None
        if not best:
            return None
        self.scanner._add(best, "copy")
        self.s.x("UPDATE tokens SET metrics=?, updated=?, last_price=?, status='watch' WHERE mint=?",
                 (json.dumps(best), time.time(), best["price"], mint))
        return best

    def find_lost_pools(self):
        """Pool einer offenen Position nicht mehr auffindbar (z. B. Migration ohne Meldung)? Neuen Pool suchen."""
        rows = self.s.q("""SELECT DISTINCT t.mint FROM tokens t JOIN positions p ON p.mint=t.mint
                           WHERE p.status IN ('pending','open') AND t.missing_since IS NOT NULL AND t.missing_since < ?""",
                        (time.time() - 120,))
        if not rows:
            return 0
        try:
            best = market.best_pair_for_mint([r["mint"] for r in rows])
        except SourceError:
            return 0
        for mint, d in best.items():
            self.s.x("UPDATE tokens SET pool=?, dex=?, missing_since=NULL, updated=0 WHERE mint=?", (d["pool"], d["dex"], mint))
            self.s.x("UPDATE positions SET pool=?, dex=? WHERE mint=? AND status IN ('pending','open')", (d["pool"], d["dex"], mint))
            self.s.decide("risk", "INFO", f"Pool gewechselt: {d['symbol']} handelt jetzt auf {d['dex']}", mint=mint, symbol=d["symbol"])
        return len(best)

    def _follow_sell(self, sg, accs):
        for a in accs:
            if not a["params"].get("follow_sells"):
                continue
            for pos in self.trader.open_positions(a["id"]):
                if pos["mint"] != sg["mint"] or pos["status"] != "open":
                    continue
                if pos["signal"] and sg["wallet"] not in pos["signal"]:
                    continue  # nur verkaufen, wenn die Wallet verkauft, der wir gefolgt sind
                d = self._token_now(sg["mint"])
                if d:
                    self.trader.exit(pos, a, 1.0, d, f"Smart Wallet {sg['wallet'][:4]}… verkauft – wir verkaufen mit", agent="copyfeed")

    def summary(self):
        """Kurzer Zustand ohne Geheimnisse – für das Log und /health."""
        from .sources import STATS
        return {
            "beobachtet": self.scanner.stats["beobachtet"], "pools_gesehen": self.scanner.stats["pools_gesehen"],
            "bewertet": self.analyst["bewertet"], "orders": self.trader.stats["orders"],
            "offen": len(self.trader.open_positions()), "rug_blockiert": self.rug.stats["blockiert"],
            "wallets_beobachtet": self.scout.stats["beobachtet"], "copy_abfragen": self.copy.stats["abgefragt"],
            "launch_feed": {k: self.launches.stats[k] for k in ("verbunden", "neue_coins", "pro_min", "aufgenommen", "migrationen", "fehler")},
            "schatten": self.shadow.stats, "markt_hitze": self.regime.heat,
            "backtest": self.backtester.progress or ("läuft" if self.backtester.running else "bereit"),
            "rug_gruende": dict(self.block_reasons.most_common(6)), "ausschluss": dict(self.skips.most_common(6)),
            "quellen": {k: f"{v['ok']} ok / {v['fehler']} Fehler" + (f" ({v['letzter_fehler']})" if v["fehler"] else "")
                        for k, v in STATS.items()},
        }

    def log_status(self):
        log.info("Status: %s", json.dumps(self.summary(), ensure_ascii=False))

    # ------------------------------------------------------------ Tagesbericht
    def daily_report(self):
        now = datetime.now(BERLIN)
        day = now.strftime("%Y-%m-%d")
        if now.hour < config.REPORT_HOUR or self.s.kv_get("report_day") == day:
            return
        self.s.kv_set("report_day", day)
        lines = []
        for a in self.trader.accounts():
            st = self.trader.account_stats(a)
            if a["kind"] == "main":
                lines.append(f"<b>{esc(a['name'])}</b>: {st['equity']:,.2f} $ ({st['pnl_pct']:+.1f} %) · {st['trades']} Trades · "
                             f"Treffer {st['winrate'] if st['winrate'] is not None else '–'} %")
        tests = [(a, self.trader.account_stats(a)) for a in self.trader.accounts() if a["kind"] == "test"]
        if tests:
            best = max(tests, key=lambda x: x[1]["pnl_pct"])
            lines.append(f"Bestes Testkonto: {esc(best[0]['name'])} ({best[1]['pnl_pct']:+.1f} %)")
        notify.send("normal", "Tagesbericht Memecoin-Bot (Spielgeld)", "\n".join(lines))

    # ------------------------------------------------------------ Für das Dashboard
    def agents_status(self):
        now = time.time()
        sc = self.scanner.stats
        return [
            {"id": "scanner", "name": "Scanner", "zeile": f"{sc['beobachtet']} Coins beobachtet", "wert": sc["pools_gesehen"],
             "einheit": "Pools gesehen", "fehler": sc["fehler"], "zuletzt": sc["letzter_scan"]},
            {"id": "launchfeed", "name": "Launch-Feed", "zeile": (f"live · {self.launches.stats['pro_min']:.0f}/min · "
                                                                 f"{self.launches.stats['aufgenommen']} aufgenommen")
                                                                if self.launches.stats["verbunden"] else "nicht verbunden",
             "wert": self.launches.stats["neue_coins"], "einheit": "neue Pump.fun-Coins", "fehler": self.launches.stats["fehler"],
             "zuletzt": self.launches.stats["letzte_nachricht"]},
            {"id": "rugcheck", "name": "Rug-Check", "zeile": "harte Ausschlussregeln", "wert": self.rug.stats["blockiert"],
             "einheit": "blockiert", "fehler": self.rug.stats["fehler"]},
            {"id": "analyst", "name": "Analyst", "zeile": (f"zuletzt {self.analyst['letzte']['symbol']}: {self.analyst['letzte']['punkte']:.0f}"
                                                         if self.analyst["letzte"] else "wartet"),
             "wert": self.analyst["bewertet"], "einheit": "bewertet"},
            {"id": "sizer", "name": "Sizer", "zeile": "Einsatz nach Punktzahl", "wert": self.trader.stats["orders"], "einheit": "Orders"},
            {"id": "executor", "name": "Executor", "zeile": f"{self.trader.stats['verworfen']} verworfen",
             "wert": self.trader.stats["ausgefuehrt"], "einheit": "ausgeführt"},
            {"id": "risk", "name": "Risk-Guard", "zeile": f"{len(self.trader.open_positions())} Positionen bewacht",
             "wert": self.trader.stats["verkaeufe"], "einheit": "Verkäufe"},
            {"id": "logger", "name": "Logger", "zeile": "Begründung je Entscheidung",
             "wert": self.s.val("SELECT COUNT(*) FROM decisions WHERE ts>?", (now - 86400,), 0), "einheit": "Einträge 24 h"},
            {"id": "scout", "name": "Wallet-Scout", "zeile": f"{self.scout.stats['beobachtet']} Smart Wallets",
             "wert": self.scout.stats["wallets_bewertet"], "einheit": "Wallets bewertet", "fehler": self.scout.stats["fehler"]},
            {"id": "copyfeed", "name": "Copy-Feed", "zeile": f"{self.copy.stats['kaeufe']} Käufe gesehen",
             "wert": self.copy.stats["abgefragt"], "einheit": "Abfragen", "fehler": self.copy.stats["fehler"],
             "zuletzt": self.copy.stats["letzte_runde"]},
            {"id": "learning", "name": "Lern-Agent", "zeile": f"{self.learner.stats['vorschlaege']} Vorschläge",
             "wert": self.learner.stats["ersetzt"], "einheit": "Varianten ersetzt", "zuletzt": self.learner.stats["letzter_lauf"]},
            {"id": "shadow", "name": "Schatten", "zeile": f"{self.shadow.stats['ausgewertet']} ausgewertet",
             "wert": self.shadow.stats["erfasst"], "einheit": "Ablehnungen verfolgt"},
        ]
