import { useState, useEffect, useMemo } from 'react'
import { getDashboard } from '../api'

const WINDOWS = [
  { label: 'Today', days: 1 },
  { label: '7 days', days: 7 },
  { label: '30 days', days: 30 },
]

// Stable color palette for job assignment — values defined as CSS tokens (--job-color-0 … --job-color-9)
const root = document.documentElement
const JOB_COLORS = Array.from({ length: 10 }, (_, i) =>
  getComputedStyle(root).getPropertyValue(`--job-color-${i}`).trim()
)

function jobColor(jobId, jobMap) {
  if (!jobMap.has(jobId)) jobMap.set(jobId, jobMap.size % JOB_COLORS.length)
  return JOB_COLORS[jobMap.get(jobId)]
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
        <h2>Dashboard</h2>
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
        <Timeline timeline={data.timeline} windowDays={data.window_days} jobColorMap={jobColorMap} />
        <DispatchChains chains={data.chains} jobColorMap={jobColorMap} />
        <ToolUsage tools={data.tools} jobColorMap={jobColorMap} />
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
        {sorted.map(job => {
          const rate = job.total > 0 ? job.completed / job.total : 1
          const pct = Math.round(rate * 100)
          const trend = getTrend(rate, job.prev_success_rate)
          const color = jobColor(job.job_id, jobColorMap)

          return (
            <div key={job.job_id} className={`health-card ${pct < 80 ? 'warn' : ''} ${pct < 50 ? 'danger' : ''}`}>
              <div className="health-card-header">
                <span className="health-job-dot" style={{ background: color }} />
                <span className="health-job-name">{job.job_name}</span>
                <span className={`health-trend ${trend}`}>{trend === 'up' ? '\u2191' : trend === 'down' ? '\u2193' : '\u2013'}</span>
              </div>
              <div className="health-rate">{pct}%</div>
              <div className="health-bar">
                <div className="health-bar-fill" style={{ width: `${pct}%` }} />
              </div>
              <div className="health-breakdown">
                <span title="Completed">{job.completed} ok</span>
                {job.failed > 0 && <span className="health-bad" title="Failed">{job.failed} fail</span>}
                {job.timed_out > 0 && <span className="health-bad" title="Timed out">{job.timed_out} timeout</span>}
                {job.cancelled > 0 && <span title="Cancelled">{job.cancelled} cancel</span>}
                {job.interrupted > 0 && <span title="Interrupted">{job.interrupted} int</span>}
                {job.rejected > 0 && <span title="Rejected">{job.rejected} rej</span>}
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
  const jobs = []
  const jobIndex = {}
  timeline.forEach(t => {
    if (!(t.job_id in jobIndex)) {
      jobIndex[t.job_id] = jobs.length
      jobs.push({ id: t.job_id, name: t.job_name, tasks: [] })
    }
    jobs[jobIndex[t.job_id]].tasks.push(t)
  })

  return (
    <section className="dashboard-section">
      <h3>Timeline</h3>
      <div className="timeline-container">
        <TimelineAxis windowStart={windowStart} now={now} />
        {jobs.map(job => {
          const color = jobColor(job.id, jobColorMap)
          return (
            <div key={job.id} className="timeline-lane">
              <div className="timeline-lane-label">{job.name}</div>
              <div className="timeline-lane-track">
                {job.tasks.map(t => {
                  const start = new Date(t.started_at + 'Z').getTime()
                  const end = t.completed_at ? new Date(t.completed_at + 'Z').getTime() : now
                  const span = now - windowStart
                  const width = Math.max(0.3, ((end - start) / span) * 100)
                  const left = Math.max(0, ((start - windowStart) / span) * 100)
                  const dur = end - start
                  const median = medians[t.job_id] || dur
                  const isOutlier = dur > median * 3 && dur > 60000
                  const isError = t.error != null

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
  // Generate ~5 evenly-spaced time ticks
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

/* ── Agent Dispatch Chains ── */

function DispatchChains({ chains, jobColorMap }) {
  if (!chains || chains.length === 0) {
    return (
      <section className="dashboard-section">
        <h3>Agent Dispatch Chains</h3>
        <p className="muted-text">No agent-dispatched tasks in this window</p>
      </section>
    )
  }

  // Build dispatch pattern: which jobs dispatch which
  // trigger_detail for agent tasks is "source_job_id#source_task_id"
  const patterns = {}  // "source_job -> target_job" -> count
  const taskJobMap = {}  // task_id -> job_id (from the chains data)
  chains.forEach(t => { taskJobMap[t.id] = t.job_id })

  /** Parse agent trigger_detail ("job-slug#123") → { jobId, taskId } or null */
  function parseAgentDetail(detail) {
    if (!detail) return null
    const idx = detail.lastIndexOf('#')
    if (idx < 1) return null
    const taskId = parseInt(detail.slice(idx + 1))
    if (isNaN(taskId)) return null
    return { jobId: detail.slice(0, idx), taskId }
  }

  // For chain depth, trace back through trigger_detail
  const depths = {}  // task_id -> depth
  const maxDepthByJob = {}

  chains.forEach(t => {
    const parsed = parseAgentDetail(t.trigger_detail)
    // Source job: prefer the explicit job ID from trigger_detail, fall back to task map
    const sourceJob = parsed ? parsed.jobId : null
    const key = `${sourceJob || '?'} -> ${t.job_id}`
    patterns[key] = (patterns[key] || 0) + 1

    // Estimate depth by tracing the dispatch chain
    let depth = 1
    let curTaskId = parsed ? parsed.taskId : null
    const visited = new Set()
    while (curTaskId && !visited.has(curTaskId)) {
      visited.add(curTaskId)
      if (taskJobMap[curTaskId]) {
        depth++
        const parent = chains.find(c => c.id === curTaskId)
        const parentParsed = parent ? parseAgentDetail(parent.trigger_detail) : null
        curTaskId = parentParsed ? parentParsed.taskId : null
      } else {
        break
      }
    }
    depths[t.id] = depth
    if (!maxDepthByJob[t.job_id] || depth > maxDepthByJob[t.job_id]) {
      maxDepthByJob[t.job_id] = depth
    }
  })

  // Aggregate job-to-job flows
  const flows = Object.entries(patterns)
    .map(([key, count]) => {
      const [source, target] = key.split(' -> ')
      return { source, target, count }
    })
    .sort((a, b) => b.count - a.count)

  const maxDepth = Math.max(...Object.values(depths), 1)

  return (
    <section className="dashboard-section">
      <h3>Agent Dispatch Chains</h3>
      <div className="chains-stats">
        <div className="chains-stat">
          <span className="chains-stat-value">{chains.length}</span>
          <span className="chains-stat-label">agent tasks</span>
        </div>
        <div className="chains-stat">
          <span className="chains-stat-value">{maxDepth}</span>
          <span className="chains-stat-label">max depth</span>
        </div>
      </div>
      {flows.length > 0 && (
        <div className="chains-flows">
          <h4>Dispatch Patterns</h4>
          {flows.map((f, i) => (
            <div key={i} className="chains-flow-row">
              <span className="chains-flow-source">{f.source}</span>
              <span className="chains-flow-arrow">&rarr;</span>
              <span className="chains-flow-target">{f.target}</span>
              <span className="chains-flow-count">&times;{f.count}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

/* ── Tool Usage Patterns ── */

function ToolUsage({ tools, jobColorMap }) {
  if (!tools || tools.length === 0) {
    return (
      <section className="dashboard-section">
        <h3>Tool Usage</h3>
        <p className="muted-text">No tool usage data in this window</p>
      </section>
    )
  }

  return (
    <section className="dashboard-section">
      <h3>Tool Usage</h3>
      <div className="tool-usage-grid">
        {tools.map(job => {
          const color = jobColor(job.job_id, jobColorMap)
          const toolEntries = Object.entries(job.tools)
            .sort((a, b) => b[1].count - a[1].count)
          const totalCalls = toolEntries.reduce((s, [, v]) => s + v.count, 0)
          const totalErrors = toolEntries.reduce((s, [, v]) => s + v.errors, 0)

          return (
            <div key={job.job_id} className="tool-usage-card">
              <div className="tool-usage-header">
                <span className="health-job-dot" style={{ background: color }} />
                <span className="tool-usage-job-name">{job.job_name}</span>
                <span className="tool-usage-summary">{totalCalls} calls{totalErrors > 0 && <span className="health-bad">, {totalErrors} errors</span>}</span>
              </div>
              <div className="tool-usage-list">
                {toolEntries.slice(0, 10).map(([name, stats]) => (
                  <div key={name} className="tool-usage-row">
                    <span className="tool-usage-name">{name}</span>
                    <span className="tool-usage-count">{stats.count}</span>
                    {stats.errors > 0 && <span className="tool-usage-errors">{stats.errors} err</span>}
                  </div>
                ))}
                {toolEntries.length > 10 && (
                  <div className="tool-usage-row muted-text">+{toolEntries.length - 10} more</div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}
