import { useState, useEffect, useCallback, useRef, useLayoutEffect } from 'react'
import Markdown from 'react-markdown'
import {
  getDispatchQueue, cancelDispatch, updateDispatch, getDispatchOutput,
  getDispatchDiff, getDispatchOutcome, getQueueSettings, setQueueSettings, processOne,
  streamDispatch, resumeDispatch, retryDispatch, approveDispatch, rejectDispatch,
  reorderDispatches, mergeDispatches, splitDispatch, rateDispatch,
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

function getTriggers(item) {
  return item.triggers || []
}

function triggerLabel(entry) {
  const base = TRIGGER_LABELS[entry.trigger] || entry.trigger
  if (entry.trigger === 'commit' && entry.detail) return `${base} (${entry.detail.slice(0, 8)})`
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
  const detailScrollRef = useRef(null)
  const [dragIdx, setDragIdx] = useState(null)
  const [dragOverIdx, setDragOverIdx] = useState(null)
  const [dropZone, setDropZone] = useState(null) // 'reorder-before' | 'reorder-after' | 'merge'

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

  const handleMergePair = async (draggedId, targetId) => {
    setActionError('')
    try {
      await mergeDispatches([draggedId, targetId])
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleSplit = async (id) => {
    setActionError('')
    try {
      await splitDispatch(id)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleRate = async (id, rating) => {
    setActionError('')
    try {
      await rateDispatch(id, rating)
      await refresh()
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

  // Scroll detail panel to top when selection changes
  useLayoutEffect(() => {
    if (detailScrollRef.current) detailScrollRef.current.scrollTop = 0
  }, [selected?.id])

  // Auto-scroll detail panel to bottom while live streaming
  useEffect(() => {
    if (!liveText || !detailScrollRef.current) return
    const el = detailScrollRef.current
    el.scrollTop = el.scrollHeight
  }, [liveText])

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

  // Compute drop zone from cursor position within a row element
  const MERGE_ZONE_RATIO = 0.5 // central 50% is merge zone, top/bottom 25% each is reorder
  const computeDropZone = (e, rowEl, draggedItem, targetItem) => {
    const rect = rowEl.getBoundingClientRect()
    const y = e.clientY - rect.top
    const ratio = y / rect.height
    const edgeSize = (1 - MERGE_ZONE_RATIO) / 2
    // Merge zone only activates for same-job pending targets
    const canMerge = targetItem && draggedItem &&
      getStatus(targetItem) === 'pending' && getStatus(draggedItem) === 'pending' &&
      targetItem.task_id === draggedItem.task_id
    if (ratio < edgeSize) return 'reorder-before'
    if (ratio > 1 - edgeSize) return 'reorder-after'
    return canMerge ? 'merge' : (ratio < 0.5 ? 'reorder-before' : 'reorder-after')
  }

  const handleDragOver = (e, idx) => {
    e.preventDefault()
    if (dragIdx === null || dragIdx === idx) { setDragOverIdx(null); setDropZone(null); return }
    const draggedItem = filtered[dragIdx]
    const targetItem = filtered[idx]
    const zone = computeDropZone(e, e.currentTarget, draggedItem, targetItem)
    setDragOverIdx(idx)
    setDropZone(zone)
  }

  const handleDragEnd = async () => {
    if (dragIdx !== null && dragOverIdx !== null && dragIdx !== dragOverIdx) {
      if (dropZone === 'merge') {
        // Merge dragged task into drop target
        const draggedItem = filtered[dragIdx]
        const targetItem = filtered[dragOverIdx]
        await handleMergePair(draggedItem.id, targetItem.id)
      } else {
        // Reorder: insert at the target position
        const reordered = [...filtered]
        const [moved] = reordered.splice(dragIdx, 1)
        const insertIdx = dropZone === 'reorder-before'
          ? (dragOverIdx > dragIdx ? dragOverIdx - 1 : dragOverIdx)
          : (dragOverIdx < dragIdx ? dragOverIdx + 1 : dragOverIdx)
        reordered.splice(insertIdx, 0, moved)
        const pendingIds = reordered.filter(i => getStatus(i) !== 'running').map(i => i.id)
        try { await reorderDispatches(pendingIds); await refresh() } catch {}
      }
    }
    setDragIdx(null)
    setDragOverIdx(null)
    setDropZone(null)
  }

  const anyRunning = items.some(i => getStatus(i) === 'running')
  const filtered = items.filter(item =>
    filter === 'upcoming' ? isUpcoming(item) : !isUpcoming(item)
  )
  if (filter === 'past') {
    filtered.sort((a, b) => (b.completed_at || '').localeCompare(a.completed_at || ''))
  } else {
    // Pending dispatches: sort_order first (null last), then created_at; running items stay at top
    filtered.sort((a, b) => {
      const aRunning = getStatus(a) === 'running' ? 0 : 1
      const bRunning = getStatus(b) === 'running' ? 0 : 1
      if (aRunning !== bRunning) return aRunning - bRunning
      const aSort = a.sort_order ?? Infinity
      const bSort = b.sort_order ?? Infinity
      if (aSort !== bSort) return aSort - bSort
      return (a.created_at || '').localeCompare(b.created_at || '')
    })
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
        <div className="toolbar-divider" />
        <label className="checkbox-label">
          <input type="checkbox" checked={autoDispatch} onChange={e => handleToggleAuto(e.target.checked)} />
          Auto
        </label>
      </div>

      <div className="split-body">
        {/* Queue list */}
        <div className="feed-list">
          {loading && <div className="loading">Loading queue...</div>}
          {!loading && filtered.length === 0 && (
            <div className="empty-state">
              {filter === 'upcoming' ? 'No pending or running dispatches' : 'No past dispatches'}
            </div>
          )}
          {filtered.map((item, i) => {
            const status = getStatus(item)
            const triggers = getTriggers(item)
            const triggerTypes = [...new Set(triggers.map(t => t.trigger))]
            // First context that has content, for preview
            const previewCtx = triggers.find(t => t.context)?.context
            const triggerCount = triggers.length > 1 ? ` (${triggers.length})` : ''
            const draggable = filter === 'upcoming' && status !== 'running'
            const isDropTarget = dragOverIdx === i && dragIdx !== null && dragIdx !== i
            const dropClass = isDropTarget
              ? (dropZone === 'merge' ? ' drop-merge' : dropZone === 'reorder-before' ? ' drop-before' : ' drop-after')
              : ''
            return (
              <div
                key={item.id}
                className={`feed-item ${selected?.id === item.id ? 'active' : ''} ${status === 'running' ? 'running' : ''} ${status === 'pending_approval' ? 'pending_approval' : ''}${dragIdx === i ? ' dragging' : ''}${dropClass}`}
                onClick={() => setSelected(item)}
                draggable={draggable}
                onDragStart={draggable ? (e => { setDragIdx(i); e.dataTransfer.effectAllowed = 'move' }) : undefined}
                onDragOver={draggable ? (e => handleDragOver(e, i)) : undefined}
                onDragLeave={draggable ? (() => { setDragOverIdx(null); setDropZone(null) }) : undefined}
                onDragEnd={draggable ? handleDragEnd : undefined}
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

            <div ref={detailScrollRef} className="detail-scroll">
              <DispatchDetail item={selected} output={output} onUpdate={refresh}
                liveText={liveText} liveTools={liveTools} isStreaming={isStreaming}
                onSplit={handleSplit} onRate={handleRate} />
            </div>

            {isUpcoming(selected) && (
              <div className="detail-actions">
                {confirmCancel === selected.id ? (
                  <div className="action-row">
                    <span style={{ fontSize: 12 }}>Cancel this dispatch?</span>
                    <button className="danger small" onClick={() => handleCancel(selected.id)}>Confirm</button>
                    <button className="small" onClick={() => setConfirmCancel(null)}>No</button>
                  </div>
                ) : (
                  <div className="action-row">
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
                      <>
                        <button
                          className="small primary"
                          onClick={() => handleProcessOne(selected.id)}
                          disabled={anyRunning}
                          title={anyRunning ? 'Another dispatch is running' : 'Run this dispatch now'}
                        >
                          ▶ Run Now
                        </button>
                        {selected.subordinate_count > 0 && (
                          <button
                            className="small"
                            onClick={() => handleSplit(selected.id)}
                            title="Split merged tasks into individual dispatches"
                          >
                            Split ({selected.subordinate_count})
                          </button>
                        )}
                      </>
                    )}
                    <button className="danger small" onClick={() => setConfirmCancel(selected.id)}>
                      Cancel
                    </button>
                    {actionError && (
                      <span className="error-text">{actionError}</span>
                    )}
                  </div>
                )}
              </div>
            )}
            {!isUpcoming(selected) && (
              <div className="detail-actions">
                {retryContext !== null ? (
                  <>
                    <label className="muted-text">Edit context before retrying</label>
                    <ContextEditor value={retryContext} onChange={e => setRetryContext(e.target.value)} autoFocus />
                    <div className="action-row compact">
                      <button className="small primary" onClick={() => handleRetry(selected.id, retryContext)}>
                        ↺ Retry
                      </button>
                      <button className="small" onClick={() => setRetryContext(null)}>Cancel</button>
                    </div>
                  </>
                ) : (
                  <div className="action-row">
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
                      onClick={() => setRetryContext('')}
                      title="Queue a fresh dispatch — edit context first"
                    >
                      ↺ Retry
                    </button>
                  </div>
                )}
                {actionError && (
                  <div className="error-text" style={{ marginTop: 6 }}>{actionError}</div>
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

function DispatchDetail({ item, output, onUpdate, liveText, liveTools, isStreaming, onSplit, onRate }) {
  const status = getStatus(item)
  const triggers = getTriggers(item)
  const assistantMsgs = output?.messages?.filter(m => m.role === 'assistant') ?? []
  const isPending = status === 'pending'

  // Selected trigger index for context display
  const [selectedTrigger, setSelectedTrigger] = useState(triggers.length - 1)

  // Editable context for pending dispatches
  const selectedCtx = triggers[selectedTrigger]?.context || ''
  const [editingContext, setEditingContext] = useState(null)
  const [saveError, setSaveError] = useState('')

  // Diff state
  const [diffData, setDiffData] = useState(null)
  const [diffOpen, setDiffOpen] = useState(false)
  const [diffLoading, setDiffLoading] = useState(false)

  // Outcome summary
  const [outcomeSummary, setOutcomeSummary] = useState(null)

  // Reset editing state when item changes
  useEffect(() => { setSelectedTrigger(triggers.length - 1); setEditingContext(null); setSaveError(''); setDiffData(null); setDiffOpen(false); setOutcomeSummary(null) }, [item.id])

  // Load outcome summary for completed dispatches with commit range
  useEffect(() => {
    if (!item.completed_at || !item.start_commit || !item.result_commit || item.start_commit === item.result_commit) return
    let cancelled = false
    getDispatchOutcome(item.id).then(data => {
      if (!cancelled) setOutcomeSummary(data.summary)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [item.id, item.completed_at, item.start_commit, item.result_commit])

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
      await updateDispatch(item.id, { context: editingContext, trigger_index: selectedTrigger })
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
      <div className="detail-section">
        <label>Status</label>
        <span className={`queue-status large ${status}`}>
          {STATUS_LABELS[status]}
        </span>
      </div>

      <div className="detail-section">
        <label>Triggers</label>
        <div className="trigger-chips">
          {triggers.map((entry, i) => (
            <span
              key={i}
              onClick={() => setSelectedTrigger(i)}
              className={`trigger-chip ${i === selectedTrigger ? 'active' : ''}`}
            >
              {TRIGGER_ICONS[entry.trigger] || ''} {triggerLabel(entry)}
            </span>
          ))}
        </div>
      </div>

      <div className="detail-section">
        <label>Context</label>
        {isPending && editingContext !== null ? (
          <>
            <ContextEditor value={editingContext} onChange={e => setEditingContext(e.target.value)} autoFocus />
            <div className="action-row compact">
              <button className="small primary" onClick={handleSaveContext}>Save</button>
              <button className="small" onClick={() => { setEditingContext(null); setSaveError('') }}>Cancel</button>
              {saveError && <span className="error-text">{saveError}</span>}
            </div>
          </>
        ) : isPending ? (
          <div className="context-pending-wrap">
            <pre
              onClick={() => setEditingContext(selectedCtx)}
              className="context-pending"
              style={{
                color: selectedCtx ? 'inherit' : 'var(--text-muted)',
                fontStyle: selectedCtx ? 'normal' : 'italic',
              }}
            >
              {selectedCtx || 'click to add context...'}
            </pre>
            <span className="context-edit-hint">✎</span>
          </div>
        ) : (
          <pre className="context-display" style={{
            color: selectedCtx ? 'inherit' : 'var(--text-muted)',
            fontStyle: selectedCtx ? 'normal' : 'italic',
          }}>
            {selectedCtx || 'not provided'}
          </pre>
        )}
      </div>

      <div className="detail-section">
        <label>Timeline</label>
        <div className="detail-meta">
          <div>Created: {formatDate(item.created_at, true)}</div>
          {item.started_at && <div>Started: {formatDate(item.started_at, true)}</div>}
          {item.completed_at && <div>Completed: {formatDate(item.completed_at, true)}</div>}
          {item.started_at && item.completed_at && (
            <div className="detail-duration">
              Duration: {formatDuration(item.started_at, item.completed_at)}
            </div>
          )}
          {item.started_at && !item.completed_at && (
            <div className="detail-duration running">
              Running for {formatDuration(item.started_at)}
            </div>
          )}
        </div>
      </div>

      {item.start_commit && item.result_commit && item.start_commit !== item.result_commit && (
        <div className="detail-section">
          <label>Commits</label>
          <div className="detail-meta">
            {`${item.start_commit.slice(0, 8)}..${item.result_commit.slice(0, 8)}`}
          </div>
        </div>
      )}

      {outcomeSummary && (
        <div className="detail-section">
          <label>Outcome</label>
          <pre className="context-display">{outcomeSummary}</pre>
        </div>
      )}

      {item.completed_at && !item.error && (
        <div className="detail-section">
          <label>Rating</label>
          <div className="rating-controls">
            <button
              className={`rating-btn ${item.rating === 'positive' ? 'active positive' : ''}`}
              onClick={() => onRate(item.id, item.rating === 'positive' ? null : 'positive')}
              title="Good result"
            >+</button>
            <button
              className={`rating-btn ${item.rating === 'negative' ? 'active negative' : ''}`}
              onClick={() => onRate(item.id, item.rating === 'negative' ? null : 'negative')}
              title="Poor result"
            >−</button>
          </div>
        </div>
      )}

      {item.error && status !== 'cancelled' && (
        <div className="detail-section">
          <label>{status === 'timed_out' ? 'Timed Out' : 'Error'}</label>
          <div className={`detail-meta ${status === 'timed_out' ? 'warning-text' : 'error-text'}`}>
            {status === 'timed_out' ? 'Dispatch exceeded timeout limit' : item.error}
          </div>
        </div>
      )}

      {/* Live streaming output — visible while streaming, or while liveText exists but stored output hasn't loaded yet */}
      {(isStreaming || (liveText && !assistantMsgs.length)) && (
        <div className="detail-section">
          <label>
            Live Output
            {isStreaming && <span className="live-indicator">
              <span className="pulse-dot">●</span> streaming
            </span>}
          </label>
          {liveTools.length > 0 && (
            <div className="live-tools">
              Tools: {liveTools.map((t, i) => (
                <span key={i} className="tool-badge">{t}</span>
              ))}
            </div>
          )}
          {liveText ? (
            <div className="md-content md-output">
              <Markdown>{mdBreaks(liveText)}</Markdown>
            </div>
          ) : isStreaming ? (
            <div className="live-connecting">
              <span className="tool-spinner" /> connecting...
            </div>
          ) : null}
        </div>
      )}

      {/* Stored output (after completion) */}
      {assistantMsgs.length > 0 && (
        <div className="detail-section">
          <label>Output</label>
          {assistantMsgs.map((msg, i) => {
            const content = getMessageContent(msg)
            if (!content.trim()) return null
            return (
              <div key={i} className="md-content md-output" style={{ marginBottom: 6 }}>
                <Markdown>{mdBreaks(content)}</Markdown>
              </div>
            )
          })}
        </div>
      )}

      {/* Dispatch diff — collapsible, for completed dispatches with commit range */}
      {item.start_commit && item.result_commit && item.start_commit !== item.result_commit && (
        <div className="detail-section">
          <label
            onClick={() => setDiffOpen(o => !o)}
            className="collapsible-label"
          >
            <span className={`collapse-arrow ${diffOpen ? 'open' : ''}`}>▸</span>
            Code Changes
            {diffData && diffData.files.length > 0 && (
              <span className="diff-stats">
                {diffData.files.length} {diffData.files.length === 1 ? 'file' : 'files'}
                {diffData.insertions > 0 && <span className="feed-stat-add">+{diffData.insertions}</span>}
                {diffData.deletions > 0 && <span className="feed-stat-del">-{diffData.deletions}</span>}
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
                      <div key={f.path} className="diff-file-row">
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
