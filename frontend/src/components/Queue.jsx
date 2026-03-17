import { useState, useEffect, useCallback, useRef, useLayoutEffect } from 'react'
import Markdown from 'react-markdown'
import {
  getTaskQueue, cancelTask, updateTask, getTaskOutput,
  getTaskDiff, getTaskOutcome, getQueueSettings, setQueueSettings, processOne,
  streamTask, approveTask, rejectTask,
  reorderTasks, mergeTasks, splitTask, uncoalesceTask, getSubordinates,
  transferTask,
} from '../api'
import {
  formatDate, formatDuration, TRIGGER_ICONS, mdBreaks,
  STATUS_LABELS, TRIGGER_LABELS, getTaskStatus, triggerLabel, getMessageContent,
} from '../util'
import History from './History'

const getStatus = getTaskStatus

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

export default function Queue() {
  const [tab, setTab] = useState('queue')
  const historyRef = useRef(null)
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [autoDispatch, setAutoDispatch] = useState(false)
  const [output, setOutput] = useState(null)
  const [confirmCancel, setConfirmCancel] = useState(null)
  const [actionError, setActionError] = useState('')

  const [refreshing, setRefreshing] = useState(false)
  const detailScrollRef = useRef(null)

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
    getQueueSettings().then(s => setAutoDispatch(s.auto_dispatch)).catch(() => {})
  }, [])

  useEffect(() => {
    const interval = setInterval(refresh, 5000)
    return () => clearInterval(interval)
  }, [refresh])

  const [liveText, setLiveText] = useState('')
  const [liveTools, setLiveTools] = useState([])
  const [isStreaming, setIsStreaming] = useState(false)

  useEffect(() => { setActionError(''); setConfirmCancel(null) }, [selected?.id])

  useEffect(() => {
    if (!selected) { setOutput(null); setLiveText(''); setLiveTools([]); setIsStreaming(false); return }
    let cancelled = false
    let sseHandle = null

    const status = getStatus(selected)
    if (status === 'running') {
      setOutput(null)
      setLiveText('')
      setLiveTools([])
      setIsStreaming(true)

      try {
        const { abort, done } = streamTask(selected.id, (event) => {
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

  useLayoutEffect(() => {
    if (detailScrollRef.current) detailScrollRef.current.scrollTop = 0
  }, [selected?.id])

  useEffect(() => {
    if (!liveText || !detailScrollRef.current) return
    const el = detailScrollRef.current
    el.scrollTop = el.scrollHeight
  }, [liveText])

  useEffect(() => {
    const handleKey = (e) => {
      if (e.key !== 'Escape') return
      if (confirmCancel !== null) { setConfirmCancel(null); return }
      if (selected) setSelected(null)
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [selected, confirmCancel])

  // Partition items into columns — Dispatch shows only pre-execution and active tasks
  const pendingItems = sortPreExecution(items.filter(i => {
    const s = getStatus(i)
    return s === 'pending' || (s === 'pending_approval' && !i.queued_at)
  }))
  const queuedItems = sortPreExecution(items.filter(i => {
    const s = getStatus(i)
    return s === 'queued' || (s === 'pending_approval' && i.queued_at)
  }))
  const activeItems = items.filter(i => getStatus(i) === 'running')
  const anyRunning = activeItems.length > 0

  // Determine which status group the selected task belongs to
  const selectedStatus = selected ? getStatus(selected) : null
  const selectedIsPreExec = selected && isPreExecution(selected)

  if (tab === 'history') {
    return (
      <>
        <div className="header-bar">
          <h1>Dispatch</h1>
          <div className="dispatch-tabs">
            <button className="dispatch-tab" onClick={() => setTab('queue')}>Upcoming</button>
            <button className="dispatch-tab active">History</button>
          </div>
          <div className="spacer" />
          <button className="small" onClick={() => historyRef.current?.refresh()} disabled={historyRef.current?.refreshing}>
            {historyRef.current?.refreshing ? <span className="tool-spinner" /> : '↻'}
          </button>
        </div>
        <History ref={historyRef} />
      </>
    )
  }

  return (
    <>
      <div className="header-bar">
        <h1>Dispatch</h1>
        <div className="dispatch-tabs">
          <button className="dispatch-tab active">Upcoming</button>
          <button className="dispatch-tab" onClick={() => setTab('history')}>History</button>
        </div>
        <div className="spacer" />
        <button className="small" onClick={handleRefresh} disabled={refreshing}>
          {refreshing ? <span className="tool-spinner" /> : '↻'}
        </button>
        <div className="toolbar-divider" />
        <label className="checkbox-label">
          <input type="checkbox" checked={autoDispatch} onChange={e => handleToggleAuto(e.target.checked)} />
          Auto-queue
        </label>
      </div>

      <div className="split-body">
        <div className="kanban-columns">
          <KanbanColumn
            title="Pending"
            items={pendingItems}
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
        </div>

        {selected && (
          <div className="detail-panel">
            <div className="detail-panel-header">
              <h3>#{selected.id} — {selected.job_name}</h3>
              <button className="small" onClick={() => setSelected(null)}>✕</button>
            </div>

            <div ref={detailScrollRef} className="detail-scroll">
              <TaskDetail item={selected} output={output} onUpdate={refresh}
                liveText={liveText} liveTools={liveTools} isStreaming={isStreaming}
                onUncoalesce={handleUncoalesce} />
            </div>

            {(selectedIsPreExec || selectedStatus === 'running') && (
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
          </div>
        )}
      </div>
    </>
  )
}


/**
 * A single kanban column with specialized drag behavior:
 * - Pending (dragMode="merge"): drop onto same-job task to coalesce
 * - Queued (dragMode="reorder"): drop between tasks to change priority
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
      // Pending column: merge if same job, otherwise no-op
      const canMerge = targetItem && draggedItem && targetItem.job_id === draggedItem.job_id
      return canMerge ? 'merge' : null
    }
    // Queued column: reorder only
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

  // Handle cross-column drops (transfer)
  const handleColumnDragOver = (e) => {
    if (dragIdx !== null) return // Internal drag — handled by row handlers
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
        {/* Active tasks appear above queued items */}
        {activeItems && activeItems.length > 0 && (
          <>
            {activeItems.map(item => (
              <div
                key={item.id}
                className={`feed-item running ${selected?.id === item.id ? 'active' : ''}`}
                onClick={() => onSelect(item)}
              >
                <div className="feed-avatar">
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
              <div className="feed-avatar">
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

function TaskDetail({ item, output, onUpdate, liveText, liveTools, isStreaming, onUncoalesce }) {
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
      // Re-fetch subordinates to get updated context
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

  return (
    <>
      <div className="detail-section">
        <label>Status</label>
        <span className={`queue-status large ${status}`}>
          {STATUS_LABELS[status]}
        </span>
      </div>

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

      <div className="detail-section">
        <label>Timeline</label>
        <div className="detail-meta">
          <div>Created: {formatDate(item.created_at, true)}</div>
          {item.queued_at && <div>Queued: {formatDate(item.queued_at, true)}</div>}
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

      {item.error && status !== 'cancelled' && (
        <div className="detail-section">
          <label>{status === 'timed_out' ? 'Timed Out' : 'Error'}</label>
          <div className={`detail-meta ${status === 'timed_out' ? 'warning-text' : 'error-text'}`}>
            {status === 'timed_out' ? 'Task exceeded timeout limit' : item.error}
          </div>
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
