# backend/src/services/supabase_service.py
"""
Supabase client and all data queries against live tables.

Provides:
  - get_client()                  Singleton Supabase client
  - get_live_aum()                SUM(Invested) from latest pfees snapshot
  - get_live_pnl()                SUM(Current PnL) from latest pfees snapshot
  - get_latest_pfees_date()       Most recent Date in user_pfees_estimation
  - get_pfees_latest_snapshot()   All rows from pfees for latest date
  - get_balance_history()         All rows from balance_history ordered by date
  - get_capital_events()          All rows from capital_events ordered by date
  - compute_fund_metrics()        AUM, PnL, TWR, sub-periods, bank balance
  - get_period_return(days)       % equity change over N days from balance_history
  - get_portfolio_kpis()          Full KpiData dict for portfolio home page
  - get_equity_curve_data(days)   Equity curve filtered by time range
  - get_pods_with_kpis()          Pod strips with live KPIs
  - get_allocation_data()         Donut chart slices by pod
  - get_pnl_contribution_data()   Bar chart PnL by pod
  - get_hierarchy_rows(type)      BreakdownRow-compatible rows for hierarchy tabs
  - list_pods()
  - list_strategies()
  - create_capital_event()
  - delete_capital_event()
  - create_pod() / update_pod() / delete_pod()
  - create_strategy() / update_strategy() / delete_strategy()
"""

import os
import threading
import time
from functools import lru_cache, reduce
from datetime import date, datetime, timedelta
from typing import Optional
from dotenv import load_dotenv
from supabase import create_client, Client

# Ensure .env is loaded even if this module is imported before main.py calls load_dotenv()
load_dotenv()


def _darwin_display(raw: str) -> str:
    """Strip Darwinex version suffix: 'CFZ.5.18' → 'CFZ', 'DRW.1.2' → 'DRW'."""
    if not raw:
        return "—"
    return raw.split(".")[0].upper()


def _sum_equity_by_date(history: list[dict]) -> dict[str, float]:
    """
    Sum investor_equity per date from balance_history rows.

    balance_history may have multiple rows per date (one per Darwinex account).
    A plain dict comprehension would overwrite — this correctly aggregates totals.
    Returns { date_str: total_investor_equity_float }.
    """
    eq: dict[str, float] = {}
    for r in history:
        d = r["date"]
        eq[d] = round(eq.get(d, 0.0) + r["investor_equity"], 2)
    return eq


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SUPABASE_URL = os.getenv("SUPABASE_URL", "")
_SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# ---------------------------------------------------------------------------
# TTL cache — reduces ~30 Supabase round-trips to ~7 per page load
# ---------------------------------------------------------------------------

_TTL   = 60          # seconds — all read-only tables cached for 60s
_cache: dict = {}    # key → { "val": ..., "ts": float }


_key_locks: dict = {}
_key_locks_guard = threading.Lock()


def _get_cached(key: str, fetcher, ttl: int = _TTL):
    """Return cached value if still fresh, else fetch, store, return.

    Per-key lock (2026-10-05, perf): the Portfolio page fires several
    requests at once; on an empty/expired cache each used to fetch the same
    table in parallel (seen: user_pfees_estimation x4). Now the first caller
    fetches and the others wait for its result. RLock so a fetcher may call
    other cached getters (including itself recursively) without deadlock.
    """
    entry = _cache.get(key)
    if entry and time.monotonic() - entry["ts"] < ttl:
        return entry["val"]
    with _key_locks_guard:
        lock = _key_locks.setdefault(key, threading.RLock())
    with lock:
        entry = _cache.get(key)                       # filled while we waited?
        if entry and time.monotonic() - entry["ts"] < ttl:
            return entry["val"]
        val = fetcher()
        _cache[key] = {"val": val, "ts": time.monotonic()}
        return val


def _invalidate(*keys: str) -> None:
    """Evict cache entries by key (call after any write operation)."""
    for k in keys:
        _cache.pop(k, None)


def invalidate_all_cache() -> None:
    """Evict every cache entry (call after CSV upload)."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Client — singleton
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_client() -> Client:
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env"
        )
    return create_client(_SUPABASE_URL, _SUPABASE_KEY)


# ---------------------------------------------------------------------------
# Live AUM + PnL from user_pfees_estimation (latest snapshot date)
# ---------------------------------------------------------------------------

def get_latest_pfees_date() -> Optional[str]:
    """Return the most recent Date in user_pfees_estimation (cached 60s)."""
    def _fetch():
        sb  = get_client()
        res = (
            sb.table("user_pfees_estimation")
            .select('"Date"')
            .order('"Date"', desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            return str(res.data[0]["Date"])
        return None
    return _get_cached("pfees_date", _fetch)


def get_pfees_history_all() -> list[dict]:
    """
    All rows from user_pfees_estimation ordered by Date asc (cached 60s).
    Used for per-Darwin period returns, drawdown, and sparklines.
    ~880 rows for 22 days × 20 Darwins × 2 accounts — fine to cache.
    """
    def _fetch():
        sb  = get_client()
        res = (
            sb.table("user_pfees_estimation")
            .select('"Date","AccountId","Darwin","Invested","Current PnL"')
            .order('"Date"', desc=False)
            .execute()
        )
        return res.data or []
    return _get_cached("pfees_history_all", _fetch)


def _compute_trader_metrics(history: list[dict]) -> dict:
    """
    Compute per-(AccountId, Darwin_raw) performance metrics from full pfees history.

    Equity = Invested + Current PnL  (Darwinex: Invested is deployed capital,
    PnL is cumulative gain/loss — equity is the true current value).

    Period returns: (equity_latest - equity_Ndays_ago) / equity_Ndays_ago
    Max drawdown:   peak-to-trough on equity series, expressed as a negative fraction.

    Returns { (account_id_int, darwin_raw_str): { pct_1d, pct_7d, pct_30d, max_drawdown } }
    """
    from collections import defaultdict

    # Build equity time series per (AccountId, Darwin)
    series: dict = defaultdict(list)
    for row in history:
        acct = int(row.get("AccountId") or 0)
        drw  = (row.get("Darwin") or "").strip()
        inv  = float(row.get("Invested")     or 0)
        pnl  = float(row.get("Current PnL") or 0)
        d    = str(row.get("Date"))
        series[(acct, drw)].append({"date": d, "equity": round(inv + pnl, 2)})

    # Sort each series by date
    for key in series:
        series[key].sort(key=lambda r: r["date"])

    result: dict = {}
    for (acct, drw), pts in series.items():
        if not pts:
            result[(acct, drw)] = {"pct_1d": 0.0, "pct_7d": 0.0, "pct_30d": 0.0, "max_drawdown": 0.0}
            continue

        latest_equity = pts[-1]["equity"]
        latest_date   = pts[-1]["date"]

        def _pct(n_days: int) -> float:
            cutoff = (datetime.fromisoformat(latest_date) - timedelta(days=n_days)).strftime("%Y-%m-%d")
            past   = [p for p in pts if p["date"] <= cutoff]
            if not past or past[-1]["equity"] == 0:
                return 0.0
            return round((latest_equity - past[-1]["equity"]) / abs(past[-1]["equity"]), 6)

        # Max drawdown: peak-to-trough on equity series
        peak   = -float("inf")
        max_dd = 0.0
        for pt in pts:
            eq = pt["equity"]
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (eq - peak) / peak
                if dd < max_dd:
                    max_dd = dd

        result[(acct, drw)] = {
            "pct_1d":       _pct(1),
            "pct_7d":       _pct(7),
            "pct_30d":      _pct(30),
            "max_drawdown": round(max_dd, 6),
        }

    return result


def get_pfees_latest_snapshot() -> list[dict]:
    """All rows from user_pfees_estimation for the latest date (cached 60s)."""
    def _fetch():
        latest_date = get_latest_pfees_date()
        if not latest_date:
            return []
        sb  = get_client()
        res = (
            sb.table("user_pfees_estimation")
            .select('"Date","AccountId","Darwin","Invested","Current PnL"')
            .eq('"Date"', latest_date)
            .execute()
        )
        return res.data or []
    return _get_cached("pfees_snapshot", _fetch)


def get_live_aum(snapshot_date: Optional[str] = None) -> float:
    """Sum of 'Invested' from latest pfees snapshot (uses cache when no date override)."""
    if snapshot_date is None:
        # Use cached snapshot — avoids an extra DB round-trip
        return round(sum(float(r.get("Invested") or 0) for r in get_pfees_latest_snapshot()), 2)
    sb  = get_client()
    res = (
        sb.table("user_pfees_estimation")
        .select('"Invested"')
        .eq('"Date"', snapshot_date)
        .execute()
    )
    return round(sum(float(r["Invested"] or 0) for r in (res.data or [])), 2)


def get_live_pnl(snapshot_date: Optional[str] = None) -> float:
    """Sum of 'Current PnL' from latest pfees snapshot (uses cache when no date override)."""
    if snapshot_date is None:
        return round(sum(float(r.get("Current PnL") or 0) for r in get_pfees_latest_snapshot()), 2)
    sb  = get_client()
    res = (
        sb.table("user_pfees_estimation")
        .select('"Current PnL"')
        .eq('"Date"', snapshot_date)
        .execute()
    )
    return round(sum(float(r["Current PnL"] or 0) for r in (res.data or [])), 2)


# ---------------------------------------------------------------------------
# Balance history
# ---------------------------------------------------------------------------

def get_balance_history() -> list[dict]:
    """
    All rows from balance_history ordered by date ascending (cached 60s).
    Returns list of { date: str, investor_equity: float }
    """
    def _fetch():
        sb  = get_client()
        res = (
            sb.table("balance_history")
            .select('"Date","Investor Equity"')
            .order('"Date"', desc=False)
            .execute()
        )
        return [
            {
                "date":            str(r["Date"]),
                "investor_equity": float(r["Investor Equity"] or 0),
            }
            for r in (res.data or [])
        ]
    return _get_cached("balance_history", _fetch)


def get_period_return(days: int) -> float:
    """
    % change in investor equity over the last N days.
    Uses the latest date in balance_history as reference (not today's date),
    so this works correctly even when data is not yet updated today.
    Returns 0.0 if insufficient data.
    """
    return _period_return_from_hist(get_balance_history(), days)


# ---------------------------------------------------------------------------
# Capital events
# ---------------------------------------------------------------------------

def get_capital_events() -> list[dict]:
    """
    All rows from capital_events ordered by event_date ascending (cached 60s).
    Returns canonical CapitalEvent-compatible dicts.
    """
    def _fetch():
        sb  = get_client()
        res = (
            sb.table("capital_events")
            .select("id,event_date,event_type,amount,notes,reference,created_at")
            .order("event_date", desc=False)
            .execute()
        )
        events = []
        for r in (res.data or []):
            amt = float(r["amount"])
            events.append({
                "event_id":   str(r["id"]),
                "date":       str(r["event_date"]),
                "event_type": r["event_type"],
                "amount":     amt if r["event_type"] == "deposit" else -amt,
                "pod_id":     None,
                "notes":      r.get("notes") or "",
                "reference":  r.get("reference") or "",
            })
        return events
    return _get_cached("capital_events", _fetch)


# ---------------------------------------------------------------------------
# TWR + sub-period computation
# ---------------------------------------------------------------------------

def _annualised(period_return: float, start_date: str, end_date: str) -> Optional[float]:
    """Annualise a sub-period return over its date range."""
    try:
        d0   = datetime.fromisoformat(start_date).date()
        d1   = datetime.fromisoformat(end_date).date()
        days = (d1 - d0).days
        if days <= 0:
            return None
        return round((1 + period_return) ** (365 / days) - 1, 6)
    except Exception:
        return None


def compute_fund_metrics() -> dict:
    """
    Build full fund ledger summary from Supabase live data.

    Sources:
      - balance_history          → daily equity curve (Investor Equity)
      - capital_events           → period boundaries + bank balance
      - user_pfees_estimation    → current AUM + PnL (latest snapshot)

    Returns FundLedgerSummary-compatible dict.
    """
    events      = get_capital_events()
    history     = get_balance_history()
    current_aum = get_live_aum()
    total_pnl   = get_live_pnl()

    # Portfolio equity per date — prefer pfees historical Invested (same source as
    # get_live_aum so end_aum always matches the AUM card).  Fall back chain:
    # pfees → user_accounts_equity → summed balance_history.
    equity_by_date: dict[str, float] = _pfees_equity_by_date()
    if not equity_by_date:
        equity_by_date = _portfolio_equity_by_date()
    if not equity_by_date:
        equity_by_date = _sum_equity_by_date(history)
    sorted_hist_dates = sorted(equity_by_date.keys())

    # Filter only external events (deposit/withdrawal) for sub-period boundaries
    external = [e for e in events if e["event_type"] in ("deposit", "withdrawal")]

    # Bank balance: Σ(deposits) − Σ(withdrawals)
    total_deposited = round(sum(e["amount"]       for e in external if e["amount"] > 0), 2)
    total_withdrawn = round(sum(abs(e["amount"])   for e in external if e["amount"] < 0), 2)
    bank_balance    = round(total_deposited - total_withdrawn, 2)

    # ── Sub-period construction — use Darwinex cash flows, not bank deposits ──
    # capital_events are bank→wallet events.  The Darwinex balance_history only
    # moves when money is actually deployed via internal_transfers (Wallet→account).
    # Using internal_transfers as boundaries gives the correct TWR.
    darwinex_flows   = _get_darwinex_cashflows()
    cashflow_by_date = {f["date"]: f["amount"] for f in darwinex_flows}

    periods      = []
    sorted_dates = [f["date"] for f in darwinex_flows]

    if not sorted_dates:
        # No transfers recorded yet — fall back to capital_events for basic TWR
        sorted_dates = sorted(set(e["date"] for e in external))
        cashflow_by_date = {d: round(sum(e["amount"] for e in external if e["date"] == d), 2)
                            for d in sorted_dates}

    last_eq  = sorted_hist_dates[-1] if sorted_hist_dates else None
    first_eq = sorted_hist_dates[0]  if sorted_hist_dates else None

    # ── Classify boundaries ─────────────────────────────────────────────────
    # pre_equity : boundary date has NO equity data before it
    #              → lump into a single inception period with cumulative cash
    # mid_equity : equity data exists before this boundary
    #              → proper TWR sub-period boundary
    # future     : boundary after last equity date → skip entirely
    pre_cash  = 0.0   # cumulative deployed before first equity data
    pre_start = None  # earliest pre-equity boundary date
    mid_dates: list[str] = []

    for d in sorted_dates:
        if last_eq and d > last_eq:
            continue  # future — skip
        cf       = cashflow_by_date.get(d, 0.0)
        before_d = [x for x in sorted_hist_dates if x < d]
        if before_d:
            mid_dates.append(d)
        else:
            pre_cash += cf
            if pre_start is None:
                pre_start = d

    # ── Build periods ────────────────────────────────────────────────────────
    # Period 0 — inception to first mid-boundary (or to last equity date):
    #   start_aum = all pre-equity cash deployed; end_aum = equity just before
    #   the first mid-boundary (or the last available equity date if none).
    if pre_cash > 0 and last_eq:
        if mid_dates:
            avail_before = [d for d in sorted_hist_dates if d < mid_dates[0]]
            p0_end       = max(avail_before) if avail_before else None
        else:
            p0_end = last_eq

        if p0_end and p0_end in equity_by_date:
            _s  = pre_start or (sorted_dates[0] if sorted_dates else str(date.today()))
            _ea = equity_by_date[p0_end]

            # ── start_aum for the base period ─────────────────────────────
            # When no mid-equity boundaries exist, all cash flows predate the
            # equity series.  Using total bank deposits as start_aum produces
            # a wrong negative return (wallet cash sits outside Darwinex).
            # Instead derive original deployed capital from pfees:
            #   original_deployed = current_pfees_equity − pfees_total_pnl
            # This guarantees TWR = total_pnl / original_deployed, consistent
            # with the PnL card.  Fall back to pre_cash if pfees data absent.
            if not mid_dates:
                _implied = round(_ea - total_pnl, 2)
                _start   = _implied if _implied > 0 else pre_cash
            else:
                # Mid boundaries exist — use first equity date as the base
                # period starting value (closest proxy to initial deployment).
                _start = equity_by_date[sorted_hist_dates[0]] if sorted_hist_dates else pre_cash

            _pnl = round(_ea - _start, 2)
            _pr  = round(_pnl / _start, 6) if _start else 0.0
            periods.append({
                "period_num":         1,
                "start_date":         _s,
                "end_date":           p0_end,
                "start_aum":          _start,
                "cash_flow_at_start": pre_cash,
                "end_aum":            _ea,
                "pnl":                _pnl,
                "period_return":      _pr,
                "annualised_return":  _annualised(_pr, _s, p0_end),
            })

    # Mid-equity sub-periods — proper TWR chain
    for j, bd in enumerate(mid_dates):
        if j + 1 < len(mid_dates):
            avail_before = [d for d in sorted_hist_dates if d < mid_dates[j + 1]]
            p_end        = max(avail_before) if avail_before else None
        else:
            p_end = last_eq

        cf            = cashflow_by_date.get(bd, 0.0)
        before_bd     = [d for d in sorted_hist_dates if d < bd]
        equity_before = equity_by_date[max(before_bd)] if before_bd else 0.0
        start_aum     = round(equity_before + cf, 2)
        end_aum       = equity_by_date.get(p_end, start_aum) if p_end else start_aum
        pnl           = round(end_aum - start_aum, 2)
        pr            = round(pnl / start_aum, 6) if start_aum else 0.0
        pnum          = len(periods) + 1
        periods.append({
            "period_num":         pnum,
            "start_date":         bd,
            "end_date":           p_end or bd,
            "start_aum":          start_aum,
            "cash_flow_at_start": cf,
            "end_aum":            end_aum,
            "pnl":                pnl,
            "period_return":      pr,
            "annualised_return":  _annualised(pr, bd, p_end or bd),
        })

    twr = round(
        reduce(lambda acc, p: acc * (1.0 + p["period_return"]), periods, 1.0) - 1.0,
        6
    ) if periods else 0.0

    initial_aum = periods[0]["start_aum"] if periods else 0.0
    # inception_date = first bank deposit (shows in ledger header), not first Darwinex trade
    cap_dates      = sorted(set(e["date"] for e in external))
    inception_date = cap_dates[0] if cap_dates else (sorted_dates[0] if sorted_dates else str(date.today()))

    return {
        "twr":              twr,
        "total_pnl":        total_pnl,
        "initial_aum":      initial_aum,
        "current_aum":      current_aum,
        "bank_balance":     bank_balance,
        "total_deposited":  total_deposited,
        "total_withdrawn":  total_withdrawn,
        "periods":          periods,
        "events":           events,
        "num_periods":      len(periods),
        "inception_date":   inception_date,
        "last_updated":     str(date.today()),
    }


# ---------------------------------------------------------------------------
# Portfolio home page — KPIs
# ---------------------------------------------------------------------------

def get_portfolio_kpis() -> dict:
    """
    Build KpiData dict for the portfolio home page.

    Sources:
      - user_pfees_estimation  → current_equity, total_pnl
      - capital_events         → initial_investment (first deposit), performance (TWR)
      - balance_history        → pct_1d, pct_7d, pct_30d
    """
    current_equity = get_live_aum()
    total_pnl      = get_live_pnl()
    pct_1d         = get_period_return(1)
    pct_7d         = get_period_return(7)
    pct_30d        = get_period_return(30)

    # TWR + initial AUM from fund metrics
    try:
        metrics     = compute_fund_metrics()
        performance = metrics["twr"]
        # Portfolio initial_investment = total capital deposited from bank (capital_events).
        # This is always correct and never double-counts internal reallocations.
        # e.g. £500K + £500K deposits = £1M regardless of how many times money moves
        #      between Chase1 ↔ Chase3xA internally.
        initial_investment = metrics["total_deposited"]
    except Exception:
        initial_investment = current_equity
        performance        = 0.0

    return {
        "initial_investment": initial_investment,
        "current_equity":     current_equity,
        "performance":        performance,
        "total_pnl":          total_pnl,
        "pct_1d":             pct_1d,
        "pct_7d":             pct_7d,
        "pct_30d":            pct_30d,
    }


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------

def get_equity_curve_data(days: Optional[int] = None) -> list[dict]:
    """
    Returns equity curve from balance_history filtered to the last N days.
    days=None → all history (SI).
    Returns list of { timestamp: str, equity: float }.
    """
    history = get_balance_history()
    if not history:
        return []

    if days is not None:
        cutoff = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        history = [r for r in history if r["date"] >= cutoff]

    return [
        {"timestamp": r["date"], "equity": r["investor_equity"]}
        for r in history
    ]


# ---------------------------------------------------------------------------
# Pod-level aggregates (from pfees + strategies + pods tables)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Broker daily-equity aggregation — feeds pod/strategy breakdown alongside pfees
#
# AXIA (axia_clients / axia_daily_equity) and IG (ig_clients / ig_daily_equity)
# are manually-entered broker NLV snapshots, GBP only — separate data sources
# from Darwinex pfees, and kept in PHYSICALLY SEPARATE tables per platform
# (confirmed with Nish 2026-08-27 — each platform exports as its own clean
# spreadsheet, not a mixed one). A strategy links to one AXIA client via
# strategies.axia_client_id, or one IG client via strategies.ig_client_id.
# Baseline (start of PnL/return calc) = that client's FIRST equity entry —
# NOT the manual "Initial Investment" field (confirmed with Nish 2026-08-18).
#
# _clients_by_id / _equity_series / _axia_strategy_agg below are parameterized
# by table name so AXIA and IG share the exact same logic without duplicating
# it — _ig_strategy_agg is a thin wrapper pointing at the IG tables/column.
# ---------------------------------------------------------------------------

def _axia_clients_by_id(table: str = "axia_clients") -> dict[str, dict]:
    """id -> {client, account, label} map for the given clients table, cached 60s."""
    def _fetch():
        rows = get_client().table(table).select("*").execute().data or []
        return {r["id"]: r for r in rows}
    return _get_cached(f"clients_by_id_{table}", _fetch)


_EQUITY_COLS = "id,client,account,trade_date,currency,equity,chg_nlv,capital_flow_type,capital_transfer_id"


def _equity_table_rows(table: str) -> list[dict]:
    """
    Every row of a daily-equity table (AXIA, IG, or a daily Data Feed),
    ordered by trade_date asc, cached 60s — ONE read per table per refresh.

    Perf 2026-10-05: previously each client was read separately for its
    series, again for its flagged-capital total, and the whole table again
    for linked ledger ids (axia_daily_equity was read 9x per Portfolio
    load). The three helpers below now filter this one cached list, giving
    identical results. Paged in 1,000s so PostgREST's row cap can never
    silently truncate it.
    """
    def _fetch():
        out, start, page = [], 0, 1000
        while True:
            rows = (
                get_client().table(table).select(_EQUITY_COLS)
                .order("trade_date", desc=False).order("id", desc=False)
                .range(start, start + page - 1)
                .execute().data or []
            )
            out.extend(rows)
            if len(rows) < page:
                return out
            start += page
    return _get_cached(f"equity_table_{table}", _fetch)


def _axia_equity_series(client: str, account: str, table: str = "axia_daily_equity") -> list[dict]:
    """
    GBP-only daily-equity rows for a client/account from the given table,
    sorted by date asc. Cached 60s. Returns [{ "date": "YYYY-MM-DD", "equity": float }, ...]
    """
    return [
        {"date": r["trade_date"], "equity": float(r["equity"])}
        for r in _equity_table_rows(table)
        if r["client"] == client and r["account"] == account and r["currency"] == "GBP"
    ]


def _daily_equity_tables() -> list[str]:
    """axia_daily_equity + ig_daily_equity + every registered daily-cadence
    Data Feed's equity table — every physical table capital_flow_type /
    capital_transfer_id can live on. Used to find equity-flow-linked ledger
    rows regardless of which feed created them, current or future."""
    tables = ["axia_daily_equity", "ig_daily_equity"]
    for feed in _data_feeds_registry():
        if feed.get("cadence") == "daily" and feed.get("equity_table"):
            tables.append(feed["equity_table"])
    return tables


def _equity_linked_capital_transfer_ids() -> set:
    """
    IDs of capital_transfers rows auto-created from an Initial Investment /
    Add-On flag on a daily-equity entry (AXIA, IG, or any registered
    daily-cadence Data Feed) — see sync_capital_flow_transfer.

    These rows stay in capital_transfers forever as an audit trail (who
    flagged what, when — matches the records-table TYPE badge), but must
    NOT be summed into Capital Invested / Capital Allocated anywhere:
    that figure is computed directly from the flagged equity rows
    themselves (see _axia_flagged_equity_total), so also summing their
    ledger mirror would double-count it — exactly the OPTIOS bug (2026-09-21).
    Manual Wallet<->Pod<->Strategy transfers created directly in Manage
    Pods & Strategies are NOT in this set and keep summing exactly as
    before. See README §10.3.
    """
    def _fetch():
        ids = set()
        for table in _daily_equity_tables():
            ids.update(int(r["capital_transfer_id"]) for r in _equity_table_rows(table)
                       if r.get("capital_transfer_id") is not None)
        return ids
    return _get_cached("equity_linked_capital_transfer_ids", _fetch)


def _axia_flagged_equity_total(client: str, account: str, table: str = "axia_daily_equity") -> float:
    """
    Sum of a client/account's GBP daily-equity rows flagged Initial
    Investment / Add-On (capital_flow_type in CAPITAL_FLOW_TYPES) — the
    contribution of each is CHG NLV if set, else Equity (matches
    sync_capital_flow_transfer's own contribution rule). This is the direct,
    single source of truth for that client's Capital Invested baseline —
    computed straight from the equity rows themselves, never from the
    capital_transfers ledger mirror those flags also create (audit trail
    only, see _equity_linked_capital_transfer_ids). 0.0 if nothing is
    flagged yet (transitional strategies pre-dating this feature).
    """
    def _fetch():
        rows = [r for r in _equity_table_rows(table)
                if r["client"] == client and r["account"] == account and r["currency"] == "GBP"]
        total = 0.0
        for r in rows:
            if r.get("capital_flow_type") not in CAPITAL_FLOW_TYPES:
                continue
            contrib = r.get("chg_nlv") if r.get("chg_nlv") is not None else r.get("equity")
            total += float(contrib or 0)
        return round(total, 2)
    return _get_cached(f"flagged_equity_total_{table}_{client}_{account}", _fetch)


def _capital_transfers_by_strategy() -> dict[int, dict]:
    """
    strategy_id -> { "in": float, "out": float } aggregated from the
    capital_transfers ledger (Wallet/Pod/Strategy typed transfers) —
    EXCLUDING rows linked from an equity-screen Initial Investment / Add-On
    flag (see _equity_linked_capital_transfer_ids). Those stay in the ledger
    as an audit trail but are counted towards Capital Invested via the
    flagged equity rows directly instead (_axia_flagged_equity_total), never
    from here too — summing both would double it.

    Separate from Darwinex's internal_transfers table — this ledger covers
    AXIA and manual/other strategies only, per the Wallet↔Pod↔Strategy
    funding-tranche model. "in" = cumulative capital moved INTO the
    strategy (never reduced — matches "Total Capital Invested" being
    monotonic). "out" = cumulative capital moved back OUT to a pod/wallet.
    """
    def _fetch():
        excluded = _equity_linked_capital_transfer_ids()
        rows = list_capital_transfers()   # same rows, shared cache (perf 2026-10-05; sums are order-independent)
        agg: dict[int, dict] = {}
        for r in rows:
            if r.get("id") in excluded:
                continue
            amt = float(r.get("amount") or 0)
            if r.get("to_type") == "strategy" and r.get("to_id") is not None:
                sid = int(r["to_id"])
                agg.setdefault(sid, {"in": 0.0, "out": 0.0})
                agg[sid]["in"] = round(agg[sid]["in"] + amt, 2)
            if r.get("from_type") == "strategy" and r.get("from_id") is not None:
                sid = int(r["from_id"])
                agg.setdefault(sid, {"in": 0.0, "out": 0.0})
                agg[sid]["out"] = round(agg[sid]["out"] + amt, 2)
        return agg
    return _get_cached("capital_transfers_by_strategy", _fetch)


def _banked_profit_by_strategy(strategies: list[dict]) -> dict[int, dict]:
    """
    strategy_id -> { "banked_profit": float, "capital_returned": float, "total_withdrawn": float }

    Banked Profit = realized profit AND loss withdrawn from a strategy —
    NOT just positive distributions. A closing withdrawal that returns less
    than the capital still allocated is a realized loss (negative value).

    total_withdrawn = capital_returned + banked_profit — the FULL cash amount
    that physically left the strategy, whichever portion it was. This is
    what reduces Capital Allocated (money that left is no longer "still
    out", whether it was profit or return of capital) — capital_returned
    alone is NOT enough, a pure-profit withdrawal (capital_returned == 0)
    must still reduce Capital Allocated by the amount withdrawn.

    Sourced from the OUTBOUND leg of each ledger (the leg that actually
    carries strategy identity):
      - capital_transfers: rows where from_type == "strategy"
      - internal_transfers (Darwinex): rows where to_account == "Wallet",
        matched to a strategy via its brokerage_account

    Rows with both capital_return_amount and profit_loss_amount still null
    are un-classified (pre-Phase-1 data) and are skipped — they don't
    silently count as profit or loss until tagged.
    """
    agg: dict[int, dict] = {}

    def _bump(sid, capital_return, profit_loss):
        agg.setdefault(sid, {"banked_profit": 0.0, "capital_returned": 0.0, "total_withdrawn": 0.0})
        cr = capital_return or 0.0
        pl = profit_loss or 0.0
        agg[sid]["banked_profit"]    = round(agg[sid]["banked_profit"] + pl, 2)
        agg[sid]["capital_returned"] = round(agg[sid]["capital_returned"] + cr, 2)
        agg[sid]["total_withdrawn"]  = round(agg[sid]["total_withdrawn"] + cr + pl, 2)

    for r in list_capital_transfers():
        if r.get("from_type") == "strategy" and r.get("from_id") is not None:
            cr = r.get("capital_return_amount")
            pl = r.get("profit_loss_amount")
            if cr is None and pl is None:
                continue
            _bump(int(r["from_id"]), cr, pl)

    broker_to_sid = {s["brokerage_account"]: s["id"] for s in strategies if s.get("brokerage_account")}
    for t in list_internal_transfers():
        if t.get("to_account") == "Wallet" and t.get("from_account") in broker_to_sid:
            cr = t.get("capital_return_amount")
            pl = t.get("profit_loss_amount")
            if cr is None and pl is None:
                continue
            _bump(broker_to_sid[t["from_account"]], cr, pl)

    return agg


def _banked_and_allocated(sid: int, initial: float, banked_agg: dict, darwinex_agg: dict) -> tuple[float, float]:
    """
    (banked_profit_gross, capital_allocated) for one strategy — the single
    source of truth used at strategy/pod/portfolio level alike.

    For a CLOSED Darwinex strategy (in darwinex_agg): banked_agg's ledger
    sum (profit_loss_amount only) is NOT reliable here — the "Final
    withdrawal" flow deliberately locks every leg as pure Capital Return
    (profit_loss_amount = 0), so banked_agg's "banked_profit"/"total_withdrawn"
    collapse to figures that don't reflect the strategy's true realized P&L
    or its (zero) remaining allocation. Derive both algebraically instead —
    identical to how banked_profit always worked before Final Withdrawal
    existed (gross cash recovered), just computed from the aggregator's
    true pnl instead of the ledger's classification:
      banked_gross = initial (frozen baseline) + agg["pnl"] (true realized)
                    = the full cash ever recovered, matching the identity
                      equity(0) + banked_gross - initial == agg["pnl"].
      allocated    = 0.0 — closed means nothing is left deployed, by
                     definition, regardless of ledger withdrawal bookkeeping.
    See README §14 / §9.
    """
    if sid in darwinex_agg:
        banked_gross = round(initial + darwinex_agg[sid]["pnl"], 2)
        return banked_gross, 0.0
    contrib = banked_agg.get(sid, {"banked_profit": 0.0, "total_withdrawn": 0.0})
    return contrib["banked_profit"], round(initial - contrib["total_withdrawn"], 2)


def _strategy_capital_invested(s: dict, net_deployed: dict, axia_agg: dict,
                                ct_by_strategy: dict, darwinex_agg: Optional[dict] = None) -> float:
    """
    Single source of truth for a strategy's "Total Capital Invested".
    Priority:
      0. CLOSED Darwinex strategy (status == "Closed", in darwinex_agg) →
         the frozen all-time Wallet->account deployed total from
         _darwinex_closed_strategy_agg. Takes priority over #1 below —
         net_deployed (a running NET of in/out flows) collapses toward the
         residual/loss amount once an account is fully closed out, which
         breaks the "never reduced by withdrawals" monotonic-invested
         principle (§4) and destroys Total ROI's denominator. See
         _darwinex_closed_strategy_agg's docstring for the full story.
      0b. Inactive/Closed with no bespoke-aggregator match → 0.0 (paused or
          wound down, nothing currently tracked — see README §14, three-state
          status model).
      1. brokerage_account set (Darwinex, still active/open) →
         net_deployed_per_account (unchanged — correct while a position is
         still open, since net_deployed represents what's still committed).
      2. AXIA/IG/Data-Feed-linked → axia_agg's own baseline (2026-09-21:
         checked BEFORE the raw ledger sum below — axia_agg's baseline
         already prioritises flagged equity rows directly over the ledger,
         see _axia_strategy_agg / _axia_flagged_equity_total. Equity-linked
         ledger rows are audit-trail only now, see
         _equity_linked_capital_transfer_ids — reading them here too would
         double what axia_agg already counted, the OPTIOS bug).
      3. capital_transfers ledger has inbound entries (manual Wallet/Pod
         /Strategy funding, no equity link at all) → sum of those.
      4. else → manual initial_investment field on the strategy row
         (transitional fallback).
    """
    darwinex_agg = darwinex_agg or {}
    status = s.get("status") or "Active"
    if s["id"] in darwinex_agg:
        return darwinex_agg[s["id"]]["baseline"]
    # Inactive (paused) or Closed (wound down) with no bespoke-aggregator
    # match: Capital Invested is 0 — a strategy with no real performance
    # data tracked shouldn't show a stale deposit figure as "invested."
    # Mark it Active once ready to record again (see README §14).
    if status in ("Inactive", "Closed"):
        return 0.0
    acct = s.get("brokerage_account")
    if acct:
        return net_deployed.get(acct, 0.0)
    if (s.get("axia_client_id") or s.get("ig_client_id") or s.get("data_feed_id")) and s["id"] in axia_agg:
        return axia_agg[s["id"]]["baseline"]
    ct = ct_by_strategy.get(s["id"])
    if ct and ct["in"] > 0:
        return round(ct["in"], 2)
    return float(s.get("initial_investment") or 0)


def _round2(x: float) -> float:
    """
    Round to 2dp the way a person expects (round-HALF-UP), not the way
    Python's built-in round() sometimes does.

    Bug found 2026-09-24 (Nish report, NEWS-01): a 50% profit share on
    £4,532.53 is exactly £2,266.265 -- but round(2266.265, 2) returns
    2266.26, not 2266.27. Reason: 2266.265 isn't exactly representable in
    binary floating point; it's actually stored as ~2266.26499999999...,
    so Python's round() (which is also banker's-rounding on true halfway
    ties, a second, separate reason it can differ from round-half-up) sees
    a value already fractionally below .265 and rounds down. Decimal,
    constructed from the value's own repr (str(x), not float(x) again --
    that would just reintroduce the same binary imprecision), sidesteps
    both problems and always rounds financial halves up, matching what
    anyone doing this by hand would expect.
    """
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _apply_watermark(strategy: dict, raw_equity: float, baseline: float):
    """
    Adjust raw fund/broker equity down to Chase's actual economic equity,
    per the strategy's watermark/profit_share_pct fields (general to any
    strategy type — not AXIA-specific).

    Below watermark: Chase retains 100% (no adjustment).
    Above watermark: Chase retains profit_share_pct% of the excess only —
    the rest is the trader's/counterparty's profit share, not Chase's.

    Both fields optional; either unset => pass-through (pre-Phase-2 behavior,
    current_equity == raw_equity).

    Returns (chase_equity, chase_pnl) where chase_pnl = chase_equity - baseline.
    Uses _round2 (round-half-up), not Python's built-in round() -- see its
    docstring for why a plain round() can silently round a profit-share
    split down by a cent on an exact-half-cent boundary (e.g. NEWS-01).
    """
    wm    = strategy.get("watermark")
    share = strategy.get("profit_share_pct")
    if wm is None or share is None:
        return _round2(raw_equity), _round2(raw_equity - baseline)
    wm    = float(wm)
    share = float(share)
    if raw_equity <= wm:
        chase_equity = raw_equity
    else:
        chase_equity = wm + (raw_equity - wm) * (share / 100.0)
    chase_equity = _round2(chase_equity)
    return chase_equity, _round2(chase_equity - baseline)


def _axia_strategy_agg(
    strategies: list[dict],
    client_field:  str = "axia_client_id",
    clients_table: str = "axia_clients",
    equity_table:  str = "axia_daily_equity",
) -> dict[int, dict]:
    """
    strategy_id -> { invested, pnl, baseline, series } for strategies with
    `client_field` set. invested = Chase's economic equity — the raw latest
    GBP equity, adjusted for watermark/profit_share_pct if the strategy has
    them set (see _apply_watermark); pass-through (== raw latest) otherwise.

    baseline (cost basis / "Total Capital Invested") — NOT watermark-adjusted,
    it's the cost basis, not the current value:
      - if the client has GBP equity rows flagged Initial Investment/Add-On,
        sum those directly (2026-09-21 — the source of truth now; accounts
        for multi-tranche funding, e.g. £100k then +£150k later, without
        relying on the capital_transfers ledger mirror those flags also
        create — that mirror is audit-trail only, see
        _equity_linked_capital_transfer_ids)
      - else if the strategy has inbound MANUAL capital_transfers logged
        (funded via Manage Pods & Strategies, no equity flag involved),
        use that sum (legacy path, pre-dating the flag feature)
      - else fall back to first GBP equity entry (transitional, pre-backfill)

    pnl = chase_equity − baseline, so it stays correct under either source.

    Parameterized by client_field/clients_table/equity_table so AXIA and IG
    (physically separate tables — see the section docstring above) share
    this exact logic with zero duplication. Default args = AXIA, unchanged
    from before parameterization. See _ig_strategy_agg for the IG wrapper.
    """
    clients_idx    = _axia_clients_by_id(clients_table)
    ct_by_strategy = _capital_transfers_by_strategy()
    out: dict[int, dict] = {}
    for s in strategies:
        cid = s.get(client_field)
        if not cid:
            continue
        cl = clients_idx.get(cid)
        if not cl:
            continue
        series = _axia_equity_series(cl["client"], cl["account"], equity_table)
        if not series:
            out[s["id"]] = {"invested": 0.0, "pnl": 0.0, "baseline": 0.0, "series": [], "raw_equity": 0.0}
            continue
        latest       = series[-1]["equity"]
        flagged_total = _axia_flagged_equity_total(cl["client"], cl["account"], equity_table)
        if flagged_total > 0:
            baseline = flagged_total
        else:
            ct = ct_by_strategy.get(s["id"])
            if ct and ct["in"] > 0:
                baseline = round(ct["in"], 2)
            else:
                baseline = series[0]["equity"]
        chase_equity, chase_pnl = _apply_watermark(s, latest, baseline)
        out[s["id"]] = {
            "invested":   chase_equity,
            "pnl":        chase_pnl,
            "baseline":   baseline,
            "series":     series,
            "raw_equity": round(latest, 2),   # gross, before watermark/profit-share -- 2026-09-24 CSV export
        }
    return out


def _ig_strategy_agg(strategies: list[dict]) -> dict[int, dict]:
    """
    IG counterpart of _axia_strategy_agg — same mechanism, sourced from the
    separate ig_clients / ig_daily_equity tables and strategies.ig_client_id.
    Merged into the same combined dict as AXIA's output at both call sites
    (a strategy can only have one or the other set, so no key collision),
    so every downstream consumer that keys off "is this strategy_id in
    axia_agg" already handles IG for free — see _build_pod_pfees_map and the
    hierarchy/breakdown row builder.
    """
    return _axia_strategy_agg(
        strategies,
        client_field="ig_client_id",
        clients_table="ig_clients",
        equity_table="ig_daily_equity",
    )


def _fund_statement_strategy_agg(strategies: list[dict], table: str = "fund_monthly_statements") -> dict[int, dict]:
    """
    strategy_id -> { invested, pnl, baseline, series, raw_invested, currency }
    for strategies fed by `table` (default fund_monthly_statements — 12-FLAGS,
    ASLAN LABS) — NAV-administrator-reported funds with no daily broker feed,
    keyed directly by strategy_id (one statement stream per strategy; no
    separate "clients" table needed the way AXIA has, since there's no
    multi-account concept here).

    Parameterized by `table` so any monthly-cadence Data Feed (see
    data_feeds registry, _data_feed_agg) reuses this exact logic against
    its own physically separate statements table — default unchanged, zero
    behavior change for 12-FLAGS/ASLAN LABS.

    raw_invested = latest period's ending_balance_gbp (already FX-converted
    by OANDA at entry time, or the raw GBP figure if currency == "GBP"). A
    strategy whose latest row has no fx_rate yet (USD statement, OANDA fetch
    failed / pending retry) is EXCLUDED here rather than fed a USD number
    mislabelled as GBP — falls through to the strategy's existing 0.0
    default until FX is fetched or manually retried.

    invested/pnl = raw_invested run through _apply_watermark against the
    strategy's own watermark/profit_share_pct (e.g. ASLAN LABS: wm 100,000,
    80% split — Chase's economic equity is NOT the raw NAV administrator
    balance once a profit-split manager is involved). Both fields optional;
    unset on either => pass-through, invested == raw_invested (12-FLAGS'
    current setup — no split, so this fix changes nothing for it).

    baseline mirrors _strategy_capital_invested's own fallback chain:
    capital_transfers ledger inbound sum if logged, else manual
    initial_investment — so pnl stays sane before the funding transfer
    (Wallet -> Strategy) has been entered.
    """
    rows = list_fund_statements(table=table)
    by_strategy: dict[int, list[dict]] = {}
    for r in rows:
        by_strategy.setdefault(r["strategy_id"], []).append(r)

    ct_by_strategy = _capital_transfers_by_strategy()
    out: dict[int, dict] = {}
    for s in strategies:
        sid    = s["id"]
        srows  = by_strategy.get(sid)
        if not srows:
            continue
        srows.sort(key=lambda r: r["period_end_date"])
        latest = srows[-1]
        if latest.get("ending_balance_gbp") is None:
            continue
        ct           = ct_by_strategy.get(sid)
        baseline     = round(ct["in"], 2) if ct and ct["in"] > 0 else float(s.get("initial_investment") or 0)
        raw_invested = round(float(latest["ending_balance_gbp"]), 2)
        chase_equity, chase_pnl = _apply_watermark(s, raw_invested, baseline)
        out[sid] = {
            "invested":     chase_equity,
            "pnl":          chase_pnl,
            "baseline":     baseline,
            "series":       srows,
            "raw_invested": raw_invested,
            "currency":     latest.get("currency"),
        }
    return out


# ---------------------------------------------------------------------------
# Data Feeds registry — self-service tab-builder (confirmed with Nish
# 2026-09-01). Each row in `data_feeds` describes one broker/statement
# source Nish creates from the "Create Data Feed" UI in Data & Reports:
# name, color, cadence ('daily' or 'monthly'), currency, and the physical
# table name(s) backing it — a daily feed gets its own <slug>_clients +
# <slug>_daily_equity pair (same shape as AXIA/IG), a monthly feed gets its
# own <slug>_monthly_statements table (same shape as fund_monthly_statements,
# 12-FLAGS/ASLAN LABS). Those physical tables are created by Nish running a
# one-time SQL script Claude generates — never run by Claude directly (see
# README standing rule) — the registry row is only added AFTER that SQL has
# run, so _data_feed_agg never queries a table that doesn't exist yet.
#
# A strategy links to at most one feed via strategies.data_feed_id (always)
# + strategies.data_feed_client_id (daily cadence only — which row in that
# feed's clients table). This is entirely separate from the legacy
# axia_client_id/ig_client_id columns, which stay untouched.
# ---------------------------------------------------------------------------

def _data_feeds_registry() -> list[dict]:
    """All registered Data Feed configs, ordered by sort_order. Cached 60s."""
    def _fetch():
        rows = get_client().table("data_feeds").select("*").order("sort_order").execute().data or []
        return rows
    return _get_cached("data_feeds_registry", _fetch)


def get_data_feed_by_slug(slug: str) -> Optional[dict]:
    """Single registry row by slug, or None if no feed with that slug exists."""
    for f in _data_feeds_registry():
        if f["slug"] == slug:
            return f
    return None


def _data_feed_agg(strategies: list[dict]) -> dict[int, dict]:
    """
    strategy_id -> { invested, pnl, baseline, series, ... } merged across
    EVERY registered Data Feed. For each feed, filter strategies down to the
    ones actually linked to THAT feed (data_feed_id match) before handing
    them to the existing parameterized aggregator — daily cadence reuses
    _axia_strategy_agg (same mechanism as AXIA/IG), monthly cadence reuses
    _fund_statement_strategy_agg (same mechanism as 12-FLAGS/ASLAN LABS).
    Zero duplicated math — both aggregators were already parameterized by
    table name for exactly this purpose.

    Merged into the same combined dict as AXIA/IG's output at both call
    sites (disjoint keys — a strategy can only be linked to one source), so
    every downstream consumer keeps working unchanged. See _ig_strategy_agg
    for the precedent.
    """
    out: dict[int, dict] = {}
    for feed in _data_feeds_registry():
        linked = [s for s in strategies if s.get("data_feed_id") == feed["id"]]
        if not linked:
            continue
        cadence = feed.get("cadence")
        if cadence == "daily":
            contrib = _axia_strategy_agg(
                linked,
                client_field="data_feed_client_id",
                clients_table=feed["clients_table"],
                equity_table=feed["equity_table"],
            )
        elif cadence == "monthly":
            contrib = _fund_statement_strategy_agg(linked, table=feed["statements_table"])
        else:
            continue
        out.update(contrib)
    return out


def _darwinex_closed_strategy_agg(strategies: list[dict]) -> dict[int, dict]:
    """
    strategy_id -> { invested, pnl, baseline, series } for CLOSED (status ==
    "Closed") Darwinex strategies — i.e. brokerage_account set, no live
    user_pfees_estimation feed for that account (Darwinex is tracked purely
    via manually-entered internal_transfers, never had a daily broker feed).

    Why this exists (bug found during the post-12-FLAGS bridge audit):
    without it, a closed Darwinex strategy falls through to the generic
    brokerage_account -> net_deployed path, which feeds _apply_watermark a
    raw_equity of 0.0 (no feed => darwin_agg lookup always misses) against a
    baseline of net_deployed (Wallet<->account NET, all time). Two problems:
      1. pnl = 0 - net_deployed wipes out the ENTIRE historical capital as
         "loss", not just the amount actually unrecovered (e.g. DARWIN-1X
         showed -£899,950 instead of its real closed-out loss of
         -£11,007.75 — net_deployed hadn't even had the final closing leg
         entered yet, and even once it is, see point 2).
      2. Once the closing leg IS entered, net_deployed collapses toward the
         residual/loss amount itself (since it's returns netted against
         inflows) — so while the raw P&L happens to come out right, both
         "Capital Invested" (no longer the true historical total, breaking
         the monotonic-invested principle, §4) and Total ROI (denominator
         collapses alongside the numerator, producing a nonsensical ~-100%
         instead of the true ~-1% of capital actually lost) break instead.

    This function sidesteps both: baseline is the frozen ALL-TIME sum of
    Wallet->account inflows (never reduced — matches every other strategy
    type's Capital Invested semantics), pnl is ALL-TIME returned minus
    ALL-TIME deployed (the true realized gain/loss, since the account is
    closed and current equity is definitionally 0 — nothing left open).

    Wallet-mediated inter-strategy rebalances (e.g. Chase1->Chase3xA,
    27-03-2026, £100,000): Darwinex has no direct account->account
    transfer row — every relocation between two live strategies shows up
    as TWO separate internal_transfers legs on the same day for the same
    amount (X->Wallet, then Wallet->Y). That's not trading profit for X
    and not a fresh capital raise for Y — it's Chase's own capital moving
    between two of its own strategies. Y's baseline already correctly
    counts the inbound leg as genuine new capital (untouched). X's
    baseline (Capital Invested) is reduced by the matched amount instead,
    so X isn't shown as having "earned back" money it actually just
    handed to a sibling strategy — this is a pure display/ROI-denominator
    adjustment: pnl (= returned − deployed, both left unreduced) is
    algebraically identical either way, so realized P&L never moves.
    The row itself stays unclassified in the ledger (no
    capital_return_amount/profit_loss_amount) — it's neither a capital
    return nor a profit/loss event, just a relocation (§12.3).

    Only applies to strategies with status == "Closed" — an ACTIVE
    Darwinex strategy with a matched live feed keeps using the existing
    acct_agg/broker-matching path untouched, and "Inactive" (paused, not
    genuinely wound down) is a separate state handled elsewhere (README
    §14) — this function only ever overrides the fully-wound-down case.
    """
    transfers = list_internal_transfers()
    by_account: dict[str, dict] = {}
    for t in transfers:
        frm, to, amt = t["from_account"], t["to_account"], float(t["amount"])
        if frm == "Wallet" and to != "Wallet":
            entry = by_account.setdefault(to, {"deployed": 0.0, "returned": 0.0, "rebalance_out": 0.0, "legs": []})
            entry["deployed"] += amt
            entry["legs"].append(t)
        elif to == "Wallet" and frm != "Wallet":
            entry = by_account.setdefault(frm, {"deployed": 0.0, "returned": 0.0, "rebalance_out": 0.0, "legs": []})
            entry["returned"] += amt
            entry["legs"].append(t)
        # account<->account rebalance (direct, no Wallet hop): excluded
        # from both sides entirely — not a Chase-level inflow/outflow.
        # (Darwinex data never actually has this row shape — see the
        # Wallet-mediated matching pass below for the real-world case.)

    # ── Wallet-mediated inter-strategy rebalance detection ──────────────
    # Same-day, same-amount X->Wallet + Wallet->Y pair = a relocation, not
    # a real capital event for X. Reduce X's baseline only; leave pnl,
    # and Y's baseline, untouched (see docstring). Y MUST be a tracked
    # Darwinex strategy's own brokerage_account — matching against ANY
    # same-day/same-amount coincidence (e.g. a small pass-through account
    # like XPF2026 that happens to move the same amount the same day) is a
    # false positive that quietly shrinks X's baseline for no real reason.
    tracked_accounts = {s["brokerage_account"] for s in strategies if s.get("brokerage_account")}
    outbound = [t for t in transfers if t["to_account"] == "Wallet" and t["from_account"] != "Wallet"]
    inbound  = [t for t in transfers if t["from_account"] == "Wallet" and t["to_account"] != "Wallet"
                and t["to_account"] in tracked_accounts]
    matched_inbound = set()
    for o in outbound:
        for i in inbound:
            if (id(i) not in matched_inbound
                    and i["to_account"] != o["from_account"]
                    and i["transfer_date"] == o["transfer_date"]
                    and round(float(i["amount"]), 2) == round(float(o["amount"]), 2)):
                by_account[o["from_account"]]["rebalance_out"] += float(o["amount"])
                matched_inbound.add(id(i))
                break

    out: dict[int, dict] = {}
    for s in strategies:
        if s.get("status") != "Closed":
            continue
        acct = s.get("brokerage_account")
        if not acct or acct not in by_account:
            continue
        agg      = by_account[acct]
        baseline = round(agg["deployed"] - agg["rebalance_out"], 2)
        pnl      = round(agg["returned"] - agg["deployed"], 2)
        out[s["id"]] = {
            "invested": 0.0,   # closed — nothing left open
            "pnl":      pnl,
            "baseline": baseline,
            "series":   sorted(agg["legs"], key=lambda t: t["transfer_date"]),
        }
    return out


def _closed_manual_strategy_agg(strategies: list[dict], banked_agg: dict) -> dict[int, dict]:
    """
    strategy_id -> { invested, pnl, baseline, series } for a CLOSED or
    Inactive strategy with NO bespoke live-feed aggregator (not Darwinex,
    AXIA, or fund-statement) that has a real recorded closing outcome via
    the Capital Transfers ledger — a Strategy -> Wallet leg with
    capital_return_amount/profit_loss_amount classified (e.g. NEWS-01:
    £16,000 initial, closed out for £20,532.53 total, £4,532.53 banked).

    Same shape and purpose as _darwinex_closed_strategy_agg, just fed by the
    manual/off-platform funding path (capital_transfers ledger) instead of
    Darwinex's internal_transfers. The RESULT of this function is merged
    directly into darwinex_agg at every call site (see _build_pod_pfees_map
    and the hierarchy/breakdown row builder) rather than threaded as a
    separate parameter — every downstream consumer (_strategy_capital_invested,
    _banked_and_allocated, has_data checks) already keys off "is this
    strategy_id in darwinex_agg", so merging means zero changes needed to
    any of that machinery.

    Without this, a manual strategy marked Inactive/Closed after a real
    profitable exit falls through to the generic "no bespoke aggregator"
    branch: Capital Invested collapses to 0.0 (see
    _strategy_capital_invested's Inactive/Closed rule), Current Equity/Total
    P&L show 0/0 instead of the real numbers, and Capital Allocated goes
    NEGATIVE (initial − total_withdrawn, with initial wrongly zeroed) —
    exactly the class of bug found for closed Darwinex strategies during
    the earlier audit (see that function's docstring), just via a different
    ledger. See README §14.

    invested = 0.0 (closed — nothing left deployed).
    pnl      = raw ledger profit/loss (banked_agg's "banked_profit" — sum of
               profit_loss_amount across its classified outbound Capital
               Transfer legs) run through _apply_watermark against the raw
               close-out equity (baseline + raw pnl). The ledger entry
               records the FULL cash amount physically returned (e.g.
               NEWS-01: £20,532.53 total moved Strategy -> Wallet), which
               is the counterparty/trader's raw result, not yet split —
               same as fund-statement strategies (ASLAN LABS): a
               watermark/profit_share_pct on the strategy means Chase's
               real economic pnl is only its share of the excess above
               watermark, not the raw ledger figure. Both fields optional;
               unset on either => pass-through (raw pnl unchanged).
    baseline = the strategy's ORIGINAL committed capital, via the same
               fallback _strategy_capital_invested uses for an Active
               strategy with no brokerage/AXIA link (inbound
               capital_transfers sum if any, else the manual
               initial_investment field) — frozen, never reduced by the
               closing withdrawal.
    """
    out: dict[int, dict] = {}
    ct_by_strategy = _capital_transfers_by_strategy()
    for s in strategies:
        sid    = s["id"]
        status = s.get("status") or "Active"
        if status not in ("Inactive", "Closed"):
            continue
        if s.get("brokerage_account") or s.get("axia_client_id") or s.get("ig_client_id") or s.get("data_feed_id"):
            continue  # handled by their own bespoke aggregators already
        contrib = banked_agg.get(sid)
        if not contrib or contrib["total_withdrawn"] == 0:
            continue  # no recorded closing outcome — leave to the generic 0.0 fallback
        ct         = ct_by_strategy.get(sid)
        baseline   = round(ct["in"], 2) if ct and ct["in"] > 0 else float(s.get("initial_investment") or 0)
        raw_pnl    = contrib["banked_profit"]
        raw_equity = round(baseline + raw_pnl, 2)
        chase_equity, chase_pnl = _apply_watermark(s, raw_equity, baseline)
        out[sid] = {
            "invested": 0.0,
            "pnl":      chase_pnl,
            "baseline": baseline,
            "series":   [],
            "raw_pnl":  raw_pnl,
        }
    return out


def _build_pod_pfees_map() -> dict:
    """
    Returns { pod_id: { "invested": float, "pnl": float, "darwins": [str] } }

    Mapping logic:
      1. pfees snapshot → Darwin codes with Invested + PnL
      2. strategies table → Darwin (strategy_code) → pod_id
      3. If strategies empty or Darwin not mapped → bucket into pod_id=-1 ("Unallocated")

    Also returns pod metadata from pods table.
    """
    snapshot   = get_pfees_latest_snapshot()
    strategies = list_strategies()         # may be empty
    pods_list  = list_pods()              # may be empty

    # ── 3-level pod mapping (mirrors hierarchy trader/strategy lookup chain) ──
    #
    # Level 1: account_id (integer) on strategy → pod_id   (most precise)
    # Level 2: brokerage_account on strategy → AccountId via ratio matching → pod_id
    #          Uses _match_pfees_accounts_to_brokerage() — works without account_id column
    # Level 3: strategy_code → Darwin display prefix → pod_id  (last resort)

    account_to_pod: dict[int, int] = {}   # pfees AccountId int → pod_id
    darwin_to_pod:  dict[str, int] = {}   # Darwin prefix str  → pod_id
    broker_to_pod:  dict[str, int] = {}   # brokerage_account  → pod_id

    for s in strategies:
        pid = s.get("pod_id")
        if pid is None:
            continue
        acct_id = s.get("account_id")
        if acct_id is not None:
            account_to_pod[int(acct_id)] = pid
        broker = s.get("brokerage_account")
        if broker:
            broker_to_pod[broker] = pid
        code = (s.get("strategy_code") or "").upper()
        if code:
            darwin_to_pod[code] = pid

    # Resolve broker→AccountId mapping once (cached)
    acct_broker_map: dict[int, str] = _match_pfees_accounts_to_brokerage()   # {acct_int: "Chase1"}

    # Aggregate pfees by pod_id
    pod_agg: dict = {}   # pod_id → { invested, pnl, darwins }
    UNALLOCATED = -1

    for row in snapshot:
        darwin_raw = (row.get("Darwin") or "")
        darwin     = _darwin_display(darwin_raw)   # CFZ.5.18 → CFZ
        invested   = float(row.get("Invested") or 0)
        pnl        = float(row.get("Current PnL") or 0)
        raw_acct   = row.get("AccountId")
        acct       = int(raw_acct) if raw_acct is not None else None

        # Level 1: direct account_id match
        if acct is not None and acct in account_to_pod:
            pod_id = account_to_pod[acct]
        # Level 2: brokerage_account → AccountId ratio match → pod
        elif acct is not None and acct in acct_broker_map and acct_broker_map[acct] in broker_to_pod:
            pod_id = broker_to_pod[acct_broker_map[acct]]
        # Level 3: Darwin display name matches strategy_code
        else:
            pod_id = darwin_to_pod.get(darwin, UNALLOCATED)

        if pod_id not in pod_agg:
            pod_agg[pod_id] = {"invested": 0.0, "pnl": 0.0, "darwins": []}
        pod_agg[pod_id]["invested"] = round(pod_agg[pod_id]["invested"] + invested, 2)
        pod_agg[pod_id]["pnl"]      = round(pod_agg[pod_id]["pnl"] + pnl, 2)
        if darwin:
            pod_agg[pod_id]["darwins"].append(darwin)

    # ── AXIA/IG-linked strategies — add their equity into pod_agg alongside
    #    pfees. Merged into one dict (disjoint keys — a strategy has at most
    #    one of axia_client_id/ig_client_id set) so every downstream consumer
    #    below keeps working unchanged for both platforms. ──
    axia_agg = {**_axia_strategy_agg(strategies), **_ig_strategy_agg(strategies), **_data_feed_agg(strategies)}
    for s in strategies:
        contrib = axia_agg.get(s["id"])
        if not contrib:
            continue
        pid = s.get("pod_id")
        if pid is None:
            pid = UNALLOCATED
        if pid not in pod_agg:
            pod_agg[pid] = {"invested": 0.0, "pnl": 0.0, "darwins": []}
        pod_agg[pid]["invested"] = round(pod_agg[pid]["invested"] + contrib["invested"], 2)
        pod_agg[pid]["pnl"]      = round(pod_agg[pid]["pnl"] + contrib["pnl"], 2)

    # ── Fund-statement strategies (e.g. 12-FLAGS) — same fold-in as AXIA ──
    fund_agg = _fund_statement_strategy_agg(strategies)
    for s in strategies:
        contrib = fund_agg.get(s["id"])
        if not contrib:
            continue
        pid = s.get("pod_id")
        if pid is None:
            pid = UNALLOCATED
        if pid not in pod_agg:
            pod_agg[pid] = {"invested": 0.0, "pnl": 0.0, "darwins": []}
        pod_agg[pid]["invested"] = round(pod_agg[pid]["invested"] + contrib["invested"], 2)
        pod_agg[pid]["pnl"]      = round(pod_agg[pid]["pnl"] + contrib["pnl"], 2)

    # ── Closed Darwinex strategies, AND closed manual/off-platform
    #    strategies with a recorded Capital Transfers exit — merged into one
    #    dict since every downstream consumer keys off "is this strategy_id
    #    in darwinex_agg" (see _closed_manual_strategy_agg's docstring).
    #    invested is always 0.0 here (closed) so only pnl actually contributes ──
    banked_agg_pm = _banked_profit_by_strategy(strategies)
    darwinex_agg  = {
        **_darwinex_closed_strategy_agg(strategies),
        **_closed_manual_strategy_agg(strategies, banked_agg_pm),
    }
    for s in strategies:
        contrib = darwinex_agg.get(s["id"])
        if not contrib:
            continue
        pid = s.get("pod_id")
        if pid is None:
            pid = UNALLOCATED
        if pid not in pod_agg:
            pod_agg[pid] = {"invested": 0.0, "pnl": 0.0, "darwins": []}
        pod_agg[pid]["invested"] = round(pod_agg[pid]["invested"] + contrib["invested"], 2)
        pod_agg[pid]["pnl"]      = round(pod_agg[pid]["pnl"] + contrib["pnl"], 2)

    # ── Active strategies with NO real performance source matched anywhere
    #    (no pfees row via account_id/brokerage_account/darwin code, not
    #    AXIA/fund/closed-Darwinex) — fold in at-par (equity == invested,
    #    pnl 0.0) so pod-level Capital Allocated reflects real recorded
    #    capital instead of silently omitting it. Inactive/Closed strategies
    #    with no data contribute nothing (their `initial` is already 0.0 —
    #    see _strategy_capital_invested). See README §14.
    net_deployed_pm   = _get_net_deployed_per_account()
    ct_by_strategy_pm = _capital_transfers_by_strategy()
    broker_to_acct_pm = {v: k for k, v in acct_broker_map.items()}
    darwin_agg_pm: dict = {}
    acct_agg_pm:   dict = {}
    for row in snapshot:
        d_ = _darwin_display(row.get("Darwin") or "")
        darwin_agg_pm[d_] = True
        a_ = row.get("AccountId")
        if a_ is not None:
            acct_agg_pm[int(a_)] = True
    for s in strategies:
        code_pm    = (s.get("strategy_code") or "").upper()
        acct_id_pm = s.get("account_id")
        broker_pm  = s.get("brokerage_account")
        has_data_pm = (
            s["id"] in axia_agg
            or s["id"] in fund_agg
            or s["id"] in darwinex_agg
            or (acct_id_pm is not None and int(acct_id_pm) in acct_agg_pm)
            or (broker_pm is not None and broker_to_acct_pm.get(broker_pm) in acct_agg_pm)
            or code_pm in darwin_agg_pm
        )
        if has_data_pm:
            continue
        status_pm = s.get("status") or "Active"
        if status_pm != "Active":
            continue
        initial_pm = _strategy_capital_invested(s, net_deployed_pm, axia_agg, ct_by_strategy_pm, darwinex_agg)
        if not initial_pm:
            continue
        pid = s.get("pod_id")
        if pid is None:
            pid = UNALLOCATED
        if pid not in pod_agg:
            pod_agg[pid] = {"invested": 0.0, "pnl": 0.0, "darwins": []}
        pod_agg[pid]["invested"] = round(pod_agg[pid]["invested"] + initial_pm, 2)

    return {
        "pod_agg":       pod_agg,
        "pods_list":     pods_list,
        "strategies":    strategies,
        "UNALLOCATED":   UNALLOCATED,
        "_snapshot":     snapshot,   # reused by _fast variants to avoid extra DB call
        "_axia_agg":     axia_agg,   # strategy_id -> { invested, pnl, baseline, series }
        "_darwinex_agg": darwinex_agg,   # strategy_id -> { invested, pnl, baseline, series }
        "_fund_agg":   fund_agg,   # strategy_id -> { invested, pnl, baseline, series }
    }


def get_pods_with_kpis() -> list[dict]:
    """
    Returns list of PodSummary-compatible dicts with live KPIs per pod.

    When pods table is empty: returns one entry per Darwin code from pfees.
    When pods table is populated: groups darwins by pod.
    """
    data         = _build_pod_pfees_map()
    pod_agg      = data["pod_agg"]
    pods_list    = data["pods_list"]
    UNALLOCATED  = data["UNALLOCATED"]

    # Build pod metadata index
    pods_idx = {p["id"]: p for p in pods_list}

    pct_1d  = get_period_return(1)
    pct_7d  = get_period_return(7)
    pct_30d = get_period_return(30)

    # Net deployed per brokerage account — pod initial = sum of net deployed
    # for all strategies in that pod that have a brokerage_account set.
    # Hybrid: auto when brokerage_account set, manual initial_investment fallback.
    net_deployed = _get_net_deployed_per_account()
    axia_agg     = data.get("_axia_agg") or {}

    def _strategy_initial(s: dict) -> float:
        """Auto from transfers if brokerage_account set, AXIA/IG baseline if
        broker-linked, else manual initial_investment."""
        acct = s.get("brokerage_account")
        if acct:
            return net_deployed.get(acct, 0.0)
        if (s.get("axia_client_id") or s.get("ig_client_id") or s.get("data_feed_id")) and s["id"] in axia_agg:
            return axia_agg[s["id"]]["baseline"]
        return float(s.get("initial_investment") or 0)

    result = []

    if pods_list:
        # Mode A: pods table populated — one card per pod
        for pod in pods_list:
            pid   = pod["id"]
            agg   = pod_agg.get(pid, {"invested": 0.0, "pnl": 0.0})
            initial = sum(
                _strategy_initial(s)
                for s in data["strategies"]
                if s.get("pod_id") == pid
            )
            result.append({
                "entity_id": f"pod_{pid}",
                "name":      pod.get("name", f"Pod {pid}"),
                "pod_code":  pod.get("pod_code", ""),
                "pod_color": pod.get("color", "#6366f1"),
                "kpis": {
                    "initial_investment": round(initial, 2),
                    "current_equity":     agg["invested"],
                    "performance":        round(agg["pnl"] / initial, 6) if initial else 0.0,
                    "total_pnl":          agg["pnl"],
                    "pct_1d":             pct_1d,
                    "pct_7d":             pct_7d,
                    "pct_30d":            pct_30d,
                },
            })

    else:
        # Mode B: no pods table — one card per Darwin (raw pfees grouping)
        # Group by display name (prefix only: CFZ.5.18 → CFZ)
        snapshot = get_pfees_latest_snapshot()
        darwin_agg: dict = {}
        for row in snapshot:
            darwin   = _darwin_display(row.get("Darwin") or "")
            invested = float(row.get("Invested") or 0)
            pnl      = float(row.get("Current PnL") or 0)
            if darwin not in darwin_agg:
                darwin_agg[darwin] = {"invested": 0.0, "pnl": 0.0}
            darwin_agg[darwin]["invested"] = round(darwin_agg[darwin]["invested"] + invested, 2)
            darwin_agg[darwin]["pnl"]      = round(darwin_agg[darwin]["pnl"] + pnl, 2)

        for i, (darwin, agg) in enumerate(darwin_agg.items()):
            result.append({
                "entity_id": f"darwin_{darwin.lower()}",
                "name":      darwin,
                "pod_code":  darwin[:3],
                "pod_color": "#6366f1",
                "kpis": {
                    "initial_investment": agg["invested"],
                    "current_equity":     agg["invested"],
                    "performance":        0.0,
                    "total_pnl":          agg["pnl"],
                    "pct_1d":             pct_1d,
                    "pct_7d":             pct_7d,
                    "pct_30d":            pct_30d,
                },
            })

    return result


def get_allocation_data() -> list[dict]:
    """
    Donut chart slices: allocation by pod.
    Returns list of { name, aum, pct }.
    """
    data       = _build_pod_pfees_map()
    pod_agg    = data["pod_agg"]
    pods_list  = data["pods_list"]
    UNALLOCATED = data["UNALLOCATED"]

    pods_idx   = {p["id"]: p for p in pods_list}
    total      = sum(v["invested"] for v in pod_agg.values())
    if total == 0:
        return []

    slices = []
    for pid, agg in pod_agg.items():
        if agg["invested"] == 0:
            continue
        if pid == UNALLOCATED:
            name = "Unallocated"
        else:
            pod = pods_idx.get(pid, {})
            name = pod.get("name") or f"Pod {pid}"
        slices.append({
            "name": name,
            "aum":  agg["invested"],
            "pct":  round(agg["invested"] / total * 100, 2),
        })

    slices.sort(key=lambda x: x["aum"], reverse=True)
    return slices


def get_pnl_contribution_data() -> list[dict]:
    """
    Bar chart: PnL contribution by pod.
    Returns list of { name, pnl }.
    """
    data        = _build_pod_pfees_map()
    pod_agg     = data["pod_agg"]
    pods_list   = data["pods_list"]
    UNALLOCATED = data["UNALLOCATED"]

    pods_idx = {p["id"]: p for p in pods_list}
    bars     = []

    for pid, agg in pod_agg.items():
        if pid == UNALLOCATED:
            name = "Unallocated"
        else:
            pod  = pods_idx.get(pid, {})
            name = pod.get("name") or f"Pod {pid}"
        bars.append({"name": name, "pnl": agg["pnl"]})

    bars.sort(key=lambda x: x["pnl"], reverse=True)
    return bars


# ---------------------------------------------------------------------------
# Hierarchy table rows (Pods / Strategies / Traders tabs)
# ---------------------------------------------------------------------------

def get_hierarchy_rows(entity_type: str) -> list[dict]:
    """
    Returns BreakdownRow-compatible dicts for the hierarchy tabs.

    entity_type in {"pod", "strategy", "trader"}

    Sources:
      - pods       + pfees → pod rows
      - strategies + pfees → strategy rows
      - trader             → empty list (populate when data available)
    """
    pct_1d  = get_period_return(1)
    pct_7d  = get_period_return(7)
    pct_30d = get_period_return(30)

    if entity_type == "pod":
        data        = _build_pod_pfees_map()
        pod_agg     = data["pod_agg"]
        pods_list   = data["pods_list"]
        UNALLOCATED = data["UNALLOCATED"]
        pods_idx    = {p["id"]: p for p in pods_list}

        total_aum        = sum(v["invested"] for v in pod_agg.values()) or 1.0
        net_deployed     = _get_net_deployed_per_account()
        hier_axia_agg    = data.get("_axia_agg") or {}
        hier_darwinex_agg = data.get("_darwinex_agg") or {}
        ct_by_strategy   = _capital_transfers_by_strategy()
        rows             = []

        for pid, agg in pod_agg.items():
            if pid == UNALLOCATED:
                pod = {"name": "Unallocated", "pod_code": "", "color": "#6b7280", "id": -1}
            else:
                pod = pods_idx.get(pid, {"name": f"Pod {pid}", "pod_code": "", "color": "#6366f1", "id": pid})

            initial = sum(
                _strategy_capital_invested(s, net_deployed, hier_axia_agg, ct_by_strategy, hier_darwinex_agg)
                for s in data["strategies"]
                if s.get("pod_id") == pid
            ) if pid != UNALLOCATED else agg["invested"]

            rows.append({
                "entity_id":      f"pod_{pid}",
                "name":           pod.get("name", ""),
                "entity_type":    "pod",
                "allocation_pct": round(agg["invested"] / total_aum * 100, 2),
                "aum":            agg["invested"],
                "pnl":            agg["pnl"],
                "pct_1d":         pct_1d,
                "pct_7d":         pct_7d,
                "pct_30d":        pct_30d,
                "drawdown":       0.0,
                "win_rate":       0.0,
                "trading_style":  None,
                "status":         "Active",
                "pod_code":       pod.get("pod_code", ""),
                "strategy_code":  None,
                "pod_color":      pod.get("color", "#6366f1"),
            })

        rows.sort(key=lambda x: x["aum"], reverse=True)
        return rows

    elif entity_type == "strategy":
        snapshot   = get_pfees_latest_snapshot()
        strategies = list_strategies()
        pods_list  = list_pods()
        pods_idx   = {p["id"]: p for p in pods_list}

        # Aggregate pfees by Darwin display name (prefix only: CFZ.5.18 → CFZ)
        darwin_agg: dict = {}
        for row in snapshot:
            darwin   = _darwin_display(row.get("Darwin") or "")
            invested = float(row.get("Invested") or 0)
            pnl      = float(row.get("Current PnL") or 0)
            if darwin not in darwin_agg:
                darwin_agg[darwin] = {"invested": 0.0, "pnl": 0.0}
            darwin_agg[darwin]["invested"] = round(darwin_agg[darwin]["invested"] + invested, 2)
            darwin_agg[darwin]["pnl"]      = round(darwin_agg[darwin]["pnl"] + pnl, 2)

        # ALSO aggregate pfees by AccountId — primary lookup when account_id set on strategy
        acct_agg: dict[int, dict] = {}
        for row in snapshot:
            acct     = int(row.get("AccountId") or 0)
            invested = float(row.get("Invested") or 0)
            pnl      = float(row.get("Current PnL") or 0)
            if acct not in acct_agg:
                acct_agg[acct] = {"invested": 0.0, "pnl": 0.0}
            acct_agg[acct]["invested"] = round(acct_agg[acct]["invested"] + invested, 2)
            acct_agg[acct]["pnl"]      = round(acct_agg[acct]["pnl"] + pnl, 2)

        strat_axia_agg     = {**_axia_strategy_agg(strategies), **_ig_strategy_agg(strategies), **_data_feed_agg(strategies)}
        strat_fund_agg     = _fund_statement_strategy_agg(strategies)
        # Merged with closed manual/off-platform exits — see
        # _closed_manual_strategy_agg's docstring for why this merges here.
        strat_darwinex_agg = {
            **_darwinex_closed_strategy_agg(strategies),
            **_closed_manual_strategy_agg(strategies, _banked_profit_by_strategy(strategies)),
        }
        total_aum          = (
            sum(v["invested"] for v in darwin_agg.values())
            + sum(v["invested"] for v in strat_axia_agg.values())
            + sum(v["invested"] for v in strat_fund_agg.values())
        ) or 1.0
        rows                = []
        strat_net_deployed  = _get_net_deployed_per_account()
        strat_ct_by_strategy = _capital_transfers_by_strategy()

        # Broker → AccountId reverse mapping (for strategies with brokerage_account set)
        acct_to_broker_s = _match_pfees_accounts_to_brokerage()   # { acct_int: "Chase1" }
        broker_to_acct_s = {v: k for k, v in acct_to_broker_s.items()}  # { "Chase1": acct_int }

        # Per-account equity series from user_accounts_equity — used for per-strategy
        # period returns (1D/7D/30D) and max drawdown
        acct_eq_series = _per_account_equity_series()   # { account_id: [{ date, equity }] }

        def _strat_account_id(s: dict) -> Optional[int]:
            """Resolve AccountId for a strategy: direct > broker match > None."""
            aid = s.get("account_id")
            if aid is not None:
                return int(aid)
            broker = s.get("brokerage_account")
            if broker:
                return broker_to_acct_s.get(broker)   # may be None
            return None

        if strategies:
            for s in strategies:
                code    = (s.get("strategy_code") or "").upper()
                acct_id = s.get("account_id")
                broker  = s.get("brokerage_account")

                # Lookup priority:
                # 1. axia_client_id/ig_client_id — AXIA/IG broker-statement equity (its own tier, not pfees)
                # 2. fund_monthly_statements — NAV-administrator-reported funds (12-FLAGS)
                # 3. closed Darwinex strategy — realized P&L from internal_transfers
                # 4. account_id column directly (if set + matches pfees AccountId)
                # 5. brokerage_account → AccountId via invest-vs-deploy matching
                # 6. strategy_code → Darwin display name fallback
                axia_contrib     = strat_axia_agg.get(s["id"]) if (s.get("axia_client_id") or s.get("ig_client_id") or s.get("data_feed_id")) else None
                fund_contrib     = strat_fund_agg.get(s["id"])
                darwinex_contrib = strat_darwinex_agg.get(s["id"])
                if axia_contrib is not None:
                    agg = {"invested": axia_contrib["invested"], "pnl": axia_contrib["pnl"]}
                elif fund_contrib is not None:
                    agg = {"invested": fund_contrib["invested"], "pnl": fund_contrib["pnl"]}
                elif darwinex_contrib is not None:
                    agg = {"invested": darwinex_contrib["invested"], "pnl": darwinex_contrib["pnl"]}
                elif acct_id is not None and int(acct_id) in acct_agg:
                    agg = acct_agg[int(acct_id)]
                elif broker and broker_to_acct_s.get(broker) in acct_agg:
                    agg = acct_agg[broker_to_acct_s[broker]]
                else:
                    agg = darwin_agg.get(code, {"invested": 0.0, "pnl": 0.0})

                pid        = s.get("pod_id")
                pod        = pods_idx.get(pid, {}) if pid else {}
                pod_joined = s.get("pods") or {}
                pod_color  = pod_joined.get("color") or pod.get("color") or "#6366f1"
                pod_code_s = pod_joined.get("pod_code") or pod.get("pod_code") or ""

                initial = _strategy_capital_invested(s, strat_net_deployed, strat_axia_agg, strat_ct_by_strategy, strat_darwinex_agg)

                # Per-strategy period returns — AXIA equity series if AXIA-linked,
                # else per-account equity series from user_accounts_equity
                if axia_contrib is not None:
                    acct_series = axia_contrib["series"]
                else:
                    resolved_aid = _strat_account_id(s)
                    acct_series  = acct_eq_series.get(resolved_aid, []) if resolved_aid else []
                s_pct_1d     = _account_period_return(acct_series, 1)  if acct_series else pct_1d
                s_pct_7d     = _account_period_return(acct_series, 7)  if acct_series else pct_7d
                s_pct_30d    = _account_period_return(acct_series, 30) if acct_series else pct_30d

                # Max drawdown from per-account equity series
                s_drawdown = 0.0
                if acct_series:
                    peak = -float("inf")
                    for pt in acct_series:
                        eq = pt["equity"]
                        if eq > peak:
                            peak = eq
                        if peak > 0:
                            dd = (eq - peak) / peak
                            if dd < s_drawdown:
                                s_drawdown = dd

                rows.append({
                    "entity_id":      f"strategy_{s['id']}",
                    "name":           s.get("name", code),
                    "entity_type":    "strategy",
                    "allocation_pct": round(agg["invested"] / total_aum * 100, 2),
                    "aum":            agg["invested"],
                    "pnl":            agg["pnl"],
                    "pct_1d":         s_pct_1d,
                    "pct_7d":         s_pct_7d,
                    "pct_30d":        s_pct_30d,
                    "drawdown":       round(s_drawdown, 6),
                    "win_rate":       0.0,
                    "trading_style":  s.get("trading_style", None),
                    "status":         s.get("status", "Active"),
                    "pod_code":       pod_code_s,
                    "strategy_code":  s.get("strategy_code", ""),
                    "pod_color":      pod_color,
                })
        else:
            # No strategies — one row per Darwin code
            for darwin, agg in darwin_agg.items():
                rows.append({
                    "entity_id":      f"darwin_{darwin.lower()}",
                    "name":           darwin,
                    "entity_type":    "strategy",
                    "allocation_pct": round(agg["invested"] / total_aum * 100, 2),
                    "aum":            agg["invested"],
                    "pnl":            agg["pnl"],
                    "pct_1d":         pct_1d,
                    "pct_7d":         pct_7d,
                    "pct_30d":        pct_30d,
                    "drawdown":       0.0,
                    "win_rate":       0.0,
                    "trading_style":  None,
                    "status":         "Active",
                    "pod_code":       darwin[:3],
                    "strategy_code":  darwin,
                    "pod_color":      "#6366f1",
                })

        rows.sort(key=lambda x: x["aum"], reverse=True)
        return rows

    elif entity_type == "trader":
        snapshot   = get_pfees_latest_snapshot()
        history    = get_pfees_history_all()          # full date history for metrics
        strategies = list_strategies()
        pods_list  = list_pods()
        pods_idx   = {p["id"]: p for p in pods_list}

        # ── Lookup chain (3 levels) ──────────────────────────────────────────
        # 1. account_id on strategy matches pfees AccountId integer directly
        acct_to_strat: dict[int, dict] = {}
        for s in strategies:
            aid = s.get("account_id")
            if aid is not None:
                acct_to_strat[int(aid)] = s

        # 2. brokerage_account on strategy matched to pfees AccountId via
        #    invested-vs-net_deployed ratio comparison (works without account_id column)
        broker_to_strat: dict[str, dict] = {}
        for s in strategies:
            broker = s.get("brokerage_account")
            if broker:
                broker_to_strat[broker] = s
        acct_to_broker = _match_pfees_accounts_to_brokerage()   # { acct_int: "Chase1" }

        # 3. strategy_code → strategy (last resort; works only if code == Darwin prefix)
        darwin_to_strat: dict[str, dict] = {}
        for s in strategies:
            code = (s.get("strategy_code") or "").upper()
            if code:
                darwin_to_strat[code] = s

        # Pre-compute per-(AccountId, Darwin) metrics from full history
        trader_metrics = _compute_trader_metrics(history)

        # Total invested per AccountId — denominator for within-strategy allocation %
        acct_total: dict[int, float] = {}
        for row in snapshot:
            acct     = int(row.get("AccountId") or 0)
            invested = float(row.get("Invested") or 0)
            acct_total[acct] = round(acct_total.get(acct, 0.0) + invested, 2)

        rows = []
        for row in snapshot:
            darwin_raw     = (row.get("Darwin") or "").strip()
            darwin_display = _darwin_display(darwin_raw)   # CFZ.5.18 → CFZ
            acct           = int(row.get("AccountId") or 0)
            invested       = float(row.get("Invested") or 0)
            pnl            = float(row.get("Current PnL") or 0)

            # Strategy lookup: account_id → brokerage_account match → darwin code
            broker = acct_to_broker.get(acct)
            strat  = (
                acct_to_strat.get(acct)                             # level 1
                or (broker_to_strat.get(broker) if broker else None)  # level 2 ← key fix
                or darwin_to_strat.get(darwin_display)              # level 3
                or {}
            )
            pid   = strat.get("pod_id")
            pod   = pods_idx.get(pid, {}) if pid else {}

            # Pod color: prefer joined pod data from list_strategies() join,
            # fall back to pods_idx lookup, then generic blue
            pod_joined  = strat.get("pods") or {}   # from .select("*,pods(name,color,pod_code)")
            pod_color   = pod_joined.get("color") or pod.get("color") or "#6366f1"
            pod_code_t  = pod_joined.get("pod_code") or pod.get("pod_code") or ""

            # Per-Darwin metrics from full history
            m         = trader_metrics.get((acct, darwin_raw), {})
            t_pct_1d  = m.get("pct_1d",       0.0)
            t_pct_7d  = m.get("pct_7d",        0.0)
            t_pct_30d = m.get("pct_30d",       0.0)
            t_max_dd  = m.get("max_drawdown",  0.0)

            # Allocation % within AccountId (strategy-level denominator)
            acct_tot  = acct_total.get(acct, 0.0)
            alloc_pct = round(invested / acct_tot * 100, 2) if acct_tot else 0.0

            rows.append({
                "entity_id":      f"trader_{darwin_raw.lower().replace('.', '_')}_{acct}",
                "name":           darwin_display,
                "entity_type":    "trader",
                "allocation_pct": alloc_pct,
                "aum":            invested,
                "pnl":            pnl,
                "pct_1d":         t_pct_1d,
                "pct_7d":         t_pct_7d,
                "pct_30d":        t_pct_30d,
                "drawdown":       t_max_dd,
                "win_rate":       0.0,
                "trading_style":  None,
                "status":         "Active",
                "pod_code":       pod_code_t,
                "strategy_code":  strat.get("strategy_code", ""),
                "pod_color":      pod_color,
            })

        rows.sort(key=lambda x: x["aum"], reverse=True)
        return rows

    # trader — return empty until trader-level data available
    return []


# ---------------------------------------------------------------------------
# _fast variants — accept pre-fetched data, zero extra DB round-trips
# Called by get_portfolio() endpoint to share data across all computations
# ---------------------------------------------------------------------------

def _period_return_from_hist(history: list[dict], days: int) -> float:
    """
    Compute % equity change over last N days from pre-fetched balance history.

    Prefers pfees historical Invested (consistent with AUM card).
    Falls back: pfees → user_accounts_equity → summed balance_history.
    Uses latest date in data as reference — not today — so stale data returns
    real period returns rather than 0.0.
    """
    eq_by_date = _pfees_equity_by_date()
    if not eq_by_date:
        eq_by_date = _portfolio_equity_by_date()
    if not eq_by_date:
        eq_by_date = _sum_equity_by_date(history)
    if len(eq_by_date) < 2:
        return 0.0
    sorted_dates = sorted(eq_by_date.keys())
    latest_date  = sorted_dates[-1]
    latest       = eq_by_date[latest_date]
    cutoff_str   = (datetime.fromisoformat(latest_date) - timedelta(days=days)).strftime("%Y-%m-%d")
    past_dates   = [d for d in sorted_dates if d <= cutoff_str]
    if not past_dates:
        return 0.0
    past_equity = eq_by_date[past_dates[-1]]
    if past_equity == 0:
        return 0.0
    return round((latest - past_equity) / past_equity, 6)


def compute_fund_metrics_fast(events: list[dict], history: list[dict]) -> dict:
    """compute_fund_metrics() using pre-fetched events + history."""
    current_aum = get_live_aum()   # single pfees query (cached)
    total_pnl   = get_live_pnl()   # single pfees query (cached)

    # Portfolio equity per date — prefer pfees historical Invested (same source as
    # get_live_aum so end_aum always matches the AUM card).  Fall back chain:
    # pfees → user_accounts_equity → summed balance_history.
    equity_by_date: dict[str, float] = _pfees_equity_by_date()
    if not equity_by_date:
        equity_by_date = _portfolio_equity_by_date()
    if not equity_by_date:
        equity_by_date = _sum_equity_by_date(history)
    sorted_hist_dates = sorted(equity_by_date.keys())

    external = [e for e in events if e["event_type"] in ("deposit", "withdrawal")]

    total_deposited = round(sum(e["amount"]       for e in external if e["amount"] > 0), 2)
    total_withdrawn = round(sum(abs(e["amount"])   for e in external if e["amount"] < 0), 2)
    bank_balance    = round(total_deposited - total_withdrawn, 2)

    # Use Darwinex internal_transfers net cash flows for accurate TWR periods
    # (cached — no extra DB call)
    darwinex_flows   = _get_darwinex_cashflows()
    cashflow_by_date = {f["date"]: f["amount"] for f in darwinex_flows}

    periods      = []
    sorted_dates = [f["date"] for f in darwinex_flows]
    if not sorted_dates:
        sorted_dates = sorted(set(e["date"] for e in external))
        cashflow_by_date = {d: round(sum(e["amount"] for e in external if e["date"] == d), 2)
                            for d in sorted_dates}

    last_eq  = sorted_hist_dates[-1] if sorted_hist_dates else None
    first_eq = sorted_hist_dates[0]  if sorted_hist_dates else None

    # ── Classify boundaries ─────────────────────────────────────────────────
    pre_cash  = 0.0
    pre_start = None
    mid_dates: list[str] = []

    for d in sorted_dates:
        if last_eq and d > last_eq:
            continue  # future — skip
        cf       = cashflow_by_date.get(d, 0.0)
        before_d = [x for x in sorted_hist_dates if x < d]
        if before_d:
            mid_dates.append(d)
        else:
            pre_cash += cf
            if pre_start is None:
                pre_start = d

    # ── Build periods ────────────────────────────────────────────────────────
    if pre_cash > 0 and last_eq:
        if mid_dates:
            avail_before = [d for d in sorted_hist_dates if d < mid_dates[0]]
            p0_end       = max(avail_before) if avail_before else None
        else:
            p0_end = last_eq

        if p0_end and p0_end in equity_by_date:
            _s   = pre_start or (sorted_dates[0] if sorted_dates else str(date.today()))
            _ea  = equity_by_date[p0_end]
            if not mid_dates:
                _implied = round(_ea - total_pnl, 2)
                _start   = _implied if _implied > 0 else pre_cash
            else:
                _start = equity_by_date[sorted_hist_dates[0]] if sorted_hist_dates else pre_cash
            _pnl = round(_ea - _start, 2)
            _pr  = round(_pnl / _start, 6) if _start else 0.0
            periods.append({
                "period_num":         1,
                "start_date":         _s,
                "end_date":           p0_end,
                "start_aum":          _start,
                "cash_flow_at_start": pre_cash,
                "end_aum":            _ea,
                "pnl":                _pnl,
                "period_return":      _pr,
                "annualised_return":  _annualised(_pr, _s, p0_end),
            })

    for j, bd in enumerate(mid_dates):
        if j + 1 < len(mid_dates):
            avail_before = [d for d in sorted_hist_dates if d < mid_dates[j + 1]]
            p_end        = max(avail_before) if avail_before else None
        else:
            p_end = last_eq

        cf            = cashflow_by_date.get(bd, 0.0)
        before_bd     = [d for d in sorted_hist_dates if d < bd]
        equity_before = equity_by_date[max(before_bd)] if before_bd else 0.0
        start_aum     = round(equity_before + cf, 2)
        end_aum       = equity_by_date.get(p_end, start_aum) if p_end else start_aum
        pnl           = round(end_aum - start_aum, 2)
        pr            = round(pnl / start_aum, 6) if start_aum else 0.0
        pnum          = len(periods) + 1
        periods.append({
            "period_num":         pnum,
            "start_date":         bd,
            "end_date":           p_end or bd,
            "start_aum":          start_aum,
            "cash_flow_at_start": cf,
            "end_aum":            end_aum,
            "pnl":                pnl,
            "period_return":      pr,
            "annualised_return":  _annualised(pr, bd, p_end or bd),
        })

    twr = round(
        reduce(lambda acc, p: acc * (1.0 + p["period_return"]), periods, 1.0) - 1.0, 6
    ) if periods else 0.0

    initial_aum = periods[0]["start_aum"] if periods else 0.0
    cap_dates      = sorted(set(e["date"] for e in external))
    inception_date = cap_dates[0] if cap_dates else (sorted_dates[0] if sorted_dates else str(date.today()))

    return {
        "twr":              twr,
        "total_pnl":        total_pnl,
        "initial_aum":      initial_aum,
        "current_aum":      current_aum,
        "bank_balance":     bank_balance,
        "total_deposited":  total_deposited,
        "total_withdrawn":  total_withdrawn,
        "periods":          periods,
        "events":           events,
        "num_periods":      len(periods),
        "inception_date":   inception_date,
        "last_updated":     str(date.today()),
    }


def get_portfolio_kpis_fast(pod_pfees_map: dict, balance_hist: list[dict],
                             capital_evts: list[dict]) -> dict:
    """
    get_portfolio_kpis() with pre-fetched data — 0 extra DB calls.

    STRATEGY CALCULATIONS -> POD AGGREGATION -> PORTFOLIO AGGREGATION.
    Portfolio = strict sum of Strategies (equivalently, sum of Pods — Pods
    are themselves a sum of their Strategies, see get_pods_with_kpis_fast).
    Every headline figure here is built by summing get_strategies_with_kpis_fast's
    already-correct per-strategy kpis — never recomputed independently from
    raw snapshot/fund/darwinex sources. This guarantees the portfolio hero
    card always reconciles exactly against the Pod and Strategy overview
    cards, by construction, with no separate "uninvested cash" concept —
    per the Capital & Performance Overview spec (README §14).
    """
    strat_rows = get_strategies_with_kpis_fast(pod_pfees_map, balance_hist)

    invested      = round(sum(r["kpis"]["initial_investment"] for r in strat_rows), 2)
    current_equity = round(sum(r["kpis"]["current_equity"] for r in strat_rows), 2)
    banked         = round(sum(r["kpis"]["banked_profit"] for r in strat_rows), 2)
    banked_true    = round(sum(r["kpis"]["banked_profit_true"] for r in strat_rows), 2)
    allocated      = round(sum(r["kpis"]["capital_allocated"] for r in strat_rows), 2)
    # Total P&L = Current Equity + Banked Profit − Total Capital Invested
    # (uses gross `banked`, the internal formula input — identical
    # derivation to computeCapitalMetrics on the frontend, and to every
    # individual strategy card, so this always matches their sum exactly).
    total_pnl = round(current_equity + banked - invested, 2)
    roi       = round(total_pnl / invested, 6) if invested else 0.0

    # active_capital_invested — DISPLAY-ONLY metric added 2026-09 (Nish
    # request), for the Portfolio hero strip's "Capital Invested" box.
    # Strictly `initial_investment` summed over strategies whose status is
    # literally "Active" only — Inactive AND Closed both excluded (unlike
    # `invested` above, which already zeroes non-Darwinex Inactive/Closed
    # strategies via _strategy_capital_invested, but still carries a frozen
    # closed-Darwinex baseline forward for historical realized-P&L tracking,
    # see §12). This ties out automatically as strategies are added,
    # activated, or deactivated — no manual bookkeeping needed.
    #
    # NOT used anywhere else — Total P&L / Total ROI / Capital Allocated
    # above still derive from `invested` (all-time), unchanged, to preserve
    # the Portfolio = strict sum of Pods = strict sum of Strategies
    # reconciliation invariant (README §14). This field is additive only.
    active_capital_invested = round(sum(
        r["kpis"]["initial_investment"] for r in strat_rows if r.get("status") == "Active"
    ), 2)

    pct_1d  = _period_return_from_hist(balance_hist, 1)
    pct_7d  = _period_return_from_hist(balance_hist, 7)
    pct_30d = _period_return_from_hist(balance_hist, 30)
    try:
        performance = compute_fund_metrics_fast(capital_evts, balance_hist)["twr"]
    except Exception:
        performance = 0.0

    return {
        "initial_investment":      invested,
        "active_capital_invested": active_capital_invested,
        "current_equity":          current_equity,
        "performance":             performance,
        "total_pnl":               total_pnl,
        "banked_profit":           banked,
        "banked_profit_true":      banked_true,
        "capital_allocated":       allocated,
        "pct_1d":                  pct_1d,
        "pct_7d":                  pct_7d,
        "pct_30d":                 pct_30d,
    }


def get_pods_with_kpis_fast(pod_pfees_map: dict, balance_hist: list[dict]) -> list[dict]:
    """get_pods_with_kpis() with pre-fetched data — 0 extra DB calls."""
    pod_agg     = pod_pfees_map["pod_agg"]
    pods_list   = pod_pfees_map["pods_list"]
    strategies  = pod_pfees_map["strategies"]
    UNALLOCATED = pod_pfees_map["UNALLOCATED"]

    pct_1d  = _period_return_from_hist(balance_hist, 1)
    pct_7d  = _period_return_from_hist(balance_hist, 7)
    pct_30d = _period_return_from_hist(balance_hist, 30)

    net_deployed   = _get_net_deployed_per_account()
    axia_agg       = pod_pfees_map.get("_axia_agg") or {}
    darwinex_agg   = pod_pfees_map.get("_darwinex_agg") or {}
    ct_by_strategy = _capital_transfers_by_strategy()
    banked_agg     = _banked_profit_by_strategy(strategies)

    result = []

    if pods_list:
        for pod in pods_list:
            pid       = pod["id"]
            agg       = pod_agg.get(pid, {"invested": 0.0, "pnl": 0.0})
            pod_strats = [s for s in strategies if s.get("pod_id") == pid]
            initial   = sum(
                _strategy_capital_invested(s, net_deployed, axia_agg, ct_by_strategy, darwinex_agg)
                for s in pod_strats
            )
            per_strat = [
                _banked_and_allocated(
                    s["id"],
                    _strategy_capital_invested(s, net_deployed, axia_agg, ct_by_strategy, darwinex_agg),
                    banked_agg, darwinex_agg,
                )
                for s in pod_strats
            ]
            banked    = round(sum(b for b, _ in per_strat), 2)
            allocated = round(sum(a for _, a in per_strat), 2)
            # banked_profit_true: see get_portfolio_kpis_fast's identical note.
            banked_true = round(sum(
                darwinex_agg[s["id"]]["pnl"] if s["id"] in darwinex_agg
                else banked_agg.get(s["id"], {}).get("banked_profit", 0.0)
                for s in pod_strats
            ), 2)
            result.append({
                "entity_id": f"pod_{pid}",
                "name":      pod.get("name", f"Pod {pid}"),
                "pod_code":  pod.get("pod_code", ""),
                "pod_color": pod.get("color", "#6366f1"),
                "status":    pod.get("status") or "Active",
                "kpis": {
                    "initial_investment": round(initial, 2),
                    "current_equity":     agg["invested"],
                    "performance":        round(agg["pnl"] / initial, 6) if initial else 0.0,
                    "total_pnl":          agg["pnl"],
                    "banked_profit":      banked,
                    "banked_profit_true": banked_true,
                    "capital_allocated":  allocated,
                    "pct_1d":             pct_1d,
                    "pct_7d":             pct_7d,
                    "pct_30d":            pct_30d,
                },
            })
    else:
        # Mode B: no pods — one card per Darwin (display prefix only)
        darwin_agg: dict = {}
        for row in pod_pfees_map.get("_snapshot", get_pfees_latest_snapshot()):
            darwin   = _darwin_display(row.get("Darwin") or "")
            invested = float(row.get("Invested") or 0)
            pnl      = float(row.get("Current PnL") or 0)
            if darwin not in darwin_agg:
                darwin_agg[darwin] = {"invested": 0.0, "pnl": 0.0}
            darwin_agg[darwin]["invested"] = round(darwin_agg[darwin]["invested"] + invested, 2)
            darwin_agg[darwin]["pnl"]      = round(darwin_agg[darwin]["pnl"] + pnl, 2)
        for darwin, agg in darwin_agg.items():
            result.append({
                "entity_id": f"darwin_{darwin.lower()}",
                "name":      darwin,
                "pod_code":  darwin[:3],
                "pod_color": "#6366f1",
                "kpis": {
                    "initial_investment": agg["invested"],
                    "current_equity":     agg["invested"],
                    "performance":        0.0,
                    "total_pnl":          agg["pnl"],
                    "banked_profit":      0.0,
                    "capital_allocated":  agg["invested"],
                    "pct_1d":             pct_1d,
                    "pct_7d":             pct_7d,
                    "pct_30d":            pct_30d,
                },
            })

    return result


def get_strategies_with_kpis_fast(pod_pfees_map: dict, balance_hist: list[dict]) -> list[dict]:
    """
    Strategy-level equivalent of get_pods_with_kpis_fast() — one KPI card per
    strategy (Initial Invested / Current Equity / Total PnL / Performance),
    used by the Portfolio page's Strategy Overview section.

    Same 4-tier lookup priority as the hierarchy 'strategy' rows:
    AXIA client equity > account_id > brokerage_account > strategy_code (Darwin).
    Strategies already come back ordered by name (list_strategies() query),
    so new strategies slot into alphabetical position automatically.
    """
    strategies   = pod_pfees_map["strategies"]
    pods_list    = pod_pfees_map["pods_list"]
    pods_idx     = {p["id"]: p for p in pods_list}
    axia_agg     = pod_pfees_map.get("_axia_agg") or {}
    fund_agg     = pod_pfees_map.get("_fund_agg") or {}
    darwinex_agg = pod_pfees_map.get("_darwinex_agg") or {}
    snapshot     = pod_pfees_map.get("_snapshot", get_pfees_latest_snapshot())

    pct_1d  = _period_return_from_hist(balance_hist, 1)
    pct_7d  = _period_return_from_hist(balance_hist, 7)
    pct_30d = _period_return_from_hist(balance_hist, 30)

    net_deployed   = _get_net_deployed_per_account()
    ct_by_strategy = _capital_transfers_by_strategy()
    banked_agg     = _banked_profit_by_strategy(strategies)

    darwin_agg: dict = {}
    acct_agg:   dict = {}
    for row in snapshot:
        darwin   = _darwin_display(row.get("Darwin") or "")
        invested = float(row.get("Invested") or 0)
        pnl      = float(row.get("Current PnL") or 0)
        darwin_agg.setdefault(darwin, {"invested": 0.0, "pnl": 0.0})
        darwin_agg[darwin]["invested"] = round(darwin_agg[darwin]["invested"] + invested, 2)
        darwin_agg[darwin]["pnl"]      = round(darwin_agg[darwin]["pnl"] + pnl, 2)

        acct = int(row.get("AccountId") or 0)
        acct_agg.setdefault(acct, {"invested": 0.0, "pnl": 0.0})
        acct_agg[acct]["invested"] = round(acct_agg[acct]["invested"] + invested, 2)
        acct_agg[acct]["pnl"]      = round(acct_agg[acct]["pnl"] + pnl, 2)

    acct_to_broker_s = _match_pfees_accounts_to_brokerage()
    broker_to_acct_s = {v: k for k, v in acct_to_broker_s.items()}

    result = []
    for s in strategies:
        code    = (s.get("strategy_code") or "").upper()
        acct_id = s.get("account_id")
        broker  = s.get("brokerage_account")

        axia_contrib     = axia_agg.get(s["id"]) if (s.get("axia_client_id") or s.get("ig_client_id") or s.get("data_feed_id")) else None
        fund_contrib     = fund_agg.get(s["id"])
        darwinex_contrib = darwinex_agg.get(s["id"])
        if axia_contrib is not None:
            agg = {"invested": axia_contrib["invested"], "pnl": axia_contrib["pnl"]}
        elif fund_contrib is not None:
            agg = {"invested": fund_contrib["invested"], "pnl": fund_contrib["pnl"]}
        elif darwinex_contrib is not None:
            agg = {"invested": darwinex_contrib["invested"], "pnl": darwinex_contrib["pnl"]}
        elif acct_id is not None and int(acct_id) in acct_agg:
            agg = acct_agg[int(acct_id)]
        elif broker and broker_to_acct_s.get(broker) in acct_agg:
            agg = acct_agg[broker_to_acct_s[broker]]
        else:
            agg = darwin_agg.get(code, {"invested": 0.0, "pnl": 0.0})

        initial = _strategy_capital_invested(s, net_deployed, axia_agg, ct_by_strategy, darwinex_agg)

        # has_data: true once ANY real performance source is matched — a
        # live pfees feed (by account_id/brokerage_account/darwin code), or
        # one of the bespoke aggregators (AXIA/fund/closed-Darwinex). False
        # means only a capital figure exists (e.g. a Capital Transfers
        # deposit with nothing tracking performance yet) — see README §14.
        has_data = (
            axia_contrib is not None
            or fund_contrib is not None
            or darwinex_contrib is not None
            or (acct_id is not None and int(acct_id) in acct_agg)
            or (broker is not None and broker_to_acct_s.get(broker) in acct_agg)
            or code in darwin_agg
        )

        # AXIA-linked strategies are already watermark-adjusted inside
        # _axia_strategy_agg (baseline used there too); fund-statement and
        # closed-Darwinex strategies are already fully resolved (their own
        # bespoke aggregators compute the correct realized figures directly,
        # against their own frozen baselines) — re-applying watermark here
        # would clobber them, since `initial` above may now differ from the
        # raw pfees-derived baseline the watermark pass-through expects.
        # Apply here only for the remaining general case (Darwinex still
        # open, manual strategies) — not AXIA/fund/closed-Darwinex-specific.
        # gross_equity (2026-09-24 CSV export, Nish): the raw equity BEFORE
        # watermark/profit-share, from whichever source this strategy is
        # actually keyed off. Captured before any of the branches below
        # overwrite `agg` with the post-watermark net figure.
        if axia_contrib is not None:
            gross_equity = axia_contrib.get("raw_equity", axia_contrib["invested"])
        elif fund_contrib is not None:
            gross_equity = fund_contrib.get("raw_invested", fund_contrib["invested"])
        elif darwinex_contrib is not None:
            gross_equity = 0.0   # closed strategy — nothing left open, gross == net == 0
        elif not has_data:
            gross_equity = initial   # at-par, no watermark applies
        else:
            gross_equity = agg["invested"]   # general branch, raw pfees-derived equity, pre-watermark

        if not has_data:
            # No real performance source tracked for this strategy yet.
            # Active: show real Capital Invested at par (equity == invested,
            # pnl 0) — NOT a fabricated 100% loss. Inactive/Closed with no
            # data: `initial` is already 0.0 (see _strategy_capital_invested),
            # so this naturally zeroes everywhere. See README §14.
            agg = {"invested": initial, "pnl": 0.0}
        elif axia_contrib is None and fund_contrib is None and darwinex_contrib is None:
            chase_equity, chase_pnl = _apply_watermark(s, agg["invested"], initial)
            agg = {"invested": chase_equity, "pnl": chase_pnl}

        banked, allocated = _banked_and_allocated(s["id"], initial, banked_agg, darwinex_agg)
        # banked_profit_true: for a CLOSED Darwinex strategy, `banked` above
        # is the gross-cash bookkeeping figure (needed for Total P&L's
        # formula to reconcile, per §12.3) — not the real realized P&L.
        # agg["pnl"] IS the real realized P&L there (returned − deployed,
        # all-time, already correct — same figure the Breakdown table
        # shows). Every other strategy type's banked figure is already the
        # true one (a genuine profit-skim withdrawal), so it passes through
        # unchanged.
        banked_true = agg["pnl"] if darwinex_contrib is not None else banked

        pid        = s.get("pod_id")
        pod        = pods_idx.get(pid, {}) if pid else {}
        pod_joined = s.get("pods") or {}
        pod_color  = pod_joined.get("color") or pod.get("color") or "#6366f1"
        pod_code_s = pod_joined.get("pod_code") or pod.get("pod_code") or ""

        # Fund-statement strategies (12-FLAGS, ASLAN LABS): surface the fund's
        # own YTD net income / return % as a sub-line (NOT just the latest
        # month's MTD figure), in the STATEMENT's own currency (e.g. GBP for
        # ASLAN LABS, USD for 12-FLAGS) — matches the NAV administrator
        # statement's own ITD/YTD convention: cumulative sum of net_income
        # within the latest row's calendar year, divided by that year's
        # FIRST beginning_balance. Same convention as fund_statements.py's
        # /api/fund-statements list endpoint's ytd_net_income/ytd_return_pct
        # (kept in sync here since the strategy-card KPI response has no
        # access to that router's per-request enrichment). This is the
        # fund's own gross figure, NOT Chase's watermark-adjusted pnl above
        # — deliberately, so it always matches what the NAV statement itself
        # says (see agg/current_equity for Chase's actual economic figure).
        fund_usd_net_income = None
        fund_usd_return_pct = None
        fund_statement_currency = None
        if fund_contrib is not None and fund_contrib.get("series"):
            series = fund_contrib["series"]  # sorted ascending by period_end_date
            latest_year = series[-1]["period_end_date"][:4]
            year_rows   = [r for r in series if r["period_end_date"][:4] == latest_year]
            year_start  = float(year_rows[0]["beginning_balance"])
            ytd_income  = round(sum(float(r["net_income"]) for r in year_rows), 2)
            fund_usd_net_income = ytd_income
            fund_usd_return_pct = round(ytd_income / year_start * 100, 2) if year_start else 0.0
            fund_statement_currency = fund_contrib.get("currency")

        result.append({
            "entity_id":        f"strategy_{s['id']}",
            "name":             s.get("name", code),
            "strategy_code":    s.get("strategy_code", ""),
            "pod_code":         pod_code_s,
            "pod_color":        pod_color,
            "status":           s.get("status") or "Active",
            "watermark":        s.get("watermark"),
            "profit_share_pct": s.get("profit_share_pct"),
            "has_data":         has_data,
            "kpis": {
                "initial_investment": round(initial, 2),
                "current_equity":     agg["invested"],
                "current_equity_gross": round(gross_equity, 2),   # before watermark/profit-share (2026-09-24 CSV export)
                "performance":        round(agg["pnl"] / initial, 6) if initial else 0.0,
                "total_pnl":          agg["pnl"],
                "banked_profit":      banked,
                "banked_profit_true": round(banked_true, 2),
                "capital_allocated":  allocated,
                "pct_1d":             pct_1d,
                "pct_7d":             pct_7d,
                "pct_30d":            pct_30d,
                "fund_usd_net_income": fund_usd_net_income,
                "fund_usd_return_pct": fund_usd_return_pct,
                "fund_statement_currency": fund_statement_currency,
            },
        })

    return result


def get_equity_curve_data_fast(history: list[dict], days: Optional[int] = None) -> list[dict]:
    """
    Equity curve — prefers user_accounts_equity (summed per date) as source of truth.
    Falls back to pre-fetched balance_history rows if accounts table is empty.
    """
    # Prefer accounts equity (more reliable, deduplicated per-account)
    eq_by_date = _portfolio_equity_by_date()
    if eq_by_date:
        sorted_dates = sorted(eq_by_date.keys())
        if days is not None:
            cutoff       = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
            sorted_dates = [d for d in sorted_dates if d >= cutoff]
        return [{"timestamp": d, "equity": eq_by_date[d]} for d in sorted_dates]

    # Fallback: balance_history rows
    if not history:
        return []
    if days is not None:
        cutoff  = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        history = [r for r in history if r["date"] >= cutoff]
    return [{"timestamp": r["date"], "equity": r["investor_equity"]} for r in history]


def get_allocation_data_fast(pod_pfees_map: dict) -> list[dict]:
    """get_allocation_data() using pre-fetched pod_pfees_map."""
    pod_agg     = pod_pfees_map["pod_agg"]
    pods_list   = pod_pfees_map["pods_list"]
    UNALLOCATED = pod_pfees_map["UNALLOCATED"]
    pods_idx    = {p["id"]: p for p in pods_list}
    total       = sum(v["invested"] for v in pod_agg.values())
    if total == 0:
        return []
    slices = []
    for pid, agg in pod_agg.items():
        if agg["invested"] == 0:
            continue
        name = "Unallocated" if pid == UNALLOCATED else (pods_idx.get(pid, {}).get("name") or f"Pod {pid}")
        slices.append({"name": name, "aum": agg["invested"], "pct": round(agg["invested"] / total * 100, 2)})
    slices.sort(key=lambda x: x["aum"], reverse=True)
    return slices


def get_pnl_contribution_data_fast(pod_pfees_map: dict) -> list[dict]:
    """get_pnl_contribution_data() using pre-fetched pod_pfees_map."""
    pod_agg     = pod_pfees_map["pod_agg"]
    pods_list   = pod_pfees_map["pods_list"]
    UNALLOCATED = pod_pfees_map["UNALLOCATED"]
    pods_idx    = {p["id"]: p for p in pods_list}
    bars = []
    for pid, agg in pod_agg.items():
        name = "Unallocated" if pid == UNALLOCATED else (pods_idx.get(pid, {}).get("name") or f"Pod {pid}")
        bars.append({"name": name, "pnl": agg["pnl"]})
    bars.sort(key=lambda x: x["pnl"], reverse=True)
    return bars


# ---------------------------------------------------------------------------
# Pods CRUD
# ---------------------------------------------------------------------------

def list_pods() -> list[dict]:
    return _get_cached("pods", lambda: (
        get_client().table("pods").select("*").order("name").execute().data or []
    ))


def create_pod(name: str, pod_code: str, color: str, date_created: str,
               status: str = "Active", notes: str = "") -> dict:
    sb  = get_client()
    res = sb.table("pods").insert({
        "name":         name,
        "pod_code":     pod_code.upper(),
        "color":        color,
        "date_created": date_created,
        "status":       status,
        "notes":        notes or None,
    }).execute()
    _invalidate("pods")
    return res.data[0] if res.data else {}


def update_pod(pod_id: int, **fields) -> dict:
    sb  = get_client()
    res = sb.table("pods").update(fields).eq("id", pod_id).execute()
    _invalidate("pods")
    return res.data[0] if res.data else {}


def delete_pod(pod_id: int) -> bool:
    sb = get_client()
    sb.table("pods").delete().eq("id", pod_id).execute()
    _invalidate("pods")
    return True


# ---------------------------------------------------------------------------
# Strategies CRUD
# ---------------------------------------------------------------------------

def list_strategies(pod_id: Optional[int] = None) -> list[dict]:
    if pod_id is not None:
        # Filtered query — skip cache (rare, management UI only)
        res = (
            get_client()
            .table("strategies")
            .select("*,pods(name,color,pod_code)")
            .eq("pod_id", pod_id)
            .order("name")
            .execute()
        )
        return res.data or []
    return _get_cached("strategies_all", lambda: (
        get_client()
        .table("strategies")
        .select("*,pods(name,color,pod_code)")
        .order("name")
        .execute()
        .data or []
    ))


def create_strategy(name: str, strategy_code: str, pod_id: Optional[int],
                    initial_investment: float, date_created: str,
                    status: str = "Active", notes: str = "",
                    brokerage_account: Optional[str] = None,
                    axia_client_id: Optional[str] = None,
                    ig_client_id: Optional[str] = None,
                    data_feed_id: Optional[str] = None,
                    data_feed_client_id: Optional[str] = None,
                    watermark: Optional[float] = None,
                    profit_share_pct: Optional[float] = None) -> dict:
    sb  = get_client()
    res = sb.table("strategies").insert({
        "name":                 name,
        "strategy_code":        strategy_code.upper(),
        "pod_id":               pod_id,
        "initial_investment":   round(initial_investment, 2),
        "date_created":         date_created,
        "status":               status,
        "notes":                notes or None,
        "brokerage_account":    brokerage_account or None,
        "axia_client_id":       axia_client_id or None,
        "ig_client_id":         ig_client_id or None,
        "data_feed_id":         data_feed_id or None,
        "data_feed_client_id":  data_feed_client_id or None,
        "watermark":            round(watermark, 2) if watermark is not None else None,
        "profit_share_pct":     round(profit_share_pct, 2) if profit_share_pct is not None else None,
    }).execute()
    _invalidate("strategies_all")
    return res.data[0] if res.data else {}


def update_strategy(strategy_id: int, **fields) -> dict:
    sb  = get_client()
    res = sb.table("strategies").update(fields).eq("id", strategy_id).execute()
    _invalidate("strategies_all")
    return res.data[0] if res.data else {}


def delete_strategy(strategy_id: int) -> bool:
    sb = get_client()
    sb.table("strategies").delete().eq("id", strategy_id).execute()
    _invalidate("strategies_all")
    return True


# ---------------------------------------------------------------------------
# Capital events CRUD
# ---------------------------------------------------------------------------

def create_capital_event(event_date: str, event_type: str,
                          amount: float, notes: str = "", reference: str = "") -> dict:
    sb  = get_client()
    res = sb.table("capital_events").insert({
        "event_date":  event_date,
        "event_type":  event_type,
        "amount":      round(abs(amount), 2),
        "notes":       notes or None,
        "reference":   reference or None,
    }).execute()
    _invalidate("capital_events")
    return res.data[0] if res.data else {}


def update_capital_event(event_id: int, **fields) -> dict:
    """Patch one or more fields on a capital event row."""
    sb = get_client()
    if "amount" in fields:
        fields["amount"] = round(abs(float(fields["amount"])), 2)
    res = sb.table("capital_events").update(fields).eq("id", event_id).execute()
    _invalidate("capital_events")
    return res.data[0] if res.data else {}


def delete_capital_event(event_id: int) -> bool:
    sb = get_client()
    sb.table("capital_events").delete().eq("id", event_id).execute()
    _invalidate("capital_events")
    return True


def get_account_ids() -> list[dict]:
    """
    Distinct AccountIds from user_accounts_equity (latest date snapshot, cached 60s).
    Returns list of { account_id: int, equity: float }.
    Auto-updates as new accounts appear in the table.
    """
    def _fetch():
        sb = get_client()
        latest_res = (
            sb.table("user_accounts_equity")
            .select('"Date"')
            .order('"Date"', desc=True)
            .limit(1)
            .execute()
        )
        if not latest_res.data:
            return []
        latest_date = latest_res.data[0]["Date"]
        res = (
            sb.table("user_accounts_equity")
            .select('"AccountId","Equity"')
            .eq('"Date"', latest_date)
            .order('"AccountId"')
            .execute()
        )
        return [
            {"account_id": r["AccountId"], "equity": float(r["Equity"] or 0)}
            for r in (res.data or [])
        ]
    return _get_cached("account_ids", _fetch)


def get_accounts_equity_history() -> list[dict]:
    """
    Full history from user_accounts_equity ordered by Date asc (cached 60s).

    Primary source of truth for:
      - Portfolio equity curve (sum Equity per date across all AccountIds)
      - TWR sub-period computation
      - Per-account equity series for strategy period returns (1D/7D/30D)

    Returns list of { date: str, account_id: int, equity: float }.
    """
    def _fetch():
        sb  = get_client()
        res = (
            sb.table("user_accounts_equity")
            .select('"Date","AccountId","Equity"')
            .order('"Date"', desc=False)
            .execute()
        )
        return [
            {
                "date":       str(r["Date"]),
                "account_id": int(r["AccountId"]),
                "equity":     float(r["Equity"] or 0),
            }
            for r in (res.data or [])
        ]
    return _get_cached("accounts_equity_history", _fetch)


def _portfolio_equity_by_date() -> dict[str, float]:
    """
    Total portfolio equity per date: SUM(Equity) across all AccountIds per Date.
    Sourced from user_accounts_equity — deduplicated at DB level via trigger.
    Returns { date_str: total_equity_float } sorted keys.
    """
    history = get_accounts_equity_history()
    eq: dict[str, float] = {}
    for r in history:
        d = r["date"]
        eq[d] = round(eq.get(d, 0.0) + r["equity"], 2)
    return eq


def _pfees_equity_by_date() -> dict[str, float]:
    """
    SUM(Invested) per Date from ALL historical user_pfees_estimation rows.

    Primary equity series for TWR computation.  Consistent with get_live_aum()
    (which uses the same table / column), so end_aum always matches the AUM
    card shown in the UI.  Falls back to _portfolio_equity_by_date() if empty.

    Returns { date_str: total_invested_float } ordered by date asc.
    """
    def _fetch():
        sb  = get_client()
        res = (
            sb.table("user_pfees_estimation")
            .select('"Date","Invested"')
            .order('"Date"', desc=False)
            .execute()
        )
        eq: dict[str, float] = {}
        for r in (res.data or []):
            d = str(r["Date"])
            eq[d] = round(eq.get(d, 0.0) + float(r.get("Invested") or 0), 2)
        return eq
    return _get_cached("pfees_all_equity", _fetch, ttl=_TTL)


def _per_account_equity_series() -> dict[int, list[dict]]:
    """
    Per-account equity time series from user_accounts_equity.
    Returns { account_id: [{ date: str, equity: float }, ...] } sorted by date.
    Used for per-strategy period returns in hierarchy table.
    """
    history = get_accounts_equity_history()
    series: dict[int, list] = {}
    for r in history:
        aid = r["account_id"]
        if aid not in series:
            series[aid] = []
        series[aid].append({"date": r["date"], "equity": r["equity"]})
    # Already sorted by Date asc from DB query
    return series


def _account_period_return(series: list[dict], days: int) -> float:
    """
    % equity change over last N days for a single account equity series.
    Uses latest date in the series as reference (not today).
    """
    if len(series) < 2:
        return 0.0
    latest_date  = series[-1]["date"]
    latest       = series[-1]["equity"]
    cutoff_str   = (datetime.fromisoformat(latest_date) - timedelta(days=days)).strftime("%Y-%m-%d")
    past         = [p for p in series if p["date"] <= cutoff_str]
    if not past or past[-1]["equity"] == 0:
        return 0.0
    return round((latest - past[-1]["equity"]) / abs(past[-1]["equity"]), 6)


# ---------------------------------------------------------------------------
# Internal Transfers CRUD
# ---------------------------------------------------------------------------

def list_internal_transfers() -> list[dict]:
    """
    All rows from internal_transfers ordered by transfer_date ASC, id ASC.
    Python-side sort used because postgrest-py chained .order() calls may
    replace rather than compound — guarantees correct secondary sort by id.
    """
    def _fetch():
        rows = (
            get_client()
            .table("internal_transfers")
            .select("*")
            .execute()
            .data or []
        )
        rows.sort(key=lambda r: (r["transfer_date"], r["id"]))
        return rows

    return _get_cached("internal_transfers", _fetch)


def _get_darwinex_cashflows() -> list[dict]:
    """
    Compute net daily cash flows INTO Darwinex accounts from internal_transfers.

    FROM Wallet  → positive  (deploying capital to a Darwinex account)
    TO   Wallet  → negative  (withdrawing capital from a Darwinex account)
    Account↔account (rebalancing inside Darwinex) → ignored (net 0 for total exposure)

    Returns sorted list of { date: str, amount: float } for days with non-zero net.
    Used as TWR sub-period boundaries instead of bank capital_events.
    """
    transfers = list_internal_transfers()
    daily: dict[str, float] = {}
    for t in transfers:
        d   = t["transfer_date"]
        amt = float(t["amount"])
        if t["from_account"] == "Wallet":
            daily[d] = round(daily.get(d, 0.0) + amt, 2)
        elif t["to_account"] == "Wallet":
            daily[d] = round(daily.get(d, 0.0) - amt, 2)
        # Rebalance between non-Wallet accounts nets to zero — not a fund cash flow
    return sorted(
        [{"date": d, "amount": a} for d, a in daily.items() if a != 0],
        key=lambda x: x["date"],
    )


def _get_net_deployed_per_account() -> dict[str, float]:
    """
    Net capital deployed per brokerage account from internal_transfers.

    Only counts Wallet↔account flows — account-to-account rebalances are excluded
    because they represent internal reallocations, not new capital deployment.

      Wallet → account  : positive (deploying capital)
      account → Wallet  : negative (returning capital)

    Returns { account_name: net_deployed_float }
    e.g. { "Chase1": 899950.0, "Chase3xA": 100000.0, "XPF2026": 50.0 }
    """
    transfers = list_internal_transfers()
    net: dict[str, float] = {}
    for t in transfers:
        amt = float(t["amount"])
        frm = t["from_account"]
        to  = t["to_account"]
        if frm == "Wallet" and to != "Wallet":
            net[to]  = round(net.get(to, 0.0) + amt, 2)
        elif to == "Wallet" and frm != "Wallet":
            net[frm] = round(net.get(frm, 0.0) - amt, 2)
        # account↔account: ignored — same capital, different location
    return net


def get_net_deployed() -> dict[str, float]:
    """
    Public wrapper — returns net deployed per brokerage account (cached via
    internal_transfers cache). Used by /api/management/net-deployed endpoint.
    """
    return _get_net_deployed_per_account()


def _match_pfees_accounts_to_brokerage() -> dict[int, str]:
    """
    Match pfees AccountId integers → brokerage account names (Chase1, Chase3xA etc.)
    by comparing total Invested per AccountId against net_deployed per brokerage account.

    Greedy closest-ratio match: largest AccountId total → closest net_deployed bucket.
    Handles leveraged accounts (3x) where invested ≠ net_deployed exactly.

    Returns { account_id_int: brokerage_account_name }
    e.g. { 12345: "Chase1", 67890: "Chase3xA" }

    Cached under "pfees_acct_broker_map" for 60s (invalidated with internal_transfers).
    """
    def _compute():
        snapshot     = get_pfees_latest_snapshot()
        net_deployed = _get_net_deployed_per_account()
        if not net_deployed or not snapshot:
            return {}

        # Sum Invested per AccountId from pfees
        acct_invested: dict[int, float] = {}
        for row in snapshot:
            acct = int(row.get("AccountId") or 0)
            inv  = float(row.get("Invested") or 0)
            acct_invested[acct] = round(acct_invested.get(acct, 0.0) + inv, 2)

        # Greedy match: sort AccountIds by total invested desc, pick closest broker
        remaining = dict(net_deployed)   # brokers not yet matched
        mapping: dict[int, str] = {}

        for acct, inv_total in sorted(acct_invested.items(), key=lambda x: x[1], reverse=True):
            if not remaining:
                break
            if inv_total == 0:
                continue
            # Closest by ratio inv_total / deployed → 1.0
            best_broker = min(
                remaining,
                key=lambda b: abs(inv_total / remaining[b] - 1.0) if remaining[b] > 0 else float("inf"),
            )
            deployed = remaining[best_broker]
            if deployed > 0:
                ratio = inv_total / deployed
                if 0.3 <= ratio <= 3.0:   # wide tolerance: 3x leverage, early PnL swings
                    mapping[acct]   = best_broker
                    del remaining[best_broker]

        return mapping

    return _get_cached("pfees_acct_broker_map", _compute)


def create_internal_transfer(transfer_date: str, from_account: str,
                              to_account: str, amount: float,
                              notes: str = "",
                              capital_return_amount: Optional[float] = None,
                              profit_loss_amount: Optional[float] = None) -> dict:
    sb  = get_client()
    res = sb.table("internal_transfers").insert({
        "transfer_date": transfer_date,
        "from_account":  from_account,
        "to_account":    to_account,
        "amount":        round(abs(amount), 2),
        "notes":         notes or None,
        "capital_return_amount": round(capital_return_amount, 2) if capital_return_amount is not None else None,
        "profit_loss_amount":    round(profit_loss_amount, 2) if profit_loss_amount is not None else None,
    }).execute()
    _invalidate("internal_transfers")
    return res.data[0] if res.data else {}


def update_internal_transfer(transfer_id: int, **fields) -> dict:
    sb = get_client()
    if "amount" in fields:
        fields["amount"] = round(abs(float(fields["amount"])), 2)
    res = sb.table("internal_transfers").update(fields).eq("id", transfer_id).execute()
    _invalidate("internal_transfers")
    return res.data[0] if res.data else {}


def delete_internal_transfer(transfer_id: int) -> bool:
    sb = get_client()
    sb.table("internal_transfers").delete().eq("id", transfer_id).execute()
    _invalidate("internal_transfers")
    return True


# ---------------------------------------------------------------------------
# Capital Transfers — Wallet ⇄ Pod ⇄ Strategy funding ledger
# Separate from Darwinex's internal_transfers above. Covers AXIA and manual
# strategies: "we moved £150,000 from the wallet into AXIA-JJ", etc.
# ---------------------------------------------------------------------------

def list_capital_transfers() -> list[dict]:
    """All rows from capital_transfers ordered by transfer_date ASC, id ASC."""
    def _fetch():
        rows = (
            get_client()
            .table("capital_transfers")
            .select("*")
            .execute()
            .data or []
        )
        rows.sort(key=lambda r: (r["transfer_date"], r["id"]))
        return rows
    return _get_cached("capital_transfers", _fetch)


def create_capital_transfer(transfer_date: str, from_type: str, from_id,
                             to_type: str, to_id, amount: float,
                             reference: str = "", notes: str = "",
                             capital_return_amount: Optional[float] = None,
                             profit_loss_amount: Optional[float] = None) -> dict:
    sb  = get_client()
    res = sb.table("capital_transfers").insert({
        "transfer_date": transfer_date,
        "from_type":     from_type,
        "from_id":       from_id,
        "to_type":       to_type,
        "to_id":         to_id,
        "amount":        round(abs(amount), 2),
        "reference":     reference or None,
        "notes":         notes or None,
        "capital_return_amount": round(capital_return_amount, 2) if capital_return_amount is not None else None,
        "profit_loss_amount":    round(profit_loss_amount, 2) if profit_loss_amount is not None else None,
    }).execute()
    _invalidate("capital_transfers", "capital_transfers_by_strategy")
    return res.data[0] if res.data else {}


def update_capital_transfer(transfer_id: int, **fields) -> dict:
    sb = get_client()
    if "amount" in fields:
        fields["amount"] = round(abs(float(fields["amount"])), 2)
    res = sb.table("capital_transfers").update(fields).eq("id", transfer_id).execute()
    _invalidate("capital_transfers", "capital_transfers_by_strategy")
    return res.data[0] if res.data else {}


def delete_capital_transfer(transfer_id: int) -> bool:
    sb = get_client()
    sb.table("capital_transfers").delete().eq("id", transfer_id).execute()
    _invalidate("capital_transfers", "capital_transfers_by_strategy")
    return True


# ---------------------------------------------------------------------------
# Capital-flow-flagged daily equity entries (2026-09-21, Nish request)
#
# Problem: AXIA/IG/generic daily-cadence equity rows (axia_daily_equity /
# ig_daily_equity / <slug>_daily_equity) record raw NLV snapshots only. A
# fresh capital injection recorded as a normal row (e.g. equity jumps
# 100 -> 500,100 because £500,000 of new client money was wired in, not
# because of trading) was previously indistinguishable from £500,000 of
# genuine trading profit — CHG NLV on that day got counted as P&L by
# _axia_strategy_agg's `pnl = latest_equity - baseline` formula, and
# "Total Capital Invested" stayed frozen at the very first entry only
# (see that function's docstring — baseline was, until now, either the
# capital_transfers ledger sum if populated, or else the first-ever
# equity entry, full stop).
#
# Fix: when a daily-equity row is flagged `capital_flow_type` ('initial' or
# 'addon'), auto-create/sync a matching `capital_transfers` ledger row
# (from_type='wallet', from_id=None — "new money in", same shape as a
# manual Wallet->Strategy funding transfer) for that day's CHG NLV amount.
# `_strategy_capital_invested` / `_axia_strategy_agg` ALREADY prioritise the
# capital_transfers ledger sum over the first-equity-entry fallback (this
# was true before today) — so simply keeping that ledger populated makes
# baseline == cumulative flagged contributions automatically, everywhere
# that reads it (Portfolio hero, Capital Flow Summary, Capital at a Glance,
# pod/strategy breakdowns) — zero changes needed to the KPI math itself.
# 'initial' vs 'addon' are functionally identical here (both are inbound
# capital, both count toward Capital Invested/Allocated) — the two labels
# exist only so the equity records table can show which rows were capital
# events vs genuine trading days; see AxiaEquityEntry.jsx.
#
# Shared by axia_equity.py, ig_equity.py and data_feeds.py (daily cadence)
# so all three stay in lockstep — one implementation, not three.
# ---------------------------------------------------------------------------

CAPITAL_FLOW_TYPES = ("initial", "addon")


def _find_linked_strategy_id(client_row_id, client_field: str, feed_id=None) -> Optional[int]:
    """
    id of the strategy linked to this specific client row (or None).
    client_field is 'axia_client_id' / 'ig_client_id' / 'data_feed_client_id'.
    For data_feed_client_id, also requires strategies.data_feed_id == feed_id
    — client-row ids are only unique within their own feed's clients table,
    not globally, so the feed must be checked too.
    """
    rows = (
        get_client().table("strategies")
        .select("id,data_feed_id")
        .eq(client_field, client_row_id)
        .execute()
        .data or []
    )
    for r in rows:
        if feed_id is None or r.get("data_feed_id") == feed_id:
            return r["id"]
    return None


def sync_capital_flow_transfer(
    *, client_table: str, client: str, account: str, client_field: str,
    feed_id, trade_date: str, contribution: float, capital_flow_type: str, label: str,
) -> int:
    """
    Create the capital_transfers ledger row backing a newly-flagged
    (Initial Investment / Add-On) daily-equity entry. Returns the new
    ledger row's id (to store back on the equity row as capital_transfer_id).
    Raises ValueError (caller should turn this into a 400) if the amount
    isn't a positive inflow, or the client isn't linked to any strategy yet.
    """
    if capital_flow_type not in CAPITAL_FLOW_TYPES:
        raise ValueError(f"capital_flow_type must be one of {CAPITAL_FLOW_TYPES}.")
    if contribution is None or contribution <= 0:
        raise ValueError(
            "Initial Investment / Add-On must be a positive capital inflow "
            "(CHG NLV, or Equity if there's no previous record yet)."
        )
    client_row = (
        get_client().table(client_table).select("id")
        .eq("client", client).eq("account", account).limit(1).execute().data
    )
    if not client_row:
        raise ValueError("Client/account not found.")
    sid = _find_linked_strategy_id(client_row[0]["id"], client_field, feed_id)
    if sid is None:
        raise ValueError(
            "This client/account isn't linked to a strategy yet — link it in "
            "Manage Pods & Strategies first, then flag Initial Investment / Add-On."
        )
    tag = "Initial Investment" if capital_flow_type == "initial" else "Add-On"
    row = create_capital_transfer(
        transfer_date=trade_date, from_type="wallet", from_id=None,
        to_type="strategy", to_id=sid, amount=contribution,
        reference=f"{label} {tag}",
        notes=f"Auto-logged from {label} daily equity entry ({client}/{account}, {trade_date}).",
    )
    return row["id"]


def resync_capital_flow_transfer(transfer_id: int, trade_date: str, contribution: float, capital_flow_type: str, label: str) -> None:
    """Update an existing linked ledger row in place (edit of a flagged equity row)."""
    if contribution is None or contribution <= 0:
        raise ValueError(
            "Initial Investment / Add-On must be a positive capital inflow "
            "(CHG NLV, or Equity if there's no previous record yet)."
        )
    tag = "Initial Investment" if capital_flow_type == "initial" else "Add-On"
    update_capital_transfer(transfer_id, transfer_date=trade_date, amount=contribution, reference=f"{label} {tag}")


def delete_capital_flow_transfer(transfer_id) -> None:
    """Remove the linked ledger row (equity row deleted, or un-flagged back to a normal trading day)."""
    if transfer_id:
        delete_capital_transfer(transfer_id)


# ---------------------------------------------------------------------------
# Fund Monthly Statements — NAV-administrator-reported funds (e.g. 12-FLAGS)
# One row per strategy per period_end_date. Keyed directly by strategy_id —
# no separate "clients" table, unlike AXIA (no multi-account concept here).
# net_income / rate_of_return_pct / ending_balance_gbp are always computed
# server-side (never trust client-sent values for these) so the numbers
# that feed the strategy's Current Equity are always internally consistent.
# ---------------------------------------------------------------------------

def list_fund_statements(strategy_id: Optional[int] = None, table: str = "fund_monthly_statements") -> list[dict]:
    """
    All rows from `table`, optionally filtered to one strategy. Sorted
    period_end_date ASC. `table` defaults to the original fund_monthly_statements
    (12-FLAGS/ASLAN LABS) — parameterized so any monthly-cadence Data Feed
    (see data_feeds registry) reuses this exact same logic against its own
    physically separate statements table, zero behavior change for the
    original callers.
    """
    def _fetch():
        rows = (
            get_client()
            .table(table)
            .select("*")
            .execute()
            .data or []
        )
        rows.sort(key=lambda r: (r["period_end_date"], r["id"]))
        return rows
    rows = _get_cached(f"fund_statements_{table}", _fetch)
    if strategy_id is not None:
        rows = [r for r in rows if r["strategy_id"] == strategy_id]
    return rows


def get_fund_statement_prev(strategy_id: int, before_date: str, table: str = "fund_monthly_statements") -> Optional[dict]:
    """Most recent statement for this strategy strictly before `before_date`."""
    rows = [r for r in list_fund_statements(strategy_id, table) if r["period_end_date"] < before_date]
    return rows[-1] if rows else None


def _compute_fund_statement_fields(beginning_balance: float, additions: float,
                                    redemptions: float, ending_balance: float) -> dict:
    """
    net_income      = ending - beginning - additions + redemptions
    rate_of_return  = net_income / beginning_balance * 100
    Matches the NAV administrator statement's own MTD Net Income / Rate of
    Return exactly (verified against the 12-FLAGS July 2026 statement:
    Beginning 648,756.57, Net Income (15,544.59), Rate of Return (2.40%)).
    """
    net_income = round(ending_balance - beginning_balance - additions + redemptions, 2)
    rate_of_return_pct = round(net_income / beginning_balance * 100, 4) if beginning_balance else 0.0
    return {"net_income": net_income, "rate_of_return_pct": rate_of_return_pct}


def create_fund_statement(strategy_id: int, period_end_date: str, ending_balance: float,
                           beginning_balance: float, additions: float = 0.0,
                           redemptions: float = 0.0, currency: str = "USD",
                           notes: str = "", table: str = "fund_monthly_statements") -> dict:
    from src.services.oanda_service import get_monthly_close
    from datetime import date as _date

    computed = _compute_fund_statement_fields(beginning_balance, additions, redemptions, ending_balance)

    fx = None
    if currency == "USD":
        fx = get_monthly_close(_date.fromisoformat(period_end_date))

    payload = {
        "strategy_id":        strategy_id,
        "period_end_date":    period_end_date,
        "currency":           currency,
        "beginning_balance":  round(beginning_balance, 2),
        "additions":          round(additions, 2),
        "redemptions":        round(redemptions, 2),
        "ending_balance":     round(ending_balance, 2),
        "net_income":         computed["net_income"],
        "rate_of_return_pct": computed["rate_of_return_pct"],
        "fx_rate":            fx["rate"]        if fx else None,
        "fx_rate_date":       fx["candle_date"] if fx else None,
        "ending_balance_gbp": round(ending_balance / fx["rate"], 2) if fx else (
            round(ending_balance, 2) if currency == "GBP" else None
        ),
        "notes": notes or None,
    }
    res = get_client().table(table).insert(payload).execute()
    _invalidate(f"fund_statements_{table}")
    return res.data[0] if res.data else {}


def update_fund_statement(record_id: int, table: str = "fund_monthly_statements", **fields) -> dict:
    """
    Recomputes net_income/rate_of_return_pct whenever any of the four input
    numbers change, and re-fetches FX whenever period_end_date or currency
    changes — same server-side-source-of-truth approach as create.
    """
    existing_rows = get_client().table(table).select("*").eq("id", record_id).execute().data
    if not existing_rows:
        return {}
    existing = existing_rows[0]
    merged   = {**existing, **fields}

    if any(k in fields for k in ("beginning_balance", "additions", "redemptions", "ending_balance")):
        computed = _compute_fund_statement_fields(
            float(merged["beginning_balance"]), float(merged["additions"]),
            float(merged["redemptions"]), float(merged["ending_balance"]),
        )
        fields.update(computed)

    if "period_end_date" in fields or "currency" in fields:
        from src.services.oanda_service import get_monthly_close
        from datetime import date as _date
        if merged.get("currency") == "USD":
            fx = get_monthly_close(_date.fromisoformat(merged["period_end_date"]))
            fields["fx_rate"]      = fx["rate"]        if fx else None
            fields["fx_rate_date"] = fx["candle_date"] if fx else None
            fields["ending_balance_gbp"] = round(float(merged["ending_balance"]) / fx["rate"], 2) if fx else None
        else:
            fields["fx_rate"] = fields["fx_rate_date"] = None
            fields["ending_balance_gbp"] = round(float(merged["ending_balance"]), 2)
    elif "ending_balance" in fields and merged.get("fx_rate"):
        # Ending balance changed but FX didn't — reconvert with the existing rate
        fields["ending_balance_gbp"] = round(float(merged["ending_balance"]) / float(merged["fx_rate"]), 2)

    res = get_client().table(table).update(fields).eq("id", record_id).execute()
    _invalidate(f"fund_statements_{table}")
    return res.data[0] if res.data else {}


def refetch_fund_statement_fx(record_id: int, table: str = "fund_monthly_statements") -> dict:
    """Manual retry — re-attempt the OANDA lookup for a row whose FX fetch failed."""
    rows = get_client().table(table).select("*").eq("id", record_id).execute().data
    if not rows:
        return {}
    row = rows[0]
    if row.get("currency") != "USD":
        return row
    from src.services.oanda_service import get_monthly_close
    from datetime import date as _date
    fx = get_monthly_close(_date.fromisoformat(row["period_end_date"]))
    if not fx:
        return row
    fields = {
        "fx_rate":            fx["rate"],
        "fx_rate_date":       fx["candle_date"],
        "ending_balance_gbp": round(float(row["ending_balance"]) / fx["rate"], 2),
    }
    res = get_client().table(table).update(fields).eq("id", record_id).execute()
    _invalidate(f"fund_statements_{table}")
    return res.data[0] if res.data else row


def delete_fund_statement(record_id: int, table: str = "fund_monthly_statements") -> bool:
    get_client().table(table).delete().eq("id", record_id).execute()
    _invalidate(f"fund_statements_{table}")
    return True


# ---------------------------------------------------------------------------
# Miscellaneous Events CRUD
# ---------------------------------------------------------------------------

def list_misc_events() -> list[dict]:
    """All rows from misc_events ordered by event_date asc (cached 60s)."""
    return _get_cached("misc_events", lambda: (
        get_client()
        .table("misc_events")
        .select("*")
        .order("event_date", desc=False)
        .execute()
        .data or []
    ))


def create_misc_event(event_date: str, event_type: str, direction: str,
                      amount: float, notes: str = "") -> dict:
    sb  = get_client()
    res = sb.table("misc_events").insert({
        "event_date": event_date,
        "event_type": event_type,
        "direction":  direction,
        "amount":     round(abs(amount), 2),
        "notes":      notes or None,
    }).execute()
    _invalidate("misc_events")
    return res.data[0] if res.data else {}


def update_misc_event(misc_id: int, **fields) -> dict:
    sb = get_client()
    if "amount" in fields:
        fields["amount"] = round(abs(float(fields["amount"])), 2)
    res = sb.table("misc_events").update(fields).eq("id", misc_id).execute()
    _invalidate("misc_events")
    return res.data[0] if res.data else {}


def delete_misc_event(misc_id: int) -> bool:
    sb = get_client()
    sb.table("misc_events").delete().eq("id", misc_id).execute()
    _invalidate("misc_events")
    return True


# ---------------------------------------------------------------------------
# Expenses CRUD — tracked record only, does NOT affect bank_balance/TWR/AUM
# ---------------------------------------------------------------------------

def list_expenses() -> list[dict]:
    """All rows from expenses ordered by expense_date asc (cached 60s)."""
    return _get_cached("expenses", lambda: (
        get_client()
        .table("expenses")
        .select("*")
        .order("expense_date", desc=False)
        .execute()
        .data or []
    ))


def create_expense(expense_date: str, description: str, amount: float,
                    recurrence: str, reference: str = "") -> dict:
    sb  = get_client()
    res = sb.table("expenses").insert({
        "expense_date": expense_date,
        "description":  description,
        "amount":       round(abs(amount), 2),
        "recurrence":   recurrence,
        "reference":    reference or None,
    }).execute()
    _invalidate("expenses")
    return res.data[0] if res.data else {}


def update_expense(expense_id: int, **fields) -> dict:
    sb = get_client()
    if "amount" in fields:
        fields["amount"] = round(abs(float(fields["amount"])), 2)
    res = sb.table("expenses").update(fields).eq("id", expense_id).execute()
    _invalidate("expenses")
    return res.data[0] if res.data else {}


def delete_expense(expense_id: int) -> bool:
    sb = get_client()
    sb.table("expenses").delete().eq("id", expense_id).execute()
    _invalidate("expenses")
    return True


# ---------------------------------------------------------------------------
# Wages/Invoices CRUD — tracked record only, does NOT affect bank_balance/TWR/AUM
# ---------------------------------------------------------------------------

def list_wages() -> list[dict]:
    """All rows from wages_invoices ordered by wage_date asc (cached 60s)."""
    return _get_cached("wages_invoices", lambda: (
        get_client()
        .table("wages_invoices")
        .select("*")
        .order("wage_date", desc=False)
        .execute()
        .data or []
    ))


def create_wage(wage_date: str, employee: str, amount: float,
                 recurrence: str, reference: str = "") -> dict:
    sb  = get_client()
    res = sb.table("wages_invoices").insert({
        "wage_date":  wage_date,
        "employee":   employee,
        "amount":     round(abs(amount), 2),
        "recurrence": recurrence,
        "reference":  reference or None,
    }).execute()
    _invalidate("wages_invoices")
    return res.data[0] if res.data else {}


def update_wage(wage_id: int, **fields) -> dict:
    sb = get_client()
    if "amount" in fields:
        fields["amount"] = round(abs(float(fields["amount"])), 2)
    res = sb.table("wages_invoices").update(fields).eq("id", wage_id).execute()
    _invalidate("wages_invoices")
    return res.data[0] if res.data else {}


def delete_wage(wage_id: int) -> bool:
    sb = get_client()
    sb.table("wages_invoices").delete().eq("id", wage_id).execute()
    _invalidate("wages_invoices")
    return True
