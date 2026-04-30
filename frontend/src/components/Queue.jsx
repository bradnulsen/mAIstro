import { useState, useEffect, useCallback, useRef, useLayoutEffect } from 'react'
import Markdown from 'react-markdown'
import {
  getTaskQueue, cancelTask, updateTask, getTaskOutput,
  getTaskDiff, getTaskOutcome, processOne,
  streamTask, approveTask, rejectTask,
  reorderTasks, mergeTasks, splitTask, uncoalesceTask, getSubordinates,
  transferTask, resumeTask, replyTask,
  getOrphanStash, restoreOrphanStash, discardOrphanStash,
  discardTaskWorkspace,
} from '../api'
import {
  formatDate, formatDuration, formatDurationSecs, TRIGGER_ICONS, mdBreaks,
  STATUS_LABELS, TRIGGER_LABELS, EVENT_LABELS, getTaskStatus, triggerLabel, getMessageContent,
} from '../util'

const getStatus = getTaskStatus

const TERMINAL_STATES = new Set(['completed', 'exhausted', 'failed', 'error', 'cancelled', 'timed_out', 'interrupted', 'rejected'])

/** Deterministic job color from CSS tokens — matches Dashboard timeline palette */
let _jobColors = null
function jobColorForId(jobId) {
  if (!_jobColors) {
    const root = document.documentElement
    _jobColors = Array.from({ length: 10 }, (_, i) =>
      getComputedStyle(root).getPropertyValue(`--job-color-${i}`).trim()
    )
    if (_jobColors.every(c => !c)) {
      _jobColors = ['#4a90d9','#d94a4a','#4ad97a','#d9a84a','#9b59b6','#1abc9c','#e67e22','#3498db','#e74c3c','#2ecc71']
    }
  }
  const s = String(jobId || '')
  let hash = 0
  for (let i = 0; i < s.length; i++) hash = ((hash << 5) - hash + s.charCodeAt(i)) | 0
  return _jobColors[((hash % 10) + 10) % 10]
}

function isPreExecution(item) {
  const s = getStatus(item)
  return s === 'pending' || s === 'queued' || s === 'pending_approval'
}

/** Sort pre-execution tasks: sort_order first, then created_at */
function sortPreExecution(items) {
  return [...items].sort((a, b) => {
    const aSort = a.sort_order ?? Infinity
    const bSort = b.sort_order ?? Infinity
    if (aSort !== bSort) return aSort - bSort
    return (a.created_at || '').localeCompare(b.created_at || '')
  })
}

/** Format execution metadata (turns, cost) as a compact string */
function execMeta(item) {
  const parts = []
  if (item.num_turns != null) parts.push(`${item.num_turns} turn${item.num_turns !== 1 ? 's' : ''}`)
  if (item.cost_usd != null) parts.push(`$${item.cost_usd < 0.01 ? item.cost_usd.toFixed(4) : item.cost_usd.toFixed(2)}`)
  return parts.length ? parts.join(' · ') : null
}

/** Human-readable inline reason for non-success terminal states */
function errorSummary(item, status) {
  if (status === 'completed') return null
  if (status === 'exhausted') return item.error || 'Hit turn limit'
  if (status === 'cancelled') return 'Cancelled by user'
  if (status === 'timed_out') return 'Exceeded timeout limit'
  if (status === 'interrupted') return 'Process interrupted'
  if (status === 'rejected') return 'Rejected before execution'
  return item.error || 'Unknown error'
}

const MIN_DRAWER_HEIGHT = 120
const DEFAULT_DRAWER_HEIGHT = 320

export default function Queue() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [output, setOutput] = useState(null)
  const [confirmCancel, setConfirmCancel] = useState(null)
  const [actionError, setActionError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [replyContext, setReplyContext] = useState(null)

  // Drawer state
  const [drawerHeight, setDrawerHeight] = useState(DEFAULT_DRAWER_HEIGHT)
  const drawerScrollRef = useRef(null)

  const refresh = useCallback(async () => {
    try {
      const queue = await getTaskQueue()
      setItems(queue)
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

  useEffect(() => {
    // Event-driven updates: subscribe to global queue-change SSE stream.
    // EventSource auto-reconnects on disconnect.
    const es = new EventSource('/api/queue/stream')
    es.addEventListener('queue_changed', () => refresh())
    // 30s fallback poll in case the SSE stalls
    const fallback = setInterval(refresh, 30000)
    return () => { es.close(); clearInterval(fallback) }
  }, [refresh])

  // Live streaming state
  const [liveText, setLiveText] = useState('')
  const [liveThinking, setLiveThinking] = useState('')
  const [liveTools, setLiveTools] = useState([])
  const [isStreaming, setIsStreaming] = useState(false)

  useEffect(() => { setActionError(''); setConfirmCancel(null); setReplyContext(null) }, [selected?.id])

  useEffect(() => {
    if (!selected) { setOutput(null); setLiveText(''); setLiveThinking(''); setLiveTools([]); setIsStreaming(false); return }
    let cancelled = false
    let sseHandle = null

    const status = getStatus(selected)
    if (status === 'running') {
      setOutput(null)
      setLiveText('')
      setLiveThinking('')
      setLiveTools([])
      setIsStreaming(true)

      try {
        const { abort, done } = streamTask(selected.id, (event) => {
          if (cancelled) return
          const type = event.type
          if (type === 'text' || type === 'assistant_complete') {
            setLiveText(prev => prev + (event.content || ''))
          } else if (type === 'thinking') {
            setLiveThinking(prev => prev + (event.content || ''))
          } else if (type === 'tool_use') {
            // New tool turn — clear thinking (it was for the decision just made)
            setLiveThinking('')
            setLiveTools(prev => {
              const tool = event.tool || '?'
              return prev.includes(tool) ? prev : [...prev, tool]
            })
          } else if (type === 'done') {
            setIsStreaming(false)
            getTaskOutput(selected.id).then(data => {
              if (!cancelled) setOutput(data)
            }).catch(() => {})
          } else if (type === 'error') {
            setIsStreaming(false)
          }
        })
        sseHandle = { abort }
        done.catch(() => {
          if (!cancelled) {
            setIsStreaming(false)
            getTaskOutput(selected.id).then(data => {
              if (!cancelled) setOutput(data)
            }).catch(() => {})
          }
        })
      } catch {
        setIsStreaming(false)
      }

      return () => { cancelled = true; if (sseHandle) sseHandle.abort() }
    } else {
      setLiveText('')
      setLiveThinking('')
      setLiveTools([])
      setIsStreaming(false)
      const load = async () => {
        try {
          const data = await getTaskOutput(selected.id)
          if (!cancelled) setOutput(data)
        } catch { if (!cancelled) setOutput(null) }
      }
      load()
      return () => { cancelled = true }
    }
  }, [selected?.id, selected?.started_at, selected?.completed_at])

  // Listen for external refresh requests (e.g. from CommandBar queue/shelve actions)
  useEffect(() => {
    const handler = () => refresh()
    window.addEventListener('queue-refresh', handler)
    return () => window.removeEventListener('queue-refresh', handler)
  }, [refresh])

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
      await approveTask(id)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleReject = async (id) => {
    setActionError('')
    try {
      await rejectTask(id)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleCancel = async (id) => {
    try {
      await cancelTask(id)
      setConfirmCancel(null)
      await refresh()
      if (selected?.id === id) setSelected(null)
    } catch {}
  }

  const handleMergePair = async (draggedId, targetId) => {
    setActionError('')
    try {
      await mergeTasks([draggedId, targetId])
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleSplit = async (id) => {
    setActionError('')
    try {
      await splitTask(id)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleUncoalesce = async (taskId, e) => {
    e.stopPropagation()
    setActionError('')
    try {
      await uncoalesceTask(taskId)
      await refresh()
    } catch (err) {
      setActionError(err.message)
    }
  }

  const handleTransfer = async (id, toQueued) => {
    setActionError('')
    try {
      await transferTask(id, toQueued)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleResume = async (id) => {
    setActionError('')
    try {
      await resumeTask(id)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleReply = async (id, context) => {
    setActionError('')
    try {
      await replyTask(id, context)
      setReplyContext(null)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  // Drawer resize via drag
  const handleDrawerDragStart = useCallback((e) => {
    e.preventDefault()
    const startY = e.clientY
    const startH = drawerHeight
    const onMove = (e) => {
      const delta = startY - e.clientY
      setDrawerHeight(Math.max(MIN_DRAWER_HEIGHT, startH + delta))
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [drawerHeight])

  useLayoutEffect(() => {
    if (drawerScrollRef.current) drawerScrollRef.current.scrollTop = 0
  }, [selected?.id])

  useEffect(() => {
    if ((!liveText && !liveThinking) || !drawerScrollRef.current) return
    const el = drawerScrollRef.current
    el.scrollTop = el.scrollHeight
  }, [liveText, liveThinking])

  useEffect(() => {
    const handleKey = (e) => {
      if (e.key !== 'Escape') return
      if (confirmCancel !== null) { setConfirmCancel(null); return }
      if (replyContext !== null) { setReplyContext(null); return }
      if (selected) setSelected(null)
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [selected, confirmCancel, replyContext])

  // Partition items into three columns
  const upcomingItems = sortPreExecution(items.filter(i => {
    const s = getStatus(i)
    return s === 'pending' || (s === 'pending_approval' && !i.queued_at)
  }))
  const queuedItems = sortPreExecution(items.filter(i => {
    const s = getStatus(i)
    return s === 'queued' || (s === 'pending_approval' && i.queued_at)
  }))
  const activeItems = items.filter(i => getStatus(i) === 'running')
  const resolvedItems = items
    .filter(i => TERMINAL_STATES.has(getStatus(i)))
    .sort((a, b) => (b.completed_at || '').localeCompare(a.completed_at || ''))

  const selectedStatus = selected ? getStatus(selected) : null
  const selectedIsPreExec = selected && isPreExecution(selected)
  const selectedIsTerminal = selected && TERMINAL_STATES.has(selectedStatus)
  const selectedIsRunning = selectedStatus === 'running'

  return (
    <>
      <div className="header-bar">
        <h1>Dispatch</h1>
        <div className="spacer" />
        <button className="small" onClick={handleRefresh} disabled={refreshing}>
          {refreshing ? <span className="tool-spinner" /> : '↻'}
        </button>
      </div>

      <div className="dispatch-body">
        <div className="kanban-columns">
          <KanbanColumn
            title="Upcoming"
            items={upcomingItems}
            column="pending"
            dragMode="merge"
            selected={selected}
            onSelect={setSelected}
            onMerge={handleMergePair}
            onTransfer={handleTransfer}
          />
          <KanbanColumn
            title="Active"
            items={queuedItems}
            column="queued"
            dragMode="reorder"
            selected={selected}
            onSelect={setSelected}
            onReorder={reorderTasks}
            onTransfer={handleTransfer}
            onRefresh={refresh}
            activeItems={activeItems}
          />
          <ResolvedColumn
            items={resolvedItems}
            loading={loading}
            selected={selected}
            onSelect={setSelected}
          />
        </div>

        {selected && (
          <div className="detail-drawer" style={{ height: drawerHeight }}>
            <div className="detail-drawer-handle" onMouseDown={handleDrawerDragStart}>
              <div className="drawer-handle-bar" />
            </div>
            <div className="detail-drawer-header">
              <h3>#{selected.id} — {selected.job_name}</h3>
              <button className="small" onClick={() => setSelected(null)}>✕</button>
            </div>

            <div ref={drawerScrollRef} className="detail-drawer-scroll">
              <TaskDetail item={selected} output={output} onUpdate={refresh}
                liveText={liveText} liveThinking={liveThinking} liveTools={liveTools} isStreaming={isStreaming}
                onUncoalesce={handleUncoalesce} />
            </div>

            {(selectedIsPreExec || selectedIsRunning) && (
              <div className="detail-actions">
                {confirmCancel === selected.id ? (
                  <div className="action-row">
                    <span className="confirm-text">Cancel this task?</span>
                    <button className="danger small" onClick={() => handleCancel(selected.id)}>Confirm</button>
                    <button className="small" onClick={() => setConfirmCancel(null)}>No</button>
                  </div>
                ) : (
                  <div className="action-row">
                    {selectedStatus === 'pending_approval' && (
                      <>
                        <button className="small primary" onClick={() => handleApprove(selected.id)}>
                          ✓ Approve
                        </button>
                        <button className="danger small" onClick={() => handleReject(selected.id)}>
                          ✕ Reject
                        </button>
                      </>
                    )}
                    {selectedStatus === 'pending' && (
                      <button
                        className="small"
                        onClick={() => handleTransfer(selected.id, true)}
                        title="Queue this task for processing"
                      >
                        ▶ Activate
                      </button>
                    )}
                    {selectedStatus === 'queued' && (
                      <button
                        className="small"
                        onClick={() => handleTransfer(selected.id, false)}
                        title="Shelve back to pending"
                      >
                        ▣ Shelve
                      </button>
                    )}
                    {selectedStatus === 'pending' && selected.subordinate_count > 0 && (
                      <button
                        className="small"
                        onClick={() => handleSplit(selected.id)}
                        title="Split merged tasks into individual items"
                      >
                        Split ({selected.subordinate_count})
                      </button>
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

            {selectedIsTerminal && (
              <div className="detail-actions">
                {replyContext !== null ? (
                  <>
                    <label className="muted-text">Add context for your reply</label>
                    <ContextEditor value={replyContext} onChange={e => setReplyContext(e.target.value)} onSubmit={() => handleReply(selected.id, replyContext)} autoFocus />
                    <div className="action-row compact">
                      <button className="small primary" onClick={() => handleReply(selected.id, replyContext)}>
                        ↺ Reply
                      </button>
                      <button className="small" onClick={() => setReplyContext(null)}>Cancel</button>
                    </div>
                  </>
                ) : (
                  <div className="action-row">
                    {selected.error && !['cancelled', 'rejected'].includes(selectedStatus) && (
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
                      onClick={() => setReplyContext('')}
                      title="Follow up on this task with additional context"
                    >
                      ↺ Reply
                    </button>
                  </div>
                )}
                {actionError && (
                  <div className="error-text">{actionError}</div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}


/**
 * A single kanban column with specialized drag behavior:
 * - Upcoming (dragMode="merge"): drop onto same-job task to coalesce
 * - Active (dragMode="reorder"): drop between tasks to change priority
 * Both columns accept cross-column drops as transfers.
 */
function KanbanColumn({
  title, items, column, dragMode, selected, onSelect,
  onMerge, onReorder, onTransfer, onRefresh,
  activeItems,
}) {
  const [dragIdx, setDragIdx] = useState(null)
  const [dragOverIdx, setDragOverIdx] = useState(null)
  const [dropZone, setDropZone] = useState(null)
  const [columnDropActive, setColumnDropActive] = useState(false)
  const columnRef = useRef(null)

  const computeDropZone = (e, rowEl, draggedItem, targetItem) => {
    if (dragMode === 'merge') {
      const canMerge = targetItem && draggedItem && targetItem.job_id === draggedItem.job_id
      return canMerge ? 'merge' : null
    }
    const rect = rowEl.getBoundingClientRect()
    const y = e.clientY - rect.top
    return (y / rect.height) < 0.5 ? 'reorder-before' : 'reorder-after'
  }

  const handleDragOver = (e, idx) => {
    e.preventDefault()
    if (dragIdx === null || dragIdx === idx) { setDragOverIdx(null); setDropZone(null); return }
    const draggedItem = items[dragIdx]
    const targetItem = items[idx]
    const zone = computeDropZone(e, e.currentTarget, draggedItem, targetItem)
    setDragOverIdx(idx)
    setDropZone(zone)
  }

  const handleDragEnd = async () => {
    if (dragIdx !== null && dragOverIdx !== null && dragIdx !== dragOverIdx && dropZone) {
      if (dropZone === 'merge') {
        const draggedItem = items[dragIdx]
        const targetItem = items[dragOverIdx]
        await onMerge(draggedItem.id, targetItem.id)
      } else if (dragMode === 'reorder') {
        const reordered = [...items]
        const [moved] = reordered.splice(dragIdx, 1)
        const insertIdx = dropZone === 'reorder-before'
          ? (dragOverIdx > dragIdx ? dragOverIdx - 1 : dragOverIdx)
          : (dragOverIdx < dragIdx ? dragOverIdx + 1 : dragOverIdx)
        reordered.splice(insertIdx, 0, moved)
        const ids = reordered.map(i => i.id)
        try { await onReorder(ids); await onRefresh() } catch {}
      }
    }
    setDragIdx(null)
    setDragOverIdx(null)
    setDropZone(null)
  }

  const handleColumnDragOver = (e) => {
    if (dragIdx !== null) return
    e.preventDefault()
    setColumnDropActive(true)
  }

  const handleColumnDragLeave = (e) => {
    if (columnRef.current && !columnRef.current.contains(e.relatedTarget)) {
      setColumnDropActive(false)
    }
  }

  const handleColumnDrop = async (e) => {
    e.preventDefault()
    setColumnDropActive(false)
    const taskId = e.dataTransfer.getData('text/x-task-id')
    const sourceColumn = e.dataTransfer.getData('text/x-source-column')
    if (taskId && sourceColumn && sourceColumn !== column) {
      await onTransfer(parseInt(taskId), column === 'queued')
    }
  }

  return (
    <div
      ref={columnRef}
      className={`kanban-column${columnDropActive ? ' column-drop-active' : ''}`}
      onDragOver={handleColumnDragOver}
      onDragLeave={handleColumnDragLeave}
      onDrop={handleColumnDrop}
    >
      <div className="kanban-column-header">
        <span className="kanban-column-title">{title}</span>
        <span className="kanban-column-count">{items.length + (activeItems?.length || 0)}</span>
      </div>
      <div className="kanban-column-body">
        {items.length === 0 && !activeItems?.length && (
          <div className="empty-state">No {title.toLowerCase()} tasks</div>
        )}
        {activeItems && activeItems.length > 0 && (
          <>
            {activeItems.map(item => (
              <div
                key={item.id}
                className={`feed-item running ${selected?.id === item.id ? 'active' : ''}`}
                onClick={() => onSelect(item)}
              >
                <div className="feed-avatar" style={{ background: jobColorForId(item.job_id) }}>
                  {(item.job_name || '?')[0].toUpperCase()}
                </div>
                <div className="feed-body">
                  <div className="feed-meta">
                    <span className="feed-trigger">{TRIGGER_ICONS[item.trigger] || ''}</span>
                    <span className="feed-author">{item.job_name}</span>
                    <span className="queue-status running">{STATUS_LABELS.running}</span>
                    <span>{formatDate(item.started_at, true)}</span>
                  </div>
                  <div className="feed-message">
                    {item.context || `${TRIGGER_LABELS[item.trigger] || item.trigger} task`}
                  </div>
                </div>
              </div>
            ))}
            {items.length > 0 && <div className="kanban-section-divider">Queue ({items.length})</div>}
          </>
        )}

        {items.map((item, i) => {
          const status = getStatus(item)
          const isDropTarget = dragOverIdx === i && dragIdx !== null && dragIdx !== i
          const dropClass = isDropTarget && dropZone
            ? (dropZone === 'merge' ? ' drop-merge' : dropZone === 'reorder-before' ? ' drop-before' : ' drop-after')
            : ''
          return (
            <div
              key={item.id}
              className={`feed-item ${selected?.id === item.id ? 'active' : ''} ${status === 'pending_approval' ? 'pending_approval' : ''}${dragIdx === i ? ' dragging' : ''}${dropClass}`}
              onClick={() => onSelect(item)}
              draggable
              onDragStart={(e) => {
                setDragIdx(i)
                e.dataTransfer.effectAllowed = 'move'
                e.dataTransfer.setData('text/x-task-id', String(item.id))
                e.dataTransfer.setData('text/x-source-column', column)
              }}
              onDragOver={(e) => handleDragOver(e, i)}
              onDragLeave={() => { setDragOverIdx(null); setDropZone(null) }}
              onDragEnd={handleDragEnd}
            >
              <div className="feed-avatar" style={{ background: jobColorForId(item.job_id) }}>
                {(item.job_name || '?')[0].toUpperCase()}
              </div>
              <div className="feed-body">
                <div className="feed-meta">
                  <span className="feed-trigger">
                    {TRIGGER_ICONS[item.trigger] || ''}
                    {item.subordinate_count > 0 ? ` (${item.subordinate_count + 1})` : ''}
                  </span>
                  <span className="feed-author">{item.job_name}</span>
                  {status === 'pending_approval' && (
                    <span className={`queue-status ${status}`}>
                      {STATUS_LABELS[status]}
                    </span>
                  )}
                  <span>{formatDate(item.created_at, true)}</span>
                </div>
                <div className="feed-message">
                  {item.context || `${TRIGGER_LABELS[item.trigger] || item.trigger} task`}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}


/**
 * Resolved column — read-only, no drag operations.
 * Shows all terminal tasks ordered by completion time.
 */
function ResolvedColumn({ items, loading, selected, onSelect }) {
  return (
    <div className="kanban-column">
      <div className="kanban-column-header">
        <span className="kanban-column-title">Resolved</span>
        <span className="kanban-column-count">{items.length}</span>
      </div>
      <div className="kanban-column-body">
        {loading && items.length === 0 && <div className="loading">Loading...</div>}
        {!loading && items.length === 0 && (
          <div className="empty-state">No resolved tasks</div>
        )}
        {items.map(item => {
          const status = getStatus(item)
          const reason = errorSummary(item, status)
          const hasCommits = item.start_commit && item.result_commit && item.start_commit !== item.result_commit
          return (
            <div
              key={item.id}
              className={`feed-item ${selected?.id === item.id ? 'active' : ''} ${status !== 'completed' ? status : ''}`}
              onClick={() => onSelect(item)}
            >
              <div className="feed-avatar" style={{ background: jobColorForId(item.job_id) }}>
                {(item.job_name || '?')[0].toUpperCase()}
              </div>
              <div className="feed-body">
                <div className="feed-meta">
                  <span className="feed-trigger">{TRIGGER_ICONS[item.trigger] || ''}</span>
                  <span className="feed-author">{item.job_name}</span>
                  <span className={`queue-status ${status}`}>
                    {STATUS_LABELS[status] || status}
                  </span>
                  <span>{formatDate(item.completed_at, true)}</span>
                </div>
                {reason ? (
                  <div className="feed-message">
                    <span className="history-error-reason">{reason}</span>
                  </div>
                ) : (
                  <>
                    <div className="feed-message">
                      {item.context || `${TRIGGER_LABELS[item.trigger] || item.trigger} task`}
                    </div>
                    {hasCommits && (
                      <div className="feed-commits">
                        {item.start_commit.slice(0, 8)}..{item.result_commit.slice(0, 8)}
                      </div>
                    )}
                    {execMeta(item) && (
                      <div className="feed-exec-meta">{execMeta(item)}</div>
                    )}
                  </>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}


function ContextEditor({ value, onChange, onSubmit, autoFocus = false }) {
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
      onKeyDown={e => {
        if (e.key === 'Enter' && !e.shiftKey && onSubmit) {
          e.preventDefault()
          onSubmit()
        }
      }}
      className="context-editor"
      autoFocus={autoFocus}
    />
  )
}

function TaskDetail({ item, output, onUpdate, liveText, liveThinking, liveTools, isStreaming, onUncoalesce }) {
  // Merge detail-level data (events, durations) from the output fetch when available
  const detail = output?.task || item
  const status = getStatus(item)
  const assistantMsgs = output?.messages?.filter(m => m.role === 'assistant') ?? []
  const isPending = status === 'pending' || status === 'queued'

  const [editingContext, setEditingContext] = useState(null)
  const [saveError, setSaveError] = useState('')

  const [diffData, setDiffData] = useState(null)
  const [diffOpen, setDiffOpen] = useState(false)
  const [diffLoading, setDiffLoading] = useState(false)

  const [outcomeSummary, setOutcomeSummary] = useState(null)

  // Subordinate triggers for coalesced tasks
  const [subordinates, setSubordinates] = useState([])
  const [selectedTrigger, setSelectedTrigger] = useState(null) // null = root

  useEffect(() => {
    setEditingContext(null); setSaveError(''); setDiffData(null); setDiffOpen(false)
    setOutcomeSummary(null); setSelectedTrigger(null); setSubordinates([])
    if (item.subordinate_count > 0) {
      getSubordinates(item.id).then(setSubordinates).catch(() => setSubordinates([]))
    }
  }, [item.id, item.subordinate_count])

  useEffect(() => {
    if (!item.completed_at || !item.start_commit || !item.result_commit || item.start_commit === item.result_commit) return
    let cancelled = false
    getTaskOutcome(item.id).then(data => {
      if (!cancelled) setOutcomeSummary(data.summary)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [item.id, item.completed_at, item.start_commit, item.result_commit])

  useEffect(() => {
    if (!diffOpen || diffData || diffLoading) return
    if (!item.start_commit || !item.result_commit || item.start_commit === item.result_commit) return
    let cancelled = false
    setDiffLoading(true)
    getTaskDiff(item.id).then(data => {
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
      const targetId = selectedTrigger ?? item.id
      await updateTask(targetId, { context: editingContext })
      setEditingContext(null)
      if (onUpdate) await onUpdate()
      if (item.subordinate_count > 0) {
        getSubordinates(item.id).then(setSubordinates).catch(() => {})
      }
    } catch (e) { setSaveError(e.message) }
  }

  const [, setTick] = useState(0)
  useEffect(() => {
    if (status !== 'running') return
    const interval = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(interval)
  }, [status])

  // Build the list of all triggers (root + subordinates)
  const allTriggers = [
    { id: item.id, trigger: item.trigger, trigger_detail: item.trigger_detail, context: item.context, isRoot: true },
    ...subordinates.map(s => ({ id: s.id, trigger: s.trigger, trigger_detail: s.trigger_detail, context: s.context, isRoot: false })),
  ]
  const activeTrigger = allTriggers.find(t => t.id === selectedTrigger) || allTriggers[0]
  const ctx = activeTrigger?.context || ''

  const hasCommits = item.start_commit && item.result_commit && item.start_commit !== item.result_commit

  return (
    <>
      {/* Row 1: Compact metadata side by side */}
      <div className="detail-meta-row">
        <div className="detail-section">
          <label>Status</label>
          <span className={`queue-status large ${status}`}>
            {STATUS_LABELS[status]}
          </span>
        </div>

        <div className="detail-section">
          <label>Timeline{detail.durations?.execution_duration != null
            ? ` — ${formatDurationSecs(detail.durations.execution_duration)}`
            : status === 'running' && item.started_at
              ? ` — ${formatDuration(item.started_at)}`
              : ''}</label>
          <div className="event-chips">
            {(detail.events || []).map((e, i) => (
              <span key={i} className={`event-chip ${e.event}`} title={formatDate(e.created_at, true)}>
                {EVENT_LABELS[e.event] || e.event}
              </span>
            ))}
            {(!detail.events || detail.events.length === 0) && (
              <span className="event-chip created" title={formatDate(item.created_at, true)}>
                Created
              </span>
            )}
          </div>
        </div>

        {hasCommits && (
          <div className="detail-section">
            <label>Commits</label>
            <div className="detail-meta">
              {`${item.start_commit.slice(0, 8)}..${item.result_commit.slice(0, 8)}`}
            </div>
          </div>
        )}

        {execMeta(item) && (
          <div className="detail-section">
            <label>Execution</label>
            <div className="detail-meta muted-text">{execMeta(item)}</div>
          </div>
        )}

        {output?.mcp_servers?.length > 0 && (
          <div className="detail-section">
            <label>MCP Servers</label>
            <div className="detail-meta muted-text">
              {output.mcp_servers.map((s, i) => (
                <span key={i} className={`tool-badge${s.enabled ? '' : ' disabled'}`} title={s.enabled ? 'Enabled' : 'Disabled'}>
                  {s.name}{!s.enabled ? ' (off)' : ''}
                </span>
              ))}
            </div>
          </div>
        )}

        {item.error && status !== 'cancelled' && (
          <div className="detail-section">
            <label>{status === 'timed_out' ? 'Timed Out' : status === 'exhausted' ? 'Exhausted' : 'Error'}</label>
            <div className={`detail-meta ${status === 'timed_out' || status === 'exhausted' ? 'warning-text' : 'error-text'}`}>
              {item.error}
            </div>
          </div>
        )}
      </div>

      {detail.orphan_stash_ref && (
        <OrphanStashBanner taskId={item.id} sha={detail.orphan_stash_ref} onChange={onUpdate} />
      )}

      {detail.worktree_path && (
        <WorkspaceBanner
          taskId={item.id}
          workspacePath={detail.worktree_path}
          branch={detail.task_branch}
          onChange={onUpdate}
        />
      )}

      {/* Row 2: Triggers — full width */}
      <div className="detail-section">
        <label>Triggers{allTriggers.length > 1 ? ` (${allTriggers.length})` : ''}</label>
        <div className="trigger-chips">
          {allTriggers.map(t => (
            <span
              key={t.id}
              className={`trigger-chip${activeTrigger.id === t.id ? ' active' : ''}${allTriggers.length > 1 ? ' selectable' : ''}`}
              onClick={() => allTriggers.length > 1 && setSelectedTrigger(t.id === item.id ? null : t.id)}
              title={`Task #${t.id}`}
            >
              {TRIGGER_ICONS[t.trigger] || ''} {triggerLabel(t)}
              {status === 'pending' && !t.isRoot && allTriggers.length > 1 && (
                <button
                  className="trigger-chip-remove"
                  onClick={(e) => onUncoalesce(t.id, e)}
                  title="Split this trigger out"
                >×</button>
              )}
            </span>
          ))}
        </div>
      </div>

      {/* Row 3: Context — full width */}
      <div className="detail-section">
        <label>Context</label>
        {isPending && editingContext !== null ? (
          <>
            <ContextEditor value={editingContext} onChange={e => setEditingContext(e.target.value)} onSubmit={handleSaveContext} autoFocus />
            <div className="action-row compact">
              <button className="small primary" onClick={handleSaveContext}>Save</button>
              <button className="small" onClick={() => { setEditingContext(null); setSaveError('') }}>Cancel</button>
              {saveError && <span className="error-text">{saveError}</span>}
            </div>
          </>
        ) : isPending ? (
          <div className="context-pending-wrap">
            <pre
              onClick={() => setEditingContext(ctx)}
              className={`context-pending${ctx ? '' : ' empty'}`}
            >
              {ctx || 'click to add context...'}
            </pre>
            <span className="context-edit-hint">✎</span>
          </div>
        ) : (
          <pre className={`context-display${ctx ? '' : ' empty'}`}>
            {ctx || 'not provided'}
          </pre>
        )}
      </div>

      {/* Outcome summary — full width */}
      {outcomeSummary && (
        <div className="detail-section">
          <label>Outcome</label>
          <pre className="context-display">{outcomeSummary}</pre>
        </div>
      )}

      {(isStreaming || (liveText && !assistantMsgs.length)) && (
        <div className="detail-section">
          <label>
            Live Output
            {isStreaming && <span className="live-indicator">
              <span className="pulse-dot">●</span> streaming
            </span>}
          </label>
          {liveThinking && (
            <div className="thinking-bubble">
              <span className="thinking-label">reasoning</span>
              <div className="thinking-content">{liveThinking}</div>
            </div>
          )}
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

      {assistantMsgs.length > 0 && (
        <div className="detail-section">
          <label>Output</label>
          {assistantMsgs.map((msg, i) => {
            const content = getMessageContent(msg)
            if (!content.trim()) return null
            return (
              <div key={i} className="md-content md-output">
                <Markdown>{mdBreaks(content)}</Markdown>
              </div>
            )
          })}
        </div>
      )}

      {hasCommits && (
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
                <div className="diff-status-note">Loading diff...</div>
              )}
              {diffData && diffData.files.length > 0 && (
                <>
                  <div className="diff-file-list">
                    {diffData.files.map(f => (
                      <div key={f.path} className="diff-file-row">
                        <span>{f.path}</span>
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
                <div className="diff-status-note">No changes</div>
              )}
            </>
          )}
        </div>
      )}
    </>
  )
}

function WorkspaceBanner({ taskId, workspacePath, branch, onChange }) {
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const [error, setError] = useState('')
  const [showMerge, setShowMerge] = useState(false)

  const handleDiscard = async () => {
    setError('')
    try {
      await discardTaskWorkspace(taskId)
      if (onChange) await onChange()
    } catch (e) { setError(e.message); setConfirmDiscard(false) }
  }

  return (
    <div className="detail-section">
      <label>Preserved Workspace</label>
      <div className="detail-meta warning-text">
        Agent's work was not integrated into main. The worktree and branch are preserved
        so you can inspect, manually merge, or discard.
      </div>
      <div className="detail-meta">
        <div><strong>Worktree:</strong> <code>{workspacePath}</code></div>
        {branch && <div><strong>Branch:</strong> <code>{branch}</code></div>}
      </div>
      <div className="action-row compact">
        {branch && (
          <button className="small" onClick={() => setShowMerge(s => !s)}>
            {showMerge ? 'Hide merge command' : 'Merge manually'}
          </button>
        )}
        {confirmDiscard ? (
          <>
            <span className="confirm-text">Delete workspace and branch?</span>
            <button className="danger small" onClick={handleDiscard}>Confirm</button>
            <button className="small" onClick={() => setConfirmDiscard(false)}>No</button>
          </>
        ) : (
          <button className="danger small" onClick={() => setConfirmDiscard(true)}>
            Discard workspace
          </button>
        )}
        {error && <span className="error-text">{error}</span>}
      </div>
      {showMerge && branch && (
        <pre className="context-display">{`git merge --no-ff ${branch}\n# resolve any conflicts, then:\n# git worktree remove --force ${workspacePath}\n# git branch -D ${branch}`}</pre>
      )}
    </div>
  )
}

function OrphanStashBanner({ taskId, sha, onChange }) {
  const [open, setOpen] = useState(false)
  const [diff, setDiff] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  const loadDiff = async () => {
    setLoading(true); setError('')
    try {
      const data = await getOrphanStash(taskId)
      setDiff(data.diff || '')
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }

  const toggle = () => {
    if (!open && diff === null) loadDiff()
    setOpen(!open)
  }

  const handleRestore = async () => {
    setError('')
    try {
      await restoreOrphanStash(taskId)
      if (onChange) await onChange()
    } catch (e) { setError(e.message) }
  }

  const handleDiscard = async () => {
    setError('')
    try {
      await discardOrphanStash(taskId)
      if (onChange) await onChange()
    } catch (e) { setError(e.message); setConfirmDiscard(false) }
  }

  return (
    <div className="detail-section orphan-stash-banner">
      <label>Orphan Changes Stashed</label>
      <div className="detail-meta warning-text">
        Agent left uncommitted edits when it exited. They were stashed (<code>{sha.slice(0, 8)}</code>) so the next task wouldn't inherit them.
      </div>
      <div className="action-row compact">
        <button className="small" onClick={toggle}>{open ? 'Hide diff' : 'View diff'}</button>
        <button className="small primary" onClick={handleRestore}>Restore to working tree</button>
        {confirmDiscard ? (
          <>
            <span className="confirm-text">Drop the stash?</span>
            <button className="danger small" onClick={handleDiscard}>Confirm</button>
            <button className="small" onClick={() => setConfirmDiscard(false)}>No</button>
          </>
        ) : (
          <button className="danger small" onClick={() => setConfirmDiscard(true)}>Discard</button>
        )}
        {error && <span className="error-text">{error}</span>}
      </div>
      {open && (
        <pre className="diff-view">
          {loading ? 'Loading…' : (diff
            ? diff.split('\n').map((line, i) => (
                <div key={i} className={
                  line.startsWith('+') ? 'diff-add' :
                  line.startsWith('-') ? 'diff-del' :
                  line.startsWith('@@') ? 'diff-hunk' : ''
                }>{line}</div>
              ))
            : '(empty)')}
        </pre>
      )}
    </div>
  )
}
