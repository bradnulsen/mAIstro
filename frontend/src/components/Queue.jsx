import { useState, useEffect, useCallback } from 'react'
import Markdown from 'react-markdown'
import {
  getDispatchQueue, cancelDispatch, updateDispatch, getDispatchOutput,
  getQueueSettings, setQueueSettings, processQueue, streamDispatch,
} from '../api'
import { formatDate, formatDuration, TRIGGER_ICONS, mdBreaks } from '../util'

const STATUS_LABELS = {
  pending: 'Pending',
  running: 'Running',
  completed: 'Completed',
  error: 'Error',
  cancelled: 'Cancelled',
  timed_out: 'Timed Out',
}

const TRIGGER_LABELS = {
  manual: 'User',
  commit: 'Commit',
  task_queue: 'Task',
}

function getStatus(item) {
  if (item.error) {
    if (item.error === 'cancelled') return 'cancelled'
    if (item.error === 'timed out') return 'timed_out'
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

  const [refreshing, setRefreshing] = useState(false)

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

  const handleRefresh = useCallback(async () => {
    setRefreshing(true)
    await refresh()
    setRefreshing(false)
  }, [refresh])

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

  // Live stream state for running dispatches
  const [liveText, setLiveText] = useState('')
  const [liveTools, setLiveTools] = useState([])
  const [isStreaming, setIsStreaming] = useState(false)

  // Load output when selection changes — SSE for running, stored for completed
  useEffect(() => {
    if (!selected) { setOutput(null); setLiveText(''); setLiveTools([]); setIsStreaming(false); return }
    let cancelled = false
    let sseHandle = null

    const status = getStatus(selected)
    if (status === 'running') {
      // Subscribe to live SSE stream
      setOutput(null)
      setLiveText('')
      setLiveTools([])
      setIsStreaming(true)

      try {
        const { abort, done } = streamDispatch(selected.id, (event) => {
          if (cancelled) return
          const type = event.type
          if (type === 'text') {
            setLiveText(prev => prev + (event.content || ''))
          } else if (type === 'tool_use') {
            setLiveTools(prev => [...prev, event.tool || '?'])
          } else if (type === 'done') {
            // Stream finished — load stored output
            setIsStreaming(false)
            getDispatchOutput(selected.id).then(data => {
              if (!cancelled) setOutput(data)
            }).catch(() => {})
          } else if (type === 'error') {
            setIsStreaming(false)
          }
        })
        sseHandle = { abort }
        done.catch(() => {
          // SSE failed — fall back to stored output
          if (!cancelled) {
            setIsStreaming(false)
            getDispatchOutput(selected.id).then(data => {
              if (!cancelled) setOutput(data)
            }).catch(() => {})
          }
        })
      } catch {
        // fetchSSE setup failed
        setIsStreaming(false)
      }

      return () => { cancelled = true; if (sseHandle) sseHandle.abort() }
    } else {
      // Completed/pending — load stored output
      setLiveText('')
      setLiveTools([])
      setIsStreaming(false)
      const load = async () => {
        try {
          const data = await getDispatchOutput(selected.id)
          if (!cancelled) setOutput(data)
        } catch { if (!cancelled) setOutput(null) }
      }
      load()
      return () => { cancelled = true }
    }
  }, [selected?.id, selected?.started_at, selected?.completed_at])

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
        <button className="small" onClick={handleRefresh} disabled={refreshing}>
          {refreshing ? <span className="tool-spinner" /> : '↻'}
        </button>
        <div style={{ borderLeft: '1px solid var(--border-light)', height: 16, margin: '0 4px' }} />
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
                      : `${TRIGGER_LABELS[item.trigger] || item.trigger} dispatch`}
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
              <DispatchDetail item={selected} output={output} onUpdate={refresh}
                liveText={liveText} liveTools={liveTools} isStreaming={isStreaming} />
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

function DispatchDetail({ item, output, onUpdate, liveText, liveTools, isStreaming }) {
  const status = getStatus(item)
  const triggers = getTriggers(item)
  const assistantMsgs = output?.messages?.filter(m => m.role === 'assistant') ?? []
  const isPending = status === 'pending'

  // Editable context for pending dispatches
  const lastCtx = triggers.length > 0 ? (triggers[triggers.length - 1].context || '') : ''
  const [editingContext, setEditingContext] = useState(null)
  const [saveError, setSaveError] = useState('')

  // Reset editing state when item changes
  useEffect(() => { setEditingContext(null); setSaveError('') }, [item.id])

  const handleSaveContext = async () => {
    if (editingContext === null) return
    setSaveError('')
    try {
      await updateDispatch(item.id, { context: editingContext })
      setEditingContext(null)
      if (onUpdate) await onUpdate()
    } catch (e) { setSaveError(e.message) }
  }

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
        <label>Context</label>
        {isPending && editingContext !== null ? (
          <>
            <textarea
              value={editingContext}
              onChange={e => setEditingContext(e.target.value)}
              className="context-editor"
              autoFocus
            />
            <div style={{ display: 'flex', gap: 4, marginTop: 4, alignItems: 'center' }}>
              <button className="small primary" onClick={handleSaveContext}>Save</button>
              <button className="small" onClick={() => { setEditingContext(null); setSaveError('') }}>Cancel</button>
              {saveError && <span style={{ fontSize: 11, color: 'var(--danger)' }}>{saveError}</span>}
            </div>
          </>
        ) : isPending ? (
          <pre
            onClick={() => setEditingContext(lastCtx)}
            className="context-pending"
            style={{
              color: lastCtx ? 'inherit' : 'var(--text-muted)',
              fontStyle: lastCtx ? 'normal' : 'italic',
            }}
            title="Click to edit context"
          >
            {lastCtx || 'click to add context...'}
          </pre>
        ) : (
          triggers.map((entry, i) => (
            <pre key={i} className="context-display" style={{
              marginBottom: triggers.length > 1 ? 4 : 0,
              color: entry.context ? 'inherit' : 'var(--text-muted)',
              fontStyle: entry.context ? 'normal' : 'italic',
            }}>
              {entry.context || 'not provided'}
            </pre>
          ))
        )}
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
          <label>{status === 'timed_out' ? 'Timed Out' : 'Error'}</label>
          <div style={{ fontSize: 11, color: status === 'timed_out' ? 'var(--warning, #e6a117)' : 'var(--danger)' }}>
            {status === 'timed_out' ? 'Dispatch exceeded timeout limit' : item.error}
          </div>
        </div>
      )}

      {/* Live streaming output — visible while streaming, or while liveText exists but stored output hasn't loaded yet */}
      {(isStreaming || (liveText && !assistantMsgs.length)) && (
        <div style={{ marginBottom: 12 }}>
          <label>
            Live Output
            {isStreaming && <span style={{ marginLeft: 6, color: 'var(--running)', fontSize: 10, fontWeight: 'normal' }}>● streaming</span>}
          </label>
          {liveTools.length > 0 && (
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6 }}>
              Tools: {liveTools.map((t, i) => (
                <span key={i} style={{
                  display: 'inline-block',
                  background: 'var(--surface)',
                  border: '1px solid var(--border-light)',
                  borderRadius: 3,
                  padding: '1px 5px',
                  marginRight: 4,
                  marginBottom: 2,
                  fontSize: 10,
                }}>{t}</span>
              ))}
            </div>
          )}
          {liveText ? (
            <div className="md-content" style={{
              fontSize: 12,
              padding: '8px 10px',
              background: 'var(--surface)',
              border: '1px solid var(--border-light)',
              borderRadius: 4,
            }}>
              <Markdown>{mdBreaks(liveText)}</Markdown>
            </div>
          ) : isStreaming ? (
            <div style={{ fontSize: 11, color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
              <span className="tool-spinner" /> connecting...
            </div>
          ) : null}
        </div>
      )}

      {/* Stored output (after completion) */}
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
