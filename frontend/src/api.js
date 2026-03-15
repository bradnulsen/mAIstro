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

// ── Tasks ──

export const listTasks = () => fetchJSON('/api/tasks/')

export const createTask = (name, properties) =>
  fetchJSON('/api/tasks/', { method: 'POST', body: JSON.stringify({ name, properties }) })

export const updateTask = (id, updates) =>
  fetchJSON(`/api/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(updates) })

export const deleteTask = (id) =>
  fetchJSON(`/api/tasks/${id}`, { method: 'DELETE' })

export const getTaskSubscriptions = (id) => fetchJSON(`/api/tasks/${id}/subscriptions`)

// ── Dispatch ──

export const dispatchTask = (taskId, context) =>
  fetchJSON(`/api/dispatch/${taskId}`, {
    method: 'POST',
    body: JSON.stringify({ context: context || undefined }),
  })

export const getDispatchQueue = () => fetchJSON('/api/dispatch/queue')

export const getDispatchOutput = (dispatchId) =>
  fetchJSON(`/api/dispatch/${dispatchId}/output`)

export const updateDispatch = (id, updates) =>
  fetchJSON(`/api/dispatch/${id}`, { method: 'PATCH', body: JSON.stringify(updates) })

export const getDispatchDiff = (id) => fetchJSON(`/api/dispatch/${id}/diff`)

export const cancelDispatch = (id) =>
  fetchJSON(`/api/dispatch/cancel/${id}`, { method: 'POST' })

export const resumeDispatch = (id) =>
  fetchJSON(`/api/dispatch/${id}/resume`, { method: 'POST' })

export const retryDispatch = (id, context) =>
  fetchJSON(`/api/dispatch/${id}/retry`, {
    method: 'POST',
    body: JSON.stringify({ context }),
  })

export const streamDispatch = (dispatchId, onEvent) =>
  fetchSSE(`/api/dispatch/${dispatchId}/stream`, {}, onEvent)  // returns { abort, done }

// ── Queue Control ──

export const getQueueSettings = () => fetchJSON('/api/queue/settings')

export const setQueueSettings = (settings) =>
  fetchJSON('/api/queue/settings', { method: 'POST', body: JSON.stringify(settings) })

export const processQueue = (all = false) =>
  fetchJSON(`/api/queue/process?all=${all}`, { method: 'POST' })

export const processOne = (dispatchId) =>
  fetchJSON(`/api/queue/process/${dispatchId}`, { method: 'POST' })

export const approveDispatch = (id) =>
  fetchJSON(`/api/dispatch/${id}/approve`, { method: 'POST' })

export const rejectDispatch = (id) =>
  fetchJSON(`/api/dispatch/${id}/reject`, { method: 'POST' })

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

// ── Config ──

export const getConfig = () => fetchJSON('/api/config/')

export const setConfig = (key, value) =>
  fetchJSON(`/api/config/${key}`, { method: 'POST', body: JSON.stringify({ value }) })

// ── MCP Servers ──

export const listMcpServers = () => fetchJSON('/api/mcp/servers')

export const createMcpServer = (name, command, args, env) =>
  fetchJSON('/api/mcp/servers', {
    method: 'POST',
    body: JSON.stringify({ name, command, args: args || [], env: env || {} }),
  })

export const deleteMcpServer = (name) =>
  fetchJSON(`/api/mcp/servers/${encodeURIComponent(name)}`, { method: 'DELETE' })

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
