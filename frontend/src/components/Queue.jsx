import { useState, useEffect, useCallback, useRef } from 'react'
import Markdown from 'react-markdown'
import {
  getDispatchQueue, cancelDispatch, updateDispatch, getDispatchOutput,
  getDispatchDiff, getQueueSettings, setQueueSettings, processQueue, processOne,
  streamDispatch, resumeDispatch, retryDispatch, approveDispatch, rejectDispatch,
} from '../api'
import { formatDate, formatDuration, TRIGGER_ICONS, mdBreaks } from '../util'

const STATUS_LABELS = {
  pending: 'Pending',
  pending_approval: 'Needs Approval',
  running: 'Running',
  completed: 'Completed',
  error: 'Error',
  cancelled: 'Cancelled',
  timed_out: 'Timed Out',
  rejected: 'Rejected',
}

const TRIGGER_LABELS = {
  manual: 'User',
  commit: 'Commit',
  task_queue: 'Task',
  resume: 'Resume',
  retry: 'Retry',
  dependency: 'Dependency',
  schedule: 'Schedule',
}

function getStatus(item) {
  if (item.error) {
    if (item.error === 'cancelled') return 'cancelled'
    if (item.error === 'timed out') return 'timed_out'
    if (item.error === 'rejected') return 'rejected'
    return 'error'
  }
  if (item.completed_at) return 'completed'
  if (item.started_at) return 'running'
  if (item.approval === 'pending') return 'pending_approval'
  return 'pending'
}

function isUpcoming(item) {
  const s = getStatus(item)
  return s === 'pending' || s === 'running' || s === 'pending_approval'
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
  if (entry.trigger === 'dependency' && entry.detail) return `${base} (${entry.detail})`
  if (entry.trigger === 'manual' && entry.detail) return `${base} @ ${entry.detail.slice(0, 8)}`
  if (entry.trigger === 'schedule' && entry.detail) return `${base} (${entry.detail})`
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
  const [confirmCancel, setConfirmCancel] = useState(null)
  const [actionError, setActionError] = useState('')

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
      setLoading(false)
      return queue
    } catch {}
    setLoading(false)
    return []
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

  // Clear transient action error when selection changes
  useEffect(() => { setActionError(''); setRetryContext(null); setConfirmCancel(null) }, [selected?.id])

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
            setLiveTools(prev => {
              const tool = event.tool || '?'
              return prev.includes(tool) ? prev : [...prev, tool]
            })
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

  const handleProcessOne = async (id) => {
    try {
      await processOne(id)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleApprove = async (id) => {
    setActionError('')
    try {
      await approveDispatch(id)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleReject = async (id) => {
    setActionError('')
    try {
      await rejectDispatch(id)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleCancel = async (id) => {
    try {
      await cancelDispatch(id)
      setConfirmCancel(null)
      await refresh()
      if (selected?.id === id) setSelected(null)
    } catch {}
  }

  const handleResume = async (id) => {
    setActionError('')
    try {
      const result = await resumeDispatch(id)
      const queue = await refresh()
      const newItem = queue.find(q => q.id === result.dispatch_id)
      if (newItem) { setSelected(newItem); setFilter('upcoming') }
    } catch (e) {
      setActionError(e.message)
    }
  }

  const [retryContext, setRetryContext] = useState(null) // null = not editing

  const handleRetry = async (id, context) => {
    setActionError('')
    try {
      const result = await retryDispatch(id, context)
      setRetryContext(null)
      const queue = await refresh()
      const newItem = queue.find(q => q.id === result.dispatch_id)
      if (newItem) { setSelected(newItem); setFilter('upcoming') }
    } catch (e) {
      setActionError(e.message)
    }
  }

  // Escape: dismiss dialogs in order, then close detail panel
  useEffect(() => {
    const handleKey = (e) => {
      if (e.key !== 'Escape') return
      if (confirmCancel !== null) { setConfirmCancel(null); return }
      if (retryContext !== null) { setRetryContext(null); return }
      if (selected) setSelected(null)
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [selected, confirmCancel, retryContext])

  const anyRunning = items.some(i => getStatus(i) === 'running')
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
            onClick={() => { if (filter !== 'upcoming') { setFilter('upcoming'); setSelected(null) } }}
          >Upcoming</button>
          <button
            className={filter === 'past' ? 'active' : ''}
            onClick={() => { if (filter !== 'past') { setFilter('past'); setSelected(null) } }}
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
                className={`feed-item ${selected?.id === item.id ? 'active' : ''} ${status === 'running' ? 'running' : ''} ${status === 'pending_approval' ? 'pending_approval' : ''}`}
                onClick={() => setSelected(item)}
              >
                <div className="feed-avatar">
                  {(item.task_name || '?')[0].toUpperCase()}
                </div>
                <div className="feed-body">
                  <div className="feed-meta">
                    <span className="feed-trigger">
                      {triggerTypes.map(t => TRIGGER_ICONS[t] || '').join('')}{triggerCount}
                    </span>
                    <span className="feed-author">{item.task_name}</span>
                    <span className={`queue-status ${status}`}>
                      {STATUS_LABELS[status]}
                    </span>
                    <span>{formatDate(isUpcoming(item) ? item.created_at : (item.completed_at || item.created_at), true)}</span>
                  </div>
                  <div className="feed-message">
                    {previewCtx || `${TRIGGER_LABELS[item.trigger] || item.trigger} dispatch`}
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
                {confirmCancel === selected.id ? (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 12 }}>Cancel this dispatch?</span>
                    <button className="danger small" onClick={() => handleCancel(selected.id)}>Confirm</button>
                    <button className="small" onClick={() => setConfirmCancel(null)}>No</button>
                  </div>
                ) : (
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    {getStatus(selected) === 'pending_approval' && (
                      <>
                        <button
                          className="small primary"
                          onClick={() => handleApprove(selected.id)}
                        >
                          ✓ Approve
                        </button>
                        <button
                          className="danger small"
                          onClick={() => handleReject(selected.id)}
                        >
                          ✕ Reject
                        </button>
                      </>
                    )}
                    {getStatus(selected) === 'pending' && (
                      <button
                        className="small primary"
                        onClick={() => handleProcessOne(selected.id)}
                        disabled={anyRunning}
                        title={anyRunning ? 'Another dispatch is running' : 'Run this dispatch now'}
                      >
                        ▶ Run Now
                      </button>
                    )}
                    <button className="danger small" onClick={() => setConfirmCancel(selected.id)}>
                      Cancel
                    </button>
                    {actionError && (
                      <span style={{ fontSize: 11, color: 'var(--danger)' }}>{actionError}</span>
                    )}
                  </div>
                )}
              </div>
            )}
            {!isUpcoming(selected) && (
              <div style={{ borderTop: '1px solid var(--border-light)', paddingTop: 12, flexShrink: 0 }}>
                {retryContext !== null ? (
                  <>
                    <label style={{ fontSize: 11 }}>Edit context before retrying</label>
                    <ContextEditor value={retryContext} onChange={e => setRetryContext(e.target.value)} autoFocus />
                    <div style={{ display: 'flex', gap: 4, marginTop: 4, alignItems: 'center' }}>
                      <button className="small primary" onClick={() => handleRetry(selected.id, retryContext)}>
                        ↺ Retry
                      </button>
                      <button className="small" onClick={() => setRetryContext(null)}>Cancel</button>
                    </div>
                  </>
                ) : (
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    {selected.error && selected.error !== 'cancelled' && (
                      <button
                        className="small primary"
                        onClick={() => handleResume(selected.id)}
                        title="Continue from the last Claude session checkpoint (--resume)"
                      >
                        ↻ Resume
                      </button>
                    )}
                    <button
                      className="small"
                      onClick={() => setRetryContext(getTriggers(selected).at(-1)?.context || '')}
                      title="Queue a fresh dispatch — edit context first"
                    >
                      ↺ Retry
                    </button>
                  </div>
                )}
                {actionError && (
                  <div style={{ fontSize: 11, color: 'var(--danger)', marginTop: 6 }}>{actionError}</div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}

function ContextEditor({ value, onChange, autoFocus = false }) {
  const ref = useRef(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 200) + 'px'
  }, [value])
  return (
    <textarea
      ref={ref}
      value={value}
      onChange={onChange}
      className="context-editor"
      autoFocus={autoFocus}
    />
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

  // Diff state
  const [diffData, setDiffData] = useState(null)
  const [diffOpen, setDiffOpen] = useState(false)
  const [diffLoading, setDiffLoading] = useState(false)

  // Reset editing state when item changes
  useEffect(() => { setEditingContext(null); setSaveError(''); setDiffData(null); setDiffOpen(false) }, [item.id])

  // Load diff when opened (lazy)
  useEffect(() => {
    if (!diffOpen || diffData || diffLoading) return
    if (!item.start_commit || !item.result_commit || item.start_commit === item.result_commit) return
    let cancelled = false
    setDiffLoading(true)
    getDispatchDiff(item.id).then(data => {
      if (!cancelled) setDiffData(data)
    }).catch(() => {}).finally(() => {
      if (!cancelled) setDiffLoading(false)
    })
    return () => { cancelled = true }
  }, [diffOpen, item.id, item.start_commit, item.result_commit])

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
            <ContextEditor value={editingContext} onChange={e => setEditingContext(e.target.value)} autoFocus />
            <div style={{ display: 'flex', gap: 4, marginTop: 4, alignItems: 'center' }}>
              <button className="small primary" onClick={handleSaveContext}>Save</button>
              <button className="small" onClick={() => { setEditingContext(null); setSaveError('') }}>Cancel</button>
              {saveError && <span style={{ fontSize: 11, color: 'var(--danger)' }}>{saveError}</span>}
            </div>
          </>
        ) : isPending ? (
          <div className="context-pending-wrap">
            <pre
              onClick={() => setEditingContext(lastCtx)}
              className="context-pending"
              style={{
                color: lastCtx ? 'inherit' : 'var(--text-muted)',
                fontStyle: lastCtx ? 'normal' : 'italic',
              }}
            >
              {lastCtx || 'click to add context...'}
            </pre>
            <span className="context-edit-hint">✎</span>
          </div>
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
          <label>Commits</label>
          <div style={{ fontSize: 11, fontFamily: 'monospace' }}>
            {item.start_commit
              ? `${item.start_commit.slice(0, 8)}..${item.result_commit.slice(0, 8)}`
              : item.result_commit.slice(0, 8)}
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
            {isStreaming && <span style={{ marginLeft: 6, color: 'var(--running)', fontSize: 10, fontWeight: 'normal' }}>
              <span style={{ display: 'inline-block', animation: 'dot-pulse 1.4s ease-in-out infinite' }}>●</span> streaming
            </span>}
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

      {/* Dispatch diff — collapsible, for completed dispatches with commit range */}
      {item.start_commit && item.result_commit && item.start_commit !== item.result_commit && (
        <div style={{ marginBottom: 12 }}>
          <label
            onClick={() => setDiffOpen(o => !o)}
            style={{ cursor: 'pointer', userSelect: 'none', display: 'flex', alignItems: 'center', gap: 4 }}
          >
            <span style={{ fontSize: 10, display: 'inline-block', transform: diffOpen ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s' }}>▶</span>
            Code Changes
            {diffData && diffData.files.length > 0 && (
              <span style={{ fontWeight: 'normal', fontSize: 11, color: 'var(--text-muted)' }}>
                {diffData.files.length} {diffData.files.length === 1 ? 'file' : 'files'}
                {diffData.insertions > 0 && <span className="feed-stat-add" style={{ marginLeft: 4 }}>+{diffData.insertions}</span>}
                {diffData.deletions > 0 && <span className="feed-stat-del" style={{ marginLeft: 2 }}>-{diffData.deletions}</span>}
              </span>
            )}
          </label>
          {diffOpen && (
            <>
              {diffLoading && (
                <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '4px 0' }}>Loading diff...</div>
              )}
              {diffData && diffData.files.length > 0 && (
                <>
                  <div style={{ marginBottom: 8 }}>
                    {diffData.files.map(f => (
                      <div key={f.path} style={{ fontSize: 11, padding: '1px 0', display: 'flex', gap: 6 }}>
                        <span style={{ flex: 1 }}>{f.path}</span>
                        {f.insertions > 0 && <span className="feed-stat-add">+{f.insertions}</span>}
                        {f.deletions > 0 && <span className="feed-stat-del">-{f.deletions}</span>}
                      </div>
                    ))}
                  </div>
                  <pre className="diff-view">
                    {diffData.diff.split('\n').map((line, i) => (
                      <div key={i} className={
                        line.startsWith('+') ? 'diff-add' :
                        line.startsWith('-') ? 'diff-del' :
                        line.startsWith('@@') ? 'diff-hunk' : ''
                      }>{line}</div>
                    ))}
                  </pre>
                </>
              )}
              {diffData && diffData.files.length === 0 && (
                <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '4px 0' }}>No changes</div>
              )}
            </>
          )}
        </div>
      )}
    </>
  )
}
