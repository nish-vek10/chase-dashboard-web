// src/components/GlobalGtxTab.jsx
/**
 * GlobalGTX tab (Analysis page) — Global Trading X daily statement tracker.
 *
 * Flow: paste daily statement email → preview (parse + dry-run recon) → save.
 * Backend replays every saved statement in date order (gtx_engine.py), so
 * everything shown here — lots, fees, swaps, closes, recon — is derived.
 *
 * Money shown truncated to 2dp (broker display convention), USD.
 */

import { Fragment, useState, useEffect, useCallback } from 'react'
import useIsMobile from '../hooks/useIsMobile.js'
import {
  fetchGtxState, previewGtxStatement, saveGtxStatement,
  fetchGtxStatementRaw, deleteGtxStatement, updateGtxSettings,
} from '../services/api.js'

const C = {
  bg: '#0D1B2E', card: '#0F2236', surface: '#132030', border: '#1E3A5F',
  accent: '#38BDF8', text: '#F1F5F9', dim: '#94A3B8', muted: '#64748B',
  pos: '#22C55E', neg: '#EF4444', warn: '#F59E0B',
}

// ─── Formatters ────────────────────────────────────────────────────────────────
const trunc2 = x => (x < 0 ? -Math.floor(-x * 100 + 1e-7) : Math.floor(x * 100 + 1e-7)) / 100
const fmtUsd = (x, { sign = false } = {}) => {
  if (x === null || x === undefined || Number.isNaN(x)) return '—'
  const v = trunc2(x)
  const s = Math.abs(v).toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  const pre = v < 0 ? '−' : sign && v > 0 ? '+' : ''
  return `${pre}$${s}`
}
const fmtNum = (x, dp = 0) => x === null || x === undefined ? '—'
  : Number(x).toLocaleString('en-GB', { minimumFractionDigits: dp, maximumFractionDigits: dp })
const fmtPx = x => x === null || x === undefined ? '—' : String(x)
const fmtDate = iso => iso ? iso.slice(0, 10).split('-').reverse().join('-') : '—'
const fmtDt = iso => iso ? `${fmtDate(iso)} ${iso.slice(11, 16)}` : '—'
const tone = x => (x > 0 ? C.pos : x < 0 ? C.neg : C.text)
const shortInst = s => (s || '').replace(/\s*\(CFD\)\s*$/i, '')

// Default statement date: today if weekday, else previous Friday
// Default = previous weekday: statements arrive ~23:00 and are entered the
// next day (Tue -> Mon, Mon/Sat/Sun -> Fri). Local time, no UTC shift.
const defaultStmtDate = () => {
  const d = new Date()
  d.setDate(d.getDate() - 1)
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() - 1)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

const errMsg = e => {
  const m = String(e?.message || e)
  try { return JSON.parse(m).detail || m } catch { return m }
}

// ─── Shared UI ─────────────────────────────────────────────────────────────────
const btn = (primary = false) => ({
  padding: '9px 16px', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: 'pointer',
  border: `1px solid ${primary ? C.accent : C.border}`,
  background: primary ? C.accent : 'transparent',
  color: primary ? '#04111F' : C.dim, whiteSpace: 'nowrap',
})

const inputStyle = {
  width: '100%', padding: '10px 12px', boxSizing: 'border-box',
  background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8,
  color: C.text, fontSize: 13, outline: 'none',
}

function Card({ title, right, children, style }) {
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 12, padding: 'clamp(14px, 3vw, 20px)', minWidth: 0, ...style }}>
      {title && (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginBottom: 14, paddingBottom: 8, borderBottom: `1px solid ${C.border}` }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.muted, letterSpacing: '1.2px', textTransform: 'uppercase' }}>{title}</div>
          {right}
        </div>
      )}
      {children}
    </div>
  )
}

function Chip({ ok, children }) {
  const col = ok === null ? C.dim : ok ? C.pos : C.neg
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '4px 10px', borderRadius: 20, fontSize: 11, fontWeight: 600, color: col, background: `${col}14`, border: `1px solid ${col}40`, whiteSpace: 'nowrap' }}>
      {children}
    </span>
  )
}

function Modal({ title, onClose, children, maxWidth = 760 }) {
  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(2,8,18,0.72)', zIndex: 1000, display: 'flex', alignItems: 'flex-start', justifyContent: 'center', padding: 'clamp(10px, 4vw, 40px)', overflowY: 'auto' }}>
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 16, padding: 'clamp(16px, 4vw, 28px)', maxWidth, width: '100%', boxSizing: 'border-box' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 18, gap: 12 }}>
          <div style={{ fontSize: 17, fontWeight: 700, color: C.text }}>{title}</div>
          <button onClick={onClose} style={{ ...btn(), padding: '4px 10px' }}>✕</button>
        </div>
        {children}
      </div>
    </div>
  )
}

// ─── KPI blocks ────────────────────────────────────────────────────────────────
function HeroKpi({ label, value, sub, color }) {
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 12, padding: '16px 18px', minWidth: 0 }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: C.muted, letterSpacing: '1px', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontSize: 'clamp(18px, 2.4vw, 24px)', fontWeight: 700, color: color || C.text, marginTop: 6, fontVariantNumeric: 'tabular-nums', overflowWrap: 'anywhere' }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: C.dim, marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

function Row({ label, value, color, bold, indent, border }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, padding: '6px 0', borderTop: border ? `1px solid ${C.border}` : 'none', marginTop: border ? 4 : 0 }}>
      <span style={{ fontSize: 12, color: indent ? C.muted : C.dim, paddingLeft: indent ? 12 : 0, minWidth: 0 }}>{label}</span>
      <span style={{ fontSize: 13, fontWeight: bold ? 700 : 500, color: color || C.text, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>{value}</span>
    </div>
  )
}

function Summary({ s }) {
  const grossPl = s.gross_realised + s.open_pl
  return (
    <>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 12 }}>
        <HeroKpi label="Current Equity" value={fmtUsd(s.current_equity)} sub={`Balance ${fmtUsd(s.current_balance)}`} />
        <HeroKpi label="Net P/L (incl. open)" value={fmtUsd(s.net_pl_total, { sign: true })} color={tone(s.net_pl_total)}
                 sub={s.starting_balance ? `${fmtNum(s.net_pl_total / s.starting_balance * 100, 2)}% on starting` : null} />
        <HeroKpi label="Running Realised" value={fmtUsd(s.running_realised, { sign: true })} color={tone(s.running_realised)} sub="Balance − starting" />
        <HeroKpi label="Open P/L" value={fmtUsd(s.open_pl, { sign: true })} color={tone(s.open_pl)} sub={`${s.n_open} open lot${s.n_open === 1 ? '' : 's'}`} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12, marginTop: 12 }}>
        <Card title="Capital">
          <Row label="Starting balance" value={fmtUsd(s.starting_balance)} />
          <Row label="Current balance" value={fmtUsd(s.current_balance)} />
          <Row label="Open P/L" value={fmtUsd(s.open_pl, { sign: true })} color={tone(s.open_pl)} />
          <Row label="Current equity" value={fmtUsd(s.current_equity)} bold border />
        </Card>

        <Card title="Trades">
          <Row label="Total profit (winners)" value={fmtUsd(s.total_profit, { sign: true })} color={C.pos} />
          <Row label="Total loss (losers)" value={fmtUsd(s.total_loss)} color={s.total_loss < 0 ? C.neg : C.text} />
          <Row label="Gross realised P/L" value={fmtUsd(s.gross_realised, { sign: true })} color={tone(s.gross_realised)} bold border />
          <Row label="Win rate" value={s.win_rate === null ? '—' : `${fmtNum(s.win_rate, 1)}%`} />
          <Row label="Closed / open lots" value={`${s.n_closed} / ${s.n_open}`} />
        </Card>

        <Card title="Costs">
          <Row label="Broker fees" value={fmtUsd(-s.fees_total)} color={C.neg} bold />
          <Row label="charged on open" value={fmtUsd(-s.fees_open)} indent />
          <Row label="charged on close" value={fmtUsd(-s.fees_close)} indent />
          <Row label="Swaps" value={fmtUsd(-s.swaps_total)} color={C.neg} bold />
          <Row label="on closed lots" value={fmtUsd(-s.swaps_closed)} indent />
          <Row label="on open lots" value={fmtUsd(-s.swaps_open)} indent />
          <Row label="Total costs" value={fmtUsd(-s.total_costs)} color={C.neg} bold border />
        </Card>

        <Card title="Result">
          <Row label="Gross P/L (realised + open)" value={fmtUsd(grossPl, { sign: true })} color={tone(grossPl)} />
          <Row label="− Broker fees" value={fmtUsd(-s.fees_total)} color={C.neg} />
          <Row label="− Swaps" value={fmtUsd(-s.swaps_total)} color={C.neg} />
          <Row label="Net P/L" value={fmtUsd(s.net_pl_total, { sign: true })} color={tone(s.net_pl_total)} bold border />
          <Row label="Net realised (closed trades)" value={fmtUsd(s.net_realised_closed, { sign: true })} color={tone(s.net_realised_closed)} />
          <Row label="Running realised (in balance)" value={fmtUsd(s.running_realised, { sign: true })} color={tone(s.running_realised)} />
        </Card>
      </div>
    </>
  )
}

// ─── Tables ────────────────────────────────────────────────────────────────────
const th = { padding: '9px 10px', fontSize: 10, fontWeight: 700, color: C.muted, letterSpacing: '0.6px', textTransform: 'uppercase', textAlign: 'right', borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap', background: C.surface }
const thL = { ...th, textAlign: 'left' }
const td = { padding: '9px 10px', fontSize: 12, color: C.text, textAlign: 'right', borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }
const tdL = { ...td, textAlign: 'left' }

function TableWrap({ minWidth, children }) {
  return (
    <div style={{ overflowX: 'auto', WebkitOverflowScrolling: 'touch', border: `1px solid ${C.border}`, borderRadius: 10 }}>
      <table style={{ width: '100%', minWidth, borderCollapse: 'collapse' }}>{children}</table>
    </div>
  )
}

function SideTag({ side }) {
  const buy = side === 1
  return <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 4, color: buy ? C.pos : C.neg, background: buy ? `${C.pos}18` : `${C.neg}18` }}>{buy ? 'BUY' : 'SELL'}</span>
}

function SwapLedger({ ledger, colSpan }) {
  return (
    <tr>
      <td colSpan={colSpan} style={{ padding: '10px 14px 14px', background: C.bg, borderBottom: `1px solid ${C.border}` }}>
        <div style={{ fontSize: 10, fontWeight: 700, color: C.muted, letterSpacing: '1px', textTransform: 'uppercase', marginBottom: 8 }}>Swap ledger — per night</div>
        {ledger.length === 0 ? <div style={{ fontSize: 12, color: C.dim }}>No swaps charged (closed before 17:00 cutoff).</div> : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(210px, 1fr))', gap: 6 }}>
            {ledger.map(l => (
              <div key={l.date} style={{ display: 'flex', justifyContent: 'space-between', gap: 8, fontSize: 12, padding: '6px 10px', background: C.card, border: `1px solid ${C.border}`, borderRadius: 6 }}>
                <span style={{ color: C.dim }}>{fmtDate(l.date)} @ {fmtPx(l.price)}{l.mult > 1 ? ` ×${l.mult}` : ''}{l.estimated ? ' *' : ''}</span>
                <span style={{ color: C.neg, fontVariantNumeric: 'tabular-nums' }}>{fmtUsd(l.amount)}</span>
              </div>
            ))}
          </div>
        )}
      </td>
    </tr>
  )
}

function OpenTable({ rows }) {
  const [open, setOpen] = useState(null)
  if (!rows.length) return <div style={{ fontSize: 13, color: C.dim, padding: '8px 0' }}>No open positions.</div>
  const tot = k => rows.reduce((a, r) => a + (r[k] || 0), 0)
  return (
    <TableWrap minWidth={1180}>
      <thead><tr>
        <th style={thL}>Instrument</th><th style={thL}>Side</th><th style={th}>Qty</th><th style={thL}>Opened</th>
        <th style={th}>Open px</th><th style={th}>Current px</th><th style={th}>Unrealised</th><th style={th}>Nights</th>
        <th style={th}>Open fee</th><th style={th}>Swaps</th><th style={th}>Est. close fee</th><th style={th}>Net if closed</th><th style={th}>Margin</th>
      </tr></thead>
      <tbody>
        {rows.map(r => (
          <Fragment key={r.lot_id}>
            <tr onClick={() => setOpen(open === r.lot_id ? null : r.lot_id)} style={{ cursor: 'pointer' }}>
              <td style={{ ...tdL, fontWeight: 600 }}>{open === r.lot_id ? '▾' : '▸'} {shortInst(r.instrument)}</td>
              <td style={tdL}><SideTag side={r.side} /></td>
              <td style={td}>{fmtNum(r.qty)}</td>
              <td style={tdL}>{fmtDt(r.open_dt)}</td>
              <td style={td}>{fmtPx(r.open_px)}</td>
              <td style={td}>{fmtPx(r.current_px)}</td>
              <td style={{ ...td, color: tone(r.unrealised) }}>{fmtUsd(r.unrealised, { sign: true })}</td>
              <td style={td}>{r.days_held}</td>
              <td style={{ ...td, color: C.neg }}>{fmtUsd(-r.open_fee)}</td>
              <td style={{ ...td, color: C.neg }}>{fmtUsd(r.swaps)}</td>
              <td style={{ ...td, color: C.dim }}>{fmtUsd(-r.est_close_fee)}</td>
              <td style={{ ...td, fontWeight: 700, color: tone(r.net_if_closed) }}>{fmtUsd(r.net_if_closed, { sign: true })}</td>
              <td style={{ ...td, color: C.dim }}>{fmtUsd(r.margin)}</td>
            </tr>
            {open === r.lot_id && <SwapLedger ledger={r.swap_ledger} colSpan={13} />}
          </Fragment>
        ))}
        <tr style={{ background: C.surface }}>
          <td style={{ ...tdL, fontWeight: 700 }} colSpan={2}>Total</td>
          <td style={{ ...td, fontWeight: 700 }}>{fmtNum(tot('qty'))}</td>
          <td style={td} colSpan={3} />
          <td style={{ ...td, fontWeight: 700, color: tone(tot('unrealised')) }}>{fmtUsd(tot('unrealised'), { sign: true })}</td>
          <td style={td} />
          <td style={{ ...td, fontWeight: 700, color: C.neg }}>{fmtUsd(-tot('open_fee'))}</td>
          <td style={{ ...td, fontWeight: 700, color: C.neg }}>{fmtUsd(tot('swaps'))}</td>
          <td style={{ ...td, fontWeight: 700, color: C.dim }}>{fmtUsd(-tot('est_close_fee'))}</td>
          <td style={{ ...td, fontWeight: 700, color: tone(tot('net_if_closed')) }}>{fmtUsd(tot('net_if_closed'), { sign: true })}</td>
          <td style={{ ...td, fontWeight: 700, color: C.dim }}>{fmtUsd(tot('margin'))}</td>
        </tr>
      </tbody>
    </TableWrap>
  )
}

function ClosedTable({ rows }) {
  const [open, setOpen] = useState(null)
  if (!rows.length) return <div style={{ fontSize: 13, color: C.dim, padding: '8px 0' }}>No closed positions yet.</div>
  const tot = k => rows.reduce((a, r) => a + (r[k] || 0), 0)
  // a lot closed in pieces shows one row per piece — same instrument + open time
  const pieces = rows.reduce((m, r) => { const k = r.instrument + r.open_dt; m[k] = (m[k] || 0) + 1; return m }, {})
  return (
    <TableWrap minWidth={1200}>
      <thead><tr>
        <th style={thL}>Instrument</th><th style={thL}>Side</th><th style={th}>Qty</th><th style={thL}>Opened</th><th style={th}>Open px</th>
        <th style={thL}>Closed</th><th style={th}>Close px</th><th style={th}>Gross P/L</th><th style={th}>Open fee</th>
        <th style={th}>Close fee</th><th style={th}>Swaps</th><th style={th}>Net P/L</th><th style={th}>Nights</th>
      </tr></thead>
      <tbody>
        {rows.map(r => (
          <Fragment key={r.lot_id}>
            <tr onClick={() => setOpen(open === r.lot_id ? null : r.lot_id)} style={{ cursor: 'pointer' }}>
              <td style={{ ...tdL, fontWeight: 600 }}>{open === r.lot_id ? '▾' : '▸'} {shortInst(r.instrument)}</td>
              <td style={tdL}><SideTag side={r.side} /></td>
              <td style={td}>
                {pieces[r.instrument + r.open_dt] > 1 && (
                  <span title="Part of a lot closed in pieces" style={{ marginRight: 6, padding: '1px 5px', borderRadius: 4, fontSize: 9, fontWeight: 700, letterSpacing: '0.4px', color: C.warn, border: `1px solid ${C.warn}55` }}>PARTIAL</span>
                )}
                {fmtNum(r.qty)}
              </td>
              <td style={tdL}>{fmtDt(r.open_dt)}</td>
              <td style={td}>{fmtPx(r.open_px)}</td>
              <td style={tdL}>{fmtDt(r.close_dt)}</td>
              <td style={td}>{fmtPx(r.close_px)}</td>
              <td style={{ ...td, color: tone(r.gross) }}>{fmtUsd(r.gross, { sign: true })}</td>
              <td style={{ ...td, color: C.neg }}>{fmtUsd(-r.open_fee)}</td>
              <td style={{ ...td, color: C.neg }}>{fmtUsd(-r.close_fee)}</td>
              <td style={{ ...td, color: C.neg }}>{fmtUsd(r.swaps)}</td>
              <td style={{ ...td, fontWeight: 700, color: tone(r.net) }}>{fmtUsd(r.net, { sign: true })}</td>
              <td style={td}>{r.days_held}</td>
            </tr>
            {open === r.lot_id && <SwapLedger ledger={r.swap_ledger} colSpan={13} />}
          </Fragment>
        ))}
        <tr style={{ background: C.surface }}>
          <td style={{ ...tdL, fontWeight: 700 }} colSpan={7}>Total ({rows.length})</td>
          <td style={{ ...td, fontWeight: 700, color: tone(tot('gross')) }}>{fmtUsd(tot('gross'), { sign: true })}</td>
          <td style={{ ...td, fontWeight: 700, color: C.neg }}>{fmtUsd(-tot('open_fee'))}</td>
          <td style={{ ...td, fontWeight: 700, color: C.neg }}>{fmtUsd(-tot('close_fee'))}</td>
          <td style={{ ...td, fontWeight: 700, color: C.neg }}>{fmtUsd(tot('swaps'))}</td>
          <td style={{ ...td, fontWeight: 700, color: tone(tot('net')) }}>{fmtUsd(tot('net'), { sign: true })}</td>
          <td style={td} />
        </tr>
      </tbody>
    </TableWrap>
  )
}

function ReconLog({ log, statements, onDelete, onViewRaw }) {
  const idByDate = Object.fromEntries(statements.map(s => [s.date, s.id]))
  const rows = [...log].reverse()
  return (
    <TableWrap minWidth={1020}>
      <thead><tr>
        {/* Balance = statement balance, with engine diff underneath (was 3 columns: stmt / engine / diff) */}
        <th style={thL}>Date</th><th style={th} title="Statement balance · Δ = engine − statement (hover a row for the engine figure)">Balance · Δ</th>
        <th style={th} title="Open gross P/L on the statement date (unrealised, before fees/swaps)">Open P/L</th>
        <th style={th}>Today realised</th><th style={th}>Fees</th><th style={th}>Swaps</th><th style={th}>Closed gross</th>
        <th style={th}>Cash flow</th><th style={th}>Implied rate</th><th style={{ ...th, textAlign: 'center' }}>Check</th><th style={th} />
      </tr></thead>
      <tbody>
        {rows.map(r => (
          <Fragment key={r.date}>
            <tr>
              <td style={{ ...tdL, fontWeight: 600 }}>{fmtDate(r.date)}</td>
              <td style={td} title={`Engine balance ${fmtUsd(r.engine_balance)}`}>
                {fmtUsd(r.stmt_balance)}
                <span style={{ marginLeft: 6, fontSize: 10, color: r.checks.balance ? C.muted : C.neg, fontWeight: r.checks.balance ? 400 : 700 }}>
                  Δ{fmtUsd(r.balance_diff)}
                </span>
              </td>
              <td style={{ ...td, color: tone(r.stmt_open_pl) }}>
                {r.stmt_open_pl ? fmtUsd(r.stmt_open_pl, { sign: true }) : <span style={{ color: C.dim }}>{fmtUsd(0)}</span>}
                {!r.checks.open_pl && (
                  <span style={{ marginLeft: 6, fontSize: 10, color: C.neg, fontWeight: 700 }}>eng {fmtUsd(r.engine_open_pl, { sign: true })}</span>
                )}
              </td>
              <td style={{ ...td, color: tone(r.stmt_realised) }}>{fmtUsd(r.stmt_realised, { sign: true })}</td>
              <td style={{ ...td, color: r.fees ? C.neg : C.dim }}>{fmtUsd(r.fees)}</td>
              <td style={{ ...td, color: r.swaps ? C.neg : C.dim }}>{fmtUsd(r.swaps)}</td>
              <td style={{ ...td, color: tone(r.gross_closed) }}>{fmtUsd(r.gross_closed, { sign: true })}</td>
              <td style={{ ...td, color: r.cash_flow ? C.accent : C.dim }}>{r.cash_flow ? fmtUsd(r.cash_flow, { sign: true }) : '—'}</td>
              <td style={td}>
                {r.implied_rate_pct ? `${fmtNum(r.implied_rate_pct, 4)}%` : '—'}
                {r.rate_inferred && (
                  <div style={{ fontSize: 10, color: C.warn, whiteSpace: 'nowrap' }} title="Swap rate change inferred from this statement">
                    ↻ rate {fmtNum(r.model_rate_pct, 5)}%
                  </div>
                )}
              </td>
              <td style={{ ...td, textAlign: 'center' }} title={Object.entries(r.checks).map(([k, v]) => `${k}: ${v ? '✓' : '✗'}`).join('  ')}>
                <span style={{ color: r.ok ? C.pos : C.neg, fontWeight: 700 }}>{r.ok ? '✓' : '✗'}</span>
              </td>
              {/* flex on an inner div, not the <td> — a flex <td> stops being a table cell and its row border drifts */}
              <td style={td}>
                <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                  <button onClick={() => onViewRaw(idByDate[r.date])} style={{ ...btn(), padding: '3px 8px', fontSize: 11 }}>Raw</button>
                  <button onClick={() => onDelete(idByDate[r.date], r.date)} style={{ ...btn(), padding: '3px 8px', fontSize: 11, color: C.neg }}>🗑</button>
                </div>
              </td>
            </tr>
            {(!r.ok || r.warnings.length > 0) && (
              <tr><td colSpan={11} style={{ padding: '6px 12px', fontSize: 11, color: r.ok ? C.warn : C.neg, background: C.bg, borderBottom: `1px solid ${C.border}`, whiteSpace: 'normal' }}>
                {!r.ok && <div>Failed: {Object.entries(r.checks).filter(([, v]) => !v).map(([k]) => k).join(', ')} — stmt open P/L {fmtUsd(r.stmt_open_pl)} vs engine {fmtUsd(r.engine_open_pl)}; stmt realised {fmtUsd(r.stmt_realised)} vs engine {fmtUsd(r.engine_realised)}</div>}
                {r.warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}
              </td></tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </TableWrap>
  )
}

// ─── Add Statement modal ───────────────────────────────────────────────────────
function AddStatementModal({ onClose, onSaved }) {
  const isMobile = useIsMobile()
  const [date, setDate] = useState(defaultStmtDate())
  const [mode, setMode] = useState('whole')            // whole | sections
  const [whole, setWhole] = useState('')
  const [sec, setSec] = useState({ account: '', open: '', trades: '' })
  const [preview, setPreview] = useState(null)
  const [overwrite, setOverwrite] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const text = mode === 'whole' ? whole : [sec.account, sec.open, sec.trades].join('\n\n\n')
  const dirty = () => { setPreview(null); setErr(null) }

  const runPreview = async () => {
    setBusy(true); setErr(null)
    try { setPreview(await previewGtxStatement(date, text)) }
    catch (e) { setErr(errMsg(e)); setPreview(null) }
    finally { setBusy(false) }
  }

  const save = async () => {
    setBusy(true); setErr(null)
    try { await saveGtxStatement(date, text, { overwrite }); onSaved() }
    catch (e) { setErr(errMsg(e)) }
    finally { setBusy(false) }
  }

  const secBox = (key, label, hint) => (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: C.dim, marginBottom: 6 }}>{label}</div>
      <textarea value={sec[key]} onChange={e => { setSec({ ...sec, [key]: e.target.value }); dirty() }}
        placeholder={hint} rows={5} style={{ ...inputStyle, fontFamily: 'ui-monospace, monospace', fontSize: 11, resize: 'vertical' }} />
    </div>
  )

  const r = preview?.recon
  const p = preview?.parsed
  return (
    <Modal title="Add Daily Statement" onClose={onClose}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'flex-end', marginBottom: 14 }}>
        <div style={{ flex: '1 1 180px', minWidth: 0 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: C.dim, marginBottom: 6 }}>Statement date (email received 23:00)</div>
          <input type="date" value={date} onChange={e => { setDate(e.target.value); dirty() }} style={{ ...inputStyle, colorScheme: 'dark' }} />
        </div>
        <div style={{ display: 'flex', border: `1px solid ${C.border}`, borderRadius: 8, overflow: 'hidden', flexShrink: 0 }}>
          {[['whole', 'Whole email'], ['sections', '3 sections']].map(([k, l]) => (
            <button key={k} onClick={() => { setMode(k); dirty() }} style={{ padding: '9px 14px', fontSize: 12, fontWeight: 600, border: 'none', cursor: 'pointer', background: mode === k ? C.accent : 'transparent', color: mode === k ? '#04111F' : C.dim }}>{l}</button>
          ))}
        </div>
      </div>

      {mode === 'whole' ? (
        <textarea value={whole} onChange={e => { setWhole(e.target.value); dirty() }} rows={isMobile ? 8 : 12}
          placeholder="Open the statement email → Ctrl+A → Ctrl+C → paste here"
          style={{ ...inputStyle, fontFamily: 'ui-monospace, monospace', fontSize: 11, resize: 'vertical' }} />
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 10 }}>
          {secBox('account', 'Account Statement Report', 'Header row + GTX0011-USD row')}
          {secBox('open', 'Open Position Report', 'Header row + position rows')}
          {secBox('trades', 'Trades Report', 'Header row + trade rows')}
        </div>
      )}

      {err && <div style={{ marginTop: 12, padding: '10px 12px', borderRadius: 8, background: `${C.neg}14`, border: `1px solid ${C.neg}40`, color: C.neg, fontSize: 12 }}>{err}</div>}

      {preview && (
        <div style={{ marginTop: 14, padding: 14, borderRadius: 10, background: C.surface, border: `1px solid ${r.ok ? C.pos : C.neg}55` }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 10 }}>
            <Chip ok={r.ok}>{r.ok ? '✓ Reconciles' : '✗ Mismatch'}</Chip>
            {Object.entries(r.checks).map(([k, v]) => <Chip key={k} ok={v}>{v ? '✓' : '✗'} {k.replace('_', ' ')}</Chip>)}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', columnGap: 20 }}>
            <Row label="Account" value={preview.account} />
            <Row label="Open lots / trades parsed" value={`${p.open_positions.length} / ${p.trades.length}`} />
            <Row label="Statement balance" value={fmtUsd(r.stmt_balance)} />
            <Row label="Engine balance" value={fmtUsd(r.engine_balance)} color={r.checks.balance ? C.text : C.neg} />
            <Row label="Today realised" value={fmtUsd(r.stmt_realised, { sign: true })} color={tone(r.stmt_realised)} />
            <Row label="Fees today" value={fmtUsd(r.fees)} color={r.fees ? C.neg : C.text} />
            <Row label="Swaps today" value={fmtUsd(r.swaps)} color={r.swaps ? C.neg : C.text} />
            <Row label="Closed gross today" value={fmtUsd(r.gross_closed, { sign: true })} color={tone(r.gross_closed)} />
            {r.cash_flow !== 0 && <Row label="Cash flow detected" value={fmtUsd(r.cash_flow, { sign: true })} color={C.accent} />}
            {r.implied_rate_pct && <Row label="Implied swap rate" value={`${fmtNum(r.implied_rate_pct, 4)}%`} />}
            {r.model_rate_pct != null && r.swaps !== 0 && (
              <Row label={r.rate_inferred ? 'Swap rate used (new — inferred)' : 'Swap rate used'}
                value={`${fmtNum(r.model_rate_pct, 5)}%`} color={r.rate_inferred ? C.warn : C.text} />
            )}
          </div>
          {r.warnings.map((w, i) => <div key={i} style={{ fontSize: 11, color: C.warn, marginTop: 6 }}>⚠ {w}</div>)}
          {preview.later_statements.length > 0 && <div style={{ fontSize: 11, color: C.dim, marginTop: 6 }}>Back-fill: {preview.later_statements.length} later statement(s) will be re-reconciled automatically.</div>}
          {preview.exists && (
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10, fontSize: 12, color: C.warn, cursor: 'pointer' }}>
              <input type="checkbox" checked={overwrite} onChange={e => setOverwrite(e.target.checked)} />
              Statement for {fmtDate(date)} already saved — overwrite
            </label>
          )}
        </div>
      )}

      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 16, flexWrap: 'wrap' }}>
        <button onClick={onClose} style={btn()}>Cancel</button>
        <button onClick={runPreview} disabled={busy || !text.trim() || !date} style={{ ...btn(!preview), opacity: busy || !text.trim() ? 0.5 : 1 }}>{busy && !preview ? 'Parsing…' : 'Preview'}</button>
        {preview && <button onClick={save} disabled={busy || (preview.exists && !overwrite)} style={{ ...btn(true), opacity: busy || (preview.exists && !overwrite) ? 0.5 : 1 }}>{busy ? 'Saving…' : 'Save statement'}</button>}
      </div>
    </Modal>
  )
}

// ─── Settings modal ────────────────────────────────────────────────────────────
const SETTING_FIELDS = [
  ['fee_per_unit', 'Broker fee per unit (USD)', 'Charged on open and on close'],
  ['swap_rate_long_pct', 'Base swap rate — long (% p.a.)', 'Before the first dated change below'],
  ['swap_rate_short_pct', 'Base swap rate — short (% p.a.)', 'Unverified until first short swap'],
  ['day_count', 'Day count', 'Swap denominator'],
  ['swap_cutoff_hour', 'Swap cutoff hour', 'Lot open at this hour → charged'],
  ['friday_multiplier', 'Friday multiplier', 'Weekend swap roll'],
  ['margin_pct', 'Margin (%)', 'Of current notional'],
]

function SettingsModal({ settings, onClose, onSaved }) {
  const [v, setV] = useState(() => Object.fromEntries(SETTING_FIELDS.map(([k]) => [k, settings[k]])))
  const [verified, setVerified] = useState(!!settings.short_rate_verified)
  // Dated swap-rate changes: [{ from: 'YYYY-MM-DD', long_pct, short_pct }]
  const [sched, setSched] = useState(() => (settings.swap_rate_schedule || []).map(e => ({ ...e })))
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const setRow = (i, k, x) => setSched(sched.map((e, j) => (j === i ? { ...e, [k]: x } : e)))

  const save = async () => {
    setBusy(true); setErr(null)
    try {
      const patch = Object.fromEntries(Object.entries(v).map(([k, x]) => [k, Number(x)]))
      if (Object.values(patch).some(Number.isNaN)) throw new Error('All fields must be numeric')
      const schedule = sched.map(e => ({ from: e.from, long_pct: Number(e.long_pct), short_pct: Number(e.short_pct) }))
      if (schedule.some(e => !/^\d{4}-\d{2}-\d{2}$/.test(e.from || '') || Number.isNaN(e.long_pct) || Number.isNaN(e.short_pct)))
        throw new Error('Each rate change needs a date and numeric long / short rates')
      if (new Set(schedule.map(e => e.from)).size !== schedule.length) throw new Error('Two rate changes share the same date')
      schedule.sort((a, b) => a.from.localeCompare(b.from))
      await updateGtxSettings({ ...patch, short_rate_verified: verified, swap_rate_schedule: schedule })
      onSaved()
    } catch (e) { setErr(errMsg(e)) } finally { setBusy(false) }
  }

  return (
    <Modal title="Engine Settings" onClose={onClose} maxWidth={620}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 12 }}>
        {SETTING_FIELDS.map(([k, label, hint]) => (
          <div key={k} style={{ minWidth: 0 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: C.dim, marginBottom: 5 }}>{label}</div>
            <input value={v[k]} onChange={e => setV({ ...v, [k]: e.target.value })} inputMode="decimal" style={inputStyle} />
            <div style={{ fontSize: 10, color: C.muted, marginTop: 4 }}>{hint}</div>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 18, paddingTop: 14, borderTop: `1px solid ${C.border}` }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: C.dim, marginBottom: 4 }}>Swap rate changes (dated)</div>
        <div style={{ fontSize: 10, color: C.muted, marginBottom: 10 }}>
          Each rate applies from its date onward; earlier days keep their old rate. Use this when the broker changes the rate — never edit the base rate for that.
        </div>
        {sched.map((e, i) => (
          <div key={i} style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', marginBottom: 8 }}>
            <input type="date" value={e.from || ''} onChange={x => setRow(i, 'from', x.target.value)}
              style={{ ...inputStyle, width: 'auto', flex: '1 1 140px', colorScheme: 'dark' }} />
            <input value={e.long_pct ?? ''} onChange={x => setRow(i, 'long_pct', x.target.value)} inputMode="decimal"
              placeholder="Long %" style={{ ...inputStyle, width: 'auto', flex: '1 1 90px' }} />
            <input value={e.short_pct ?? ''} onChange={x => setRow(i, 'short_pct', x.target.value)} inputMode="decimal"
              placeholder="Short %" style={{ ...inputStyle, width: 'auto', flex: '1 1 90px' }} />
            <button onClick={() => setSched(sched.filter((_, j) => j !== i))} style={{ ...btn(), padding: '8px 12px' }}>✕</button>
          </div>
        ))}
        <button onClick={() => setSched([...sched, { from: '', long_pct: '', short_pct: '' }])} style={btn()}>+ Add rate change</button>
      </div>
      <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 14, fontSize: 12, color: C.dim, cursor: 'pointer' }}>
        <input type="checkbox" checked={verified} onChange={e => setVerified(e.target.checked)} /> Short swap rate verified against a statement
      </label>
      {err && <div style={{ marginTop: 10, color: C.neg, fontSize: 12 }}>{err}</div>}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 18 }}>
        <button onClick={onClose} style={btn()}>Cancel</button>
        <button onClick={save} disabled={busy} style={btn(true)}>{busy ? 'Saving…' : 'Save'}</button>
      </div>
    </Modal>
  )
}

// ─── Main ──────────────────────────────────────────────────────────────────────
export default function GlobalGtxTab() {
  const isMobile = useIsMobile()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [adding, setAdding] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [showRecon, setShowRecon] = useState(false)
  const [raw, setRaw] = useState(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try { setData(await fetchGtxState()) }
    catch (e) { setError(errMsg(e)) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const handleDelete = async (id, date) => {
    if (!id || !window.confirm(`Delete statement for ${fmtDate(date)}? All later days re-reconcile.`)) return
    try { await deleteGtxStatement(id); load() } catch (e) { setError(errMsg(e)) }
  }
  const handleViewRaw = async id => {
    if (!id) return
    try { setRaw(await fetchGtxStatementRaw(id)) } catch (e) { setError(errMsg(e)) }
  }

  const s = data?.summary
  const log = data?.recon_log || []
  const failed = log.filter(x => !x.ok).length
  // Rate in force on the last statement (dated schedule aware), not the flat base rate.
  const cfgRate = s?.current_swap_rate_long_pct ?? data?.settings?.swap_rate_long_pct
  const rateDrift = s?.last_implied_rate_pct && cfgRate ? Math.abs(s.last_implied_rate_pct - cfgRate) > 0.05 : false

  return (
    <div style={{ background: C.bg, minHeight: 'calc(100vh - 110px)', padding: isMobile ? '14px 12px 40px' : '24px 32px 48px' }}>
      <div style={{ maxWidth: 1400, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 16 }}>

        {/* Header */}
        <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 'clamp(18px, 2.4vw, 22px)', fontWeight: 700, color: C.text }}>GlobalGTX — Statement Reconciliation</div>
            <div style={{ fontSize: 12, color: C.muted, marginTop: 4 }}>
              {data?.account || 'GTX0011-USD'} · USD · {s?.last_statement ? `last statement ${fmtDate(s.last_statement)}` : 'no statements yet'}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button onClick={() => setSettingsOpen(true)} disabled={!data} style={btn()}>⚙ Settings</button>
            <button onClick={() => setAdding(true)} style={btn(true)}>+ Add Statement</button>
          </div>
        </div>

        {error && <div style={{ padding: '12px 14px', borderRadius: 10, background: `${C.neg}14`, border: `1px solid ${C.neg}40`, color: C.neg, fontSize: 13 }}>{error}</div>}
        {loading && !data && <div style={{ color: C.muted, fontSize: 13, textAlign: 'center', padding: 40 }}>Loading…</div>}

        {data && log.length === 0 && (
          <Card>
            <div style={{ textAlign: 'center', padding: '30px 10px' }}>
              <div style={{ fontSize: 15, fontWeight: 600, color: C.text }}>No statements yet</div>
              <div style={{ fontSize: 12, color: C.dim, marginTop: 8, lineHeight: 1.6 }}>Paste daily statements oldest first. First statement sets starting balance.</div>
              <button onClick={() => setAdding(true)} style={{ ...btn(true), marginTop: 16 }}>+ Add first statement</button>
            </div>
          </Card>
        )}

        {s && log.length > 0 && (
          <>
            {/* Status chips */}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              <Chip ok={failed === 0}>{failed === 0 ? `✓ All ${log.length} statements reconcile` : `✗ ${failed} of ${log.length} days mismatch`}</Chip>
              {s.last_implied_rate_pct && <Chip ok={!rateDrift}>Implied swap {fmtNum(s.last_implied_rate_pct, 4)}% vs {fmtNum(cfgRate, 5)}%</Chip>}
              {s.rate_changes?.length > 0 && (() => {
                const rc = s.rate_changes[s.rate_changes.length - 1]
                return <Chip ok={null}>Swap rate → {fmtNum(rc.rate_pct, 5)}% from {fmtDate(rc.date)} (inferred)</Chip>
              })()}
              {!data.settings.short_rate_verified && <Chip ok={null}>Short swap rate unverified</Chip>}
            </div>

            <Summary s={s} />

            <Card title={`Open Positions (${data.open_positions.length})`} right={<span style={{ fontSize: 10, color: C.muted }}>tap row → swap ledger</span>}>
              <OpenTable rows={data.open_positions} />
            </Card>

            <Card title={`Closed Positions (${data.closed_positions.length})`}>
              <ClosedTable rows={data.closed_positions} />
            </Card>

            <Card title="Daily Reconciliation Log"
                  right={<button onClick={() => setShowRecon(!showRecon)} style={{ ...btn(), padding: '4px 10px', fontSize: 11 }}>{showRecon ? 'Hide' : `Show (${log.length})`}</button>}>
              {showRecon
                ? <ReconLog log={log} statements={data.statements} onDelete={handleDelete} onViewRaw={handleViewRaw} />
                : <div style={{ fontSize: 12, color: C.dim }}>Engine vs statement, per day — balance, realised, open P/L, margin.</div>}
            </Card>
          </>
        )}
      </div>

      {adding && <AddStatementModal onClose={() => setAdding(false)} onSaved={() => { setAdding(false); load() }} />}
      {settingsOpen && data && <SettingsModal settings={data.settings} onClose={() => setSettingsOpen(false)} onSaved={() => { setSettingsOpen(false); load() }} />}
      {raw && (
        <Modal title={`Raw statement — ${fmtDate(raw.stmt_date)}`} onClose={() => setRaw(null)}>
          <pre style={{ margin: 0, padding: 12, background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, color: C.dim, fontSize: 11, overflowX: 'auto', maxHeight: '60vh', whiteSpace: 'pre' }}>{raw.raw_text}</pre>
        </Modal>
      )}
    </div>
  )
}
