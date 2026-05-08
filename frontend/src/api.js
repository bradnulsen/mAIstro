/** API client for mAistro backend */

const BASE = ''  // proxied by vite

async function fetchJSON(path, opts = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  })
  if (!res.ok) {
    const err = await res.text()
    throw new Error(`${res.status}: ${err}`)
  }
  return res.json()
}

// ── Project ──

export const getProject = () => fetchJSON('/api/project/')

export const openProject = (path) =>
  fetchJSON('/api/project/open', { method: 'POST', body: JSON.stringify({ path }) })

export const browseProject = () =>
  fetchJSON('/api/project/browse', { method: 'POST' })

export const getRecentProjects = () => fetchJSON('/api/project/recent')

export const removeRecentProject = (path) =>
  fetchJSON(`/api/project/recent?path=${encodeURIComponent(path)}`, { method: 'DELETE' })

// ── Jobs (persistent config) ──

export const listJobs = () => fetchJSON('/api/jobs/')

export const createJob = (name, properties) =>
  fetchJSON('/api/jobs/', { method: 'POST', body: JSON.stringify({ name, properties }) })

export const updateJob = (id, updates) =>
  fetchJSON(`/api/jobs/${id}`, { method: 'PATCH', body: JSON.stringify(updates) })

export const deleteJob = (id) =>
  fetchJSON(`/api/jobs/${id}`, { method: 'DELETE' })

export const getJobSubscriptions = (id) => fetchJSON(`/api/jobs/${id}/subscriptions`)

export const reorderJobs = (jobIds) =>
  fetchJSON('/api/jobs/reorder', { method: 'POST', body: JSON.stringify({ job_ids: jobIds }) })

// ── Learnings (per-job structured guidance) ──

export const listLearnings = (jobId) => fetchJSON(`/api/jobs/${jobId}/learnings`)

export const createLearning = (jobId, { summary, body }) =>
  fetchJSON(`/api/jobs/${jobId}/learnings`, {
    method: 'POST',
    body: JSON.stringify({ summary, body }),
  })

export const updateLearning = (id, updates) =>
  fetchJSON(`/api/learnings/${id}`, { method: 'PATCH', body: JSON.stringify(updates) })

export const deleteLearning = (id) =>
  fetchJSON(`/api/learnings/${id}`, { method: 'DELETE' })

export const reorderLearnings = (jobId, learningIds) =>
  fetchJSON(`/api/jobs/${jobId}/learnings/reorder`, {
    method: 'POST',
    body: JSON.stringify({ learning_ids: learningIds }),
  })

// ── Tasks (atomic work items) ──

export const enqueueTrigger = (jobId, context) =>
  fetchJSON(`/api/triggers/${jobId}`, {
    method: 'POST',
    body: JSON.stringify({ context: context || undefined }),
  })

export const getTriggerQueue = () => fetchJSON('/api/triggers/queue')

export const getTriggerOutput = (taskId) =>
  fetchJSON(`/api/triggers/${taskId}/output`)

export const updateTrigger = (id, updates) =>
  fetchJSON(`/api/triggers/${id}`, { method: 'PATCH', body: JSON.stringify(updates) })

export const getTriggerDiff = (id) => fetchJSON(`/api/triggers/${id}/diff`)

export const getTriggerOutcome = (id) => fetchJSON(`/api/triggers/${id}/outcome`)

export const integrateTriggerWorkspace = (id, strategy = 'default') =>
  fetchJSON(`/api/triggers/${id}/workspace/integrate?strategy=${strategy}`, { method: 'POST' })

export const discardTriggerWorkspace = (id) =>
  fetchJSON(`/api/triggers/${id}/workspace/discard`, { method: 'POST' })

export const getOrphanStash = (id) => fetchJSON(`/api/triggers/${id}/orphan-stash`)

export const restoreOrphanStash = (id) =>
  fetchJSON(`/api/triggers/${id}/orphan-stash/restore`, { method: 'POST' })

export const discardOrphanStash = (id) =>
  fetchJSON(`/api/triggers/${id}/orphan-stash/discard`, { method: 'POST' })

export const cancelTrigger = (id) =>
  fetchJSON(`/api/triggers/cancel/${id}`, { method: 'POST' })

export const resumeTrigger = (id) =>
  fetchJSON(`/api/triggers/${id}/resume`, { method: 'POST' })

export const replyTrigger = (id, context) =>
  fetchJSON(`/api/triggers/${id}/reply`, {
    method: 'POST',
    body: JSON.stringify({ context }),
  })

export const streamTrigger = (taskId, onEvent) =>
  fetchSSE(`/api/triggers/${taskId}/stream`, {}, onEvent)

// ── Queue Control ──

export const getQueueSettings = () => fetchJSON('/api/queue/settings')

export const setQueueSettings = (settings) =>
  fetchJSON('/api/queue/settings', { method: 'POST', body: JSON.stringify(settings) })

export const queueAll = () =>
  fetchJSON('/api/queue/queue-all', { method: 'POST' })

export const shelveAll = () =>
  fetchJSON('/api/queue/shelve-all', { method: 'POST' })

export const processOne = (taskId) =>
  fetchJSON(`/api/queue/process/${taskId}`, { method: 'POST' })

export const reorderTriggers = (taskIds) =>
  fetchJSON('/api/queue/reorder', { method: 'POST', body: JSON.stringify({ task_ids: taskIds }) })

export const approveTrigger = (id) =>
  fetchJSON(`/api/triggers/${id}/approve`, { method: 'POST' })

export const rejectTrigger = (id) =>
  fetchJSON(`/api/triggers/${id}/reject`, { method: 'POST' })

export const mergeTriggers = (taskIds) =>
  fetchJSON('/api/triggers/merge', { method: 'POST', body: JSON.stringify({ task_ids: taskIds }) })

export const splitTrigger = (id) =>
  fetchJSON(`/api/triggers/${id}/split`, { method: 'POST' })

export const uncoalesceTrigger = (id) =>
  fetchJSON(`/api/triggers/${id}/uncoalesce`, { method: 'POST' })

export const transferTrigger = (id, toQueued) =>
  fetchJSON(`/api/triggers/${id}/transfer`, {
    method: 'POST',
    body: JSON.stringify({ to_queued: toQueued }),
  })

export const getSubordinates = (id) =>
  fetchJSON(`/api/triggers/${id}/subordinates`)

// ── Feed ──

export const getFeed = (params = {}) => {
  const qs = new URLSearchParams(params).toString()
  return fetchJSON(`/api/feed/?${qs}`)
}

// ── Governor (threads) ──

export const listGovernorThreads = (status) => {
  const qs = status ? `?status=${encodeURIComponent(status)}` : ''
  return fetchJSON(`/api/governor/threads${qs}`)
}

export const getGovernorThread = (id) =>
  fetchJSON(`/api/governor/threads/${id}`)

export const createGovernorThread = ({ title, body }) =>
  fetchJSON('/api/governor/threads', {
    method: 'POST',
    body: JSON.stringify({ title, body }),
  })

export const replyGovernorThread = (id, body) =>
  fetchJSON(`/api/governor/threads/${id}/reply`, {
    method: 'POST',
    body: JSON.stringify({ body }),
  })

export const closeGovernorThread = (id) =>
  fetchJSON(`/api/governor/threads/${id}/close`, { method: 'POST' })

export const reopenGovernorThread = (id) =>
  fetchJSON(`/api/governor/threads/${id}/reopen`, { method: 'POST' })

export const markGovernorThreadRead = (id) =>
  fetchJSON(`/api/governor/threads/${id}/mark-read`, { method: 'POST' })

export const getGovernorStatus = () => fetchJSON('/api/governor/status')

export const getGovernorDebug = () => fetchJSON('/api/governor/debug')

export const getGovernorRuns = () => fetchJSON('/api/governor/runs')

// ── Job Templates ──

export const listTemplates = () => fetchJSON('/api/templates')

export const saveTemplate = (name, properties) =>
  fetchJSON('/api/templates', { method: 'POST', body: JSON.stringify({ name, properties }) })

export const updateTemplate = (id, name, properties) =>
  fetchJSON(`/api/templates/${id}`, { method: 'PATCH', body: JSON.stringify({ name, properties }) })

export const deleteTemplate = (id) =>
  fetchJSON(`/api/templates/${id}`, { method: 'DELETE' })

// ── Dashboard ──

export const getDashboard = (window = 7) =>
  fetchJSON(`/api/dashboard?window=${window}`)

// ── Config ──

export const getConfig = () => fetchJSON('/api/config/')

export const setConfig = (key, value) =>
  fetchJSON(`/api/config/${key}`, { method: 'POST', body: JSON.stringify({ value }) })

// ── Tools ──

export const getToolInventory = () => fetchJSON('/api/tools/inventory')

// ── MCP Servers ──

export const listMcpServers = () => fetchJSON('/api/mcp/servers')

export const createMcpServer = (name, command, args, env) =>
  fetchJSON('/api/mcp/servers', {
    method: 'POST',
    body: JSON.stringify({ name, command, args: args || [], env: env || {} }),
  })

export const updateMcpServer = (name, updates) =>
  fetchJSON(`/api/mcp/servers/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    body: JSON.stringify(updates),
  })

export const deleteMcpServer = (name) =>
  fetchJSON(`/api/mcp/servers/${encodeURIComponent(name)}`, { method: 'DELETE' })

export const probeMcpServer = (name) =>
  fetchJSON(`/api/mcp/servers/${encodeURIComponent(name)}/tools`)

export const getMcpServerJobs = (name) =>
  fetchJSON(`/api/mcp/servers/${encodeURIComponent(name)}/jobs`)

// ── MCP Bundles (.mcpb import) ──

/** Stream a .mcpb file to the preview endpoint. Returns staging metadata. */
export async function previewMcpBundle(file) {
  const res = await fetch('/api/mcp/bundles/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: file,
  })
  if (!res.ok) {
    const err = await res.text()
    throw new Error(`${res.status}: ${err}`)
  }
  return res.json()
}

/** Finalize a staged bundle. On 409 returns {conflict: true} so caller can prompt overwrite. */
export async function installMcpBundle({ stagingId, name, userConfig, overwrite }) {
  const res = await fetch('/api/mcp/bundles/install', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      staging_id: stagingId,
      name,
      user_config: userConfig || {},
      overwrite: !!overwrite,
    }),
  })
  if (res.status === 409) {
    return { conflict: true }
  }
  if (!res.ok) {
    const err = await res.text()
    throw new Error(`${res.status}: ${err}`)
  }
  return res.json()
}

export const cancelMcpBundleStaging = (stagingId) =>
  fetch(`/api/mcp/bundles/staging/${encodeURIComponent(stagingId)}`, { method: 'DELETE' })

/** Best-effort cancel for tab-close. Sync, no await. */
export function beaconCancelStaging(stagingId) {
  if (!stagingId || !navigator.sendBeacon) return
  try {
    navigator.sendBeacon(
      `/api/mcp/bundles/staging/${encodeURIComponent(stagingId)}`,
      new Blob([], { type: 'application/json' })
    )
  } catch {}
}

// ── Files ──

export const searchFiles = (pattern) =>
  fetchJSON(`/api/files/?pattern=${encodeURIComponent(pattern)}`)

export const readFile = (path) => fetchJSON(`/api/git/file/${path}`)

// ── Git ──

export const getGitDiff = (hash) => fetchJSON(`/api/git/diff/${hash}`)

// ── SSE Helper ──

/**
 * Connect to an SSE endpoint and deliver events via callback.
 * Returns { abort, done } synchronously — abort() cancels the connection,
 * done is a Promise that resolves when the stream finishes.
 */
function fetchSSE(url, opts, onEvent) {
  const controller = new AbortController()

  const done = (async () => {
    const res = await fetch(url, {
      ...opts,
      headers: { 'Content-Type': 'application/json', ...opts.headers },
      signal: controller.signal,
    })

    if (!res.ok) {
      const err = await res.text()
      throw new Error(`${res.status}: ${err}`)
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    let currentEventType = null
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEventType = line.slice(7).trim()
          } else if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6))
              if (currentEventType && !data.type) data.type = currentEventType
              onEvent(data)
            } catch {
              // ignore parse errors
            }
            currentEventType = null
          } else if (line === '') {
            currentEventType = null
          }
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') throw err
    }
  })()

  return { abort: () => controller.abort(), done }
}
