import { useState, useEffect, useCallback, useRef } from 'react'
import {
  listGovernorThreads,
  getGovernorThread,
  createGovernorThread,
  replyGovernorThread,
  closeGovernorThread,
  reopenGovernorThread,
  markGovernorThreadRead,
  getGovernorStatus,
  getGovernorDebug,
  getGovernorRuns,
} from '../api'

export default function Governor({ onBadgeChange }) {
  const [threads, setThreads] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [thread, setThread] = useState(null)
  const [composing, setComposing] = useState(false)
  const [showClosed, setShowClosed] = useState(false)
  const [showDebug, setShowDebug] = useState(false)

  const refreshList = useCallback(async () => {
    try {
      const list = await listGovernorThreads()
      setThreads(list)
      const status = await getGovernorStatus()
      if (onBadgeChange) {
        onBadgeChange((status.unread_threads || 0) + (status.pending_proposals || 0))
      }
    } catch {}
  }, [onBadgeChange])

  const refreshSelected = useCallback(async () => {
    if (!selectedId) return
    try {
      const t = await getGovernorThread(selectedId)
      setThread(t)
    } catch {}
  }, [selectedId])

  useEffect(() => {
    refreshList()
    const interval = setInterval(refreshList, 5000)
    return () => clearInterval(interval)
  }, [refreshList])

  useEffect(() => {
    refreshSelected()
    if (!selectedId) return
    const interval = setInterval(refreshSelected, 5000)
    return () => clearInterval(interval)
  }, [selectedId, refreshSelected])

  const handleSelect = async (id) => {
    setSelectedId(id)
    try {
      await markGovernorThreadRead(id)
    } catch {}
    refreshList()
  }

  const handleNewThread = async ({ title, body }) => {
    const { thread_id } = await createGovernorThread({ title, body })
    setComposing(false)
    await refreshList()
    setSelectedId(thread_id)
  }

  const handleReply = async (body) => {
    if (!selectedId) return
    await replyGovernorThread(selectedId, body)
    refreshSelected()
    refreshList()
  }

  const handleClose = async () => {
    if (!selectedId) return
    await closeGovernorThread(selectedId)
    refreshSelected()
    refreshList()
  }

  const handleReopen = async () => {
    if (!selectedId) return
    await reopenGovernorThread(selectedId)
    refreshSelected()
    refreshList()
  }

  const open = threads.filter(t => t.status === 'open')
  const closed = threads.filter(t => t.status === 'closed')

  return (
    <div className="governor">
      <div className="governor-pane-list">
        <div className="governor-pane-header">
          <h2>Governor</h2>
          <button
            className="primary small"
            onClick={() => setComposing(true)}
            disabled={composing}
          >+ New Thread</button>
        </div>

        <div className="governor-thread-list">
          {open.length === 0 && !composing && (
            <div className="governor-empty">
              <p className="muted-text">No open threads.</p>
              <p className="muted-text">
                Start a conversation with the Governor by opening a new thread,
                or wait for the next survey (every 10 completed tasks).
              </p>
            </div>
          )}
          {open.map(t => (
            <ThreadRow
              key={t.id}
              thread={t}
              selected={t.id === selectedId}
              onSelect={() => handleSelect(t.id)}
            />
          ))}

          {closed.length > 0 && (
            <button
              className="governor-closed-toggle"
              onClick={() => setShowClosed(!showClosed)}
            >
              {showClosed ? '▾' : '▸'} Closed ({closed.length})
            </button>
          )}
          {showClosed && closed.map(t => (
            <ThreadRow
              key={t.id}
              thread={t}
              selected={t.id === selectedId}
              onSelect={() => handleSelect(t.id)}
            />
          ))}
        </div>

        <DebugDrawer expanded={showDebug} onToggle={() => setShowDebug(!showDebug)} />
      </div>

      <div className="governor-pane-thread">
        {thread ? (
          <ThreadView
            thread={thread}
            onReply={handleReply}
            onClose={handleClose}
            onReopen={handleReopen}
          />
        ) : (
          <div className="governor-empty">
            <p className="muted-text">Select a thread, or open a new one.</p>
          </div>
        )}
      </div>

      {composing && (
        <NewThreadComposer
          onCancel={() => setComposing(false)}
          onSubmit={handleNewThread}
        />
      )}
    </div>
  )
}


function ThreadRow({ thread, selected, onSelect }) {
  const last = thread.last_message
  const proposalPending = last && last.author === 'governor'
    && last.body && last.body.length > 0
    // NOTE: the precise "unexecuted action_payload" detection lives on
    // the backend (count_unexecuted_proposals); the row preview can't
    // re-derive it without per-thread fetches. We surface it via the
    // thread row's own data when the API includes it (future).
  return (
    <button
      className={`governor-thread-row${selected ? ' selected' : ''}${thread.status === 'closed' ? ' closed' : ''}`}
      onClick={onSelect}
    >
      <div className="governor-thread-row-line1">
        <span className="governor-thread-title">{thread.title}</span>
        {thread.unread_for_human && <span className="governor-thread-unread-dot" title="Unread Governor message" />}
      </div>
      <div className="governor-thread-row-line2">
        {last ? (
          <>
            <span className={`governor-thread-author governor-thread-author-${last.author}`}>{last.author}</span>
            <span className="governor-thread-preview">{last.body.slice(0, 80)}</span>
          </>
        ) : (
          <span className="muted-text">(empty)</span>
        )}
      </div>
      <div className="governor-thread-row-line3">
        <span className="muted-text">
          {thread.message_count || 0} message{thread.message_count === 1 ? '' : 's'} · {formatTime(thread.last_activity_at)}
        </span>
      </div>
    </button>
  )
}


function ThreadView({ thread, onReply, onClose, onReopen }) {
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const messagesEndRef = useRef(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [thread.messages])

  const handleSend = async () => {
    const body = draft.trim()
    if (!body || sending) return
    setSending(true)
    try {
      await onReply(body)
      setDraft('')
    } catch {}
    setSending(false)
  }

  const isOpen = thread.status === 'open'

  return (
    <div className="governor-thread-view">
      <div className="governor-thread-header">
        <span className="governor-thread-view-title">{thread.title}</span>
        {isOpen ? (
          <button className="small" onClick={onClose} title="Close (mute) thread">Close</button>
        ) : (
          <span className="muted-text">closed</span>
        )}
      </div>

      <div className="governor-messages">
        {(thread.messages || []).map(m => (
          <MessageCard key={m.id} message={m} />
        ))}
        <div ref={messagesEndRef} />
      </div>

      {isOpen ? (
        <div className="governor-compose">
          <textarea
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder="Reply to the Governor..."
            rows={Math.max(2, draft.split('\n').length)}
            onKeyDown={e => {
              if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault()
                handleSend()
              }
            }}
          />
          <div className="governor-compose-actions">
            <span className="muted-text governor-compose-hint">Ctrl+Enter to send</span>
            <button
              className="primary small"
              onClick={handleSend}
              disabled={!draft.trim() || sending}
            >
              {sending ? 'Sending…' : 'Send'}
            </button>
          </div>
        </div>
      ) : (
        <div className="governor-compose">
          <button className="primary small" onClick={onReopen}>Reopen Thread</button>
        </div>
      )}
    </div>
  )
}


function MessageCard({ message }) {
  const m = message
  return (
    <div className={`governor-message governor-message-${m.author}`}>
      <div className="governor-message-header">
        <span className={`governor-message-author governor-message-author-${m.author}`}>
          {m.author}
        </span>
        <span className="muted-text governor-message-time">{formatTime(m.created_at)}</span>
      </div>
      <div className="governor-message-body">{m.body}</div>
      {m.action_payload && <ActionPayloadCard payload={m.action_payload} />}
    </div>
  )
}


function ActionPayloadCard({ payload }) {
  const action = payload?.action || '(unknown action)'
  return (
    <div className="governor-action-payload">
      <div className="governor-action-payload-header">
        <span className="governor-action-payload-label">Proposed change</span>
        <span className="governor-action-payload-action">{action}</span>
      </div>
      <pre className="governor-action-payload-body">{JSON.stringify(payload, null, 2)}</pre>
    </div>
  )
}


function NewThreadComposer({ onCancel, onSubmit }) {
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async () => {
    const t = title.trim()
    const b = body.trim()
    if (!t || !b || submitting) return
    setSubmitting(true)
    try {
      await onSubmit({ title: t, body: b })
    } catch {
      setSubmitting(false)
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      onCancel()
    } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      handleSubmit()
    }
  }

  return (
    <div
      className="dispatch-modal-backdrop"
      onMouseDown={e => { if (e.target === e.currentTarget) onCancel() }}
    >
      <div className="dispatch-modal" onKeyDown={handleKeyDown}>
        <div className="dispatch-modal-header">
          <span className="dispatch-modal-title">New Thread</span>
          <button
            className="dispatch-modal-close"
            onClick={onCancel}
            title="Close (Esc)"
          >✕</button>
        </div>
        <div className="governor-new-thread-modal-body">
          <input
            type="text"
            className="governor-new-thread-modal-title-input"
            value={title}
            onChange={e => setTitle(e.target.value)}
            placeholder="Subject"
            maxLength={200}
            autoFocus
          />
          <textarea
            className="governor-new-thread-modal-body-textarea"
            value={body}
            onChange={e => setBody(e.target.value)}
            placeholder="What do you want to discuss with the Governor?"
          />
        </div>
        <div className="dispatch-modal-footer">
          <span className="dispatch-modal-hint">Ctrl+Enter to send · Esc to close</span>
          <div className="dispatch-modal-actions">
            <button onClick={onCancel}>Cancel</button>
            <button
              className="primary"
              onClick={handleSubmit}
              disabled={!title.trim() || !body.trim() || submitting}
            >
              {submitting ? 'Opening…' : 'Open Thread'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}


function DebugDrawer({ expanded, onToggle }) {
  const [debug, setDebug] = useState(null)
  const [runs, setRuns] = useState([])

  useEffect(() => {
    if (!expanded) return
    let cancelled = false
    const refresh = async () => {
      try {
        const [d, r] = await Promise.all([
          getGovernorDebug(),
          getGovernorRuns(),
        ])
        if (!cancelled) {
          setDebug(d)
          setRuns(r)
        }
      } catch {}
    }
    refresh()
    const interval = setInterval(refresh, 5000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [expanded])

  return (
    <div className="governor-debug-drawer">
      <button className="governor-debug-toggle" onClick={onToggle}>
        {expanded ? '▾' : '▸'} Debug
      </button>
      {expanded && (
        <div className="governor-debug-body">
          {!debug ? (
            <p className="muted-text">Loading...</p>
          ) : (
            <>
              <div className="governor-debug-row">
                <span>Counter</span><span>{debug.counter}/10</span>
              </div>
              <div className="governor-debug-row">
                <span>Last run</span><span>{debug.last_run ? formatTime(debug.last_run) : '—'} ({debug.last_trigger || '—'})</span>
              </div>
              <div className="governor-debug-row">
                <span>Queue depth</span><span>{debug.queue_depth ?? 0}</span>
              </div>
              <div className="governor-debug-row">
                <span>Running</span><span>{debug.running_lock ? 'yes' : 'no'}</span>
              </div>
              <div className="governor-debug-runs">
                {runs.length === 0 && <p className="muted-text">No runs yet.</p>}
                {runs.map(r => (
                  <div key={r.id} className="governor-debug-run">
                    <span className="governor-debug-run-trigger">{r.trigger}</span>
                    <span className="governor-debug-run-time">{formatTime(r.started_at)}</span>
                    <span className="governor-debug-run-msgs">
                      {r.message_count} msg{r.message_count === 1 ? '' : 's'}
                    </span>
                    {r.error && <span className="governor-debug-run-error" title={r.error}>error</span>}
                    {!r.completed_at && <span className="governor-debug-run-active">running</span>}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}


function formatTime(ts) {
  if (!ts) return ''
  try {
    const d = new Date(ts + (ts.includes('Z') || ts.includes('+') ? '' : 'Z'))
    const now = new Date()
    const diff = now - d
    if (diff < 60000) return 'just now'
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`
    return d.toLocaleDateString()
  } catch {
    return ts
  }
}
