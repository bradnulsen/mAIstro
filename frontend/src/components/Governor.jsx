import { useState, useEffect, useCallback } from 'react'
import {
  getGovernorFindings,
  getGovernorStatus,
  getGovernorRuns,
  approveGovernorFinding,
  declineGovernorFinding,
  readGovernorFinding,
  dismissGovernorFinding,
  triggerGovernor,
} from '../api'

export default function Governor({ onBadgeChange }) {
  const [findings, setFindings] = useState([])
  const [runs, setRuns] = useState([])
  const [status, setStatus] = useState(null)
  const [showRuns, setShowRuns] = useState(false)
  const [triggering, setTriggering] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [f, s, r] = await Promise.all([
        getGovernorFindings(),
        getGovernorStatus(),
        getGovernorRuns(),
      ])
      setFindings(f)
      setStatus(s)
      setRuns(r)
      if (onBadgeChange) {
        onBadgeChange((s.pending_suggestions || 0) + (s.unread_observations || 0))
      }
    } catch {}
  }, [onBadgeChange])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, 5000)
    return () => clearInterval(interval)
  }, [refresh])

  const handleTrigger = async () => {
    setTriggering(true)
    try {
      await triggerGovernor()
      // Poll briefly for the run to start
      setTimeout(refresh, 1000)
    } catch {}
    setTriggering(false)
  }

  const handleApprove = async (id) => {
    await approveGovernorFinding(id).catch(() => {})
    refresh()
  }

  const handleDecline = async (id) => {
    await declineGovernorFinding(id).catch(() => {})
    refresh()
  }

  const handleRead = async (id) => {
    await readGovernorFinding(id).catch(() => {})
    refresh()
  }

  const handleDismiss = async (id) => {
    await dismissGovernorFinding(id).catch(() => {})
    refresh()
  }

  const statusText = status
    ? status.running
      ? 'Running...'
      : `Idle (${status.counter}/10 tasks)`
    : 'Loading...'

  return (
    <div className="governor">
      <div className="governor-header">
        <div className="governor-header-left">
          <h2>Governor</h2>
          <span className="governor-status">{statusText}</span>
        </div>
        <div className="governor-header-right">
          <button
            className="primary small"
            onClick={handleTrigger}
            disabled={triggering || (status && status.running)}
          >
            {triggering || (status && status.running) ? 'Running...' : 'Run Analysis'}
          </button>
        </div>
      </div>

      <div className="governor-findings">
        {findings.length === 0 && (
          <div className="governor-empty">
            <p>No findings yet.</p>
            <p className="muted-text">
              The Governor runs automatically every 10 completed tasks,
              or you can trigger it manually.
            </p>
          </div>
        )}

        {findings.map(f => (
          <FindingCard
            key={f.id}
            finding={f}
            onApprove={handleApprove}
            onDecline={handleDecline}
            onRead={handleRead}
            onDismiss={handleDismiss}
          />
        ))}
      </div>

      <div className="governor-runs-section">
        <button
          className="governor-runs-toggle"
          onClick={() => setShowRuns(!showRuns)}
        >
          {showRuns ? '▾' : '▸'} Run History ({runs.length})
        </button>

        {showRuns && (
          <div className="governor-runs">
            {runs.length === 0 && <p className="muted-text">No runs yet.</p>}
            {runs.map(r => (
              <div key={r.id} className="governor-run">
                <span className="governor-run-trigger">{r.trigger}</span>
                <span className="governor-run-time">{formatTime(r.started_at)}</span>
                <span className="governor-run-findings">
                  {r.findings_count} finding{r.findings_count !== 1 ? 's' : ''}
                </span>
                {r.error && <span className="governor-run-error" title={r.error}>error</span>}
                {!r.completed_at && <span className="governor-run-active">running</span>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function FindingCard({ finding, onApprove, onDecline, onRead, onDismiss }) {
  const [expanded, setExpanded] = useState(false)
  const f = finding

  const isSuggestion = f.type === 'suggestion'
  const isPending = f.status === 'pending'
  const isUnread = f.status === 'unread'

  // Auto-mark observations as read when expanded
  useEffect(() => {
    if (expanded && isUnread) {
      onRead(f.id)
    }
  }, [expanded, isUnread, f.id, onRead])

  return (
    <div className={`governor-finding ${f.type} ${f.status}`}>
      <div className="governor-finding-header" onClick={() => setExpanded(!expanded)}>
        <span className={`governor-finding-type ${f.type}`}>
          {isSuggestion ? '💡' : '👁'}
        </span>
        <span className="governor-finding-title">{f.title}</span>
        <span className={`governor-finding-status ${f.status}`}>{f.status}</span>
        <span className="governor-finding-time">{formatTime(f.created_at)}</span>
      </div>

      {expanded && (
        <div className="governor-finding-body">
          <p>{f.body}</p>

          {f.execution_result && (
            <div className="governor-finding-result">
              <strong>Result:</strong> {f.execution_result}
            </div>
          )}

          <div className="governor-finding-actions">
            {isSuggestion && isPending && (
              <>
                <button className="primary small" onClick={() => onApprove(f.id)}>Approve</button>
                <button className="small" onClick={() => onDecline(f.id)}>Decline</button>
              </>
            )}
            {!isSuggestion && (isUnread || f.status === 'read') && (
              <button className="small" onClick={() => onDismiss(f.id)}>Dismiss</button>
            )}
          </div>
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
