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

// ── Tasks (atomic work items) ──

export const enqueueTask = (jobId, context) =>
  fetchJSON(`/api/tasks/${jobId}`, {
    method: 'POST',
    body: JSON.stringify({ context: context || undefined }),
  })

export const getTaskQueue = () => fetchJSON('/api/tasks/queue')

export const getTaskOutput = (taskId) =>
  fetchJSON(`/api/tasks/${taskId}/output`)

export const updateTask = (id, updates) =>
  fetchJSON(`/api/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(updates) })

export const getTaskDiff = (id) => fetchJSON(`/api/tasks/${id}/diff`)

export const getTaskOutcome = (id) => fetchJSON(`/api/tasks/${id}/outcome`)

export const cancelTask = (id) =>
  fetchJSON(`/api/tasks/cancel/${id}`, { method: 'POST' })

export const resumeTask = (id) =>
  fetchJSON(`/api/tasks/${id}/resume`, { method: 'POST' })

export const replyTask = (id, context) =>
  fetchJSON(`/api/tasks/${id}/reply`, {
    method: 'POST',
    body: JSON.stringify({ context }),
  })

export const streamTask = (taskId, onEvent) =>
  fetchSSE(`/api/tasks/${taskId}/stream`, {}, onEvent)

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

export const reorderTasks = (taskIds) =>
  fetchJSON('/api/queue/reorder', { method: 'POST', body: JSON.stringify({ task_ids: taskIds }) })

export const approveTask = (id) =>
  fetchJSON(`/api/tasks/${id}/approve`, { method: 'POST' })

export const rejectTask = (id) =>
  fetchJSON(`/api/tasks/${id}/reject`, { method: 'POST' })

export const mergeTasks = (taskIds) =>
  fetchJSON('/api/tasks/merge', { method: 'POST', body: JSON.stringify({ task_ids: taskIds }) })

export const splitTask = (id) =>
  fetchJSON(`/api/tasks/${id}/split`, { method: 'POST' })

export const uncoalesceTask = (id) =>
  fetchJSON(`/api/tasks/${id}/uncoalesce`, { method: 'POST' })

export const transferTask = (id, toQueued) =>
  fetchJSON(`/api/tasks/${id}/transfer`, {
    method: 'POST',
    body: JSON.stringify({ to_queued: toQueued }),
  })

export const getSubordinates = (id) =>
  fetchJSON(`/api/tasks/${id}/subordinates`)

// ── Feed ──

export const getFeed = (params = {}) => {
  const qs = new URLSearchParams(params).toString()
  return fetchJSON(`/api/feed/?${qs}`)
}

// ── Chat ──

export function sendChatMessage(message, sessionId, context, onEvent) {
  const body = JSON.stringify({
    message,
    session_id: sessionId || undefined,
    context: context || undefined,
  })
  return fetchSSE('/api/chat/', { method: 'POST', body }, onEvent)
}

export const getChatSessions = () => fetchJSON('/api/chat/sessions')

export const getChatMessages = (sessionId) =>
  fetchJSON(`/api/chat/sessions/${sessionId}/messages`)

export const deleteChatSession = (sessionId) =>
  fetchJSON(`/api/chat/sessions/${sessionId}`, { method: 'DELETE' })

export const getChatSessionStatus = (sessionId) =>
  fetchJSON(`/api/chat/sessions/${sessionId}/status`)

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
