import { useState, useEffect, useCallback } from 'react'
import Markdown from 'react-markdown'
import {
  getDispatchQueue, cancelDispatch, getDispatchOutput,
  getQueueSettings, setQueueSettings, processQueue,
} from '../api'
import { formatDate, TRIGGER_ICONS, mdBreaks } from '../util'

const STATUS_LABELS = {
  pending: 'Pending',
  running: 'Running',
  completed: 'Completed',
  error: 'Error',
  cancelled: 'Cancelled',
}

const TRIGGER_LABELS = {
  manual: 'User',
  commit: 'Commit',
  task_queue: 'Task',
}

function getStatus(item) {
  if (item.error) {
    if (item.error === 'cancelled') return 'cancelled'
    return 'error'
  }
  if (item.completed_at) return 'completed'
  if (item.started_at) return 'running'
  return 'pending'
}

function formatDuration(startStr, endStr) {
  const start = new Date(startStr + 'Z')
  const end = endStr ? new Date(endStr + 'Z') : new Date()
  const secs = Math.max(0, Math.round((end - start) / 1000))
  if (secs < 60) return `${secs}s`
  const mins = Math.floor(secs / 60)
  const remSecs = secs % 60
  if (mins < 60) return `${mins}m ${remSecs}s`
  const hrs = Math.floor(mins / 60)
  const remMins = mins % 60
  return `${hrs}h ${remMins}m`
}

function isUpcoming(item) {
  const s = getStatus(item)
  return s === 'pending' || s === 'running'
}

/** Normalize the triggers array, falling back to scalar fields for older records. */
function getTriggers(item) {
  if (item.triggers && item.triggers.length > 0) return item.triggers
  return [{ trigger: item.trigger, detail: item.trigger_detail, context: item.context }]
}

function triggerLabel(entry) {
  const base = TRIGGER_LABELS[entry.trigger] || entry.trigger
  if (entry.trigger === 'commit' && entry.detail) return `${base} (${entry.detail.slice(0, 8)})`
  if (entry.trigger === 'task_queue' && entry.detail) return `${base} (${entry.detail})`
  if (entry.trigger === 'manual' && entry.detail) return `${base} @ ${entry.detail.slice(0, 8)}`
  return base
}

function getMessageContent(msg) {
  if (typeof msg.content === 'string') return msg.content
  if (Array.isArray(msg.content)) return msg.content.filter(b => b.type === 'text').map(b => b.text).join('\n\n')
  return String(msg.content ?? '')
}

export default function Queue() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [filter, setFilter] = useState('upcoming') // 'upcoming' | 'past'
  const [autoDispatch, setAutoDispatch] = useState(false)
  const [output, setOutput] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const queue = await getDispatchQueue()
      setItems(queue)
      // Keep selected item in sync with fresh data
      setSelected(prev => {
        if (!prev) return null
        const updated = queue.find(q => q.id === prev.id)
        return updated || null
      })
    } catch {}
    setLoading(false)
  }, [])

  useEffect(() => { refresh() }, [refresh])

  // Load queue settings
  useEffect(() => {
    getQueueSettings().then(s => setAutoDispatch(s.auto_dispatch)).catch(() => {})
  }, [])

  // Auto-refresh every 5s
  useEffect(() => {
    const interval = setInterval(refresh, 5000)
    return () => clearInterval(interval)
  }, [refresh])

  // Load output when selection changes
  useEffect(() => {
    if (!selected) { setOutput(null); return }
    let cancelled = false
    const load = async () => {
      try {
        const data = await getDispatchOutput(selected.id)
        if (!cancelled) setOutput(data)
      } catch { if (!cancelled) setOutput(null) }
    }
    load()
    // Poll if running
    const status = getStatus(selected)
    if (status === 'running') {
      const interval = setInterval(load, 2000)
      return () => { cancelled = true; clearInterval(interval) }
    }
    return () => { cancelled = true }
  }, [selected, selected?.completed_at])

  const handleToggleAuto = async (val) => {
    await setQueueSettings({ auto_dispatch: val })
    setAutoDispatch(val)
  }

  const handleProcess = async (all) => {
    await processQueue(all)
    await refresh()
  }

  const handleCancel = async (id) => {
    try {
      await cancelDispatch(id)
      await refresh()
      if (selected?.id === id) setSelected(null)
    } catch {}
  }

  const filtered = items.filter(item =>
    filter === 'upcoming' ? isUpcoming(item) : !isUpcoming(item)
  )
  if (filter === 'past') {
    filtered.sort((a, b) => (b.completed_at || '').localeCompare(a.completed_at || ''))
  } else {
    filtered.sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''))
  }

  return (
    <>
      <div className="header-bar">
        <h1>Queue</h1>
        <div className="spacer" />
        <div className="mode-toggle">
          <button
            className={filter === 'upcoming' ? 'active' : ''}
            onClick={() => { setFilter('upcoming'); setSelected(null) }}
          >Upcoming</button>
          <button
            className={filter === 'past' ? 'active' : ''}
            onClick={() => { setFilter('past'); setSelected(null) }}
          >Past</button>
        </div>
        <button className="small" onClick={refresh}>↻</button>
        <div style={{ borderLeft: '1px solid #ccc', height: 16, margin: '0 4px' }} />
        <label className="checkbox-label" style={{ fontSize: 11, marginBottom: 0, width: 'auto' }}>
          <input type="checkbox" checked={autoDispatch} onChange={e => handleToggleAuto(e.target.checked)} />
          Auto
        </label>
        {!autoDispatch && (
          <>
            <button className="small primary" onClick={() => handleProcess(false)}>▶ Next</button>
            <button className="small" onClick={() => handleProcess(true)}>▶▶ All</button>
          </>
        )}
      </div>

      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {/* Queue list */}
        <div className="feed-list" style={{ flex: 1 }}>
          {loading && <div className="loading">Loading queue...</div>}
          {!loading && filtered.length === 0 && (
            <div className="empty-state">
              {filter === 'upcoming' ? 'No pending or running dispatches' : 'No past dispatches'}
            </div>
          )}
          {filtered.map(item => {
            const status = getStatus(item)
            const triggers = getTriggers(item)
            const triggerTypes = [...new Set(triggers.map(t => t.trigger))]
            // First context that has content, for preview
            const previewCtx = triggers.find(t => t.context)?.context
            const triggerCount = triggers.length > 1 ? ` (${triggers.length})` : ''
            return (
              <div
                key={item.id}
                className={`feed-item ${selected?.id === item.id ? 'active' : ''}`}
                onClick={() => setSelected(item)}
              >
                <div className="feed-avatar">
                  {(item.task_name || '?')[0].toUpperCase()}
                </div>
                <div className="feed-body">
                  <div className="feed-meta">
                    <span className="feed-author">{item.task_name}</span>
                    <span className="feed-trigger">
                      {triggerTypes.map(t => TRIGGER_ICONS[t] || '').join('')}{triggerCount}
                    </span>
                    <span className={`queue-status ${status}`}>
                      {STATUS_LABELS[status]}
                    </span>
                    <span>{formatDate(isUpcoming(item) ? item.created_at : (item.completed_at || item.created_at), true)}</span>
                  </div>
                  <div className="feed-message">
                    {previewCtx
                      ? previewCtx.length > 80 ? previewCtx.slice(0, 80) + '...' : previewCtx
                      : `${item.trigger} dispatch`}
                  </div>
                </div>
              </div>
            )
          })}
        </div>

        {/* Detail panel */}
        {selected && (
          <div className="detail-panel">
            <div className="detail-panel-header">
              <h3>#{selected.id} — {selected.task_name}</h3>
              <button className="small" onClick={() => setSelected(null)}>✕</button>
            </div>

            <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
              <DispatchDetail item={selected} output={output} />
            </div>

            {isUpcoming(selected) && (
              <div style={{ borderTop: '1px solid var(--border-light)', paddingTop: 12, flexShrink: 0 }}>
                <button className="danger small" onClick={() => handleCancel(selected.id)}>
                  Cancel Dispatch
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}

function DispatchDetail({ item, output }) {
  const status = getStatus(item)
  const triggers = getTriggers(item)
  const assistantMsgs = output?.messages?.filter(m => m.role === 'assistant') ?? []

  // Tick every second while running so duration stays current
  const [, setTick] = useState(0)
  useEffect(() => {
    if (status !== 'running') return
    const interval = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(interval)
  }, [status])

  return (
    <>
      <div style={{ marginBottom: 12 }}>
        <label>Status</label>
        <span className={`queue-status ${status}`} style={{ fontSize: 12 }}>
          {STATUS_LABELS[status]}
        </span>
      </div>

      <div style={{ marginBottom: 12 }}>
        <label>Queued by</label>
        <div style={{ fontSize: 12 }}>
          {triggers.map((entry, i) => (
            <div key={i}>
              {TRIGGER_ICONS[entry.trigger] || ''} {triggerLabel(entry)}
            </div>
          ))}
        </div>
      </div>

      <div style={{ marginBottom: 12 }}>
        <label>Dispatched</label>
        <div style={{ fontSize: 12 }}>
          {item.dispatch_method || '—'}
        </div>
      </div>

      <div style={{ marginBottom: 12 }}>
        <label>Context</label>
        {triggers.map((entry, i) => (
          <pre key={i} style={{
            fontSize: 11, background: '#f5f5f0', padding: 8,
            borderRadius: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            border: '1px solid #ddd', marginBottom: triggers.length > 1 ? 4 : 0,
            color: entry.context ? 'inherit' : '#aaa',
            fontStyle: entry.context ? 'normal' : 'italic',
          }}>
            {entry.context || 'not provided'}
          </pre>
        ))}
      </div>

      <div style={{ marginBottom: 12 }}>
        <label>Timeline</label>
        <div style={{ fontSize: 11 }}>
          <div>Created: {formatDate(item.created_at, true)}</div>
          {item.started_at && <div>Started: {formatDate(item.started_at, true)}</div>}
          {item.completed_at && <div>Completed: {formatDate(item.completed_at, true)}</div>}
          {item.started_at && item.completed_at && (
            <div style={{ marginTop: 2, color: 'var(--text)' }}>
              Duration: {formatDuration(item.started_at, item.completed_at)}
            </div>
          )}
          {item.started_at && !item.completed_at && (
            <div style={{ marginTop: 2, color: 'var(--running)' }}>
              Running for {formatDuration(item.started_at)}
            </div>
          )}
        </div>
      </div>

      {item.result_commit && (
        <div style={{ marginBottom: 12 }}>
          <label>Result Commit</label>
          <div style={{ fontSize: 11, fontFamily: 'monospace' }}>
            {item.result_commit.slice(0, 8)}
          </div>
        </div>
      )}

      {item.error && status !== 'cancelled' && (
        <div style={{ marginBottom: 12 }}>
          <label>Error</label>
          <div style={{ fontSize: 11, color: 'var(--danger)' }}>{item.error}</div>
        </div>
      )}

      {assistantMsgs.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <label>Output</label>
          {assistantMsgs.map((msg, i) => {
            const content = getMessageContent(msg)
            if (!content.trim()) return null
            return (
              <div key={i} className="md-content" style={{
                fontSize: 12,
                padding: '8px 10px',
                background: 'var(--surface)',
                border: '1px solid var(--border-light)',
                borderRadius: 4,
                marginBottom: 6,
              }}>
                <Markdown>{mdBreaks(content)}</Markdown>
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
