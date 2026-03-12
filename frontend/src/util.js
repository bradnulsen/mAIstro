/** Shared utilities for mAistro frontend */

export const TRIGGER_ICONS = {
  commit: '⚡',
  task_queue: '↗',
  manual: '→',
  human: '👤',
  task: '🤖',
  resume: '↻',
  retry: '⟳',
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
