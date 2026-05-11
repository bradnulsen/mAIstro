import { useState, useEffect, useMemo, useCallback } from 'react'
import { getDashboard, getFeed, getGitDiff } from '../api'
import { formatDate, TRIGGER_ICONS } from '../util'

const WINDOWS = [
  { label: 'Today', days: 1 },
  { label: '7 days', days: 7 },
  { label: '30 days', days: 30 },
]

// Stable color palette for job assignment — values defined as CSS tokens (--job-color-0 … --job-color-9).
// Lazy-initialized: getComputedStyle at module load can return empty strings if CSS hasn't parsed yet.
let _jobColors = null
function getJobColors() {
  if (!_jobColors) {
    const root = document.documentElement
    _jobColors = Array.from({ length: 10 }, (_, i) =>
      getComputedStyle(root).getPropertyValue(`--job-color-${i}`).trim()
    )
    // Fallback if CSS tokens are still empty (shouldn't happen after first paint)
    if (_jobColors.every(c => !c)) {
      _jobColors = [
        '#4a90d9', '#d94a4a', '#4ad97a', '#d9a84a', '#9b59b6',
        '#1abc9c', '#e67e22', '#3498db', '#e74c3c', '#2ecc71',
      ]
    }
  }
  return _jobColors
}

function jobColor(jobId, jobMap) {
  if (!jobMap.has(jobId)) jobMap.set(jobId, jobMap.size % getJobColors().length)
  return getJobColors()[jobMap.get(jobId)]
}

export default function Dashboard() {
  const [data, setData] = useState(null)
  const [window, setWindow] = useState(7)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getDashboard(window)
      .then(d => { if (!cancelled) { setData(d); setError(null) } })
      .catch(e => { if (!cancelled) setError(e.message) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [window])

  // Auto-refresh every 30s
  useEffect(() => {
    const interval = setInterval(() => {
      getDashboard(window).then(setData).catch(() => {})
    }, 30000)
    return () => clearInterval(interval)
  }, [window])

  // Stable job color map across sections
  const jobColorMap = useMemo(() => new Map(), [data])

  if (loading && !data) return <div className="dashboard-loading">Loading dashboard...</div>
  if (error && !data) return <div className="dashboard-error">{error}</div>
  if (!data) return null

  return (
    <div className="dashboard">
      <div className="dashboard-header">
        <h2>Activity</h2>
        <div className="dashboard-window-selector">
          {WINDOWS.map(w => (
            <button
              key={w.days}
              className={`small ${window === w.days ? 'active' : ''}`}
              onClick={() => setWindow(w.days)}
            >{w.label}</button>
          ))}
        </div>
      </div>

      <div className="dashboard-sections">
        <HealthSummary health={data.health} jobColorMap={jobColorMap} />
        <JobImpact impact={data.job_impact} jobColorMap={jobColorMap} />
        <Timeline timeline={data.timeline} windowDays={data.window_days} jobColorMap={jobColorMap} />
        <CommitHistory windowDays={data.window_days} />
      </div>
    </div>
  )
}

/* ── Job Health Summary ── */

function HealthSummary({ health, jobColorMap }) {
  if (!health || health.length === 0) {
    return (
      <section className="dashboard-section">
        <h3>Job Health</h3>
        <p className="muted-text">No completed tasks in this window</p>
      </section>
    )
  }

  // Sort by health: lowest success rate first, then degrading trends
  const sorted = [...health].sort((a, b) => {
    const rateA = a.total > 0 ? a.completed / a.total : 1
    const rateB = b.total > 0 ? b.completed / b.total : 1
    if (rateA !== rateB) return rateA - rateB
    return (b.total || 0) - (a.total || 0)
  })

  return (
    <section className="dashboard-section">
      <h3>Job Health</h3>
      <div className="health-grid">
        {sorted.map(g => {
          const rate = g.total > 0 ? g.completed / g.total : 1
          const pct = Math.round(rate * 100)
          const trend = getTrend(rate, g.prev_success_rate)
          const color = jobColor(g.job_id, jobColorMap)

          return (
            <div key={g.job_id} className={`health-card ${pct < 80 ? 'warn' : ''} ${pct < 50 ? 'danger' : ''}`}>
              <div className="health-card-header">
                <span className="health-job-dot" style={{ background: color }} />
                <span className="health-job-name">{g.job_name}</span>
                <span className={`health-trend ${trend}`}>{trend === 'up' ? '↑' : trend === 'down' ? '↓' : '–'}</span>
              </div>
              <div className="health-rate">{pct}%</div>
              <div className="health-bar">
                <div className="health-bar-fill" style={{ width: `${pct}%` }} />
              </div>
              <div className="health-breakdown">
                <span title="Completed">{g.completed} ok</span>
                {g.failed > 0 && <span className="health-bad" title="Failed">{g.failed} fail</span>}
                {g.timed_out > 0 && <span className="health-bad" title="Timed out">{g.timed_out} timeout</span>}
                {g.cancelled > 0 && <span title="Cancelled">{g.cancelled} cancel</span>}
                {g.interrupted > 0 && <span title="Interrupted">{g.interrupted} int</span>}
                {g.rejected > 0 && <span title="Rejected">{g.rejected} rej</span>}
                {g.exhausted > 0 && <span className="health-bad" title="Exhausted (max turns)">{g.exhausted} exhaust</span>}
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function getTrend(currentRate, prevRate) {
  if (prevRate == null) return 'flat'
  const diff = currentRate - prevRate
  if (diff > 0.05) return 'up'
  if (diff < -0.05) return 'down'
  return 'flat'
}

/* ── Job Impact ── */

function JobImpact({ impact, jobColorMap }) {
  if (!impact || impact.length === 0) {
    return (
      <section className="dashboard-section">
        <h3>Job Impact</h3>
        <p className="muted-text">No commits or executions in this window</p>
      </section>
    )
  }

  return (
    <section className="dashboard-section">
      <h3>Job Impact</h3>
      <div className="impact-grid">
        {impact.map((g, i) => {
          const key = g.job_id ?? `pseudo:${i}`
          const dotColor = g.is_operator ? 'var(--text-muted)'
            : (g.job_id != null ? jobColor(g.job_id, jobColorMap) : 'var(--text-muted)')
          return (
            <div key={key} className={`impact-card ${g.is_operator ? 'impact-operator' : ''}`}>
              <div className="impact-header">
                <span className="health-job-dot" style={{ background: dotColor }} />
                <span className="impact-name">{g.job_name}</span>
              </div>
              <div className="impact-metrics">
                <div className="impact-metric">
                  <span className="impact-value">{g.commits}</span>
                  <span className="impact-label">commit{g.commits === 1 ? '' : 's'}</span>
                </div>
                {(g.insertions > 0 || g.deletions > 0) && (
                  <div className="impact-metric">
                    <span className="impact-lines">
                      {g.insertions > 0 && <span className="impact-add">+{g.insertions}</span>}
                      {g.deletions > 0 && <span className="impact-del">-{g.deletions}</span>}
                    </span>
                    <span className="impact-label">{g.files_changed} file{g.files_changed === 1 ? '' : 's'}</span>
                  </div>
                )}
              </div>
              {!g.is_operator && (g.executions > 0 || g.total_cost_usd > 0) && (
                <div className="impact-exec">
                  <span title="Successful executions">{g.completed}/{g.executions} run{g.executions === 1 ? '' : 's'}</span>
                  {g.non_success > 0 && <span className="health-bad" title="Non-success terminals">{g.non_success} fail</span>}
                  {g.total_turns > 0 && <span title="Total agent turns">{g.total_turns} turn{g.total_turns === 1 ? '' : 's'}</span>}
                  {g.total_cost_usd > 0 && <span className="impact-cost" title="Total execution cost">{formatCost(g.total_cost_usd)}</span>}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}

function formatCost(usd) {
  if (usd >= 1) return `$${usd.toFixed(2)}`
  if (usd >= 0.01) return `$${usd.toFixed(2)}`
  return `$${usd.toFixed(4)}`
}

/* ── Timeline ── */

function Timeline({ timeline, windowDays, jobColorMap }) {
  if (!timeline || timeline.length === 0) {
    return (
      <section className="dashboard-section">
        <h3>Timeline</h3>
        <p className="muted-text">No task activity in this window</p>
      </section>
    )
  }

  // Compute time bounds
  const now = Date.now()
  const windowStart = now - windowDays * 86400000

  // Compute per-job median duration for outlier detection
  const durationsByJob = {}
  timeline.forEach(t => {
    if (!t.completed_at) return
    const dur = new Date(t.completed_at + 'Z') - new Date(t.started_at + 'Z')
    if (!durationsByJob[t.job_id]) durationsByJob[t.job_id] = []
    durationsByJob[t.job_id].push(dur)
  })
  const medians = {}
  for (const [jid, durs] of Object.entries(durationsByJob)) {
    durs.sort((a, b) => a - b)
    medians[jid] = durs[Math.floor(durs.length / 2)]
  }

  // Group by job for swimlanes
  const lanes = []
  const jobIndex = {}
  timeline.forEach(t => {
    if (!(t.job_id in jobIndex)) {
      jobIndex[t.job_id] = lanes.length
      lanes.push({ id: t.job_id, name: t.job_name, tasks: [] })
    }
    lanes[jobIndex[t.job_id]].tasks.push(t)
  })

  return (
    <section className="dashboard-section">
      <h3>Timeline</h3>
      <div className="timeline-container">
        <TimelineAxis windowStart={windowStart} now={now} />
        {lanes.map(g => {
          const color = jobColor(g.id, jobColorMap)
          return (
            <div key={g.id} className="timeline-lane">
              <div className="timeline-lane-label">{g.name}</div>
              <div className="timeline-lane-track">
                {g.tasks.map(t => {
                  const start = new Date(t.started_at + 'Z').getTime()
                  const end = t.completed_at ? new Date(t.completed_at + 'Z').getTime() : now
                  const span = now - windowStart
                  const width = Math.max(0.3, ((end - start) / span) * 100)
                  const left = Math.max(0, Math.min(100 - width, ((start - windowStart) / span) * 100))
                  const dur = end - start
                  const median = medians[t.job_id] || dur
                  const isOutlier = dur > median * 3 && dur > 60000
                  // Non-happy-path: any terminal outcome other than 'completed',
                  // or an explicit error string for legacy data without status
                  const isError = (t.status && t.status !== 'completed' && t.completed_at)
                    || (!t.status && t.error != null)

                  return (
                    <div
                      key={t.id}
                      className={`timeline-bar ${isOutlier ? 'outlier' : ''} ${isError ? 'error' : ''}`}
                      style={{
                        left: `${left}%`,
                        width: `${width}%`,
                        background: color,
                      }}
                      title={`#${t.id} ${t.job_name} — ${formatMs(dur)}${isError ? ' (' + t.error + ')' : ''}`}
                    />
                  )
                })}
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function TimelineAxis({ windowStart, now }) {
  const span = now - windowStart
  const ticks = []
  // Generate ~5 evenly-spaced time ticks, oldest on the left, "now" on the right.
  for (let i = 0; i <= 4; i++) {
    const t = windowStart + (span * i) / 4
    const d = new Date(t)
    const label = span > 86400000 * 2
      ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
      : d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    ticks.push({ pct: (i / 4) * 100, label })
  }
  return (
    <div className="timeline-axis">
      {ticks.map((t, i) => (
        <span key={i} className="timeline-tick" style={{ left: `${t.pct}%` }}>{t.label}</span>
      ))}
    </div>
  )
}

function formatMs(ms) {
  const secs = Math.round(ms / 1000)
  if (secs < 60) return `${secs}s`
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ${secs % 60}s`
  return `${Math.floor(mins / 60)}h ${mins % 60}m`
}

/* ── Commit History (absorbed from Feed.jsx) ── */

function CommitHistory({ windowDays }) {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [selected, setSelected] = useState(null)
  const [diff, setDiff] = useState('')
  const [loadingDiff, setLoadingDiff] = useState(false)

  // Close detail on Escape
  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'Escape' && selected) setSelected(null)
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [selected])

  const refresh = useCallback(async () => {
    try {
      const feed = await getFeed({ limit: 100 })
      setItems(feed)
    } catch {}
    setLoading(false)
  }, [])

  const handleRefresh = useCallback(async () => {
    setRefreshing(true)
    await refresh()
    setRefreshing(false)
  }, [refresh])

  useEffect(() => { refresh() }, [refresh])

  // Auto-refresh every 30s (matches surrounding dashboard cadence)
  useEffect(() => {
    const interval = setInterval(refresh, 30000)
    return () => clearInterval(interval)
  }, [refresh])

  const selectItem = async (item) => {
    setSelected(item)
    setDiff('')
    setLoadingDiff(true)
    try {
      const d = await getGitDiff(item.hash)
      setDiff(d.diff || '')
    } catch {
      setDiff('')
    }
    setLoadingDiff(false)
  }

  // Client-side window filter — keeps the table aligned with the
  // window selector that drives the rest of the dashboard. The backend
  // returns dates as "YYYY-MM-DD HH:MM:SS ±HHMM" (git iso) which Date()
  // parses directly; the prior `.replace(' ', 'T')` produced a bad ISO
  // string ("...T...:09 -0400") that returned NaN and filtered out
  // every commit.
  const cutoffMs = windowDays ? Date.now() - windowDays * 86400000 : null
  const filtered = cutoffMs
    ? items.filter(i => i.date && new Date(i.date).getTime() >= cutoffMs)
    : items

  return (
    <section className="dashboard-section">
      <div className="dashboard-section-header">
        <h3>Commits</h3>
        <button className="small" onClick={handleRefresh} disabled={refreshing}>
          {refreshing ? <span className="tool-spinner" /> : '↻'}
        </button>
      </div>
      <div className="commit-history">
        <div className="feed-list">
          {loading && <div className="loading">Loading commits...</div>}
          {!loading && filtered.length === 0 && (
            <div className="empty-state">No commits in this window</div>
          )}
          {filtered.map(item => (
            <div
              key={item.hash}
              className={`feed-item ${selected?.hash === item.hash ? 'active' : ''}`}
              onClick={() => selectItem(item)}
            >
              <div className="feed-avatar">
                {(item.author || '?')[0].toUpperCase()}
              </div>
              <div className="feed-body">
                <div className="feed-meta">
                  <span className="feed-author">{item.author}</span>
                  <span className="feed-trigger">
                    {TRIGGER_ICONS[item.trigger] || ''}
                  </span>
                  <span>{formatDate(item.date)}</span>
                </div>
                <div className="feed-message">{item.message}</div>
              </div>
              {(item.files?.length > 0 || item.insertions > 0 || item.deletions > 0) && (
                <div className="feed-stats">
                  <span className="feed-stat-files">{item.files?.length || 0} {item.files?.length === 1 ? 'file' : 'files'}</span>
                  {(item.insertions > 0 || item.deletions > 0) && (
                    <span className="feed-stat-lines">
                      {item.insertions > 0 && <span className="feed-stat-add">+{item.insertions}</span>}
                      {item.deletions > 0 && <span className="feed-stat-del">-{item.deletions}</span>}
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>

        {selected && (
          <div className="detail-panel">
            <div className="detail-panel-header">
              <h3>Commit Detail</h3>
              <button className="small" onClick={() => setSelected(null)}>✕</button>
            </div>
            <div className="commit-meta">
              {selected.hash?.slice(0, 8)} by {selected.author}
            </div>
            <div className="commit-headline">
              {selected.message}
            </div>
            {selected.files && (
              <div className="detail-section commit-files-section">
                <label>Changed Files</label>
                {selected.files.map(f => (
                  <div key={f} className="commit-changed-file">{f}</div>
                ))}
              </div>
            )}
            {loadingDiff && (
              <div className="diff-status-note">Loading diff...</div>
            )}
            {diff && (
              <div className="diff-wrapper">
                <label>Diff</label>
                <pre className="diff-view">
                  {diff.split('\n').map((line, i) => (
                    <div key={i} className={
                      line.startsWith('+') ? 'diff-add' :
                      line.startsWith('-') ? 'diff-del' :
                      line.startsWith('@@') ? 'diff-hunk' : ''
                    }>{line}</div>
                  ))}
                </pre>
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
