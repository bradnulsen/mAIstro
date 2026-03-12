/** Shared utilities for mAistro frontend */

export const TRIGGER_ICONS = {
  commit: '⚡',
  task_queue: '↗',
  manual: '→',
  human: '👤',
  task: '🤖',
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

/** Convert single newlines to double so markdown renders them as paragraph breaks */
export function mdBreaks(text) {
  if (!text) return text
  return text.replace(/\n(?!\n)/g, '\n\n')
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
