import { useState, useEffect, useCallback, useRef, useLayoutEffect } from 'react'
import Markdown from 'react-markdown'
import {
  getTaskQueue, getTaskOutput, getTaskDiff, getTaskOutcome,
  resumeTask, retryTask, getSubordinates, rateTask,
} from '../api'
import { formatDate, formatDuration, TRIGGER_ICONS, mdBreaks } from '../util'

const STATUS_LABELS = {
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
  return null
}

function triggerLabel(item) {
  const base = TRIGGER_LABELS[item.trigger] || item.trigger
  if (item.trigger === 'commit' && item.trigger_detail) return `${base} (${item.trigger_detail.slice(0, 8)})`
  if (item.trigger === 'dependency' && item.trigger_detail) return `${base} (${item.trigger_detail})`
  if (item.trigger === 'manual' && item.trigger_detail) return `${base} @ ${item.trigger_detail.slice(0, 8)}`
  if (item.trigger === 'schedule' && item.trigger_detail) return `${base} (${item.trigger_detail})`
  return base
}

function getMessageContent(msg) {
  if (typeof msg.content === 'string') return msg.content
  if (Array.isArray(msg.content)) return msg.content.filter(b => b.type === 'text').map(b => b.text).join('\n\n')
  return String(msg.content ?? '')
}

export default function History() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [output, setOutput] = useState(null)
  const [actionError, setActionError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [retryContext, setRetryContext] = useState(null)
  const detailScrollRef = useRef(null)

  const refresh = useCallback(async () => {
    try {
      const queue = await getTaskQueue()
      const completed = queue
        .filter(i => i.completed_at)
        .sort((a, b) => (b.completed_at || '').localeCompare(a.completed_at || ''))
      setItems(completed)
      setSelected(prev => {
        if (!prev) return null
        const updated = completed.find(q => q.id === prev.id)
        return updated || null
      })
      setLoading(false)
      return completed
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
    const interval = setInterval(refresh, 10000)
    return () => clearInterval(interval)
  }, [refresh])

  useEffect(() => { setActionError(''); setRetryContext(null) }, [selected?.id])

  useEffect(() => {
    if (!selected) { setOutput(null); return }
    let cancelled = false
    const load = async () => {
      try {
        const data = await getTaskOutput(selected.id)
        if (!cancelled) setOutput(data)
      } catch { if (!cancelled) setOutput(null) }
    }
    load()
    return () => { cancelled = true }
  }, [selected?.id])

  const handleResume = async (id) => {
    setActionError('')
    try {
      await resumeTask(id)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleRetry = async (id, context) => {
    setActionError('')
    try {
      await retryTask(id, context)
      setRetryContext(null)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleRate = async (id, rating) => {
    setActionError('')
    try {
      await rateTask(id, rating)
      await refresh()
    } catch (e) {
      setActionError(e.message)
    }
  }

  useLayoutEffect(() => {
    if (detailScrollRef.current) detailScrollRef.current.scrollTop = 0
  }, [selected?.id])

  useEffect(() => {
    const handleKey = (e) => {
      if (e.key !== 'Escape') return
      if (retryContext !== null) { setRetryContext(null); return }
      if (selected) setSelected(null)
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [selected, retryContext])

  return (
    <>
      <div className="split-body">
        <div className="feed-list">
          {loading && <div className="loading">Loading history...</div>}
          {!loading && items.length === 0 && (
            <div className="empty-state">No completed tasks</div>
          )}
          {items.map(item => {
            const status = getStatus(item)
            return (
              <div
                key={item.id}
                className={`feed-item ${selected?.id === item.id ? 'active' : ''}`}
                onClick={() => setSelected(item)}
              >
                <div className="feed-avatar">
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
                  <div className="feed-message">
                    {item.context || `${TRIGGER_LABELS[item.trigger] || item.trigger} task`}
                  </div>
                </div>
              </div>
            )
          })}
        </div>

        {selected && (
          <div className="detail-panel">
            <div className="detail-panel-header">
              <h3>#{selected.id} — {selected.job_name}</h3>
              <button className="small" onClick={() => setSelected(null)}>✕</button>
            </div>

            <div ref={detailScrollRef} className="detail-scroll">
              <HistoryDetail item={selected} output={output} onRate={handleRate} />
            </div>

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
                    title="Queue a fresh task — edit context first"
                  >
                    ↺ Retry
                  </button>
                </div>
              )}
              {actionError && (
                <div className="error-text" style={{ marginTop: 6 }}>{actionError}</div>
              )}
            </div>
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


function HistoryDetail({ item, output, onRate }) {
  const status = getStatus(item)
  const assistantMsgs = output?.messages?.filter(m => m.role === 'assistant') ?? []

  const [diffData, setDiffData] = useState(null)
  const [diffOpen, setDiffOpen] = useState(false)
  const [diffLoading, setDiffLoading] = useState(false)
  const [outcomeSummary, setOutcomeSummary] = useState(null)
  const [subordinates, setSubordinates] = useState([])
  const [selectedTrigger, setSelectedTrigger] = useState(null)

  useEffect(() => {
    setDiffData(null); setDiffOpen(false); setOutcomeSummary(null)
    setSelectedTrigger(null); setSubordinates([])
    if (item.subordinate_count > 0) {
      getSubordinates(item.id).then(setSubordinates).catch(() => setSubordinates([]))
    }
  }, [item.id, item.subordinate_count])

  useEffect(() => {
    if (!item.start_commit || !item.result_commit || item.start_commit === item.result_commit) return
    let cancelled = false
    getTaskOutcome(item.id).then(data => {
      if (!cancelled) setOutcomeSummary(data.summary)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [item.id, item.start_commit, item.result_commit])

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
          {STATUS_LABELS[status] || status}
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
            </span>
          ))}
        </div>
      </div>

      <div className="detail-section">
        <label>Context</label>
        <pre className="context-display" style={{
          color: ctx ? 'inherit' : 'var(--text-muted)',
          fontStyle: ctx ? 'normal' : 'italic',
        }}>
          {ctx || 'not provided'}
        </pre>
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

      {!item.error && (
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
            {status === 'timed_out' ? 'Task exceeded timeout limit' : item.error}
          </div>
        </div>
      )}

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
