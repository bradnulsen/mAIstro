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
  coalesced: 'Coalesced',
  error: 'Error',
  cancelled: 'Cancelled',
}

function getStatus(item) {
  if (item.error) {
    if (item.error === 'cancelled') return 'cancelled'
    if (item.error.startsWith('coalesced')) return 'coalesced'
    return 'error'
  }
  if (item.completed_at) return 'completed'
  if (item.started_at) return 'running'
  return 'pending'
}

function isUpcoming(item) {
  const s = getStatus(item)
  return s === 'pending' || s === 'running'
}

export default function Queue({ tasks }) {
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
                      {TRIGGER_ICONS[item.trigger] || ''}
                    </span>
                    <span className={`queue-status ${status}`}>
                      {STATUS_LABELS[status]}
                    </span>
                    <span>{formatDate(item.created_at, true)}</span>
                  </div>
                  <div className="feed-message">
                    {item.context
                      ? item.context.length > 80 ? item.context.slice(0, 80) + '...' : item.context
                      : `${item.trigger} dispatch`}
                  </div>
                  {item.trigger_detail && (
                    <div className="feed-files">{item.trigger_detail}</div>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        {/* Detail panel */}
        {selected && (
          <div className="detail-panel" style={{ overflow: 'auto' }}>
            <div className="detail-panel-header">
              <h3>Dispatch Detail</h3>
              <button className="small" onClick={() => setSelected(null)}>✕</button>
            </div>

            <div style={{ fontSize: 11, color: '#888', marginBottom: 4 }}>
              #{selected.id} — {selected.task_name}
            </div>

            <div style={{ marginBottom: 12 }}>
              <label>Status</label>
              <span className={`queue-status ${getStatus(selected)}`} style={{ fontSize: 12 }}>
                {STATUS_LABELS[getStatus(selected)]}
              </span>
            </div>

            <div style={{ marginBottom: 12 }}>
              <label>Trigger</label>
              <div style={{ fontSize: 12 }}>
                {TRIGGER_ICONS[selected.trigger] || ''} {selected.trigger}
                {selected.trigger_detail && (
                  <span style={{ color: '#888' }}> — {selected.trigger_detail}</span>
                )}
              </div>
            </div>

            {selected.context && (
              <div style={{ marginBottom: 12 }}>
                <label>Context</label>
                <pre style={{
                  fontSize: 11, background: '#f5f5f0', padding: 8,
                  borderRadius: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                  border: '1px solid #ddd',
                }}>
                  {selected.context}
                </pre>
              </div>
            )}

            <div style={{ marginBottom: 12 }}>
              <label>Timeline</label>
              <div style={{ fontSize: 11 }}>
                <div>Created: {formatDate(selected.created_at, true)}</div>
                {selected.started_at && <div>Started: {formatDate(selected.started_at, true)}</div>}
                {selected.completed_at && <div>Completed: {formatDate(selected.completed_at, true)}</div>}
              </div>
            </div>

            {selected.result_commit && (
              <div style={{ marginBottom: 12 }}>
                <label>Result Commit</label>
                <div style={{ fontSize: 11, fontFamily: 'monospace' }}>
                  {selected.result_commit.slice(0, 8)}
                </div>
              </div>
            )}

            {selected.error && getStatus(selected) !== 'coalesced' && (
              <div style={{ marginBottom: 12 }}>
                <label>Error</label>
                <div style={{ fontSize: 11, color: 'var(--danger)' }}>{selected.error}</div>
              </div>
            )}

            {getStatus(selected) === 'coalesced' && selected.error && (
              <div style={{ marginBottom: 12 }}>
                <label>Note</label>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', fontStyle: 'italic' }}>
                  {selected.error}
                </div>
              </div>
            )}

            {(() => {
              const assistantMsgs = output?.messages?.filter(m => m.role === 'assistant') ?? []
              if (assistantMsgs.length === 0) return null
              return (
                <div style={{ marginBottom: 12 }}>
                  <label>Output</label>
                  {assistantMsgs.map((msg, i) => {
                    const content = typeof msg.content === 'string'
                      ? msg.content
                      : Array.isArray(msg.content)
                        ? msg.content.filter(b => b.type === 'text').map(b => b.text).join('\n\n')
                        : String(msg.content ?? '')
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
              )
            })()}

            {isUpcoming(selected) && (
              <button className="danger small" onClick={() => handleCancel(selected.id)}>
                Cancel Dispatch
              </button>
            )}
          </div>
        )}
      </div>
    </>
  )
}


