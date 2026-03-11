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

// ── Agents ──

export const listAgents = () => fetchJSON('/api/agents/')

export const getAgent = (id) => fetchJSON(`/api/agents/${id}`)

export const createAgent = (name, properties) =>
  fetchJSON('/api/agents/', { method: 'POST', body: JSON.stringify({ name, properties }) })

export const updateAgent = (id, updates) =>
  fetchJSON(`/api/agents/${id}`, { method: 'PATCH', body: JSON.stringify(updates) })

export const deleteAgent = (id) =>
  fetchJSON(`/api/agents/${id}`, { method: 'DELETE' })

export const getAgentArtifacts = (id) => fetchJSON(`/api/agents/${id}/artifacts`)

// ── Dispatch ──

export function dispatchAgent(agentId, instructions, onEvent) {
  const body = instructions ? JSON.stringify({ instructions }) : '{}'
  const eventSource = new EventSource(
    // EventSource only supports GET, so we use fetch for POST SSE
    `/api/dispatch/${agentId}`
  )

  // Use fetch with SSE parsing for POST
  return fetchSSE(`/api/dispatch/${agentId}`, { method: 'POST', body }, onEvent)
}

export const getDispatchQueue = () => fetchJSON('/api/dispatch/queue')

export const cancelDispatch = (id) =>
  fetchJSON(`/api/dispatch/cancel/${id}`, { method: 'POST' })

// ── Feed ──

export const getFeed = (params = {}) => {
  const qs = new URLSearchParams(params).toString()
  return fetchJSON(`/api/feed/?${qs}`)
}

export const getFeedItem = (hash) => fetchJSON(`/api/feed/${hash}`)

// ── Chat ──

export function sendChatMessage(agentId, message, sessionId, context, onEvent) {
  const body = JSON.stringify({
    agent_id: agentId,
    message,
    session_id: sessionId || undefined,
    context: context || undefined,
  })
  return fetchSSE('/api/chat/', { method: 'POST', body }, onEvent)
}

export const getChatSessions = (agentId) => {
  const qs = agentId ? `?agent_id=${agentId}` : ''
  return fetchJSON(`/api/chat/sessions${qs}`)
}

export const getChatMessages = (sessionId) =>
  fetchJSON(`/api/chat/sessions/${sessionId}/messages`)

export const deleteChatSession = (sessionId) =>
  fetchJSON(`/api/chat/sessions/${sessionId}`, { method: 'DELETE' })

// ── Git ──

export const getGitLog = (limit = 50) => fetchJSON(`/api/git/log?limit=${limit}`)

export const getGitDiff = (hash) => fetchJSON(`/api/git/diff/${hash}`)

export const getGitFile = (path) => fetchJSON(`/api/git/file/${path}`)

export const writeGitFile = (path, content, message) =>
  fetchJSON(`/api/git/file/${path}`, {
    method: 'PUT',
    body: JSON.stringify({ content, message }),
  })

// ── SSE Helper ──

async function fetchSSE(url, opts, onEvent) {
  const controller = new AbortController()
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

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            onEvent(data)
          } catch {
            // ignore parse errors
          }
        } else if (line.startsWith('event: ')) {
          // event type line — next data line will have the payload
        }
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') throw err
  }

  return { abort: () => controller.abort() }
}
