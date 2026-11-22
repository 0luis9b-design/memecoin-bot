"""Lern-Agent: verbessert die Regeln mit Testkonten – kontrolliert.

Jede Strategie hat ein Hauptkonto und mehrere Testkonten mit abgewandelten Regeln. Alle bekommen dieselben Signale.
  * Ist ein Testkonto nach genug Trades klar besser als das Hauptkonto im selben Zeitraum, entsteht ein Vorschlag.
    Das Hauptkonto ändert sich erst, wenn Luis ihn im Dashboard freigibt.
  * Ist ein Testkonto klar schlechter, wird es abgeschaltet und durch eine Abwandlung des besten Kontos ersetzt
    (das betrifft nur Testkonten).
"""
import json
import random
import time

from .. import config, notify, params
from ..notify import esc


class Learner:
    name = "learning"

    def __init__(self, store, trader):
        self.s = store
        self.t = trader
        self.stats = {"letzter_lauf": None, "vorschlaege": 0, "ersetzt": 0}
        self.rng = random.Random()

    def _window_return(self, acc, since):
        """Rendite eines Kontos seit einem Zeitpunkt in % (aus der Kontostand-Kurve)."""
        row = self.s.one("SELECT equity FROM equity WHERE account_id=? AND ts>=? ORDER BY ts LIMIT 1", (acc["id"], since))
        base = row["equity"] if row else acc["start_balance"]
        now_eq = self.t.equity(acc)
        return (now_eq / base - 1) * 100 if base else 0.0

    def compare(self, main, var):
        since = var["created"]
        sv = self.t.account_stats(var, since)
        sm = self.t.account_stats(main, since)
        rv = (self.t.equity(var) / var["start_balance"] - 1) * 100
        rm = self._window_return(main, since)
        return {"testkonto": var["name"], "trades": sv["trades"], "rendite_test": round(rv, 1), "rendite_haupt": round(rm, 1),
                "vorsprung": round(rv - rm, 1), "pf_test": sv["profit_factor"], "pf_haupt": sm["profit_factor"],
                "dd_test": sv["max_drawdown"], "dd_haupt": sm["max_drawdown"], "trefferquote_test": sv["winrate"],
                "trades_haupt": sm["trades"]}

    def run(self):
        now = time.time()
        self.stats["letzter_lauf"] = now
        for strategy in ("scanner", "copy"):
            accs = self.t.accounts(strategy)
            mains = [a for a in accs if a["kind"] == "main"]
            if not mains:
                continue
            main = mains[0]
            tests = [a for a in accs if a["kind"] == "test"]
            results = []
            for v in tests:
                c = self.compare(main, v)
                results.append((v, c))
                if c["trades"] < config.LEARN_MIN_TRADES:
                    continue
                better = (c["vorsprung"] >= config.LEARN_MIN_EDGE_PCT and (c["pf_test"] or 0) > (c["pf_haupt"] or 0)
                          and c["dd_test"] <= c["dd_haupt"] * 1.5 + 5)
                if better and not self.s.one("SELECT id FROM proposals WHERE variant_id=? AND status='offen'", (v["id"],)):
                    changes = params.diff(main["params"], v["params"])
                    if changes:
                        self.s.x("INSERT INTO proposals(ts,account_id,variant_id,changes,stats) VALUES(?,?,?,?,?)",
                                 (now, main["id"], v["id"], json.dumps(changes), json.dumps(c)))
                        self.stats["vorschlaege"] += 1
                        self.s.decide("learning", "PROPOSAL", f"{v['name']} ist {c['vorsprung']:+.1f} %-Punkte besser als "
                                      f"{main['name']} ({c['trades']} Trades) – Freigabe nötig", main["id"], data={"aenderungen": changes, **c})
                        lines = "\n".join(f"• {esc(params.LABELS.get(k, k))}: {a} → {b}" for k, (a, b) in changes.items())
                        notify.send("hoch", f"Lern-Vorschlag für {main['name']}",
                                    f"Testkonto „{esc(v['name'])}“ ist {c['vorsprung']:+.1f} %-Punkte besser "
                                    f"({c['trades']} Trades).\n{lines}\nFreigabe im Dashboard unter „Lernen“.", key=f"prop-{v['id']}")
            # schwache Testkonten ersetzen
            for v, c in results:
                if c["trades"] >= config.LEARN_MIN_TRADES and c["vorsprung"] <= -15 and c["rendite_test"] < 0:
                    self._replace(v, main, [r for r in results if r[0]["id"] != v["id"]], c)
        return True

    def _replace(self, weak, main, others, c):
        pool = [(main, (self.t.equity(main) / main["start_balance"] - 1) * 100)]
        pool += [(o, oc["rendite_test"]) for o, oc in others if o["active"]]
        parent = max(pool, key=lambda x: x[1])[0]
        new_params, changed = params.mutate(parent["params"], weak["strategy"], self.rng)
        gen = (parent.get("generation") or 0) + 1
        name = f"{'Scanner' if weak['strategy'] == 'scanner' else 'Copy'} G{gen}-{self.rng.randint(100, 999)}"
        self.s.x("UPDATE accounts SET active=0, note=? WHERE id=?",
                 (f"abgeschaltet: {c['vorsprung']:+.1f} %-Punkte hinter dem Hauptkonto nach {c['trades']} Trades", weak["id"]))
        # offene Positionen des abgeschalteten Kontos laufen nicht weiter
        self.s.x("UPDATE positions SET status='cancelled', reason_out='Testkonto abgeschaltet' WHERE account_id=? AND status IN ('pending','open')",
                 (weak["id"],))
        note = "Abwandlung von " + parent["name"] + ": " + ", ".join(f"{params.LABELS.get(k, k)} {a}→{b}" for k, (a, b) in changed.items())
        self.s.x("""INSERT INTO accounts(name,strategy,kind,params,start_balance,cash,created,active,parent,note,generation)
                    VALUES(?,?,'test',?,?,?,?,1,?,?,?)""",
                 (name, weak["strategy"], json.dumps(new_params), config.START_BALANCE, config.START_BALANCE, time.time(),
                  parent["id"], note[:400], gen))
        self.stats["ersetzt"] += 1
        self.s.decide("learning", "MUTATE", f"{weak['name']} ersetzt durch {name} – {note}"[:480], weak["id"])

    # ------------------------------------------------------------ Freigabe
    def decide_proposal(self, pid, accept):
        p = self.s.one("SELECT * FROM proposals WHERE id=? AND status='offen'", (pid,))
        if not p:
            return False, "Vorschlag nicht (mehr) offen"
        now = time.time()
        if accept:
            var = self.s.one("SELECT * FROM accounts WHERE id=?", (p["variant_id"],))
            main = self.s.one("SELECT * FROM accounts WHERE id=?", (p["account_id"],))
            if not var or not main:
                return False, "Konto fehlt"
            old = json.loads(main["params"])
            self.s.x("UPDATE accounts SET params=? WHERE id=?", (var["params"], main["id"]))
            self.s.x("UPDATE proposals SET status='angenommen', decided=? WHERE id=?", (now, pid))
            self.s.decide("learning", "APPLY", f"Regeln von {var['name']} auf {main['name']} übernommen (Freigabe durch Luis)",
                          main["id"], data={"vorher": old, "nachher": json.loads(var["params"])})
            return True, "übernommen"
        self.s.x("UPDATE proposals SET status='abgelehnt', decided=? WHERE id=?", (now, pid))
        self.s.decide("learning", "REJECT", "Vorschlag abgelehnt", p["account_id"])
        return True, "abgelehnt"
