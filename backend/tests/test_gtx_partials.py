# backend/tests/test_gtx_partials.py
"""
Regression for partial closes + per-position margin (2026-10-06).
Starts from the 02-10-2026 book (3 Energy Vault lots + QIAGEN 10,000) and checks:
  A. real 05-10-2026 statement: QIAGEN 10,000 closed as 2 x 5,000 + new Chipotle (30% margin)
  B. partial close with the remainder still open (Energy Vault 10,000 -> 4,000 sold)
  C. lot opened and partly closed the same day (remainder open)
  D. lot opened and closed the same day in two pieces
Run from backend/:  python tests\\test_gtx_partials.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "services"))
from gtx_parser import parse_statement
from gtx_engine import replay, trunc2

N, Q, C, A = "ENERGY VAULT HOLDINGS (CFD)", "QIAGEN (CFD)", "CHIPOLTE MEXICAN (CFD)", "APPLE (CFD)"
HDR = ("Account name\tBalance\tOpen gross P/L\tProjected balance\tAvailable funds\tInitial margin req\t"
       "Margin available\tToday's realized P/L\tAccount currency\tStop out value\tCredit value\tAccount ID\t"
       "Blocked for fixed income\tFixed income value\tFixed income orders req")
OPH = "Account\tInstrument\tAmount\tOpen price\tBuy/Sell\tOpen date/time\tCurrent price\tProfit\tCurrency\tMargin"
TRH = "Account\tInstrument\tOperation\tDate\tPrice\tAmount\tProfit"
SETT = {"swap_rate_long_pct": 7.9, "swap_rate_short_pct": 7.9}
MR = {N: 0.5, Q: 0.5, C: 0.3, A: 0.25}
f = lambda x: f"{x:,.2f}"


def email(bal, real, positions, trades):
    """positions: (inst, qty, open_px, open_dt, cur_px); trades: (inst, op, dt, px, qty, profit)"""
    opl = sum((cp - o) * q for _, q, o, _, cp in positions)
    mg = sum(q * cp * MR[i] for i, q, _, _, cp in positions)
    acc = "\t".join([f(bal), f(opl), f(bal + opl), f(bal + opl - mg), f(mg), f(bal + opl - mg), f(real),
                     "USD", f(mg), "0.00", "2776", "0.00", "0.00", "0.00"])
    ops = "\n".join(f"GTX0011-USD\t{i}\t{q:,.2f}\t{o}\tBuy\t{t}\t{cp:.2f}\t{f((cp-o)*q)}\tUSD\t{f(q*cp*MR[i])}"
                    for i, q, o, t, cp in positions) or "There is no data available"
    trs = "\n".join(f"GTX0011-USD\t{i}\t{op}\t{dt}\t{px}\t{q:,.2f}\t{f(pr)}" for i, op, dt, px, q, pr in trades) \
        or "There is no data available"
    return (f"Account Statement Report GTX\n{HDR}\nGTX0011-USD\t{acc}\n\n\nOpen Position Report GTX\n{OPH}\n{ops}"
            f"\n\n\nTrades Report GTX\n{TRH}\n{trs}\n")


EV = [(N, 25000, 4.347, "24-09-2026 10:42:19.447"), (N, 10000, 4.21986, "24-09-2026 12:10:03.117"),
      (N, 15000, 4.4334, "25-09-2026 10:50:39.866")]
BOOK = EV + [(Q, 10000, 44.330451, "30-09-2026 09:55:59.549")]


def day1():
    pos = [(i, q, o, t, {N: 3.90, Q: 44.0}[i]) for i, q, o, t in BOOK]
    d = {"date": "2026-10-02", **parse_statement(email(1346692.92, 0.0, pos, []))}
    real = replay([d], SETT)["recon_log"][0]["engine_realised"]
    return {"date": "2026-10-02", **parse_statement(email(1346692.92, trunc2(real), pos, []))}


def next_day(d1, date, positions, trades):
    """Build day 2 with the broker's own figures, computed independently of the engine."""
    from datetime import datetime
    prev = replay([d1], SETT)["recon_log"][-1]["engine_balance"]
    cut = datetime.strptime(date + " 17:00", "%Y-%m-%d %H:%M")
    swap = sum(q * cp * 0.079 / 360 for _, q, _, t, cp in positions
               if datetime.strptime(t[:19], "%d-%m-%Y %H:%M:%S") <= cut)
    gross = sum(pr_exact for *_, pr_exact in [(t[0], t[5]) for t in trades])
    fees = sum(t[4] * 0.05 for t in trades)
    real = gross - fees - swap
    bal = trunc2(prev + real)
    shown = [(i, op, dt, px, q, trunc2(pr)) for i, op, dt, px, q, pr in trades]
    return {"date": date, **parse_statement(email(bal, trunc2(real), positions, shown))}


def check(label, days, expect_closed, expect_open):
    out = replay(days, SETT)
    r = out["recon_log"][-1]
    closed = sorted((x["instrument"][:8], x["qty"]) for x in out["closed_positions"] if x["close_dt"][:10] == days[-1]["date"])
    opened = sorted((x["instrument"][:8], x["qty"]) for x in out["open_positions"])
    ok = r["ok"] and not r["warnings"] and closed == sorted(expect_closed) and opened == sorted(expect_open)
    print(f"{'✓' if ok else '✗'} {label}: checks {r['checks']} warnings {r['warnings']}")
    print(f"    closed today {closed}\n    open {opened}")
    return ok


ok = True
d1 = day1()

# A. real 05-10 statement text
real0510 = open(os.path.join(os.path.dirname(__file__), "fixtures", "gtx_2026-10-05.txt")).read()
ok &= check("A real 05-10 (QIAGEN 2 x 5,000 partials + Chipotle 30% margin)",
            [d1, {"date": "2026-10-05", **parse_statement(real0510)}],
            [("QIAGEN (", 5000), ("QIAGEN (", 5000)],
            [("CHIPOLTE", 5000), ("ENERGY V", 10000), ("ENERGY V", 15000), ("ENERGY V", 25000)])

# B. sell 4,000 of the Energy Vault 10,000 lot; 6,000 stays open
B_pos = [(N, 25000, 4.347, EV[0][3], 3.95), (N, 6000, 4.21986, EV[1][3], 3.95), (N, 15000, 4.4334, EV[2][3], 3.95)]
ok &= check("B partial close, remainder open", [d1, next_day(d1, "2026-10-05", B_pos, [
    (N, "Sell", "05-10-2026 11:00:00.000", 3.95, 4000, (3.95 - 4.21986) * 4000),
    (Q, "Sell", "05-10-2026 11:05:00.000", 45.0, 10000, (45.0 - 44.330451) * 10000)])],
    [("ENERGY V", 4000), ("QIAGEN (", 10000)], [("ENERGY V", 6000), ("ENERGY V", 15000), ("ENERGY V", 25000)])

# C. Apple opened 8,000 today, 3,000 sold today, 5,000 still open
C_pos = [(i, q, o, t, {N: 3.9, Q: 44.5}[i]) for i, q, o, t in BOOK] + [(A, 5000, 180.0, "05-10-2026 10:00:00.000", 181.0)]
ok &= check("C opened + partly closed same day", [d1, next_day(d1, "2026-10-05", C_pos, [
    (A, "Buy", "05-10-2026 10:00:00.000", 180.0, 8000, 0.0),
    (A, "Sell", "05-10-2026 12:00:00.000", 182.0, 3000, (182.0 - 180.0) * 3000)])],
    [("APPLE (C", 3000)], [("APPLE (C", 5000), ("ENERGY V", 10000), ("ENERGY V", 15000), ("ENERGY V", 25000), ("QIAGEN (", 10000)])

# D. Apple opened 8,000 and closed in 2 pieces the same day
D_pos = [(i, q, o, t, {N: 3.9, Q: 44.5}[i]) for i, q, o, t in BOOK]
ok &= check("D intraday round trip in two pieces", [d1, next_day(d1, "2026-10-05", D_pos, [
    (A, "Buy", "05-10-2026 10:00:00.000", 180.0, 8000, 0.0),
    (A, "Sell", "05-10-2026 12:00:00.000", 182.0, 3000, (182.0 - 180.0) * 3000),
    (A, "Sell", "05-10-2026 15:00:00.000", 179.0, 5000, (179.0 - 180.0) * 5000)])],
    [("APPLE (C", 3000), ("APPLE (C", 5000)], [("ENERGY V", 10000), ("ENERGY V", 15000), ("ENERGY V", 25000), ("QIAGEN (", 10000)])

print("\nALL PARTIAL-CLOSE SCENARIOS RECONCILED ✓" if ok else "\nFAILURES ABOVE")
assert ok
