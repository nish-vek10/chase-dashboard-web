# backend/tests/verify_equity_cache.py
"""
Read-only equivalence check for the 2026-10-05 performance change: the new
one-read-per-table helpers must return exactly what the old per-client
queries returned, for every client of every daily-equity table (AXIA, IG
and every daily Data Feed). Writes nothing.

Run from backend/ with the venv active:
    python tests\\verify_equity_cache.py
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.services import supabase_service as sb

cl = sb.get_client()

def old_series(table, client, account):            # previous implementation
    rows = (cl.table(table).select("trade_date,equity,currency")
            .eq("client", client).eq("account", account).eq("currency", "GBP")
            .order("trade_date", desc=False).execute().data or [])
    return [{"date": r["trade_date"], "equity": float(r["equity"])} for r in rows]

def old_flagged(table, client, account):           # previous implementation
    rows = (cl.table(table).select("equity,chg_nlv,capital_flow_type,currency")
            .eq("client", client).eq("account", account).eq("currency", "GBP")
            .execute().data or [])
    t = 0.0
    for r in rows:
        if r.get("capital_flow_type") not in sb.CAPITAL_FLOW_TYPES:
            continue
        c = r.get("chg_nlv") if r.get("chg_nlv") is not None else r.get("equity")
        t += float(c or 0)
    return round(t, 2)

def old_linked():                                  # previous implementation
    ids = set()
    for table in sb._daily_equity_tables():
        rows = cl.table(table).select("capital_transfer_id").execute().data or []
        ids.update(int(r["capital_transfer_id"]) for r in rows if r.get("capital_transfer_id") is not None)
    return ids

sb.invalidate_all_cache()
fail = 0
for table in sb._daily_equity_tables():
    rows = sb._equity_table_rows(table)
    pairs = sorted({(r["client"], r["account"]) for r in rows})
    print(f"{table:<26} {len(rows):5d} rows  {len(pairs)} client/account")
    for c, a in pairs:
        s_new, s_old = sb._axia_equity_series(c, a, table), old_series(table, c, a)
        same_dates = [x["date"] for x in s_new] == [x["date"] for x in s_old]
        same_vals = sorted((x["date"], x["equity"]) for x in s_new) == sorted((x["date"], x["equity"]) for x in s_old)
        f_new, f_old = sb._axia_flagged_equity_total(c, a, table), old_flagged(table, c, a)
        ok = same_dates and same_vals and f_new == f_old
        fail += not ok
        print(f"    {'OK  ' if ok else 'DIFF'} {c}/{a}: {len(s_new)} GBP rows, capital flagged {f_new:,.2f}"
              + ("" if ok else f"  <-- old {len(s_old)} rows, flagged {f_old:,.2f}"))
l_new, l_old = sb._equity_linked_capital_transfer_ids(), old_linked()
fail += l_new != l_old
print(f"linked ledger ids: {'OK' if l_new == l_old else 'DIFF'} ({len(l_new)} ids)")
print("\nALL IDENTICAL ✓" if not fail else f"\n{fail} DIFFERENCE(S) — do not push")
