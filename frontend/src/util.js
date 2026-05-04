/** Shared utilities for mAistro frontend */

export const TRIGGER_ICONS = {
  commit: '⚡',
  task_queue: '↗',
  manual: '✋',
  human: '👤',
  task: '🤖',
  resume: '↻',
  reply: '⟳',
  cascade: '⛓',
  schedule: '⏰',
}

/**
 * Format a date string as relative time (e.g. "5m ago").
 * @param {string} dateStr - Date string to format
 * @param {boolean} utc - If true, append 'Z' before parsing (for UTC backend dates without timezone)
 */
export function formatDate(dateStr, utc = false) {
  if (!dateStr) return ''
  try {
    const d = new Date(utc ? dateStr + 'Z' : dateStr)
    const now = new Date()
    const diff = (now - d) / 1000
    if (diff < 60) return 'just now'
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
    return d.toLocaleDateString()
  } catch {
    return dateStr
  }
}

/** Convert single newlines to double so markdown renders paragraph breaks,
 *  but preserve single newlines inside markdown table blocks (consecutive | rows). */
export function mdBreaks(text) {
  if (!text) return text
  const lines = text.split('\n')
  let result = lines[0]
  for (let i = 1; i < lines.length; i++) {
    const prev = lines[i - 1]
    const curr = lines[i]
    const inTable = prev.trimStart().startsWith('|') && curr.trimStart().startsWith('|')
    if (curr === '' || prev === '' || inTable) {
      result += '\n' + curr
    } else {
      result += '\n\n' + curr
    }
  }
  return result
}

/**
 * Format a duration between two date strings as a human-readable string.
 * If endStr is null, uses the current time.
 */
export function formatDuration(startStr, endStr) {
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

/** Format a duration in seconds as a human-readable string. */
export function formatDurationSecs(secs) {
  if (secs == null) return ''
  secs = Math.max(0, Math.round(secs))
  if (secs < 60) return `${secs}s`
  const mins = Math.floor(secs / 60)
  const remSecs = secs % 60
  if (mins < 60) return `${mins}m ${remSecs}s`
  const hrs = Math.floor(mins / 60)
  const remMins = mins % 60
  return `${hrs}h ${remMins}m`
}

/** Human-readable labels for task event types. */
export const EVENT_LABELS = {
  created: 'Created',
  queued: 'Queued',
  unqueued: 'Unqueued',
  active: 'Started',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  timed_out: 'Timed Out',
  interrupted: 'Interrupted',
  rejected: 'Rejected',
  approved: 'Approved',
  coalesced: 'Coalesced',
  uncoalesced: 'Uncoalesced',
}

/* ── Task status derivation and labels ── */

export const STATUS_LABELS = {
  pending: 'Pending',
  queued: 'Queued',
  pending_approval: 'Needs Approval',
  running: 'Running',
  completed: 'Completed',
  exhausted: 'Exhausted',
  error: 'Error',
  cancelled: 'Cancelled',
  timed_out: 'Timed Out',
  interrupted: 'Interrupted',
  rejected: 'Rejected',
  resolved: 'Resolved',
}

// Terminal states where a per-task worktree is preserved for inspection.
// Mirror of backend db_tasks.NON_SUCCESS_TERMINAL_STATUSES — these are the
// only states where the WorkspaceBanner ("preserved workspace, decide
// what to do") and the workspace-discard endpoint are valid. Active tasks
// have a worktree but it's mid-flight; completed tasks already cleared
// it on integration; pending/queued/rejected never had one.
export const NON_SUCCESS_TERMINAL_STATUSES = ['exhausted', 'failed', 'cancelled', 'interrupted', 'timed_out']

export const TRIGGER_LABELS = {
  manual: 'User',
  commit: 'Commit',
  resume: 'Resume',
  reply: 'Reply',
  cascade: 'Cascade',
  schedule: 'Schedule',
}

const NON_SUCCESS_TERMINAL_SET = new Set(NON_SUCCESS_TERMINAL_STATUSES)

/** Get task status — uses authoritative status column from backend.
 *  The status column is materialized from the task_events log.
 *
 *  A non-success terminal (failed/exhausted/cancelled/interrupted/timed_out)
 *  whose workspace pointer has been cleared — operator integrated, operator
 *  discarded, or sweep cleared a stale pointer — renders as 'resolved'.
 *  The original lifecycle status is unchanged in the DB; this is purely a
 *  UI signal that there's nothing left for the operator to act on. */
export function getTaskStatus(item) {
  const s = item.status
  if (!s) return 'pending'
  // Map backend statuses to UI statuses
  if (s === 'active') return 'running'
  if (NON_SUCCESS_TERMINAL_SET.has(s) && !item.worktree_path) return 'resolved'
  if (s === 'failed') return 'error'
  // Check approval gate overlay (orthogonal to lifecycle status)
  if ((s === 'pending' || s === 'queued') && item.approval === 'pending') return 'pending_approval'
  return s
}

/** Format trigger type with detail suffix */
export function triggerLabel(item) {
  const base = TRIGGER_LABELS[item.trigger] || item.trigger
  if (item.trigger === 'commit' && item.trigger_detail) return `${base} (${item.trigger_detail.slice(0, 8)})`
  if (item.trigger === 'cascade' && item.trigger_detail) return `${base} (${item.trigger_detail})`
  if (item.trigger === 'manual' && item.trigger_detail) return `${base} @ ${item.trigger_detail.slice(0, 8)}`
  if (item.trigger === 'schedule' && item.trigger_detail) return `${base} (${item.trigger_detail})`
  return base
}

/** Extract text content from a chat message */
export function getMessageContent(msg) {
  if (typeof msg.content === 'string') return msg.content
  if (Array.isArray(msg.content)) return msg.content.filter(b => b.type === 'text').map(b => b.text).join('\n\n')
  return String(msg.content ?? '')
}
