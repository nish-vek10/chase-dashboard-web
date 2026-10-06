# Chase Capital — Dashboard

Internal capital, performance, and ledger tracking system for Chase Capital.
React (Vite) frontend, FastAPI backend, Supabase (Postgres) database.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Data Model](#3-data-model)
4. [Capital & Performance Formulas](#4-capital--performance-formulas)
5. [Watermark / Profit-Share Model](#5-watermark--profit-share-model)
6. [Two-Leg Capital Movement Model](#6-two-leg-capital-movement-model)
7. [Ledger Tabs Reference](#7-ledger-tabs-reference)
8. [UI/UX Conventions](#8-uiux-conventions)
9. [Banked Profit/Loss Drill-Down](#9-banked-profitloss-drill-down)
10. [AXIA Daily Equity — Pagination](#10-axia-daily-equity--pagination)
11. [Fund Monthly Statements & OANDA FX Bridge (12-FLAGS)](#11-fund-monthly-statements--oanda-fx-bridge-12-flags)
12. [Darwinex Closed-Strategy P&L Bridge](#12-darwinex-closed-strategy-pl-bridge)
13. [Roadmap — Completed](#13-roadmap--completed)
14. [Capital Pipeline: Strategy → Pod → Portfolio](#14-capital-pipeline-strategy--pod--portfolio)
15. [Data Feeds — Self-Service Tab Builder](#15-data-feeds--self-service-tab-builder)
    - [15.7 USD P/L → GBP entry (INVESTGTX)](#157-usd-pl--gbp-entry-investgtx-2026-09-30)
16. [Temporarily Hidden / Paused Metrics — Review Before Re-Enabling](#16-temporarily-hidden--paused-metrics--review-before-re-enabling)
17. [AXIA Trade Analysis Tool — GBP FX Bridge](#17-axia-trade-analysis-tool--gbp-fx-bridge)
    - [17.6 GlobalGTX — Statement Reconciliation](#176-globalgtx--statement-reconciliation-2026-09-29)
18. [Roadmap — Outstanding](#18-roadmap--outstanding)
19. [Known Limitations](#19-known-limitations)
20. [Dev Workflow](#20-dev-workflow)

---

## 1. Overview

Chase Capital dashboard tracks:

- Portfolio → Pod → Strategy → Trader hierarchy
- Capital invested, allocated, withdrawn, banked profit/loss
- Live equity from AXIA (`user_pfees_estimation`) and Darwinex/manual strategies
- Watermark / profit-share adjusted equity where applicable
- Full capital movement ledger (bank ↔ wallet ↔ pod ↔ strategy)
- Expenses, wages/invoices, misc events
- Excel/PDF/CSV reporting

Deployed via Netlify (frontend) + Railway (backend), but all development and
testing is done against live Supabase data.

---

## 2. Architecture

```
chase-dashboard-web/
├── src/                        # React frontend (Vite)
│   ├── components/
│   │   ├── CapitalOverview.jsx     # Hero cards, Capital Flow Summary table, glance chart,
│   │   │                           #   Banked Profit/Loss drill-down modal (§9)
│   │   ├── SummaryStrip.jsx        # All ledger tabs (Capital Events, Transfers, Misc, Expenses, Wages)
│   │   ├── PodStrategyManager.jsx  # Pod/Strategy CRUD forms
│   │   ├── AxiaEquityEntry.jsx     # AXIA daily equity (NLV) entry + paginated history (§10)
│   │   ├── Flags12StatementEntry.jsx  # 12-FLAGS monthly NAV statement entry + OANDA FX (§11)
│   │   └── layout/Navbar.jsx
│   ├── pages/
│   │   ├── Portfolio.jsx           # Portfolio home — hero + pod/strategy overview cards + hierarchy tabs
│   │   ├── DrillDown.jsx           # Entity drill-down (pod/strategy/trader)
│   │   ├── HomePage.jsx
│   │   └── Reports.jsx             # AXIA equity entry + 12-FLAGS statement entry
│   ├── hooks/usePortfolioData.js   # React Query hooks
│   └── services/api.js             # All HTTP calls
│
└── backend/src/
    ├── routers/
    │   ├── portfolio.py            # /api/portfolio — KPIs, hierarchy, drilldown
    │   ├── management.py           # /api/management — CRUD for all ledger tables
    │   ├── axia_equity.py          # /api/axia — daily equity CRUD, paginated
    │   ├── axia_analysis.py        # /api/axia — analysis endpoints
    │   └── fund_statements.py      # /api/fund-statements — monthly NAV statement CRUD (§11)
    ├── services/
    │   ├── supabase_service.py     # All aggregation logic (2600+ lines)
    │   └── oanda_service.py        # OANDA GBP/USD daily-close FX lookups (§11)
    └── models/schemas.py           # Pydantic response models
```

**Data flow:** Supabase tables → `supabase_service.py` aggregation functions
→ Pydantic schemas → FastAPI routers → React Query hooks → components.

---

## 3. Data Model

### Core hierarchy tables

| Table | Purpose |
|---|---|
| `pods` | Top-level grouping (e.g. AXIA, Darwinex). Has `status` (Active/Inactive). |
| `strategies` | Belongs to a pod. Has `status`, `watermark`, `profit_share_pct`. |
| `user_pfees_estimation` | Daily equity/PnL snapshot per AXIA strategy (live feed). |
| `balance_history` | Portfolio-level equity curve snapshots. |
| `axia_daily_equity` | Manually-entered AXIA daily NLV, paginated 50/page (§10). |
| `fund_monthly_statements` | Manually-entered NAV-administrator monthly statements for fund strategies with no daily feed (e.g. 12-FLAGS), one row per `strategy_id` per `period_end_date` (§11). |

### Capital movement tables

| Table | Purpose |
|---|---|
| `capital_events` | External bank ⇄ wallet flows (deposits/withdrawals). No strategy linkage. |
| `capital_transfers` | Internal wallet ⇄ pod ⇄ strategy flows (AXIA/manual). Typed `from_type`/`to_type`/`from_id`/`to_id`. |
| `internal_transfers` | Darwinex-specific wallet ⇄ account flows. String-based `from_account`/`to_account`. |
| `misc_events` | Ad-hoc capital events not fitting the above. |
| `expenses` | Operating expenses. |
| `wages` | Wages/invoices. |

### Profit/loss classification columns (added Phase 1)

`capital_transfers` and `internal_transfers` both carry:

- `capital_return_amount` (numeric, nullable)
- `profit_loss_amount` (numeric, signed, nullable)

Constraint: both null (unclassified), or both set with
`round(capital_return_amount + profit_loss_amount, 2) = round(amount, 2)`.

### Watermark columns (added Phase 2)

`strategies` table:

- `watermark` (numeric, nullable) — equity level above which profit-share applies
- `profit_share_pct` (numeric 0–100, nullable) — % of excess-over-watermark retained by Chase

Both null = no watermark adjustment (pass-through).

---

## 4. Capital & Performance Formulas

All formulas implemented in `computeCapitalMetrics()`
(`src/components/CapitalOverview.jsx`) and cross-checked line-by-line
against the manager's spec.

### Total Capital Invested

Cumulative, monotonic. Never reduced by withdrawals.

```
Total Capital Invested = Σ all capital allocated in, ever
```

### Banked Profit / Loss

Signed sum of the profit/loss leg of every classified outbound transfer
(strategy/account → wallet). Can be negative (realised loss).

```
Banked Profit / Loss = Σ profit_loss_amount   (across all classified withdrawals)
```

### Capital Allocated (Still Out)

Total invested minus **everything withdrawn** — both the capital-return
portion and the profit/loss portion.

```
Total Withdrawn    = Σ (capital_return_amount + profit_loss_amount)
Capital Allocated  = Total Capital Invested − Total Withdrawn
```

> **Fixed bug (this project's Phase 4 audit):** formula previously only
> subtracted `capital_return_amount`, leaving Capital Allocated unchanged
> after a pure-profit withdrawal. Now correctly subtracts the full
> withdrawn amount.

### Current Equity (Economic Interest)

Chase's live economic interest in the strategy/pod, watermark-adjusted
where applicable (see [§5](#5-watermark--profit-share-model)).

### Total P&L

```
Total P&L = Current Equity + Banked Profit / Loss − Total Capital Invested
```

> **Fixed bug (this project's Phase 4 audit):** frontend previously used
> the backend's raw `kpis.total_pnl` field (a broker-snapshot PnL
> disconnected from banked profit). Now always computed via the formula
> above, at every level (strategy/pod/portfolio).
>
> **Note:** the manager's spec claims `Total P&L = Current Equity −
> Capital Allocated`. This only holds when there is **no capital-return
> withdrawal component**. Once one exists (e.g. Darwinex return-of-capital),
> the two forms diverge — the `Equity + Banked − Invested` form is the one
> implemented, and is the mathematically correct one in general.

### Total ROI

```
Total ROI = Total P&L / Total Capital Invested × 100
```

---

## 5. Watermark / Profit-Share Model

Applies at the **strategy** level. Given raw broker equity `E`, watermark
`W`, and profit-share % `S`:

```
if W is null or S is null:
    chase_equity = E                          # pass-through, no adjustment

elif E <= W:
    chase_equity = E                          # below watermark — 100% retained

else:
    chase_equity = W + (E − W) × (S / 100)    # above watermark — only S% of excess retained
```

Implemented in `_apply_watermark(strategy, raw_equity, baseline)`,
`backend/src/services/supabase_service.py`.

**Coverage:**

| Level | AXIA strategies | Non-AXIA (Darwinex/manual) strategies |
|---|---|---|
| Strategy | ✅ Applied | ✅ Applied |
| Pod | ✅ Applied (additive from adjusted strategy figures) | ⚠️ Not applied — see [§19](#19-known-limitations) |
| Portfolio | ✅ Applied | ⚠️ Not applied — see [§19](#19-known-limitations) |

---

## 6. Two-Leg Capital Movement Model

Institutional capital movements are split into two independent legs:

**External — `capital_events`**
Bank ⇄ Wallet. No strategy/pod linkage. Deposits and withdrawals only.

**Internal — `capital_transfers` (AXIA/manual) or `internal_transfers` (Darwinex)**
Wallet ⇄ Pod ⇄ Strategy. Typed endpoints. This is where profit/loss
classification (§3) is recorded — always on the **outbound** leg
(Strategy/Account → Wallet).

Darwinex uses a separate table (`internal_transfers`, string-based
`from_account`/`to_account`) rather than `capital_transfers`, by earlier
explicit design decision — kept fully separate from the AXIA/manual model.

### Recording a profit/loss withdrawal (worked example — AXIA-TW)

Real entries made this project, used as the reference pattern for all
future profit withdrawals:

| Date | Ref | Amount | Capital Return | Profit/Loss |
|---|---|---|---|---|
| 15-04-2026 | AXIA MARKET PRO | £156,000.00 | £0.00 | £156,000.00 |
| 06-07-2026 | AXIA MARKET PRO | £24,000.00 | £0.00 | £24,000.00 |

**Steps:**
1. Capital Transfers tab → New Transfer.
2. From = Strategy (AXIA-TW), To = Wallet.
3. Amount = withdrawal amount.
4. Classification box appears (outbound strategy→wallet leg only): enter
   Capital Return and Profit/Loss so they sum to Amount.
5. Reference + notes as normal.

---

## 7. Ledger Tabs Reference

| Tab | Table | Classifiable? |
|---|---|---|
| Capital Events | `capital_events` | No — external, no strategy linkage |
| Capital Transfers | `capital_transfers` | Yes — outbound Strategy→Wallet legs only |
| Darwinex (Internal) Transfers | `internal_transfers` | Yes — outbound Account→Wallet legs only |
| Misc Events | `misc_events` | No |
| Expenses | `expenses` | No |
| Wages / Invoices | `wages` | No |

All tabs use consistent formatting (§8).

---

## 8. UI/UX Conventions

- **Dates:** `DD-MM-YYYY` everywhere except chart axis ticks. Formatted via
  `fmtDate()` in `SummaryStrip.jsx`.
- **Money:** full 2dp `£` everywhere except chart axis ticks.
- **Event-log rows:** left side = icon/badges/arrows on top line,
  reference + notes stacked below; right side = bold date on top, amount
  below, right-aligned. Applied consistently across all 6 ledger tabs.
- **Number inputs (Profit/Loss, Profit Share %):** `type="text"
  inputMode="decimal"` — no spinner arrows, numeric keypad on mobile.
  Bounds still enforced server-side via Pydantic.
- **Overview cards (Pod/Strategy):** header row shows name, watermark
  badge (`WM £X · Y%`, purple, only if set), and a bold-caps
  green/red Active/Inactive status badge, right-aligned.
- **Capital Flow Summary table:** zebra/hover row shading via
  `.glass-table` CSS; cells carry horizontal (12–14px) and header
  top (10px) padding so shading doesn't run text to the table edges.
- **Responsive:** every view works on both desktop and mobile.

---

## 9. Banked Profit/Loss Drill-Down

Clicking the Banked Profit/Loss hero card, or its Capital Flow Summary
table row, opens a modal listing every classified withdrawal leg that
contributed to the total — same underlying data as the ledger tabs, no
separate backend endpoint.

**Implementation:** `BankedProfitModal` in `CapitalOverview.jsx`. Self-fetches
via React Query, reusing the exact same query keys the ledger tabs already
use (`['capital_transfers']`, `['internal_transfers']`, `['pods']`,
`['strategies_all']`) — so the modal is always cache-consistent with the
ledger tabs, with zero extra network calls beyond what's already loaded.

**Filter logic:** rows where `profit_loss_amount != null` (classified) and
the transfer direction is outbound (Strategy/Account → Wallet). Sorted
newest-first. Each row shows: strategy/pod name, capital-return amount,
signed profit/loss amount (green if ≥ 0, red if < 0), and date. A running
`netTotal` (only the net figure, not the full breakdown) is shown wherever
Banked Profit/Loss appears in preview form (hero card, table row) —
matching the "only net shown in the preview" requirement.

**Closed-strategy return legs are tagged neutrally, not "Profit," and the
"Banked Profit/Loss" figure — everywhere it appears — now shows the true
net realized P&L, not gross recovered cash.**

For a closed Darwinex strategy (§12), `profit_loss_amount` on its return
legs is set to the FULL cash amount by bookkeeping necessity (§12.3
explains why — Total P&L/ROI have to reconcile against the frozen Capital
Invested baseline, and there's no other field to carry that
reconciliation). That figure is NOT trading profit — most of it is
principal coming back from a strategy that may have lost money overall.
Originally, EVERY use of "Banked Profit/Loss" (per-leg modal tags, the
strategy/pod/portfolio KPI cards, the Capital Flow Summary row) summed this
same gross figure directly, so a closed strategy's principal repayment
inflated the headline number — e.g. the portfolio showed **£1,067,992.25**
Banked P&L when the true realized figure (AXIA-TW's £180,000 genuine profit
skim + Chase1's −£12,007.75 realized loss) is **£167,992.25**.

**Fix — two parts, kept deliberately separate so Total P&L/ROI/Capital
Allocated never move:**

1. Backend: every KPI response (`get_strategies_with_kpis_fast`,
   `get_pods_with_kpis_fast`, `get_portfolio_kpis_fast`) now also returns
   `banked_profit_true` alongside the existing `banked_profit`. For a
   closed Darwinex strategy, `banked_profit_true` = its aggregator's own
   realized pnl (`_darwinex_closed_strategy_agg`'s `pnl`, already correct —
   the same figure the Breakdown table shows). For every other strategy
   type, `banked_profit_true` is identical to `banked_profit` (a genuine
   profit-skim withdrawal already IS the true figure, e.g. AXIA-TW).
   Summed at pod/portfolio level the same way, per strategy, before
   summing — never derived by re-aggregating the gross figure.
2. Frontend: `computeCapitalMetrics()` (`CapitalOverview.jsx`) now computes
   `pnl` from `bankedGross` (`kpis.banked_profit`, unchanged, internal
   only — never rendered) but *displays* `banked` from
   `kpis.banked_profit_true` (falling back to `bankedGross` if absent).
   `BankedProfitModal`'s net-total calculation was also fixed to match:
   it sums ordinary (non-closing) legs' `profit_loss_amount` as before,
   but counts a closed strategy's realized P&L **once per strategy** (via
   `kpis.total_pnl`), not once per return leg — summing raw leg amounts
   would double/triple-count a strategy with more than one closing leg
   (Chase1 has two: £50 and £887,942.25, both gross).

Net effect: Total P&L, Total ROI, and Capital Allocated are completely
unaffected (still computed from the unchanged `bankedGross`/aggregator
figures) — only "Banked Profit/Loss," wherever it's shown to a human, now
reads as true realized profit/loss instead of gross recovered cash.

---

## 10. AXIA Daily Equity — Pagination

`GET /api/axia/equity` — paginated, `{rows, total}` shape, `limit`/`offset`
query params (default `limit=50`). Frontend (`AxiaEquityEntry.jsx`) shows
50 records per page with Previous/Next controls and a "Page X of Y"
indicator. Applies uniformly to every AXIA client, not just one.

> **Fixed bug (this project):** `PATCH /api/axia/equity/{id}` used to build
> its update payload with `{k: v for k, v in body.model_dump().items() if v
> is not None}` — which silently **dropped any field explicitly set to
> `null`**, so clearing the Notes field and hitting Save had no effect (the
> old note stayed in the DB). Fixed to `body.model_dump(exclude_unset=True)`
> — only fields genuinely absent from the request are skipped; an explicit
> `null` now saves correctly. This `exclude_unset` pattern was then applied
> proactively to `PATCH /api/fund-statements/{id}` (§11) from the start, to
> avoid the same class of bug there.

### 10.1 Bidirectional Equity / CHG NLV Entry (2026-09-09)

`AxiaEquityEntry.jsx` is the one shared "New Entry" form behind AXIA, IG,
and every daily-cadence Data Feed created via `DataFeedManager.jsx`
(instantiated per feed with a different `apiPrefix` — see §16.x below and
the component's own header comment). One fix here applies to all of them.

Previously only Equity (NLV) could be typed, auto-computing CHG NLV as
`equity − previous day's equity` (via `GET {apiPrefix}/equity/prev`); CHG
NLV was a free-text override box with no link back to Equity.

Now both fields are live and bidirectional against the same previous-day
anchor:
- Type **Equity (NLV)** → CHG NLV auto-fills as `equity − prevEquity`
  (unchanged behaviour).
- Type **CHG NLV** → Equity (NLV) auto-fills as `prevEquity + chgNlv` (new).

Whichever field the user edits last is not clobbered by the other — each
`onChange` only pushes a value into the *other* field, not its own. If no
previous record exists for that client/account/date/currency (first entry),
neither field can auto-derive and both must be entered manually, same as
before. Submit still only requires Equity (NLV) to be present — it's
populated either by direct entry or by the CHG NLV auto-fill.

### 10.2 Capital Flow Flag — Initial Investment / Add-On (2026-09-21)

**Problem.** A daily-equity row only ever recorded a raw NLV snapshot. If a
CHG NLV jump was new client money in (a funding wire), not trading, there
was no way to say so — it silently got counted as trading P&L, and
"Total Capital Invested" for that strategy stayed frozen at whatever the
very first-ever equity entry happened to be (see `_axia_strategy_agg`'s
docstring in `supabase_service.py` — baseline was, until now, either the
`capital_transfers` ledger sum if one had been logged manually elsewhere,
or else the first equity entry, full stop). Real example that surfaced
this: an AXIA account funded in three tranches — a £100 test, then £500,000,
then another £500,000 — with no trading in between. The dashboard showed
Capital Invested as £100 and the £1,000,000 of deposits as pure P&L.

**Fix.** The New Entry form in `AxiaEquityEntry.jsx` (shared by AXIA, IG,
and every daily-cadence Data Feed — one component, `apiPrefix`-parameterized)
now has a Capital Flow toggle: **Trading Day** (default), **Initial
Investment**, or **Add-On**. Flagging a row auto-creates a matching
`capital_transfers` ledger row (`from_type='wallet'`, `from_id=null` — "new
money in", same shape as a manual Wallet→Strategy funding transfer) for
that day's CHG NLV amount, linked to whichever strategy this client/account
is linked to.

No changes were needed to the KPI math itself —
`_strategy_capital_invested` / `_axia_strategy_agg` already prioritised the
`capital_transfers` ledger sum over the first-equity-entry fallback, before
this feature existed. Keeping that ledger populated from the equity-entry
screen (instead of requiring a second manual entry in the Capital Ledger
UI) is the entire fix. Baseline becomes the sum of every flagged
contribution, so pnl = latest equity − that sum, correctly excluding
capital days from trading P&L — and the same number feeds Total Capital
Invested / Capital Allocated everywhere that reads it (Portfolio hero
strip, Capital Flow Summary, Capital at a Glance, pod/strategy breakdowns),
since it's the identical ledger those already read.

"Initial Investment" and "Add-On" are functionally identical (both are
inbound capital, both count the same way) — the two labels exist purely so
the equity records table can show which rows were capital events vs
genuine trading days. Contribution amount = that day's CHG NLV (or the
full Equity value if there's no previous record, e.g. the very first
entry for a brand-new client). Must be a positive inflow — the form
validates client-side, the backend re-validates and 400s otherwise;
withdrawals stay on the existing Banked Profit / Capital Return flow,
unaffected. If the client/account isn't yet linked to a strategy (Manage
Pods & Strategies), the checkbox is still selectable but submit is blocked
with an inline explanation — nothing to attribute the transfer to yet.

Editing a flagged row's date/CHG NLV updates the linked ledger row in
place; un-flagging it (back to "Trading Day") deletes the ledger row;
deleting the equity row deletes its linked ledger row too — one-directional
sync equity-entry → ledger. Editing the ledger row directly from the
Capital Ledger UI does not write back to the CHG NLV column (known,
acceptable limitation — rare, and the capital math is still correct either
way since the ledger is the source of truth).

**Schema (additive, run once — see `supabase_capital_flow_migration.sql`):**
`capital_flow_type` (`text`, nullable, `'initial'|'addon'|NULL`) and
`capital_transfer_id` (`bigint`, nullable, application-level link, not a
hard FK) added to `axia_daily_equity`, `ig_daily_equity`, and every
already-registered daily-cadence Data Feed's `<slug>_daily_equity` table.
The Data Feed "Generate SQL" template (`data_feeds.py`) now includes both
columns by default, so any feed created from now on needs no extra
migration.

### 10.3 Bugfix — write-order caused an orphan ledger row to double-count (2026-09-21)

**Bug.** OPTIOS showed Capital Invested exactly DOUBLE its one visible
equity row, Total P&L wrongly negative, despite only one equity record
existing. Root cause: `create_equity`/`update_equity` (in `axia_equity.py`,
`ig_equity.py`, `data_feeds.py` — all three routers, identical pattern)
called `sync_capital_flow_transfer` (which commits a `capital_transfers`
row as its last statement) **before** the equity-table insert/update. If
that later write then failed — e.g. a duplicate/retried submit hitting the
`(client,account,trade_date,currency)` unique constraint — the ledger row
it had already committed had nothing to roll it back, permanently baking
an extra contribution into the strategy's baseline with no equity row to
show for it.

**Fix.** All three routers now write the equity row FIRST (its own
duplicate-catch, zero side effects on failure), and only sync the ledger
once that succeeds — with compensating rollback (delete the just-inserted
row on create-failure; revert to the pre-update values on update-failure)
if the ledger step itself then fails. A genuine duplicate submit now 409s
cleanly with no orphan row possible, in any of AXIA, IG, or any Data Feed.

### 10.4 Capital Invested computed directly from flagged equity rows, ledger is audit-trail only (2026-09-21)

**Problem.** §10.2's fix worked by keeping `capital_transfers` populated
from the equity-entry screen and letting the KPI math read that ledger sum
(unchanged since before the feature existed). But that meant Capital
Invested depended on there being exactly one ledger row per flagged equity
entry — any bug, retry, or manual edit of the ledger could silently throw
the number off with no direct way to audit it back to source, and it
mixed one audit trail (manual Wallet/Pod/Strategy funding) with another
(auto-logged equity flags) in the same sum.

**Fix.** `capital_transfers` rows created by `sync_capital_flow_transfer`
(from an equity-screen Initial Investment / Add-On flag) are now
**reference/audit-trail only** — they still get created and updated
exactly as before (visible in the Capital Ledger, still show "AXIA
Initial Investment" etc.), but are no longer summed into any KPI:

- `_axia_flagged_equity_total(client, account, table)` (new,
  `supabase_service.py`) sums the client's GBP equity rows directly where
  `capital_flow_type` is set — contribution = CHG NLV if set, else Equity
  — the single source of truth for that client's baseline now.
- `_axia_strategy_agg`'s baseline (used by AXIA/IG and every daily-cadence
  Data Feed alike) checks this flagged-equity total FIRST, ahead of the
  `capital_transfers` ledger sum. The ledger sum is now only a fallback for
  strategies funded through the ledger before this feature existed and
  never flagged since.
- `_equity_linked_capital_transfer_ids()` (new) finds every
  `capital_transfer_id` referenced from `axia_daily_equity`,
  `ig_daily_equity`, or any daily-cadence Data Feed's equity table, and
  `_capital_transfers_by_strategy()` now excludes those ids from its sum —
  so manual Wallet/Pod/Strategy transfers (made directly in Manage Pods &
  Strategies, unaffected, still sum exactly as before) never get double
  -read alongside the flagged-equity total.
- `_strategy_capital_invested` now checks the AXIA/IG/Data-Feed aggregator
  (which already prioritises flagged-equity) BEFORE the raw ledger sum, for
  any strategy linked to a client.

Banked Profit / Capital Allocated / Total P&L are untouched by this
change — they were already sourced from the OUTBOUND leg of
`capital_transfers` (`capital_return_amount`/`profit_loss_amount`, see
§9), which the equity-flag feature never writes to (its rows are always
inbound "new money", both fields null) — so those numbers already only
ever moved on a genuine capital return / banked profit-loss event, exactly
as intended. This change only closes the gap on the *invested* side.

---

## 11. Fund Monthly Statements & OANDA FX Bridge (12-FLAGS)

### 11.1 What this is

Some strategies (currently: **12-FLAGS**, a Cayman feeder fund reported via
NAV Fund Services) have **no daily broker feed** — performance only exists
as a monthly Investor Statement (Beginning Balance, Ending Balance,
Additions, Redemptions, Net Income, Rate of Return), denominated in **USD**.
This is fundamentally different from AXIA (daily feed, GBP) and Darwinex
(daily feed via `internal_transfers`), so it gets its own table, service,
and entry UI — but bridges into the exact same strategy → pod → portfolio
KPI pipeline as every other strategy type.

Entry point: **Reports page → "12-FLAGS Monthly Statement (USD)"**, below
the AXIA Daily Equity section. One statement per calendar month.

### 11.2 Data entered vs data computed

You key in **one number** each month — the statement's **Ending Balance
(USD)**. Everything else is either auto-carried or computed server-side
(never trusted from the client):

| Field | Source |
|---|---|
| Period End Date | You enter (statement date) |
| Ending Balance (USD) | You enter — the only required manual figure |
| Beginning Balance | Auto-carried from the previous record's Ending Balance (first-ever record: manual, one-time) |
| Additions / Redemptions | You enter only if the statement shows any that period (collapsed by default) |
| Net Income | **Computed server-side** |
| Rate of Return % | **Computed server-side** |
| FX Rate + Date | **Fetched from OANDA server-side** |
| Ending Balance (GBP) | **Computed server-side** from Ending Balance ÷ FX Rate |

### 11.3 Formulas

```
Net Income      = Ending Balance − Beginning Balance − Additions + Redemptions
Rate of Return % = Net Income ÷ Beginning Balance × 100
```

Verified against the 12-FLAGS July 2026 statement: Beginning 648,756.57,
Ending 633,211.98 → Net Income = −15,544.59, Rate of Return = −2.3958% ≈
−2.40% (statement's own quoted figure).

**YTD** (shown as a column in the records table, and as the strategy-card
USD sub-line, §11.6) is **not** chain-linked/compounded — it's a plain
cumulative sum within the calendar year, matching the NAV administrator's
own ITD/YTD convention exactly:

```
YTD Net Income  = Σ Net Income for every record in the same calendar year as the latest record
YTD Return %    = YTD Net Income ÷ (that year's FIRST record's Beginning Balance) × 100
```

Verified: May→June (+... or −1,243.43) + June→July (−15,544.59) =
**−16,788.02**, matching the statement's own ITD Net Income exactly;
−16,788.02 ÷ 650,000 = **−2.58%**, matching the statement's own ITD Rate of
Return exactly.

### 11.4 OANDA FX conversion

**Why:** the statement is USD; the rest of the dashboard (Capital Invested,
Current Equity, Total P&L) is GBP. Each statement needs a GBP-converted
Ending Balance to feed into the strategy's Current Equity.

**Pair:** `GBP_USD` (base GBP, quote USD) — 1 GBP = `rate` USD.
Conversion: `GBP = USD ÷ rate`.

**Source:** OANDA practice/demo API (`https://api-fxpractice.oanda.com/v3`).
The practice API's pricing feed mirrors live market data — safe to use for
historical rate lookups even on a demo account (read-only candle queries
only, no trades ever placed).

**Candle selection — daily, not monthly:** `oanda_service.get_monthly_close()`
requests a **daily** (`granularity="D"`) candle, midpoint price
(`price="M"`), `count=1&to=<period_end + 1 day>` — this unambiguously
returns the one trading day at or immediately before the statement's period
end. An earlier version requested an actual `granularity="M"` monthly
candle with manual month-start/month-end bounds; this proved unreliable —
OANDA anchors monthly candles to its own broker-day boundary
(~21:00–22:00 UTC, DST-dependent), and adjacent from/to windows could
straddle that boundary and silently return the **same** candle for two
different statement months. Daily granularity has no such ambiguity.

**Broker-day date correction:** OANDA's daily candle `time` field is the
candle's **open** timestamp in UTC, and the broker day opens ~21:00–22:00
UTC the evening before (5pm New York convention) — so a naive date
truncation mislabels the candle one calendar day early (e.g. Friday
31-07-2026's close came back timestamped `2026-07-30T21:00:00Z`). Fixed
with a `+timedelta(hours=6)` correction before deriving the candle's
calendar date — verified: `2026-07-30T21:00:00Z + 6h → 2026-07-31`, correct.

**Staleness flag:** a fetched rate is flagged stale (⚠, orange, with a
manual "↻ re-fetch" button) if the candle date is more than 10 days from
the statement's period end — normally only happens for a genuinely
future-dated period (OANDA can only return its most recent *real* candle).
Same 10-day threshold enforced identically on both backend
(`oanda_service.py`) and frontend (`Flags12StatementEntry.jsx`'s
`isFxStale()`), kept in sync deliberately.

**Manual re-fetch:** every FX badge always shows a small "↻" button
(`POST /api/fund-statements/{id}/refetch-fx`), not just when stale/pending —
useful for re-pulling a rate that predates a fix, or forcing a fresh lookup.

### 11.5 Three-level strategy → pod → portfolio bridge

Mirrors the AXIA bridge pattern exactly, at all three levels:

```
strategy_id
  └─ fund_monthly_statements rows (sorted by period_end_date)
       └─ latest row's ending_balance_gbp  →  strategy's Current Equity
            └─ folded into the strategy's pod's pod_agg   →  pod's Current Equity
                 └─ folded into portfolio-wide current_equity / total_pnl
```

Implemented via `_fund_statement_strategy_agg(strategies)` in
`supabase_service.py` — returns `{strategy_id: {invested, pnl, baseline,
series}}`:

- **`invested`** = latest record's `ending_balance_gbp`. If the latest
  record's FX hasn't been fetched yet (`ending_balance_gbp is None` —
  pending OANDA fetch or failed lookup), the strategy is **excluded**
  entirely from `fund_agg` rather than fed a USD number mislabelled as GBP
  — it falls through to the strategy's existing `0.0` default until FX is
  fetched or manually retried.
- **`baseline`** = `capital_transfers` ledger inbound sum for that strategy
  if any exist, else the strategy's manual `initial_investment` field —
  same fallback chain as `_strategy_capital_invested()`, so Capital
  Invested stays consistent everywhere it's shown.
- **`pnl`** = `invested − baseline`.
- **`series`** = full sorted history — used to compute the YTD sub-line
  (§11.6) without a second DB round-trip.

Folded in at all three levels the AXIA pattern already establishes:

| Level | Function | Fold-in |
|---|---|---|
| Strategy | `get_strategies_with_kpis_fast` | `fund_contrib` branch (after the `axia_contrib` check) sets `agg = {invested, pnl}` directly from `fund_agg` |
| Pod | `_build_pod_pfees_map` → `pod_agg` | Same additive fold-in block as AXIA's, right after it |
| Portfolio | `get_portfolio_kpis_fast` | `current_equity`/`total_pnl` explicitly summed with `fund_agg` values (fund-statement strategies never appear in the `pfees` snapshot AXIA/Darwinex use, so this level needs an explicit add rather than inheriting from a shared snapshot sum) |
| Breakdown/Hierarchy table (strategy tab) | `get_hierarchy_rows` (`entity_type == "strategy"`) | Same `fund_contrib` branch added — **this was a gap found and fixed during the post-build bridge audit**: this function builds its own `darwin_agg`/`acct_agg` from the pfees snapshot independently of the strategy-KPI function above, and had no fund-statement awareness, so 12-FLAGS would have shown £0 AUM/PnL specifically in the Breakdown table (Portfolio page → Strategy tab) even though its own Strategy Overview card was already correct |

No double-counting: `get_portfolio_kpis_fast`'s `current_equity`/`total_pnl`
are computed from the raw `pfees` snapshot (`_snapshot`), **not** from
`pod_agg` — so the portfolio-level fund-statement fold-in and the pod-level
one are additive from two genuinely separate base sums, not the same
number added twice.

### 11.6 USD sub-line (GBP vs USD, and why they diverge)

The 12-FLAGS Strategy Overview card shows **Total P&L** and **Total ROI**
in GBP, like every other strategy — plus a small USD sub-line underneath
each, showing the fund's own YTD Net Income / Return % straight from the
statements (§11.3's YTD formula), un-translated.

**Why both are needed:** the GBP figures are correct, but they include a
genuine FX-translation effect on top of the fund's own USD performance,
because Capital Invested and Current Equity get converted at **two
different historical FX rates** — Capital Invested at the original deposit
date's rate (Ebury FX, 29-05-2026, implied ≈1.3375), Current Equity at the
latest statement's OANDA close rate (≈1.3484, a stronger GBP). A weaker USD
book translated at a stronger-GBP rate shows a larger GBP loss than the
fund's own USD-denominated loss — confirmed via direct calculation, not a
bug. The USD sub-line exists purely so this never reads as a discrepancy
against the fund's own quoted NAV statement numbers.

**Backend:** `KpiData.fund_usd_net_income` / `fund_usd_return_pct`
(`schemas.py`, both `Optional[float] = None`) — populated in
`get_strategies_with_kpis_fast` only when `fund_contrib is not None`, from
`fund_contrib["series"]`'s YTD computation (§11.3). `None` for every other
strategy type — AXIA and Darwinex never populate these fields.

**Frontend:** `StatBox` (`Portfolio.jsx`) takes an optional `subLine` prop;
`OverviewCard` passes a formatted "USD YTD: ..." string only when
`kpis.fund_usd_net_income`/`fund_usd_return_pct` are non-null.

### 11.7 Credentials

OANDA practice/demo token + API URL, read from environment variables
(`OANDA_TOKEN`, `OANDA_API_URL` in `backend/.env`), with an inline fallback
to the practice values supplied at setup — works out of the box, but
production should rely on the real `.env`/Railway config values, never the
inline fallback.

---

## 12. Darwinex Closed-Strategy P&L Bridge

### 12.1 The bug

Two Darwinex strategies — **DARWIN-1X** (`Chase1`, closed 22-05-2026) and
**DARWIN-3X** (`Chase3xA`, closed 27-07-2026) — are both `status: "Inactive"`
and have **no `user_pfees_estimation` feed at all** (Darwinex is tracked
purely through manually-entered `internal_transfers`, never had a daily
broker feed the way AXIA does). This exposed a real bug: when a strategy has
`brokerage_account` set but no live feed row matches it, the code treated
raw equity as **£0** and computed P&L as `0 − net_deployed` — wiping out the
**entire historical capital ever deployed** as "loss," not just whatever
was actually unrecovered. DARWIN-1X alone was showing roughly **−£899,950**
this way (its true realized loss, once fully closed, is only **−£12,007.75**)
— this single bug accounted for most of the −£2,731,618.31 / −75.61%
portfolio-wide P&L seen before this fix.

A second, subtler issue: even once every `internal_transfers` leg (including
the final close) is entered, `net_deployed` — a running **net** of
Wallet↔account flows — collapses toward the residual/loss amount once an
account is fully closed out. That breaks two things at once: "Capital
Invested" stops being the monotonic historical total the rest of the
dashboard promises (§4), and **Total ROI's denominator shrinks alongside its
numerator**, producing a nonsensical ≈ −100% instead of the true ≈ −1% of
capital actually lost.

### 12.2 The fix

`_darwinex_closed_strategy_agg(strategies)` in `supabase_service.py` — a
third bespoke aggregator alongside `_axia_strategy_agg` and
`_fund_statement_strategy_agg`. For every strategy with `status ==
"Inactive"` and a `brokerage_account`, it walks all `internal_transfers` for
that account and computes:

```
baseline (Capital Invested) = Σ all-time Wallet → account transfers   (frozen, never reduced)
current equity               = 0.0                                    (closed — nothing left open)
realized P&L                 = Σ all-time account → Wallet transfers − baseline
```

Account↔account rebalances (e.g. the £100,000 Chase1→Chase3xA reallocation
on 27-03-2026) are excluded from both sides — same convention
`_get_net_deployed_per_account()` already uses — they're a relocation, not a
Chase-level capital event.

Folded into the same three-level bridge AXIA and 12-FLAGS use (§11.5),
plus the Breakdown/Hierarchy table's strategy branch, plus
`_strategy_capital_invested()` (given top priority there, ahead of the
generic `brokerage_account → net_deployed` branch, specifically for closed
strategies) — and a watermark-reapplication guard fix in
`get_strategies_with_kpis_fast` (the existing `if axia_contrib is None:`
watermark pass-through would otherwise silently overwrite this aggregator's
correct output; now reads `if axia_contrib is None and fund_contrib is None
and darwinex_contrib is None:`).

**Verified** by extracting the aggregator logic and running it standalone
against the corrected `internal_transfers` rows (§12.3):

| Strategy | Capital Invested | Closing return | Realized P&L | ROI |
|---|---|---|---|---|
| DARWIN-1X (Chase1) | £900,000.00¹ | £887,942.25 | **−£12,007.75** | ≈ −1.33% |
| DARWIN-3X (Chase3xA) | £238,173.76 | £226,223.26 | **−£11,950.50** | ≈ −5.02% |

¹ £1,000,000.00 ever deployed to Chase1, less £100,000.00 rebalanced
straight through to Chase3xA on 27-03-2026 (§12.3) — not a withdrawal, a
same-day relocation between two Chase strategies, so it's excluded from
Chase1's own Capital Invested/ROI base. Chase1's realized P&L is unaffected
either way (the rebalance leg cancels out of the P&L formula regardless of
whether it's included).

Combined real Darwinex P&L: **−£23,958.25** — versus the −£999,950+ the bug
was contributing before the fix.

**Two independent display paths — both needed a fix, not just one.** The
Breakdown/Hierarchy table (`BreakdownTable.jsx`) reads `row.pnl` straight
from the backend — `_darwinex_closed_strategy_agg`'s output alone is enough
to make that view correct. But the Portfolio page's Strategy Overview cards
(`OverviewCard` → `computeCapitalMetrics()` in `CapitalOverview.jsx`)
**don't trust the backend's `total_pnl` field at all** — they deliberately
recompute `pnl = current_equity + banked_profit − initial_investment`
client-side (see §4's note on why `total_pnl` alone is unsafe: it's a raw
broker-snapshot figure that ignores already-withdrawn profit). For a closed
Darwinex strategy, `current_equity` is correctly 0.0 (from the backend fix
above) and `initial_investment` is correctly the frozen historical total —
but `banked_profit` comes from a **separate** function,
`_banked_profit_by_strategy()`, which only counts `internal_transfers` rows
that carry an explicit `profit_loss_amount` classification. Leave those
unclassified (as they are today) and the Portfolio page's own formula
computes `0 + 0 − 1,000,000 = −£1,000,000` — the exact same class of bug,
recreated entirely client-side, downstream of a backend that's already
correct. **Both paths have to be right independently** — there's no single
choke point that guarantees it. See §12.3 for the classification this
requires.

### 12.3 Data reconciliation

Reconstructed from the Darwinex platform's own wallet History export
against what was already logged in `internal_transfers`. All 6 existing
rows turned out to be genuine (an initial read of the first, partial
History export made the £1,000 opening leg on Chase1 look like test data —
the full export shows it's real: the first of three tranches funding Chase1
from the wallet's two external deposits, £500,000 + £500,000 = £1,000,000
on 17/18-03-2026):

- **Added:** DARWIN-1X's final closing leg (Chase1 → Wallet, £887,942.25,
  22-05-2026) — previously missing entirely, which is why the strategy still
  showed its full historical basis as an open loss.
- **Added:** DARWIN-3X's mid-life top-up (Wallet → Chase3xA, £138,173.76,
  25-06-2026) — previously missing, so Capital Invested understated the
  true amount ever deployed to that account.
- **Added:** DARWIN-3X's final closing leg (Chase3xA → Wallet, £226,223.26,
  27-07-2026) — entered as **4 separate rows**, matching the Darwinex
  platform's own History export exactly (£100,000.00 + £100,000.00 +
  £25,000.00 + £1,223.26, all same-day), not combined into one row.
- **Excluded:** two same-minute "DarwinexIndex" wallet legs (25-06-2026,
  £138,173.76 in then immediately back out) — net zero at the wallet, money
  passed through that wrapper and straight back out without ever being
  economically held there. Not treated as a third strategy.
- **Out of scope for the strategy P&L bridge** (internal_transfers only) but
  worth checking separately in Capital Events for ledger completeness: the
  two external Bank⇄Wallet legs implied by this history — £1,000,000 in
  (two £500,000 deposits, 17 & 18-03-2026) and £976,223.26 out (£750,000 on
  22-05-2026 + £226,223.26 on 27-07-2026, both "Withdrawal | Transfer" rows).

**One leg is a rebalance, not a return — handled in code, not classification.**
The 27-03-2026 £100,000 leg (Chase1 → Wallet) isn't Chase1 taking profit —
same day, that exact £100,000 was redeployed Wallet → Chase3xA (it's the
seed capital for DARWIN-3X). Classifying it as Profit/Loss would falsely
show Chase1 "earning" £100,000 it just handed to a sibling strategy. Since
Capital Invested is architecturally frozen (§4, never reduced by any
withdrawal, classified or not), the only place this can be corrected without
distorting Chase1's realized P&L is the backend aggregator itself:
`_darwinex_closed_strategy_agg` now detects same-day, same-amount
`X → Wallet` + `Wallet → Y` pairs and reduces **X's baseline only** (Capital
Invested) by the matched amount — Y's baseline is untouched (that £100,000
is genuine new capital to Chase3xA). `pnl = returned − deployed` uses the
*unreduced* deployed figure, so this is algebraically invariant on realized
P&L — Chase1's loss stays exactly −£12,007.75, only its Capital
Invested/ROI base shrinks (£1,000,000 → £900,000, ROI −1.20% → −1.33%,
reflecting that only £900,000 was ever truly at risk *within* Chase1 for
its full life — the other £100,000 was passed straight through to
Chase3xA). **This row stays unclassified in the ledger** — no
`capital_return_amount`, no `profit_loss_amount` — it's neither, it's a
relocation.

**Every other `→ Wallet` return leg (not part of a same-day matched pair)
does need classification**, for the reason given above: the Breakdown table
only needs the backend aggregator, but the Portfolio page's
`computeCapitalMetrics()` needs `banked_profit`, which comes from
`_banked_profit_by_strategy()` — and that function only counts
`internal_transfers` rows carrying an explicit `capital_return_amount` /
`profit_loss_amount` split. Convention: **Capital Return = £0.00,
Profit/Loss = the full leg amount**, on every genuine (non-rebalance)
return leg.

| `internal_transfers` row | Direction | Amount | Capital Return | Profit/Loss |
|---|---|---|---|---|
| 27-03-2026, id=4 (existing) | Chase1 → Wallet | £100,000.00 | — | — (unclassified — rebalance to Chase3xA, see above) |
| 14-04-2026, id=6 (existing — edit) | Chase1 → Wallet | £50.00 | £0.00 | £50.00 |
| 22-05-2026 (new) | Chase1 → Wallet | £887,942.25 | £0.00 | £887,942.25 |
| 27-07-2026 (new, leg 1/4) | Chase3xA → Wallet | £100,000.00 | £0.00 | £100,000.00 |
| 27-07-2026 (new, leg 2/4) | Chase3xA → Wallet | £100,000.00 | £0.00 | £100,000.00 |
| 27-07-2026 (new, leg 3/4) | Chase3xA → Wallet | £25,000.00 | £0.00 | £25,000.00 |
| 27-07-2026 (new, leg 4/4) | Chase3xA → Wallet | £1,223.26 | £0.00 | £1,223.26 |

Sum of Profit/Loss classified above = −£12,007.75 for DARWIN-1X (50 +
887,942.25 = £887,992.25 banked − £900,000.00 reduced-baseline invested)
and −£11,950.50 for DARWIN-3X (100,000 + 100,000 + 25,000 + 1,223.26 =
£226,223.26 returned − £238,173.76 invested) — matching §12.2's
verification table exactly. Inbound `Wallet →` legs (the £1,000 / £499,000 /
£500,000 deployments to Chase1, the £100,000 initial + £138,173.76 top-up
to Chase3xA) are **not** classified — they're capital going out, not
coming back.

### 12.4 Known caveat

**"Capital Allocated" still shows the small unrecovered residual (e.g.
£12,007.75 for DARWIN-1X) as "still allocated," even though the strategy is
fully closed with nothing actually outstanding.** This stat is computed via
a separate formula (`Capital Invested − Total Withdrawn`, §4), and even with
the §12.3 classification in place (Capital Return = £0.00 on every
classified leg), `Total Withdrawn` only ever sums to the amount actually
returned (£887,992.25 of £900,000.00 for DARWIN-1X) — the unrecovered
£12,007.75
necessarily remains "allocated" by this formula's definition, because it
was never returned. This is expected, not a bug: it's the realized loss
amount, correctly surfaced as the gap between what was invested and what
came back. Total P&L and Total ROI (the numbers that were actually broken)
are both correct. Not requested to be fixed further.

---

## 13. Roadmap — Completed

- [x] **Phase 1 — Profit/Loss classification on withdrawals**
  SQL columns + constraint, backend banked-profit aggregation, frontend
  split-entry UI, wired into Capital Overview + Pod/Strategy cards.
- [x] **Phase 2 — Watermark / profit-share**
  SQL columns, `_apply_watermark()`, Strategy form fields, inline
  watermark badge + status badge on overview cards.
- [x] **Phase 3a — AXIA-TW real profit withdrawals recorded**
  £156,000 (15-04-2026) + £24,000 (06-07-2026), both classified as pure
  profit under ref "AXIA MARKET PRO".
- [x] **Ledger-wide date/layout consistency pass**
  All 6 ledger tabs use `fmtDate()` + the standard bold-date/amount-below
  row layout.
- [x] **Capital Allocated formula fix**
  Now subtracts full withdrawn amount (capital return + profit/loss),
  not just capital return.
- [x] **Total P&L formula fix**
  Always computed as `Equity + Banked − Invested`, no longer reliant on
  backend's disconnected raw `total_pnl` field.
- [x] **Banked Profit → Banked Profit / Loss rename**
  Applied across hero card, table, chart, small print, sign-aware
  colour/icon.
- [x] **Phase 4 — Verification pass**
  Formula reconciliation, hand-reconciled numbers, build/compile checks,
  no-regression check on other pods/strategies.
- [x] **Venue concept fully removed**
  Frontend + backend, 11 files, zero remaining references (verified via
  grep sweep).
- [x] **Number input UX fix**
  Profit/Loss and Profit Share % fields — no spinner arrows.
- [x] **Capital Flow Summary table visual padding fix**
  Row shading no longer flush against cell edges (incl. header top
  padding).
- [x] **Banked Profit/Loss drill-down modal** (§9)
  Click-through from hero card / table row to the full classified-leg
  breakdown, reusing existing ledger query keys.
- [x] **AXIA Daily Equity — pagination** (§10)
  50/page, Previous/Next, applies to every AXIA client.
- [x] **AXIA notes-clearing PATCH bug fix** (§10)
  `exclude_unset=True` instead of an `is not None` filter — explicit
  `null` fields now save correctly.
- [x] **12-FLAGS Fund Monthly Statement + OANDA FX bridge — full build** (§11)
  SQL table, `oanda_service.py`, `fund_statements.py` router,
  `Flags12StatementEntry.jsx`, 3-level strategy/pod/portfolio KPI bridge,
  YTD column, USD sub-line on the strategy card.
- [x] **OANDA candle-selection + date-labelling bug fixes** (§11.4)
  Monthly→daily granularity switch (fixed two different months returning
  the same rate); `+6h` broker-day correction (fixed a Friday close
  showing as the day before); nanosecond-timestamp parsing made
  Python-version-safe.
- [x] **USD sub-line on 12-FLAGS strategy card** (§11.6)
  Total P&L / Total ROI stay GBP; a small USD YTD sub-line underneath
  each makes the FX-translation effect explicit instead of reading as a
  discrepancy against the statement's own quoted return.
- [x] **Post-build bridge audit — Breakdown/Hierarchy table gap fixed** (§11.5)
  `get_hierarchy_rows`'s `entity_type == "strategy"` branch had no
  fund-statement awareness (built its own darwin/account aggregation
  independently of the strategy-KPI function) — 12-FLAGS would have shown
  £0 AUM/PnL specifically in the Breakdown table despite its Strategy
  Overview card already being correct. Added the same `fund_contrib`
  branch used everywhere else. No double-counting found elsewhere —
  verified `get_portfolio_kpis_fast`'s current_equity/total_pnl sum from
  the raw pfees snapshot, not from `pod_agg`, so the portfolio- and
  pod-level fund fold-ins are additive from separate base sums.
- [x] **Phase 3b — Darwinex real data entry + closed-strategy P&L bridge** (§12)
  DARWIN-1X and DARWIN-3X `internal_transfers` reconciled against the
  Darwinex platform's own complete wallet History export (3 missing legs
  added via the Darwinex Transfers tab). Found and fixed the actual root
  cause of the portfolio's −£2,731,618.31 / −75.61% P&L: a closed strategy
  with no live feed had its entire historical capital wiped out as loss
  instead of just the realized shortfall. New
  `_darwinex_closed_strategy_agg()` aggregator, wired through the same
  3-level bridge + Breakdown table + a watermark reapplication-guard fix.
  Verified standalone: DARWIN-1X −£12,007.75, DARWIN-3X −£11,950.50 (both
  ≈ realistic single-digit-% losses, not −100%).
- [x] **Phase 5 — Three-state status model + `has_data` gate** (§14.4–14.5)
  Added Closed as a third status alongside Active/Inactive (was previously
  a binary Active/non-Active, with a dead "Paused" dropdown option).
  `has_data` computed per strategy — an Active strategy with real recorded
  capital but no performance source wired up now shows Capital Invested at
  par instead of a fabricated −100% loss; Inactive-with-no-data now
  contributes £0 everywhere. "No data yet" badge added to overview cards.
- [x] **Final Withdrawal feature + gross/true Banked Profit split** (§14.6)
  New checkbox on the Darwinex Transfers form's outbound leg — locks the
  closing leg as pure Capital Return, live-previews the resulting P&L, and
  auto-sets the strategy to Closed. `_banked_and_allocated()` derives
  Banked Profit/Capital Allocated algebraically for any closed-strategy
  aggregator match, since the ledger's `profit_loss_amount` classification
  is now always £0 for those strategies.
- [x] **Portfolio-level Capital & Performance Overview rebuilt as a strict
  sum of Strategies** (§14.1)
  Previously mixed two incompatible bases — Total Capital Invested from
  all-time bank deposits, Equity/Banked/P&L from raw snapshot/aggregator
  sources — producing a phantom multi-hundred-thousand-pound loss from
  undeployed cash and Inactive strategies. `get_portfolio_kpis_fast()` now
  sums straight from `get_strategies_with_kpis_fast()`'s per-strategy
  figures, guaranteeing Portfolio always reconciles exactly against the Pod
  and Strategy cards, per the manager's spec §13.
- [x] **GlobalGTX tab on the Analysis page** (§17.6, 2026-09-29, commit
  `57cb437`) — paste the daily Global Trading X statement email; stateless
  replay engine re-derives fees, swaps, opens/closes and reconciles every
  day to the cent. 17-09 → 28-09-2026 loaded, all reconcile.
- [x] **INVESTGTX USD P/L → GBP entry** (§15.7, 2026-09-30, commit
  `6b3b16f`) — record closed-trade net P/L in USD on the INVESTGTX Data Feed;
  auto-converted at OANDA GBP/USD (live 1-min close today, daily close for
  past dates) and saved as a GBP row the Portfolio reads.
- [x] **Backend load-time pass** (2026-10-05) — measured with
  `backend/tests/perf_profile.py` (read-only: DB calls + time per endpoint,
  cold/warm, and the Portfolio page's calls fired together). Each
  daily-equity table is now read once per cache refresh and filtered in
  memory (`_equity_table_rows`, paged in 1,000s) instead of once per client
  per helper (axia_daily_equity was read 9×); `capital_transfers_by_strategy`
  reuses the cached `list_capital_transfers()`; `_get_cached` has a per-key
  lock so simultaneous requests share one fetch. Portfolio cold load
  43 → 25 DB calls, 1.9 s → 1.2 s; page with its calls together 1.06 s →
  0.56 s; hierarchy tabs 1.2–1.4 s → 0.6 s. Results proven identical by
  `backend/tests/verify_equity_cache.py` (old vs new, every client/table).

---

## 14. Capital Pipeline: Strategy → Pod → Portfolio

This is the definitive reference for the six Capital & Performance Overview
headline metrics — what each one means, its formula, and how it flows
through the three-level hierarchy. Cross-checked line-by-line against the
manager's own spec (quoted inline below); the one deliberate departure from
that spec — how Banked Profit/Loss is now split into a gross/true pair — is
flagged explicitly in §14.5.

### 14.1 The hierarchy

```
STRATEGY CALCULATIONS  →  POD AGGREGATION  →  PORTFOLIO AGGREGATION
```

Every strategy calculates its own six numbers first (including its own
watermark/profit-share economics, §5). A Pod is the straight sum of its
member strategies' numbers — nothing is recalculated at pod level. The
Portfolio is the straight sum of every strategy's numbers, portfolio-wide
(equivalently, the sum of all pods — the two are identical because pods
themselves are sums of strategies). No level ever re-derives a number from
raw broker/ledger data independently of the level below it — this is what
guarantees Portfolio always reconciles exactly against what the Pod and
Strategy cards show, with no separate "uninvested cash" or reconciling
item anywhere.

Implementation: `get_strategies_with_kpis_fast()` computes every strategy's
kpis dict once; `get_pods_with_kpis_fast()` and `get_portfolio_kpis_fast()`
both sum straight from those same per-strategy figures — never from the
pfees snapshot, AXIA table, or ledger directly. (Historically the portfolio
level used `total_deposited` — all-time bank deposits — as its "Total
Capital Invested," independent of what was actually deployed into any pod
or strategy. That created a phantom loss whenever cash sat undeployed in
the wallet, or in an Inactive strategy that legitimately contributes
nothing: Equity/Banked only covered *deployed* capital, while Invested
covered *all* capital ever banked, so the P&L formula effectively treated
idle cash as a loss. Fixed by making Portfolio a strict sum of Strategies,
per the manager's spec §13.)

### 14.2 The six metrics

**Total Capital Invested** — total Chase capital ever put into a Strategy,
Pod, or Portfolio. Cumulative and monotonic: a withdrawal never reduces it.

```
Strategy:  Σ all capital ever deployed into this strategy
Pod:       Σ Total Capital Invested across its strategies
Portfolio: Σ Total Capital Invested across every strategy
```

**Banked Profit / Loss** — profit (or realized loss) physically withdrawn
and returned to Chase. Answers "how much have we actually banked?" Signed —
a closing withdrawal that returns less than was deployed is a realized
loss, shown negative.

```
Strategy:  Σ profit/loss distributions received from this strategy
Pod:       Σ Banked Profit/Loss across its strategies
Portfolio: Σ Banked Profit/Loss across every strategy
```

**Capital Allocated** — how much of Chase's original capital is still
economically out. Reduced by *everything* that's left the strategy,
whether that departure was profit or a return of principal.

```
Capital Allocated = Total Capital Invested − Banked Profit/Loss
```

**Current Equity** — the value of Chase's economic interest today, after
any watermark/profit-share adjustment (§5). Summed straight from strategy
level — never recalculated with a pod- or portfolio-level watermark
(watermarks are a strategy-specific economic arrangement; aggregating them
first and adjusting second would double-count or misattribute the fee).

**Total P&L** — total economic profit or loss since inception, banked and
unbanked combined.

```
Total P&L = Current Equity + Banked Profit/Loss − Total Capital Invested
```

**Total ROI** — Total P&L as a percentage of Total Capital Invested.

```
Total ROI = Total P&L ÷ Total Capital Invested × 100
```

### 14.3 Worked example (AXIA pod, matches the manager's own spec exactly)

| Strategy | Capital Invested | Current Balance | Status | Chase Current Equity |
|---|---|---|---|---|
| AXIA 01 | £0 | £0 | Inactive | £0 |
| AXIA 02 | £0 | £0 | Inactive | £0 |
| AXIA JJ | £250,000 | £275,000 | Active | £261,250 (below/above-watermark split, §5) |
| AXIA TW | £500,000 | £430,000 | Active | £430,000 (below its £500,000 watermark) |

AXIA TW has separately banked £180,000 of profit historically.

| Metric | AXIA Pod |
|---|---|
| Total Capital Invested | £750,000 |
| Banked Profit/Loss | £180,000 |
| Capital Allocated | £570,000 |
| Current Equity | £691,250 |
| Total P&L | £121,250 |
| Total ROI | +16.17% |

Live numbers differ slightly from this illustration (AXIA JJ has since
banked profit of its own) — the mechanics above are exact.

### 14.4 Three-state status model

Every Pod and Strategy carries one of three statuses. This determines
whether it contributes to the portfolio at all:

- **Active** — currently running. Contributes its real Capital Invested and
  Current Equity once it has a genuine performance source wired up (see
  §14.5, `has_data`). Until then, it contributes Capital Invested "at par"
  (Current Equity = Capital Invested, Total P&L = £0) rather than a
  fabricated loss.
- **Inactive** — paused, not currently running, but could restart. With no
  performance source wired up, contributes **£0 everywhere** — Capital
  Invested, Capital Allocated, Current Equity, Total P&L. This is the
  recommended parking state for any strategy you haven't wired up data for
  yet ("if nothing recorded yet, mark it Inactive for now").
- **Closed** — fully wound down, all money returned or banked. Uses its own
  bespoke aggregator when one exists (e.g. `_darwinex_closed_strategy_agg`
  for a closed Darwinex account, §12) to compute the true realized P&L from
  all-time cash in vs all-time cash out — never a ledger-classification
  guess.

Any strategy can move between all three states at any time (e.g. re-funding
a Closed strategy moves it back to Active). Status is a manual field, set
via Manage Pods & Strategies → Strategies → Status.

### 14.5 `has_data` — what happens automatically when you add a new Pod or Strategy

**New Pod:** nothing to configure. A pod's numbers are always the sum of
whatever strategies are assigned to it — add a strategy to a pod, its
numbers flow up on the next page load. No separate pod-level setup exists
or is needed.

**New Strategy:** the backend computes a `has_data` flag per strategy —
true the moment ANY real performance source is matched (a live pfees feed
via `account_id`/`brokerage_account`/Darwin code, or one of the bespoke
aggregators: AXIA, 12-FLAGS fund statements, or closed-Darwinex). Until
then `has_data` is false, and:

- **Active + no data** → Capital Invested shows the real amount recorded
  (e.g. via a Capital Transfers deposit), Current Equity = Capital Invested,
  Total P&L = £0. A grey "No data yet" badge appears next to the strategy's
  status badge on its overview card, in every location strategies are shown.
- **Inactive + no data** → everything shows £0, as described in §14.4.

Practically: create a new strategy, deposit capital into it via Capital
Transfers, leave it Active — the dashboard shows real Capital Invested at
par immediately, with no misleading -100% loss, until a live feed or
aggregator is wired up (at which point `has_data` flips true automatically
and real performance takes over).

Implementation: `has_data` computed once per strategy inside
`get_strategies_with_kpis_fast()`, returned on `StrategySummary.has_data`
(`schemas.py`). `_strategy_capital_invested()` and `_build_pod_pfees_map()`
apply the matching Inactive/Closed-with-no-data zeroing at pod level so pod
and portfolio sums always agree with what the strategy card shows.

### 14.6 Gross vs true Banked Profit/Loss — and the Final Withdrawal feature

**Deliberate departure from the manager's spec.** The spec's Banked Profit
formula assumes every withdrawal is already correctly classified as either
profit or return-of-capital at the moment it's recorded. In practice, for a
strategy that's fully closing out, that classification is often not known
leg-by-leg — you only know the true profit/loss once the LAST withdrawal
lands and you can compare total-in vs total-out. Two internal fields exist
to solve this without ever showing a misleading number:

- `banked_profit` (**gross**, internal only, never displayed) — feeds the
  Total P&L formula (§14.2). For a Closed strategy with no bespoke
  aggregator match, this is `Capital Invested + true realized P&L` — i.e.
  the full cash ever recovered, algebraically derived, not summed from the
  ledger's `profit_loss_amount` column (see below for why).
- `banked_profit_true` (**displayed everywhere** — hero card, pod/strategy
  cards, Capital Flow Summary, Breakdown modal) — the real net realized
  P&L. For a Closed strategy with a bespoke aggregator, this is that
  aggregator's own realized pnl (all-time cash returned − all-time cash
  deployed). For every other strategy type, identical to the gross figure
  (a genuine profit-skim withdrawal already IS the true number).

**Final Withdrawal feature** (Darwinex Transfers tab): recording a strategy's
last withdrawal leg is done differently from a partial/early profit-take.
Tick "Final withdrawal — closes X" on the outbound leg:

1. The leg is locked as pure Capital Return in the ledger
   (`capital_return_amount = amount`, `profit_loss_amount = 0`) — no manual
   profit/loss guess is required or possible for the closing leg.
2. The strategy's status is automatically set to Closed.
3. A live preview shows Deployed / Returned / Resulting P&L before you
   save, computed the same way the dashboard will show it afterward.

Because every leg (including the final one) is now pure Capital Return,
the ledger's `profit_loss_amount` sum for a Closed strategy is always
£0 — it can no longer be used to derive Banked Profit/Loss or Capital
Allocated. `_banked_and_allocated()` in `supabase_service.py` is the single
override used at strategy/pod/portfolio level: for any strategy present in
a closed-strategy aggregator (e.g. `_darwinex_closed_strategy_agg`), it
derives `banked_profit` (gross) and `capital_allocated` algebraically —

```
banked_profit (gross) = Capital Invested + true realized P&L
capital_allocated      = £0   (Closed means nothing is left deployed, by definition)
```

— instead of summing the ledger's classification, which the Final
Withdrawal feature deliberately leaves at zero. Every other strategy type
(AXIA, fund-statement, still-open Darwinex, manual) is unaffected — those
continue to sum the ledger's `capital_return_amount`/`profit_loss_amount`
columns exactly as before, per the manager's spec.

**Non-final legs** (a partial/early profit-take while the strategy is still
running) still use the manual Capital Return / Profit-Loss split — only the
strategy's actual final withdrawal is auto-computed.

---

## 15. Data Feeds — Self-Service Tab Builder

Self-service system (added 2026-09) for onboarding a new strategy's
recurring performance data (daily broker/CFD NLV, or monthly NAV statement)
**without a developer writing new backend code**. Lives alongside — does
NOT replace — the legacy AXIA/IG (daily) and 12-FLAGS/ASLAN (monthly)
systems, which stay hardcoded exactly as before.

### 15.1 Architecture

**`data_feeds` registry table** — one row per user-created tab:

```
id, slug (unique), name, color, cadence ('daily'|'monthly'), currency,
clients_table, equity_table, statements_table, sort_order, created_at
```

**`strategies.data_feed_id`** (uuid, FK → `data_feeds.id`) — set when a
strategy is linked to any feed, daily or monthly.
**`strategies.data_feed_client_id`** (uuid) — daily cadence only, points to
a row in that feed's `<slug>_clients` table. Monthly cadence needs no
client — one statement stream per strategy, matched directly by
`data_feed_id` (same convention as 12-FLAGS/ASLAN's `strategy_code`
matching, just by id instead of code).

**Two physical table shapes**, chosen by cadence, both exact mirrors of the
existing AXIA/12-FLAGS shapes so nothing new had to be invented:
- **Daily**: `<slug>_clients` + `<slug>_daily_equity` — identical columns to
  `axia_clients`/`axia_daily_equity`.
- **Monthly**: `<slug>_monthly_statements` — identical columns to
  `fund_monthly_statements` (beginning/ending balance, additions,
  redemptions, OANDA FX bridge for USD statements).

### 15.2 Creation flow — never runs SQL itself

1. Data & Reports → **⚙ Manage Data Feeds** → **+ New Data Feed** → fill
   name / slug / cadence / currency / color → **Generate SQL** calls
   `POST /api/data-feeds/preview-sql` (pure string templating, zero
   execution) → exact `CREATE TABLE` text shown with a copy button.
2. Run that SQL once in Supabase.
3. Tick "I've run this SQL" → **Create Feed** fires `POST /api/data-feeds`,
   which only inserts the registry row (assumes the table(s) already
   exist — a "relation does not exist" error means step 2 was skipped).

Once registered, the feed appears immediately as a new colored tab on
Data & Reports (`Reports.jsx` reads `GET /api/data-feeds`, sorted by
`sort_order`) and as a selectable **Data Feed** on the strategy form in
Manage Pods & Strategies (`PodStrategyManager.jsx`).

### 15.3 Backend — `backend/src/routers/data_feeds.py`

Full endpoint set, all dynamic on `{slug}` resolved via the registry:
`GET/POST/PATCH/DELETE /api/data-feeds[/{id}]`,
`POST /api/data-feeds/preview-sql`,
`GET/POST/PATCH/DELETE /api/data-feeds/{slug}/clients[/{id}]` (daily),
`GET/POST/PATCH/DELETE /api/data-feeds/{slug}/equity[/{id}]` +
`/equity/prev` (daily),
`GET/POST/PATCH/DELETE /api/data-feeds/{slug}/statements[/{id}]` +
`/statements/prev` + `/statements/{id}/refetch-fx` (monthly).

Backed by the same parameterized service functions AXIA/IG/12-FLAGS
already used — `_axia_strategy_agg(..., client_field, clients_table,
equity_table)` and `_fund_statement_strategy_agg(..., table)` — plus
`_data_feed_agg(strategies)` in `supabase_service.py`, which loops the
registry, calls the right parameterized aggregator per feed by cadence,
and merges every feed's contribution into one `{strategy_id: {...}}` dict.

**Merge-into-combined-dict pattern** (established for
AXIA/IG/12-FLAGS/ASLAN, now extended): both central computation sites use
`{**_axia_strategy_agg(...), **_ig_strategy_agg(...), **_data_feed_agg(...)}`.
Downstream KPI builders need zero further changes once a feed is merged in.

**Gate-check pattern**: every place deciding "does this strategy have a
bespoke performance source" checks all three link fields —
`s.get("axia_client_id") or s.get("ig_client_id") or s.get("data_feed_id")`.
Live call sites (all updated, `supabase_service.py`): `_strategy_capital_invested`
(§14), `_closed_manual_strategy_agg`'s exclusion guard, and both hierarchy/
strategy-KPI `axia_contrib` lines. **Note**: `get_pods_with_kpis()` (undecorated,
no `_fast` suffix) also has a copy of this gate but is dead code — no router
calls it, `get_pods_with_kpis_fast()` is what's live (`portfolio.py`). Left
as-is rather than fixed, since touching unused code risked scope creep;
harmless as long as nothing starts calling it again without the ct_by_strategy
check too.

### 15.4 Frontend

- `DataFeedManager.jsx` — the create/edit/delete UI described in §15.2.
- `Reports.jsx` — colored tab bar: **Reports** (downloads, blotter upload,
  AXIA Statement Parser/Merge) → **AXIA** → **IG** → **12-FLAGS** →
  **ASLAN LABS** → one tab per registered Data Feed, in `sort_order`.
- `AxiaEquityEntry.jsx` — already generic (`apiPrefix`/`label`/
  `clientLinkField` props, added for IG). A daily Data Feed tab reuses it
  unchanged: `<AxiaEquityEntry apiPrefix="/api/data-feeds/{slug}" clientLinkField="data_feed_client_id" />`.
  Since 2026-09-30 `Reports.jsx` also passes `fxFrom="USD"` when the feed's
  registry currency is USD (INVESTGTX only today) — enables the USD P/L →
  GBP entry mode (§15.7). GBP feeds pass `fxFrom=null` and render exactly
  as before.
- `Flags12StatementEntry.jsx` — generalized this window with `feedSlug`/
  `dataFeedId` props (legacy `<Flags12StatementEntry />` and the ASLAN
  instance are untouched, both default to `feedSlug=null` → old
  `/api/fund-statements` behaviour). A monthly Data Feed tab passes
  `feedSlug={feed.slug} dataFeedId={feed.id}` — strategy resolution then
  matches by `data_feed_id` instead of `strategy_code`.
- `PodStrategyManager.jsx` — **Data Feed** dropdown (any registered feed)
  + a conditional **Client/Account** dropdown (daily cadence only, scoped
  to that feed's clients table) — additive alongside the existing separate
  AXIA/IG fields, which are untouched. The strategy list also shows a
  colored Data Feed badge (feed name + cadence) when linked, same row
  slot as the AXIA/IG client badges.

### 15.5 ⚠ Linking pitfall — read before linking a strategy to a feed

Setting **Data Feed** on a strategy switches it to **auto mode** — the
manual **Initial Investment** field disappears from the form entirely, and
the backend ignores whatever value is stored there, because it now expects
Capital Invested/Current Equity to come from that feed's linked client's
equity history (or the ledger, see §14's priority chain).

**Only link a Data Feed if you are actually going to record data on that
feed's tab.** A strategy funded once with a static, no-statement figure
(no recurring updates expected — e.g. a private-company stake) should be
left with **Data Feed = None** and its value entered directly in **Initial
Investment**. Linking it to an empty feed with no client/statement yet
makes Capital Invested show £0.00 everywhere — this happened once (OPTIOS,
2026-09-01) and was fixed by clearing the Data Feed link and re-entering
the manual figure. If a feed was created by mistake, delete it via
Manage Data Feeds (blocked automatically if any strategy is still linked).

### 15.6 Onboarding a new strategy — decision tree

When a new strategy email/instruction comes in, pick ONE of these — don't
mix:

1. **Static / one-off figure, no recurring statement** (e.g. a private
   investment held at cost until further notice). Manage Pods & Strategies
   → create strategy → Data Feed = **None** → set **Initial Investment**
   directly. Nothing else to build. *(Example: OPTIOS.)*

2. **Daily broker/CFD equity, no live API access yet** (manager reports
   end-of-day NLV manually, e.g. "will be a CFD broker account, updated
   daily"). Data & Reports → ⚙ Manage Data Feeds → New Data Feed → cadence
   **Daily** → run the generated SQL → create feed → new colored tab
   appears → add the client/account on that tab → link the strategy to it
   (Data Feed + Client/Account dropdowns). *(Example: INVESTGTX.)*

3. **Monthly NAV/fund-administrator statement** (like 12-FLAGS/ASLAN, but
   a new fund). Same as #2 but cadence **Monthly** — no client needed,
   strategy links by Data Feed alone; record each month's Ending Balance
   on that tab.

4. **AXIA or IG sub-account** (already-integrated platforms, more accounts
   under the same broker). No new feed needed — just add a new
   client/account on the existing AXIA or IG tab and link it via the
   existing AXIA/IG Client dropdown on the strategy form (§10, the
   platform-parity pattern from the IG build).

5. **Darwinex-mirrored strategy**. Set `brokerage_account` on the strategy
   — fully automatic via `internal_transfers`/`pfees`, no manual recording
   at all (§12).

### 15.7 USD P/L → GBP entry (INVESTGTX, 2026-09-30)

**Why.** INVESTGTX trades on Global Trading X in **USD**, but the strategy's
capital (initial investment, watermark, profit split) is in **GBP**, and the
Portfolio only reads **GBP** rows from any daily-equity table
(`_axia_equity_series` / `_axia_flagged_equity_total` filter
`currency = 'GBP'`). A plain USD row would be silently ignored.

**How it's used.** Only on the days a position is **closed**, record that
trade's **net realised P/L in USD** (gross − open fee − close fee − swaps —
the "Net P/L" column of the GlobalGTX Closed Positions table, §17.6).
Data & Reports → INVESTGTX → Entry mode **USD P/L → GBP** → date → amount →
Submit. The USD figures never appear on the Portfolio — the GlobalGTX tab
keeps the USD book.

**What gets saved.** A normal GBP trading-day row:

| Field | Value |
|---|---|
| `currency` | `GBP` |
| `chg_nlv` | `round(USD ÷ rate, 2)` — rate = USD per £1 (GBP_USD) |
| `equity` | previous GBP row's equity + `chg_nlv` (computed server-side) |
| `notes` | `+$4,495.70 @ 1.32405 (daily close 23-09-2026)` — shown in the records table |
| `source_currency` / `source_amount` / `fx_rate` / `fx_rate_at` | USD audit trail (additive columns, `fx_rate_at` null for a daily close) |

The Portfolio, TWR, watermark and profit-split logic then treat it exactly
like any other GBP daily-equity row — e.g. £5,100.58 total GBP gain × 60%
profit split = £3,060.35 Total P&L on the INVESTGTX card.

**Which rate** (`oanda_service.get_conversion_rate`, OANDA GBP_USD midpoint):

| Entry date | Rate used |
|---|---|
| Today (UTC) or later | Close of the last **complete 1-minute** candle (`get_latest_minute_close`) — live; re-fetched at save |
| Past date | That day's **daily close** (`get_daily_close`); weekend/holiday → last trading day before |

`get_daily_close` asks OANDA with `to = <date> 12:00 UTC`, which returns
the candle that *closes* on that date (OANDA daily candles run 17:00 NY →
17:00 NY, so day D opens ~21:00/22:00 UTC on D−1). The older
`get_monthly_close` (12-FLAGS, §11.4) uses `to = <date+1> 00:00 UTC`, which
was found to return the **next** day's candle — see §19; it is left
untouched here so saved 12-FLAGS figures don't move.

**Backend** (`data_feeds.py`):
- `GET /api/data-feeds/{slug}/fx-preview?amount=&date=` → `{rate, source
  ("minute"|"daily"), rate_at, candle_date, stale, amount, gbp}` — drives
  the live preview in the form. Saves nothing.
- `POST /api/data-feeds/{slug}/equity` with `source_currency: "USD"` +
  `source_amount` → USD → GBP path. `equity` is now optional in the model
  (still required, with the same 400, on the normal GBP path).

**Guards** (all reject with nothing saved):
- Feed currency must be USD (GBP feeds → 400).
- Capital-flow flags refused — record Initial/Add-On in **GBP NLV** mode.
- Needs an earlier GBP row to build on (the GBP initial investment).
- A USD entry dated **before** an existing GBP row → 409 (record in date
  order, so the running equity chain can't break).
- OANDA unreachable → 502.

**Frontend** (`AxiaEquityEntry.jsx`, `fxFrom` prop): Entry mode toggle
(**USD P/L → GBP** default | **GBP NLV** = the original form, unchanged).
USD mode shows *Realised change (USD)* → *CHG NLV (GBP)* → *Equity (NLV,
GBP)* (both auto), the rate line, and a confirm popup. To correct a
converted row: delete it and re-add it (Edit changes GBP fields only).

**SQL run** (2026-09-30, additive):
```sql
alter table public.investgtx_daily_equity
  add column if not exists source_currency text,
  add column if not exists source_amount   numeric,
  add column if not exists fx_rate         numeric,
  add column if not exists fx_rate_at      timestamptz;
```
The Data Feed SQL template (`preview-sql`) now includes these 4 nullable
columns, so any future USD daily feed gets them from creation.

**Recorded so far:**

| Date | USD (net, closed trade) | Rate | GBP CHG | GBP Equity |
|---|---|---|---|---|
| 23-09-2026 | +4,495.70 (AMBARELLA) | 1.32405 | +3,395.42 | 1,003,495.42 |
| 24-09-2026 | +2,254.02 (QIAGEN) | 1.32188 | +1,705.16 | 1,005,200.58 |

---

## 16. Temporarily Hidden / Paused Metrics — Review Before Re-Enabling

Metrics below are deliberately hidden, paused, or redefined in the UI —
not deleted, not broken. Flagged here so a future pass remembers to
revisit them, and so nobody "fixes" a component that's working exactly as
asked. History kept in date order since this section gets revisited.

### 2026-09-07 — Portfolio KpiData schema bugfix; Capital Flow Summary/Chart paused; info box synced

**Bugfix — `active_capital_invested` was silently dropped from every API
response.** Root cause found and fixed today: `backend/src/models/schemas.py`
`KpiData` (the Pydantic response model for `/api/portfolio/`) never declared
`active_capital_invested`. FastAPI/Pydantic silently strips any dict key not
present on the response model before serialising — so the backend logic
(§ below, added 2026-09-03) was correct the whole time, but the field never
reached the frontend. This was NOT a deploy/caching issue, despite looking
exactly like one (same value shown before and after multiple Railway
redeploys). Fixed by adding `active_capital_invested: Optional[float] = None`
to `KpiData` in `schemas.py`. Any future Portfolio-level-only field added to
`get_portfolio_kpis_fast()`'s return dict must also be added to `KpiData`,
or it will be dropped the same way — check the schema first if a new
computed field ever "doesn't show up" despite the backend logic looking
right.

**Capital Flow Summary table + Capital at a Glance chart — paused (not
deleted).** `src/pages/Portfolio.jsx`: the grid rendering
`<CapitalFlowTable>` + `<CapitalAtGlanceChart>` is now wrapped in a JSX
comment, directly above `<CapitalInfoBox>` (kept, now the sole capital-flow
summary on the page). Both components remain fully defined and imported in
`CapitalOverview.jsx` — re-enabling is a single uncomment in `Portfolio.jsx`,
no other change needed.

**`CapitalInfoBox` — "We invested …" figure now matches the hero strip
exactly.** `CapitalOverview.jsx`'s `CapitalInfoBox` previously read
`invested` (all-time, e.g. £3,541,842.11) while the hero strip's "Capital
Invested" box read `investedActive` (Active-strategies-only, e.g.
£2,387,668.35) — two different numbers on the same page for what reads as
the same claim. Fixed: `CapitalInfoBox` now destructures `investedActive`
from `computeCapitalMetrics()` and uses it in the "We invested X" sentence.
Every other figure in the box (banked, allocated, equity, pnl, roi) is
unchanged — still all-time, per §14's reconciliation invariant.

### 2026-09-03 — Portfolio hero strip back to 6 boxes, new "Capital Invested" box

Supersedes the 2026-09-02 change below (that 4-box interim state no longer
exists). Current state, in `src/components/CapitalOverview.jsx` +
`src/pages/Portfolio.jsx`:

**New first box — "Capital Invested" (Portfolio hero strip only).** NOT
the same metric as the old "Total Capital Invested" box (see retirement
note below) — this is a live, **Active-strategies-only** sum:

```
Capital Invested = Σ initial_investment, for every strategy where status == "Active"
```

Backend: `active_capital_invested`, computed in `get_portfolio_kpis_fast()`
(`backend/src/services/supabase_service.py`) by filtering `strat_rows` on
`status == "Active"` before summing `initial_investment` — Inactive AND
Closed strategies are both excluded entirely (no exception for a frozen
closed-Darwinex baseline, unlike the all-time `invested` figure — see
below). Ticks up automatically the moment a new strategy is created
Active, or an existing one is switched to Active; drops out the moment a
strategy is marked Inactive or Closed. No manual bookkeeping, no SQL,
nothing else to wire up.

Frontend: `computeCapitalMetrics()` exposes it as `investedActive`,
falling back to the all-time `invested` if an older cached API response
doesn't have the new field yet (safe default, never crashes/zeroes).

**Important — this is a display-only, additive field.** Total P&L, Total
ROI, and Capital Allocated all still derive from the ORIGINAL all-time
`invested`/`initial_investment` (unchanged) — that preserves the
Portfolio = ΣPods = ΣStrategies reconciliation invariant (§14) exactly as
before. `active_capital_invested` is never fed into any other formula.

**Hero strip now shows all 6 boxes again**, in this order: **Capital
Invested (NEW, active-only) → Capital Allocated → Current Equity → Total
P&L → Banked Profit/Loss → Total ROI (still paused, shows "—").** Desktop
grid back to `repeat(6, ...)`; mobile stays `repeat(2, ...)` (3 rows of 2).

**Capital Flow Summary table and Capital at a Glance chart** also got the
new "Capital Invested (Active Strategies)" row/bar added back, first
position, same order as the hero strip (both components still stop at 5
items — no Total ROI row/bar there, matching the pre-existing pattern).
Chart bar thickness tuned back down slightly from the 2026-09-02 4-bar
values now that there are 5 bars again.

**Retired** (not the same as "hidden" — semantically superseded, kept
purely as a commented reference in case the all-time figure is ever needed
as its own box again): the old "Total Capital Invested" hero card / table
row / chart bar — all-time, monotonic, every strategy regardless of
status. Comments sit directly above each `cards`/`rows`/`data` array in
`CapitalOverview.jsx` with the exact object to paste back if needed.

**Total ROI remains paused** — see the entry below, unchanged. Still
shows "—" everywhere (hero strip's last box, and Pods/Strategies cards).

### 2026-09-02 — Total ROI paused on Pods Overview / Strategies Overview cards

`src/pages/Portfolio.jsx`, `OverviewCard`'s **Total ROI** `StatBox`: box
stays in its usual grid position, but shows a plain **"—"** instead of a
computed value — the formula (`fmtPctSigned(roi)`,
`tone={roi >= 0 ? 'pos' : 'neg'}`) is commented directly above the live
JSX, not deleted. This ROI figure is still being finalized at the
pod/strategy level (ties into the known pod/portfolio-level watermark gap,
§19) — reinstate once that's settled by uncommenting and swapping the
`value`/`tone` props back. The Portfolio hero strip's own Total ROI box
(last position) is paused the same way, same reason.

**Not touched, either date**: the underlying `roi`/`invested` values
themselves — `computeCapitalMetrics()` still computes them every render,
every backend KPI field is untouched, and
`Capital_Performance_Overview_Explained.md` (the manager-facing explainer)
still documents the full formula set. Only the display layer is paused.

---

## 17. AXIA Trade Analysis Tool — GBP FX Bridge

> **Analysis page tabs (since 2026-09-29):** **AXIA Analyse** (default — this
> section) | **GlobalGTX** (§17.6). Both stay mounted; switching tabs never
> loses AXIA upload state.

Separate feature from the Portfolio/Pods/Strategies dashboard — a standalone
statement-analysis tool at the **Analysis** tab. Upload an AXIA trade
statement (`.xlsx`) → get a full quant breakdown (net P&L by instrument,
per-currency books, commission breakdown, top winners/losers) → optionally
convert every currency to GBP and export a professional Excel report or save
a shareable read-only link. Previously undocumented in this README — added
2026-09-08 after debugging a GBP-conversion bug (below).

**Files:**

| Layer | File |
|---|---|
| Frontend page | `src/pages/Analysis.jsx` |
| Frontend dashboard | `src/components/AxiaAnalysisDashboard.jsx` |
| Backend router | `backend/src/routers/axia_analysis.py` (`/api/analysis/*`) |
| Backend service | `backend/src/services/analysis_service.py` |

**Flow:** upload → `parse_statement()` parses the Excel, aggregates by
instrument/currency, computes GBP view inline if rates are available →
cached server-side in-memory by `analysis_id` (UUID) → `/export` downloads
an Excel report (native-currency or GBP, `?gbp=true`) → `/save` persists to
Supabase for a shareable `/analysis/shared/{id}` read-only link (Save &
Share button — **disabled until GBP rates are successfully fetched**, since
the shared/boss view is GBP-only by design).

**GBP conversion — separate OANDA integration from §11.** This tool has its
own currency→GBP OANDA bridge (`_CCY_GBP_PAIR` map in `analysis_service.py`
— USD, EUR, JPY, CAD, CHF, AUD, NZD, HKD, CNH, SGD, each mapped to an OANDA
pair + invert flag), independent of the 12-FLAGS fund-statement OANDA bridge
described in §11 (`oanda_service.py`). Same underlying OANDA practice API
and token, two separate call sites — worth knowing if either one needs a
token rotation, since both `.env` vars (`OANDA_TOKEN`, `OANDA_API_URL`) feed
both.

### Bugfix 2026-09-08 — "GBP rates unavailable" for every currency at once

**Symptom:** Analysis page showed `GBP rates unavailable (CAD, CHF, CNH,
EUR, HKD, JPY, USD)` — every currency failing simultaneously, Save & Share
permanently disabled (`hasGbpData` gate never passes).

**Root cause:** `_fetch_oanda_pair_series()` built its OANDA `to` date as
`last_trade_date + 2 days` (a buffer, presumably meant to catch late
weekend/holiday candles). For any statement whose last trading day falls
within 2 days of "now", that pushes the `to` timestamp **into the future**
relative to OANDA's own clock. OANDA rejects any future `to` with HTTP 400
(`"Invalid value specified for 'to'. Time is in the future"`). Confirmed by
reproducing the exact request directly against OANDA's API and getting that
exact error back. The old code caught the non-200 response and returned
`{}` — graceful degradation by design, but silent, so every currency (all
sharing the same shared date range) failed the same way with no real error
surfaced to the user.

Notably, §11's OANDA bridge (12-FLAGS fund statements) already had its own
future-date clamp for exactly this class of problem — this tool's separate,
newer OANDA integration just hadn't been given the same safeguard.

**Fix:** `_fetch_oanda_pair_series()` now clamps the computed `to` timestamp
to `min(to_date + 2 days, datetime.utcnow())` — never sends OANDA a
timestamp later than the current UTC time. Applies automatically to every
future statement upload; no other code path affected (`_CCY_GBP_PAIR`,
`_build_rate_lookup`, `_compute_gbp_view`, the `/refresh-gbp` endpoint's
error handling — all unchanged).

**Not fixed here** (future hardening, not requested yet): the silent
`except Exception: return {}` / `if status_code != 200: return {}` pattern
still swallows the *specific* OANDA error message before it reaches the
frontend — the UI shows "unavailable" for any failure reason (bad token,
network issue, rate-limited, unsupported pair, anything), not just the
future-date case just fixed. If GBP rates ever fail again for a different
reason, check Railway backend logs for the raw OANDA response, since the
UI alone won't say why.

---

## 17.5 Statement Parser — Multi-Account, Multi-Day Batch (2026-09-23)

**Problem.** A single AXIA "Daily Detail Statement" PDF can carry MULTIPLE
accounts under one Client id (e.g. Client `4751R` → Accounts `47511`,
`47512`, `47513`, `47514`, `47515`), each account's pages grouped
sequentially, each starting with its own FINANCIAL SUMMARY page repeating
that account's own Client/Account/Trade Date header. The old parser
(`statement_service.py`) locked onto the FIRST Client/Account it saw on
page 1 and never re-read the header again, so every subsequent page's
trades — regardless of which account they actually belonged to — got
accumulated into that one account's totals. An instrument traded on two
different accounts the same day (not rare — same underlying, different
books) silently summed together under one label. There was also no
Account column anywhere downstream (Excel schema, merge, Analysis parser)
even after the correct account was known, so there was no way to recover
per-account granularity once written to a spreadsheet.

**Fix — parser.** `statement_service.py`'s `StatementParser` now re-reads
Client/Account on every page (Trade Date is read once, same for the whole
statement). When the (client, account) pair changes from the page before,
that's a new account block — an isolated `{"conf": {}, "pnl": {}}`
accumulator is created for it and section/product tracking resets, so
nothing bleeds across the boundary. `extract_statement()` now returns
`rows` (every account's rows, each tagged `client`+`account`) and
`accounts` (a per-account breakdown: row count, currencies, trade date).

**Fix — Excel schema.** `excel_writer.py` adds CLIENT (col B) and ACCOUNT
(col C) columns to the Statement sheet (shifts everything after it two
columns right — this is a breaking schema change from the pre-2026-09-23
layout, intentional). The TOTALS block at the bottom is now grouped by
**(ACCOUNT, CURRENCY)** instead of CURRENCY alone — summing across accounts
into one subtotal would silently mix two books back together, exactly the
bug this fix removes. An "Info" sheet lists every account detected with its
row count/currencies/date, even accounts with zero activity that day (kept
visible so a quiet account isn't mistaken for a missing one).
`merge_service.py` (legacy Excel-to-Excel merge) reads/writes the same
CLIENT/ACCOUNT columns so old-style merging of already-downloaded Excels
stays correct too.

**Fix — batch upload + account picker.** `statement.py` now exposes:
- `POST /api/statement/upload` (one PDF) and `POST /api/statement/upload-batch`
  (any number of PDFs — e.g. every daily statement from 16-Feb through
  today in one drop) — both return the SAME shape: `{batch_token, accounts,
  date_range, total_rows, skipped}`. Parsed rows are cached server-side
  in-memory (`_PARSED_CACHE`, same recency-trimmed pattern as
  `analysis_service`'s cache) keyed by `batch_token` — nothing is written
  to disk until a download is actually requested.
- `GET /api/statement/batch/{batch_token}/download?accounts=A,B&mode=combined|separate`
  — `combined` (default) returns one workbook with every selected account
  tagged + totalled separately inside it; `separate` with 2+ accounts
  returns a ZIP of one workbook per account. This is what
  `StatementUpload.jsx`'s new account-picker table calls once the person
  has ticked which account(s) they want.
- An account that only starts appearing partway through a batch (e.g.
  opened mid-year) is handled automatically — its `date_from` in the
  breakdown is simply later, no special-casing needed; it's derived purely
  from the rows actually present for that account.

`StatementUpload.jsx` was redesigned around this: drop one or many PDFs →
Parse → an account-picker table (client, account, date range, row count,
currencies, a checkbox per account, defaulting to every account that had
trade activity) → Download Combined or Download Separately (ZIP).

**Fix — Analysis tab account filter.** `analysis_service.py`'s aggregation
body was factored out into `_compute_analysis(raw)` so it can run on any
subset of rows, not just everything in the uploaded file. `parse_statement()`
now also reads CLIENT/ACCOUNT off the Excel and tags every raw row with
them; the returned `data['accounts']` lists every distinct account present.
A new `GET /api/analysis/{analysis_id}/filter?accounts=A,B` (empty/omitted
= every account combined) recomputes the full analysis dict from the
cached `_raw` rows scoped to just those accounts — same numbers `/upload`
would have produced had only that account's Excel been uploaded, computed
without re-uploading anything. `Analysis.jsx` renders an account filter bar
above the dashboard (only when the upload actually contains 2+ accounts)
that calls this endpoint on each change; `AxiaAnalysisDashboard.jsx` itself
is untouched — it just receives whichever `data` the filter currently
resolves to. Export/Save still operate on whatever was last uploaded/
filtered into `analysisData` — no separate change needed there.

---

## 17.6 GlobalGTX — Statement Reconciliation (2026-09-29)

**What it is.** Second tab on the **Analysis** page. Tracks the INVESTGTX
strategy's **Global Trading X** account (`GTX0011-USD`) trade by trade, in
**USD**, from the broker's daily statement email (arrives ~23:00
weekdays; the broker's operations CSV is not normally available). Every
fee, swap, open and close is re-derived and reconciled against what the
broker says, to the cent.

**How it works.**
1. **+ Add Statement** → pick the statement date → paste the whole email
   (or its 3 sections: Account Statement, Open Position, Trades) →
   **Preview** (parse + dry-run reconciliation, nothing saved) → **Save
   statement**.
2. Only the **pasted text + parsed JSON** are stored (`gtx_statements`,
   one row per date; `gtx_settings`, one row of engine overrides). There is
   **no positions table** — every page load replays all saved statements in
   date order (`gtx_engine.replay`), so nothing can drift out of sync.
3. Paste oldest first. The first statement's balance becomes the starting
   balance (shown as a cash-flow on that row).

**Broker rules the engine uses** (verified against the operations CSV and
7 statements, 17-09 → 25-09-2026, exact to the cent):

| Item | Rule |
|---|---|
| Broker fee | qty × **0.05** USD, charged on **open** and again on **close** |
| Swap | qty × price × **rate** ÷ **360**, **×3 on Friday**; charged if the lot is open at **17:00**. Rate = \|Swap_rate_buy\| + base: **8.45145%** to 28-09-2026, **7.9%** (4.0 + base 3.9) from 29-09-2026 — see *Swap rate changes* below |
| Gross P/L | (close − open) × qty × side |
| Precision | broker keeps full precision; the statement **displays truncated 2dp** — the engine does the same |

The operations CSV shows each row truncated separately, so summing CSV
rows can be ±0.01 off a statement's "Today's realized P/L". The statement
(full precision, then truncated) is the source of truth; balance diff has
been £0.00 on every day.

### What each box shows

All figures USD, truncated to 2dp. Worked with the 28-09-2026 figures.

**Top KPI row**

| Box | Formula | Meaning | 28-09 |
|---|---|---|---|
| **Current Equity** | Current balance + Open P/L | What the account is worth marked-to-market right now (before the fee to close open lots). Sub-line = cash balance. | $1,350,560.70 |
| **Net P/L (incl. open)** | Current equity − Starting balance | **Everything** the strategy has made or lost since the start: realised trades, every fee paid, every swap paid, **plus** the unrealised P/L on open lots. The truest "how are we doing" number. % = on starting balance. Excludes only the not-yet-charged close fee on open lots. | +$6,633.21 (0.49%) |
| **Running Realised** | Current balance − Starting balance | Only what has **actually hit cash**: closed-trade profits minus **all** fees and swaps paid so far — including the open fees and swaps already charged on lots that are **still open**. Ignores open-lot price moves. | +$4,007.81 |
| **Open P/L** | Σ (current price − open price) × qty × side, open lots | Paper gain/loss on open lots at the statement's current price, **gross** — before their open fee, swaps and future close fee. | +$2,625.40 (3 lots) |

How they tie together (28-09):

```
Net P/L (incl. open)   6,633.21 = Running realised 4,007.81 + Open P/L 2,625.40
Running realised       4,007.81 = Closed gross 7,734.65 − all fees 3,250.00 − all swaps 476.84
Net P/L (incl. open)   6,633.21 = Net realised closed 6,749.73 + open lots so far (2,625.40 − 2,500.00 open fees − 241.92 swaps)
```

**Capital card** — Starting balance (sum of cash-flows, i.e. deposits; 17-09
= $1,343,927.49 = 114.99 + 672,443.50 + 671,369.00), Current balance (last
statement), Open P/L, Current equity.

**Trades card** — Total profit (winning closed trades, gross), Total loss
(losing closed trades, gross), Gross realised P/L, Win rate (closed trades),
Closed / open lot count.

**Costs card** — Broker fees (split: charged on open — every lot ever opened;
charged on close — closed lots only), Swaps (split: on closed lots / on
still-open lots), Total costs = fees + swaps.

**Result card**

| Line | Meaning |
|---|---|
| Gross P/L (realised + open) | Closed gross + Open P/L — before any costs |
| − Broker fees / − Swaps | All costs paid so far |
| **Net P/L** | = the Net P/L (incl. open) KPI |
| **Net realised (closed trades)** | Closed trades only, each fully costed: gross − its open fee − its close fee − its swaps. **This is the per-trade figure recorded in USD on the INVESTGTX Data Feed (§15.7)** |
| Running realised (in balance) | = the Running Realised KPI |

**Status chips** — *All N statements reconcile* (every day passes all
checks) · *Implied swap x% vs 8.45145%* (rate backed out of the statement;
wobbles 8.450–8.453% on small days because it's computed from the displayed
2dp figures — green while within 0.05%) · *Short swap rate unverified* (no
short trade seen yet; the short rate is assumed = long).

**Open Positions table** — per lot: qty, open date/price, current price,
Unrealised, Nights (swaps charged), Open fee, Swaps, Est. close fee (qty ×
0.05), **Net if closed** = Unrealised − open fee − est. close fee + swaps,
Margin (50% of notional). Tap a row → per-night swap ledger.

**Partial closes** (2026-10-06) — a lot can be closed in several trades
(e.g. QIAGEN 10,000 sold as 2 × 5,000 on 05-10-2026). For every lot the
engine works out how much was closed (its qty − its qty still in the Open
Position Report) and matches each closing trade to the lot of that
instrument whose open price reproduces the broker's own profit for the
trade. A partial close splits the lot: the closed piece becomes its own
closed row (tagged **PARTIAL**) with its pro-rata share of the open fee and
of the swaps paid so far, its own close fee (qty × $0.05) and gross; the
rest stays open at the reduced qty and only that qty is charged swaps from
then on. Also handled: a lot opened and partly closed the same day, and an
intraday round trip closed in pieces. Win rate counts each piece.

**Margin** (2026-10-06) — margin rates differ by instrument (50% Energy
Vault / QIAGEN, 30% Chipotle), so each position's rate is taken from the
statement's own Margin column; the ⚙ Margin % setting is only the fallback.

Regression: `python tests\test_gtx_partials.py` (real 05-10 statement in
`tests/fixtures/gtx_2026-10-05.txt` + 3 synthetic partial scenarios).

**Closed Positions table** — per trade: gross, open fee, close fee, swaps,
**Net P/L** (fully costed), nights held.

**Daily Reconciliation Log** — per statement: Balance · Δ (statement balance
with the engine − statement Δ beside it in small type, red on a mismatch;
hover for the engine figure), **Open P/L** (the statement's open gross P/L
that day — unrealised, before fees/swaps; engine figure beside it in red only
on a mismatch; added 2026-10-05), today realised, fees, swaps, closed gross, cash flow (deposits /
withdrawals detected as balance change not explained by realised P/L),
implied swap rate, ✓/✗ (balance, realised, open P/L, margin). **Raw** shows
the pasted text; 🗑 deletes a statement (the rest re-replay automatically).

### Files

| File | Role |
|---|---|
| `sql/2026-09-29_gtx_statements.sql` | `gtx_statements` + `gtx_settings` (additive, RLS on, service key bypasses) |
| `backend/src/services/gtx_parser.py` | Parses the pasted email (whole or 3 sections; tabs or 2+ spaces) |
| `backend/src/services/gtx_engine.py` | Stateless replay: lots, fees, swaps, closes, reconciliation, summary |
| `backend/src/services/gtx_service.py` | Supabase I/O, preview, save (409 on existing date unless overwrite), state |
| `backend/src/routers/gtx.py` | `GET /api/gtx/state`, `POST /api/gtx/preview`, `POST /api/gtx/statements`, `GET /api/gtx/statements/{id}/raw`, `DELETE /api/gtx/statements/{id}`, `GET`/`PATCH /api/gtx/settings` |
| `backend/tests/test_gtx_replay.py` | Regression: 17-09 → 25-09 must all reconcile (`python tests\test_gtx_replay.py`) |
| `src/components/GlobalGtxTab.jsx` | Tab UI (desktop + mobile) |
| `src/pages/Analysis.jsx` | Tab shell: AXIA Analyse (default) \| GlobalGTX |
| `src/services/api.js` | 7 GTX API functions |

**Settings (⚙)** — fee per unit, base long/short swap %, day count, swap
cutoff hour, Friday multiplier, margin %. These apply to **every** day on
replay, so changing one re-prices history — don't change them to "fix" a
single day.

**Swap rate changes (dated)** (2026-09-30) — ⚙ Settings → *+ Add rate
change* → date, long %, short %. Stored as `swap_rate_schedule` in
`gtx_settings.settings` (no SQL); `gtx_engine.swap_rates(s, d)` returns the
rate in force on each day, so earlier days keep their old rate. The
*Implied swap vs x%* chip compares against the rate in force on the last
statement (`summary.current_swap_rate_long_pct`); each log row also carries
`model_rate_pct`.

**Automatic rate detection — no CSV needed** (2026-09-30). The rate in force
is carried forward day to day. If a statement doesn't reconcile at the
carried rate, the engine infers the new rate from that statement alone:
fees (qty × 0.05) and closed gross are exact, so the swap is the only
unknown. It estimates the rate from the balance move, then picks the
**simplest rate (fewest decimals) that reconciles both today's realised
P/L and the balance to the cent**, and carries it forward. The preview
shows *⚠ Swap rate changed x% → y% — inferred from this statement* and
*Swap rate used (new — inferred)*; the log row shows *↻ rate y%*; the header
shows a *Swap rate → y% from DD-MM-YYYY (inferred)* chip.

Inference only runs when the swap is the sole unknown — it is skipped
(the day shows ✗ instead) when: it's the first statement; a weekday
statement is missing before it; a trade couldn't be matched; a swap price
had to be estimated; long and short lots are charged together; or the
implied rate is implausible (≤ 0 or > 3× the old rate, e.g. a deposit —
that is reported as a cash flow instead). A deposit on the *same* day as a
rate change can't be separated — add a dated entry for that day.

Precision: a statement pins the rate to about ±0.002% (one cent on the
day's swap), so e.g. a true 8.45145% may be inferred as 8.45 — every figure
still reconciles to the cent; if it drifts a cent later the engine simply
re-infers. When the broker confirms an exact rate (email/CSV), add it as a
**dated entry** — dated entries always take precedence on their date.
Current dated schedule: `[{"from": "2026-09-29", "long_pct": 7.9,
"short_pct": 7.9}]` (confirmed by CSV; the engine infers the same 7.9%
without it). Current schedule: `[{"from": "2026-09-29", "long_pct":
7.9, "short_pct": 7.9}]` (Ben, GTX, 30-09-2026: "swap = 4 above base; base
currently 3.9" — the CSV shows Swap_rate_sell −4.0 as a display rounding of
3.9; `Swap_size` confirms exactly 7.9%).

### Data log

| Date | Note |
|---|---|
| 17-09 → 25-09-2026 | Pasted from the broker emails — all reconcile |
| 28-09-2026 | Broker's email was an unchanged copy of 25-09 (their price feed error). **Reconstructed** from the operations CSV (3 swaps only, −51.64775, price 4.40, no trades) — file `GTX_2026-09-28_reconstructed.txt`. Reconciles; the **Raw** view shows the reconstruction, not the broken email. |
| 29-09-2026 | Broker cut the swap rate to **7.9%** (operations CSV 30-09: 3 swaps at price 3.97 = −43.55972). Added the dated rate schedule + float fixes below; the real email then reconciles. |

---

## 18. Roadmap — Outstanding

- [ ] **Pod/portfolio-level watermark adjustment for non-AXIA strategies**
  See [§19](#19-known-limitations) — documented gap, not yet requested.
- [x] **GlobalGTX 29-09-2026 statement / swap rate change** — resolved
  2026-09-30 with the dated `swap_rate_schedule` (§17.6).
- [x] **GlobalGTX engine float-edge fixes** (2026-09-30): `trunc2()` now
  rounds to 6dp before truncating (−18,874.5999… → −18,874.60, not .59), and
  a ±0.01 gap from two displayed-2dp figures is treated as rounding, not a
  cash flow (`|cash| <= tolerance`).
- [ ] **GlobalGTX missing-weekday swaps** — the engine charges swaps only on
  days that have a statement. A skipped weekday's swap would show up as a
  "cash flow" on the next statement. Avoid by reconstructing the missing
  day (as done for 28-09).
- [ ] **12-FLAGS month-end FX — possible one-day-late candle** (§19). Check a
  saved statement against OANDA before changing anything.

---

## 19. Known Limitations

**Watermark not applied at pod/portfolio level for non-AXIA strategies.**
`get_pods_with_kpis_fast` / `get_portfolio_kpis_fast` aggregate Darwinex/
manual strategy equity via `_build_pod_pfees_map`'s `pod_agg`, which sums
raw `pfees` rows directly — before per-strategy watermark resolution. This
path isn't additively isolable per-strategy the way the AXIA path is, so
watermark adjustment currently only reaches the strategy level for
non-AXIA strategies. Flagged to Nish; not yet requested to be fixed.

**12-FLAGS monthly FX may use the next day's close.** `get_monthly_close`
(§11.4) requests daily candles with `to = period_end + 1 day 00:00 UTC`. An
OANDA daily candle for day D opens ~21:00/22:00 UTC on D−1, so that request
returns the candle that closes on **D+1**. Found 2026-09-30 while building
§15.7 (which uses the corrected `get_daily_close`). Not changed — it would
move already-saved 12-FLAGS GBP figures; verify first.

**GlobalGTX short swap rate unverified.** No short trade seen yet; the engine
assumes short rate = long rate (8.45145%). Confirm on the first short.

**12-FLAGS capital transfer (Wallet → Strategy) not yet logged.** The
£485,981.31 Ebury deposit (29-05-2026, Ref 1087948) is recorded in the
external ledger (`capital_events`) but not yet as an internal
`capital_transfers` Wallet→Strategy leg — Capital Invested for 12-FLAGS
currently comes from the strategy's manual `initial_investment` field
(`_strategy_capital_invested`'s fallback chain, §11.5), not the ledger.
Functionally correct today; log the internal transfer when convenient so
12-FLAGS moves onto the same ledger-sourced baseline as every other
strategy.

---

## 20. Dev Workflow

```bash
# Frontend
cd chase-dashboard-web
npm install
npm run dev          # http://localhost:5173

# Backend
cd chase-dashboard-web/backend
pip install -r requirements.txt
uvicorn src.main:app --reload   # http://localhost:8000
```

**Rules in force for all changes to this project:**
- Never run SQL directly — always hand over exact SQL + verification steps.
- Schema changes are additive-only (no drops/renames on live tables).
- Never push to git/Netlify unless explicitly asked.
- Two git remotes exist: `origin` (chase1977/chase-dashboard-web) and `fork` (nish-vek10/chase-dashboard-web) -- **Netlify watches `fork`, not `origin`**. Every git push command given must push both remotes (`git push origin main && git push fork main`), confirmed with Nish 2026-09-23 after a deploy silently didn't update because only `origin` was pushed.
- No scope creep — confirm before adding anything not explicitly requested.
- All UI must work on desktop and mobile.
