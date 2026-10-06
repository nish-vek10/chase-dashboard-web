// frontend/src/pages/Portfolio.jsx
/**
 * Portfolio home page.
 *
 * Sections (top to bottom):
 *   1. Page header       — title + last updated timestamp
 *   2. Summary strip     — SummaryStrip: 4 equal cards (Money Allocated, AUM, TWR, PnL)
 *   3. Portfolio KPIs    — 7-card strip
 *   4. Pod overview      — one KPI strip per pod, clickable to drill down
 *   5. Charts row        — Equity curve | Allocation donut | PnL bars
 *   6. Hierarchy tabs    — Pods | Strategies | Traders
 */

import { useState, useEffect, useRef } from 'react'
import { useNavigate }         from 'react-router-dom'
import { useQuery }            from '@tanstack/react-query'
import { RefreshCw, BookOpen } from 'lucide-react'

import { usePortfolio, useHierarchyTable } from '../hooks/usePortfolioData.js'
import useIsMobile         from '../hooks/useIsMobile.js'
import {
  computeCapitalMetrics, fmtGBP, fmtPctSigned, CapitalOverviewHero,
  CapitalFlowTable, CapitalAtGlanceChart, CapitalInfoBox,
} from '../components/CapitalOverview.jsx'
import EquityChart         from '../components/charts/EquityChart.jsx'
import { DonutChart }      from '../components/charts/DonutChart.jsx'
import PnlBarChart         from '../components/charts/PnlBarChart.jsx'
import BreakdownTable      from '../components/tables/BreakdownTable.jsx'
import SummaryStrip        from '../components/SummaryStrip.jsx'
import PodStrategyManager  from '../components/PodStrategyManager.jsx'

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const API             = import.meta.env.VITE_API_BASE ?? ''
const POD_COLORS     = ['#0EA5E9', '#F59E0B', '#34D399', '#A78BFA', '#F472B6']
const HIERARCHY_TABS = ['pod', 'strategy', 'trader']


// ---------------------------------------------------------------------------
// Small reusable sub-components
// ---------------------------------------------------------------------------

function SectionLabel({ children }) {
  return (
    <div style={{
      fontSize: 10, fontWeight: 600, letterSpacing: '0.8px',
      textTransform: 'uppercase', color: '#475569', paddingBottom: 10,
    }}>
      {children}
    </div>
  )
}

function ChartCard({ title, children, style = {} }) {
  return (
    <div className="glass-panel" style={{ padding: 16, ...style }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: '#94A3B8', marginBottom: 12 }}>
        {title}
      </div>
      {children}
    </div>
  )
}

const TAB_LABELS = {
  pod:      'Pods',
  strategy: 'Strategies',
  trader:   'Traders',
}

function TabButton({ label, active, onClick }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '6px 14px', borderRadius: 6, fontSize: 12,
        fontWeight: 500, border: 'none', cursor: 'pointer',
        background: active ? '#1E3A5F' : 'transparent',
        color:      active ? '#38BDF8' : '#475569',
        transition: 'all 0.15s',
      }}
    >
      {TAB_LABELS[label] ?? label.charAt(0).toUpperCase() + label.slice(1) + 's'}
    </button>
  )
}

// ---------------------------------------------------------------------------
// Overview card — glass, 4-box 2x2 mini-grid (Pod Overview / Strategies Overview)
// No 1D/7D/30D here — those live in the drill-down only.
// ---------------------------------------------------------------------------

function hexToRgba(hex, alpha) {
  const h = hex.replace('#', '')
  const full = h.length === 3 ? h.split('').map(c => c + c).join('') : h
  const r = parseInt(full.substring(0, 2), 16)
  const g = parseInt(full.substring(2, 4), 16)
  const b = parseInt(full.substring(4, 6), 16)
  return `rgba(${r},${g},${b},${alpha})`
}

function GlassStyles() {
  return (
    <style>{`
      .ov-card {
        position: relative;
        background: linear-gradient(155deg, #17273D 0%, #0F1D30 55%, #0B1522 100%);
        backdrop-filter: blur(18px) saturate(160%);
        -webkit-backdrop-filter: blur(18px) saturate(160%);
        border: 1px solid rgba(255,255,255,0.09);
        border-left: 3px solid var(--accent, #38BDF8);
        border-radius: 14px;
        padding: 18px 20px;
        cursor: pointer;
        overflow: hidden;
        transition: transform 0.25s cubic-bezier(.2,.8,.2,1), box-shadow 0.25s ease, border-color 0.25s ease, background 0.25s ease;
        box-shadow: 0 1px 0 rgba(255,255,255,0.05) inset, 0 10px 28px -14px rgba(0,0,0,0.7);
      }
      .ov-card::after {
        content: '';
        position: absolute; inset: 0;
        background: radial-gradient(130% 110% at 0% 0%, var(--accent-soft, rgba(56,189,248,0.14)) 0%, transparent 62%);
        opacity: 0.55; pointer-events: none;
        transition: opacity 0.25s ease;
      }
      .ov-card:hover::after { opacity: 0.75; }
      .ov-card:hover {
        transform: translateY(-4px);
        border-color: var(--accent-soft, rgba(56,189,248,0.35));
        background: linear-gradient(155deg, #1A2C46 0%, #112036 55%, #0C1726 100%);
        box-shadow: 0 1px 0 rgba(255,255,255,0.06) inset, 0 18px 36px -12px var(--accent-glow, rgba(56,189,248,0.35)), 0 10px 24px -14px rgba(0,0,0,0.8);
      }
      .ov-header {
        display: flex; align-items: center; gap: 9px;
        margin-bottom: 14px; padding-bottom: 12px;
        border-bottom: 1px solid rgba(255,255,255,0.07);
        position: relative;
      }
      .ov-dot {
        width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0;
        background: var(--accent, #38BDF8);
        box-shadow: 0 0 0 3px var(--accent-soft, rgba(56,189,248,0.18)), 0 0 12px 1px var(--accent-glow, rgba(56,189,248,0.5));
      }
      .ov-name { font-size: 13.5px; font-weight: 700; color: #F1F5F9; letter-spacing: 0.2px; }
      .ov-hint {
        font-size: 9.5px; color: #475569; margin-left: auto; font-weight: 600;
        opacity: 0; transform: translateX(-4px);
        transition: opacity 0.2s ease, transform 0.2s ease, color 0.2s ease;
      }
      .ov-card:hover .ov-hint { opacity: 1; transform: translateX(0); color: var(--accent, #38BDF8); }
      .stat-grid {
        display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 8px;
        position: relative;
      }
      .stat-grid.stat-grid-2col {
        grid-template-columns: repeat(2, minmax(0,1fr));
      }
      .cap-hero-card {
        display: flex; flex-direction: column;
        min-height: 172px;
      }
      .stat-box {
        position: relative;
        background: rgba(255,255,255,0.035);
        border: 1px solid rgba(255,255,255,0.07);
        border-radius: 10px;
        padding: 9px 10px;
        min-width: 0;
        transition: background 0.25s ease, border-color 0.25s ease;
      }
      .ov-card:hover .stat-box {
        background: rgba(255,255,255,0.06);
        border-color: rgba(255,255,255,0.12);
      }
      .stat-label {
        font-size: 8.7px; font-weight: 600; letter-spacing: 0.4px;
        text-transform: uppercase; color: #64748B; margin-bottom: 5px;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .stat-value {
        font-size: 12.5px; font-weight: 700; font-variant-numeric: tabular-nums;
        display: flex; align-items: center; gap: 3px; flex-wrap: wrap;
        line-height: 1.25; word-break: break-word;
      }
      .ov-grid {
        display: grid; gap: 16px;
      }

      /* ── Lighter glass treatment — charts + breakdown table ── */
      .glass-panel {
        position: relative;
        background: linear-gradient(160deg, rgba(28,45,71,0.38) 0%, rgba(13,24,38,0.55) 100%);
        backdrop-filter: blur(12px) saturate(140%);
        -webkit-backdrop-filter: blur(12px) saturate(140%);
        border: 1px solid rgba(255,255,255,0.07);
        border-radius: 12px;
        box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset, 0 8px 20px -14px rgba(0,0,0,0.55);
        transition: border-color 0.25s ease, box-shadow 0.25s ease;
      }
      .glass-panel:hover {
        border-color: rgba(56,189,248,0.16);
        box-shadow: 0 1px 0 rgba(255,255,255,0.05) inset, 0 10px 26px -14px rgba(56,189,248,0.14), 0 8px 20px -14px rgba(0,0,0,0.6);
      }
      .glass-table {
        position: relative;
        background: linear-gradient(160deg, rgba(24,39,62,0.35) 0%, rgba(11,20,32,0.55) 100%);
        backdrop-filter: blur(12px) saturate(140%);
        -webkit-backdrop-filter: blur(12px) saturate(140%);
        border: 1px solid rgba(255,255,255,0.07);
        border-radius: 12px;
        overflow: hidden;
        box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset, 0 8px 20px -14px rgba(0,0,0,0.55);
      }
      .glass-table .gt-bar {
        background: rgba(255,255,255,0.02) !important;
        backdrop-filter: blur(8px);
      }
      .glass-table thead th {
        background: rgba(15,23,42,0.55) !important;
        backdrop-filter: blur(8px);
      }
      .glass-table tbody tr:nth-child(odd) {
        background: rgba(255,255,255,0.02) !important;
      }
      .glass-table tbody tr:hover {
        background: rgba(56,189,248,0.07) !important;
      }
    `}</style>
  )
}

function StatBox({ label, value, tone = 'default', subLine }) {
  const color = tone === 'pos' ? '#34D399' : tone === 'neg' ? '#F87171' : '#F1F5F9'
  const arrow = tone === 'pos' ? '▲' : tone === 'neg' ? '▼' : null
  return (
    <div className="stat-box">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={{ color }}>
        {arrow && <span style={{ fontSize: 9 }}>{arrow}</span>}
        {value}
      </div>
      {subLine && (
        <div style={{ fontSize: 9.5, color: '#64748B', marginTop: 2, fontFamily: 'monospace' }}>
          {subLine}
        </div>
      )}
    </div>
  )
}

function WatermarkBadge({ watermark, profitSharePct }) {
  if (watermark == null) return null
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4, padding: '2px 8px',
      borderRadius: 6, fontSize: 9.5, fontWeight: 700, color: '#A78BFA',
      background: 'rgba(167,139,250,0.12)', border: '1px solid rgba(167,139,250,0.28)',
      whiteSpace: 'nowrap',
    }}>
      WM {fmtGBP(watermark)}{profitSharePct != null ? ` · ${profitSharePct}%` : ''}
    </span>
  )
}

// Active=green, Inactive=amber, Closed=red — three-state model, README §14.
const STATUS_BADGE_COLOR = { active: '#34D399', inactive: '#F59E0B', closed: '#F87171' }

function StatusBadge({ status }) {
  const key   = (status || 'Active').toLowerCase()
  const color = STATUS_BADGE_COLOR[key] || STATUS_BADGE_COLOR.inactive
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', padding: '2px 8px',
      borderRadius: 6, fontSize: 9.5, fontWeight: 800, letterSpacing: '0.4px',
      textTransform: 'uppercase', color,
      background: `${color}1F`, border: `1px solid ${color}50`,
      whiteSpace: 'nowrap',
    }}>
      {(status || 'Active').toUpperCase()}
    </span>
  )
}

// No live performance source tracked yet (only a capital figure exists).
// Shown next to StatusBadge so it's obvious a card's numbers are "at par"
// rather than a real recorded outcome — see README §14.
function NoDataBadge() {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', padding: '2px 8px',
      borderRadius: 6, fontSize: 9.5, fontWeight: 800, letterSpacing: '0.4px',
      textTransform: 'uppercase', color: '#94A3B8',
      background: 'rgba(148,163,184,0.12)', border: '1px solid rgba(148,163,184,0.3)',
      whiteSpace: 'nowrap',
    }}>
      No data yet
    </span>
  )
}

function OverviewCard({ name, color, kpis, onClick, isMobile, status, watermark, profitSharePct, hasData }) {
  const vars = {
    '--accent':       color,
    '--accent-soft':  hexToRgba(color, 0.20),
    '--accent-glow':  hexToRgba(color, 0.35),
  }
  const { invested, banked, allocated, equity, pnl, roi } = computeCapitalMetrics(kpis)

  // Fund-statement strategies (12-FLAGS): the fund's own USD net income /
  // return % from its NAV administrator statement — undefined for every
  // other strategy type, so this sub-line only ever appears there. Shown
  // so a GBP FX-translation effect (baseline converted at deposit-date rate
  // vs current equity at latest rate) never reads as a discrepancy against
  // the fund's own quoted numbers.
  const fundUsdNet = kpis?.fund_usd_net_income
  const fundUsdPct = kpis?.fund_usd_return_pct
  const hasFundUsd = fundUsdNet != null && fundUsdPct != null
  const fundUsdPnlLine = hasFundUsd
    ? `USD YTD: ${fundUsdNet >= 0 ? '+' : '-'}$${Math.abs(fundUsdNet).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : null
  const fundUsdRoiLine = hasFundUsd
    ? `USD YTD: ${fundUsdPct >= 0 ? '+' : ''}${fundUsdPct.toFixed(2)}%`
    : null

  return (
    <div className="ov-card" onClick={onClick} style={vars}>
      <div className="ov-header">
        <div className="ov-dot" />
        <div className="ov-name">{name}</div>
        <WatermarkBadge watermark={watermark} profitSharePct={profitSharePct} />
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
          <StatusBadge status={status} />
          {hasData === false && <NoDataBadge />}
          <div className="ov-hint" style={{ marginLeft: 0 }}>Drill down →</div>
        </div>
      </div>

      <div className={`stat-grid${isMobile ? ' stat-grid-2col' : ''}`}>
        <StatBox label="Capital Invested" value={fmtGBP(invested)} />
        <StatBox label="Banked Profit / Loss" value={fmtGBP(banked)} tone={banked >= 0 ? 'pos' : 'neg'} />
        <StatBox label="Capital Allocated" value={fmtGBP(allocated)} />
        <StatBox label="Current Equity"   value={fmtGBP(equity)} />
        <StatBox
          label="Total P&L"
          value={fmtGBP(pnl)}
          tone={pnl >= 0 ? 'pos' : 'neg'}
          subLine={fundUsdPnlLine}
        />
        {/* PAUSED 2026-09-02 (Nish request) — Total ROI formula still being
            finalized on Pods/Strategies cards. Box stays, value shows "—"
            until reinstated. Original: value={fmtPctSigned(roi)}
            tone={roi >= 0 ? 'pos' : 'neg'} subLine={fundUsdRoiLine} */}
        <StatBox
          label="Total ROI"
          value="—"
          tone="default"
        />
      </div>
    </div>
  )
}


function Spinner() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: 200, color: '#334155', gap: 8 }}>
      <RefreshCw size={14} style={{ animation: 'spin 1s linear infinite' }} />
      <span style={{ fontSize: 12 }}>Loading...</span>
      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )
}

function ErrorMsg({ message }) {
  return (
    <div style={{
      padding: 20, color: '#F87171', fontSize: 12, borderRadius: 8,
      background: 'rgba(248,113,113,0.05)', border: '1px solid rgba(248,113,113,0.15)',
    }}>
      Error loading data: {message}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Hierarchy tab — lazy-loads its own data per tab selection
// ---------------------------------------------------------------------------

function HierarchyTab({ entityType, onRowClick }) {
  const { data, isLoading, error } = useHierarchyTable(entityType)
  if (isLoading) return <Spinner />
  if (error)     return <ErrorMsg message={error.message} />
  return <BreakdownTable rows={data?.rows ?? []} onRowClick={onRowClick} />
}


// ---------------------------------------------------------------------------
// Main page component
// ---------------------------------------------------------------------------

export default function Portfolio({ timeRange, initialTab }) {
  const navigate  = useNavigate()
  const isMobile  = useIsMobile()
  const [activeTab,    setActiveTab]    = useState(initialTab || 'pod')
  const [showManager,  setShowManager]  = useState(false)
  const [exportingCsv, setExportingCsv] = useState(false)
  const summaryStripRef = useRef(null)

  // ── Strategies CSV export (2026-09-24, Nish) — every strategy, every
  // status, straight from the same KPI engine the cards below render from,
  // so it always matches what's on screen. See backend/src/routers/portfolio.py.
  const handleExportStrategiesCsv = async () => {
    setExportingCsv(true)
    try {
      const res = await fetch(`${API}/api/portfolio/strategies/export-csv`)
      if (!res.ok) throw new Error('Export failed')
      const blob = await res.blob()
      const url  = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      const today = new Date().toISOString().slice(0, 10)
      link.download = `Chase-Strategies-${today}.csv`
      link.click()
      URL.revokeObjectURL(url)
    } catch {
      // Non-fatal — a failed export shouldn't disrupt the page
    } finally {
      setExportingCsv(false)
    }
  }

  // Sync tab when route changes (e.g. clicking Traders in navbar)
  useEffect(() => {
    if (initialTab) setActiveTab(initialTab)
  }, [initialTab])

  const { data, isLoading, error } = usePortfolio(timeRange)

  // Fund ledger — capital events, TWR, bank balance
  const { data: fundLedger, isLoading: ledgerLoading } = useQuery({
    queryKey:        ['fund_ledger'],
    queryFn:         () =>
      fetch('/api/portfolio/fund_ledger').then(r => r.json()),
    refetchInterval: 60_000,
    staleTime:       30_000,
  })

  if (isLoading) return <div style={{ padding: 24 }}><Spinner /></div>
  if (error)     return <div style={{ padding: 24 }}><ErrorMsg message={error.message} /></div>

  const {
    portfolio_name, last_updated, kpis, pods, strategies,
    equity_curve, allocation, pnl_contribution,
  } = data

  return (
    <div style={{ padding: isMobile ? '14px 14px 40px' : '16px 24px 48px' }}>
      <GlassStyles />

      {/* SummaryStrip mounted invisibly — owns the Ledger/Equity/TWR modals,
          triggered externally via summaryStripRef from the header button. */}
      <SummaryStrip
        ref={summaryStripRef}
        data={fundLedger}
        equityCurve={equity_curve}
        loading={ledgerLoading}
        cardsVisible={false}
      />

      {/* ── Page header ──
          Mobile (2026-09-07, Nish request): Capital Ledger button now sits
          top-right, in line with the "Chase Capital" title row, instead of
          stacking full-width underneath it. Row layout on both breakpoints
          now — only the button's own size/padding shrinks on mobile. Header
          row shares the same horizontal padding as the hero card grid below
          (both direct children of the page's outer container), so the
          button's right edge and the title's left edge line up with the
          card row automatically — no extra alignment needed. */}
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        alignItems: isMobile ? 'flex-start' : 'flex-end',
        flexDirection: 'row',
        gap: isMobile ? 8 : 0,
        marginBottom: 20,
      }}>
        <div>
          <h1 style={{ fontSize: isMobile ? 19 : 22, fontWeight: 600, color: '#F1F5F9', margin: 0 }}>
            {portfolio_name}
          </h1>
          <div style={{ fontSize: 11, color: '#475569', marginTop: 3 }}>
            Portfolio · All pods · {timeRange === 'SI' ? 'Since inception' : timeRange}
          </div>
        </div>
        <button
          onClick={() => summaryStripRef.current?.openLedger()}
          className="ov-card"
          style={{
            '--accent': '#38BDF8',
            display: 'flex', alignItems: 'center', gap: isMobile ? 6 : 9,
            padding: isMobile ? '7px 10px' : '10px 16px', cursor: 'pointer',
            fontSize: isMobile ? 11.5 : 12.5, fontWeight: 700, color: '#E2E8F0',
            border: '1px solid rgba(56,189,248,0.25)',
            flexShrink: 0, whiteSpace: 'nowrap',
          }}
        >
          <BookOpen size={isMobile ? 13 : 15} color="#38BDF8" />
          Capital Ledger
        </button>
      </div>

      {/* ── Capital & Performance Overview — 6-box hero strip ── */}
      <SectionLabel>Capital &amp; Performance Overview</SectionLabel>
      <div style={{ marginBottom: 20 }}>
        <CapitalOverviewHero kpis={kpis} isMobile={isMobile} />
      </div>

      {/* ── Divider ── */}
      <div style={{ height: 1, background: '#1E3A5F', margin: '0 0 16px' }} />

      {/* PAUSED 2026-09-07 (Nish request) — Capital Flow Summary table and
          Capital at a Glance chart hidden from Portfolio. CapitalInfoBox
          (the text summary strip) below is now the sole capital-flow
          summary shown here. Components themselves (CapitalFlowTable,
          CapitalAtGlanceChart) are untouched in CapitalOverview.jsx — still
          fully wired, still imported above — so re-enabling is a single
          uncomment, no rebuild needed. See README §16.
      <div style={{
        display: 'grid',
        gridTemplateColumns: isMobile ? '1fr' : '0.9fr 1.1fr',
        gap: 12, marginBottom: 16,
      }}>
        <CapitalFlowTable kpis={kpis} />
        <CapitalAtGlanceChart kpis={kpis} height={isMobile ? 260 : 300} />
      </div>
      */}
      <div style={{ marginBottom: 20 }}>
        <CapitalInfoBox kpis={kpis} />
      </div>

      {/* ── Divider ── */}
      <div style={{ height: 1, background: '#1E3A5F', margin: '4px 0 16px' }} />

      {/* ── Pod overview ── */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingBottom: 10 }}>
        <SectionLabel>Pod Overview</SectionLabel>
        <button
          onClick={() => setShowManager(true)}
          style={{
            display:    'flex', alignItems: 'center', gap: 6,
            padding:    '6px 12px', borderRadius: 7, border: '1px solid rgba(14,165,233,0.25)',
            cursor:     'pointer', fontSize: 11, fontWeight: 600,
            background: 'rgba(14,165,233,0.08)', color: '#38BDF8',
            marginBottom: 10,
          }}
        >
          Manage Pods &amp; Strategies
        </button>
      </div>
      <div className="ov-grid" style={{
        gridTemplateColumns: isMobile ? '1fr' : 'repeat(2, minmax(0, 1fr))',
        marginBottom: 26,
      }}>
        {pods.map((pod, idx) => (
          <OverviewCard
            key={pod.entity_id}
            name={pod.name}
            color={pod.pod_color || POD_COLORS[idx % POD_COLORS.length]}
            kpis={pod.kpis}
            isMobile={isMobile}
            status={pod.status}
            onClick={() => navigate(`/drilldown/${pod.entity_id}`)}
          />
        ))}
      </div>

      {/* ── Divider ── */}
      <div style={{ height: 1, background: '#1E3A5F', margin: '0 0 16px' }} />

      {/* ── Strategies overview ── */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingBottom: 10 }}>
        <SectionLabel>Strategies Overview</SectionLabel>
        <button
          onClick={handleExportStrategiesCsv}
          disabled={exportingCsv}
          title="Every strategy, active and inactive — Initial Investment, Current Equity (Gross, before watermark/profit-share) and Net"
          style={{
            display:    'flex', alignItems: 'center', gap: 6,
            padding:    '6px 12px', borderRadius: 7, border: '1px solid rgba(52,211,153,0.3)',
            cursor:     exportingCsv ? 'not-allowed' : 'pointer', fontSize: 11, fontWeight: 600,
            background: 'rgba(52,211,153,0.08)', color: '#34D399',
            marginBottom: 10, opacity: exportingCsv ? 0.6 : 1,
          }}
        >
          {exportingCsv ? '⏳ Exporting…' : '⬇ Export Strategies CSV'}
        </button>
      </div>
      <div className="ov-grid" style={{
        gridTemplateColumns: isMobile ? '1fr' : 'repeat(2, minmax(0, 1fr))',
        marginBottom: 26,
      }}>
        {(strategies ?? []).map((strat, idx) => (
          <OverviewCard
            key={strat.entity_id}
            name={strat.name}
            color={strat.pod_color || POD_COLORS[idx % POD_COLORS.length]}
            kpis={strat.kpis}
            isMobile={isMobile}
            status={strat.status}
            watermark={strat.watermark}
            profitSharePct={strat.profit_share_pct}
            hasData={strat.has_data}
            onClick={() => navigate(`/drilldown/${strat.entity_id}`)}
          />
        ))}
      </div>

      {/* ── Divider ── */}
      <div style={{ height: 1, background: '#1E3A5F', margin: '0 0 20px' }} />

      {/* ── Charts row ── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: isMobile ? '1fr' : '2fr 1fr 1fr',
        gap: 12, marginBottom: 28,
      }}>
        <ChartCard title="Equity (with drawdown)">
          <EquityChart data={equity_curve} height={isMobile ? 240 : 300} />
        </ChartCard>
        <ChartCard title="Allocation by Pod">
          <DonutChart data={allocation} height={isMobile ? 220 : 260} />
        </ChartCard>
        <ChartCard title="PnL Contribution (Pod)">
          <PnlBarChart data={pnl_contribution} height={isMobile ? 220 : 260} />
        </ChartCard>
      </div>

      {/* ── Hierarchy tabs ── */}
      <div className="glass-table">
        <div style={{
          display: 'flex', gap: 2, padding: '12px 12px 10px',
          borderBottom: '1px solid rgba(255,255,255,0.07)',
          overflowX: 'auto', WebkitOverflowScrolling: 'touch',
        }}>
          {HIERARCHY_TABS.map(t => (
            <TabButton key={t} label={t} active={activeTab === t} onClick={() => setActiveTab(t)} />
          ))}
        </div>
        <HierarchyTab
          entityType={activeTab}
          onRowClick={id => navigate(`/drilldown/${id}`)}
        />
      </div>

      {/* ── Pod / Strategy Manager modal ── */}
      {showManager && (
        <PodStrategyManager
          onClose={() => setShowManager(false)}
          onSaved={() => {}}
        />
      )}

    </div>
  )
}