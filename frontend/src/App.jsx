import { useState, useEffect, useCallback, useRef } from 'react'
import { getProject, openProject, browseProject, getRecentProjects, removeRecentProject, listGoals, enqueueTask, getQueueSettings, setQueueSettings, queueAll, shelveAll } from './api'
import Feed from './components/Feed'
import Tasks from './components/Tasks'
import Queue from './components/Queue'
import Chat from './components/Chat'
import Files from './components/Files'
import Settings from './components/Settings'
import McpServers from './components/McpServers'
import Dashboard from './components/Dashboard'

const VIEWS = { feed: 'feed', tasks: 'tasks', queue: 'queue', files: 'files', mcp: 'mcp', dashboard: 'dashboard', settings: 'settings' }

export default function App() {
  const [project, setProject] = useState(null)
  const [loading, setLoading] = useState(true)
  const [view, setView] = useState(VIEWS.dashboard)
  const [goals, setGoals] = useState([])
  const [chatOpen, setChatOpen] = useState(false)
  const [chatWidth, setChatWidth] = useState(380)
  const chatTrayRef = useRef(null)

  useEffect(() => {
    getProject()
      .then(p => { if (p.loaded) setProject(p) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const refreshGoals = useCallback(async () => {
    try {
      const list = await listGoals()
      setGoals(list)
    } catch {}
  }, [])

  useEffect(() => {
    if (project) refreshGoals()
  }, [project, refreshGoals])

  useEffect(() => {
    if (!project) return
    const interval = setInterval(refreshGoals, 5000)
    return () => clearInterval(interval)
  }, [project, refreshGoals])

  const handleTabMouseDown = useCallback((e) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = chatOpen ? chatWidth : 380
    let didDrag = false

    if (chatTrayRef.current) chatTrayRef.current.classList.add('dragging')

    const onMove = (e) => {
      const delta = startX - e.clientX
      if (!didDrag && Math.abs(delta) > 5) didDrag = true
      if (didDrag) {
        const newW = Math.max(250, Math.min(800, startW + delta))
        setChatWidth(newW)
        if (!chatOpen) setChatOpen(true)
      }
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      if (chatTrayRef.current) chatTrayRef.current.classList.remove('dragging')
      if (!didDrag) setChatOpen(o => !o)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [chatOpen, chatWidth])

  if (loading) return <div className="loading">Loading...</div>
  if (!project) return <ProjectOpener onOpen={setProject} />

  return (
    <div className="app-shell">
      <nav className="rail">
        <div className="rail-logo" onClick={() => { setProject(null); setGoals([]) }} title="Switch project">⬡</div>
        <button
          className={`rail-icon ${view === VIEWS.dashboard ? 'active' : ''}`}
          onClick={() => setView(VIEWS.dashboard)}
          title="Dashboard"
        >⊞</button>
        <button
          className={`rail-icon ${view === VIEWS.queue ? 'active' : ''}`}
          onClick={() => setView(VIEWS.queue)}
          title="Dispatch"
        >▶</button>
        <button
          className={`rail-icon ${view === VIEWS.feed ? 'active' : ''}`}
          onClick={() => setView(VIEWS.feed)}
          title="Activity"
        >☰</button>
        <button
          className={`rail-icon ${view === VIEWS.tasks ? 'active' : ''}`}
          onClick={() => setView(VIEWS.tasks)}
          title="Goals"
        >◉</button>
        <button
          className={`rail-icon ${view === VIEWS.files ? 'active' : ''}`}
          onClick={() => setView(VIEWS.files)}
          title="Files"
        >&#9783;</button>
        <button
          className={`rail-icon ${view === VIEWS.mcp ? 'active' : ''}`}
          onClick={() => setView(VIEWS.mcp)}
          title="MCP Servers"
        >⧈</button>
        <div className="rail-spacer" />
        <button
          className={`rail-icon ${view === VIEWS.settings ? 'active' : ''}`}
          onClick={() => setView(VIEWS.settings)}
          title="Settings"
        >&#9881;</button>
      </nav>

      <div className="main-area">
        <CommandBar goals={goals} onNavigate={setView} refreshGoals={refreshGoals} />

        {view === VIEWS.feed && <Feed />}
        {view === VIEWS.tasks && <Tasks goals={goals} onRefresh={refreshGoals} />}
        {view === VIEWS.queue && <Queue />}
        {view === VIEWS.files && <Files />}
        {view === VIEWS.mcp && <McpServers />}
        {view === VIEWS.dashboard && <Dashboard />}
        {view === VIEWS.settings && <Settings />}
      </div>

      <div ref={chatTrayRef} className={`chat-tray ${chatOpen ? 'open' : ''}`} style={chatOpen ? { width: chatWidth, minWidth: chatWidth } : undefined}>
        <div className="chat-tray-tab" onMouseDown={handleTabMouseDown}>
          Chat
        </div>
        <div className="chat-tray-content">
          <div className="chat-tray-header">
            <span>Chat</span>
            <button className="small" onClick={() => setChatOpen(false)}>✕</button>
          </div>
          <Chat />
        </div>
      </div>
    </div>
  )
}

function CommandBar({ goals, onNavigate, refreshGoals }) {
  const [autoQueue, setAutoQueue] = useState(false)
  const [popout, setPopout] = useState(null) // goal id
  const [context, setContext] = useState('')
  const [dispatching, setDispatching] = useState(false)
  const popoutRef = useRef(null)

  // Load auto-queue setting
  useEffect(() => {
    getQueueSettings().then(s => setAutoQueue(s.auto_dispatch)).catch(() => {})
  }, [])

  // Close popout on outside click
  useEffect(() => {
    if (!popout) return
    const handler = (e) => {
      if (popoutRef.current && !popoutRef.current.contains(e.target)) {
        setPopout(null)
        setContext('')
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [popout])

  const handleAutoQueueToggle = async () => {
    const next = !autoQueue
    setAutoQueue(next)
    await setQueueSettings({ auto_dispatch: next }).catch(() => setAutoQueue(!next))
  }

  const handleDispatch = async (goalId) => {
    setDispatching(true)
    try {
      await enqueueTask(goalId, context || undefined)
      setPopout(null)
      setContext('')
      refreshGoals()
    } catch {}
    setDispatching(false)
  }

  const handleQueueAll = async () => {
    await queueAll().catch(() => {})
    refreshGoals()
    window.dispatchEvent(new Event('queue-refresh'))
  }

  const handleShelveAll = async () => {
    await shelveAll().catch(() => {})
    refreshGoals()
    window.dispatchEvent(new Event('queue-refresh'))
  }

  const hasPending = goals.some(g => (g.properties?.pending_count || 0) > 0)
  const hasQueued = goals.some(g => (g.properties?.queued_count || 0) > 0)

  return (
    <div className="command-bar">
      <div className="command-bar-goals">
        {goals.map((g, i) => {
          const p = g.properties || {}
          const colorIdx = i % 10
          const state = p.running ? 'active' : (p.queued_count > 0 ? 'queued' : (p.pending_count > 0 ? 'pending' : 'idle'))
          return (
            <div key={g.id} className="command-bar-indicator-wrap" ref={popout === g.id ? popoutRef : undefined}>
              <button
                className={`command-bar-indicator ${state}`}
                style={{ '--goal-c': `var(--goal-color-${colorIdx})` }}
                onClick={() => { setPopout(popout === g.id ? null : g.id); setContext('') }}
                title={`${g.name} — ${state}`}
              >
                <span className={`command-bar-dot ${state}`} />
                <span className="command-bar-name">{g.name}</span>
              </button>
              {popout === g.id && (
                <div className="command-bar-popout">
                  <div className="command-bar-popout-header">{g.name}</div>
                  <textarea
                    className="command-bar-popout-context"
                    value={context}
                    onChange={e => setContext(e.target.value)}
                    placeholder="Context (optional)"
                    rows={2}
                    autoFocus
                    onKeyDown={e => {
                      if (e.key === 'Enter' && !e.shiftKey && !dispatching) {
                        e.preventDefault()
                        handleDispatch(g.id)
                      }
                      if (e.key === 'Escape') { setPopout(null); setContext('') }
                    }}
                  />
                  <button
                    className="primary small"
                    onClick={() => handleDispatch(g.id)}
                    disabled={dispatching}
                  >
                    {dispatching ? 'Dispatching...' : '▶ Dispatch'}
                  </button>
                </div>
              )}
            </div>
          )
        })}
        {goals.length === 0 && <span className="muted-text">No goals configured</span>}
      </div>

      <div className="command-bar-actions">
        <button className="small" onClick={handleQueueAll} disabled={!hasPending} title="Queue all pending tasks">▶ Queue all</button>
        <button className="small" onClick={handleShelveAll} disabled={!hasQueued} title="Shelve all queued tasks">▣ Shelve all</button>
        <label className="command-bar-toggle" title="Auto-queue: new tasks skip pending and go directly to queued">
          <input type="checkbox" checked={autoQueue} onChange={handleAutoQueueToggle} />
          <span>Auto</span>
        </label>
      </div>
    </div>
  )
}

function ProjectOpener({ onOpen }) {
  const [path, setPath] = useState('')
  const [error, setError] = useState('')
  const [opening, setOpening] = useState(false)
  const [browsing, setBrowsing] = useState(false)
  const [recent, setRecent] = useState([])

  useEffect(() => {
    getRecentProjects().then(setRecent).catch(() => {})
  }, [])

  const handleOpen = async (openPath) => {
    const target = (openPath || path).trim()
    if (!target) return
    setOpening(true)
    setError('')
    try {
      await openProject(target)
      const p = await getProject()
      onOpen(p)
    } catch (e) {
      setError(e.message)
    } finally {
      setOpening(false)
    }
  }

  const handleBrowse = async () => {
    setBrowsing(true)
    setError('')
    try {
      const result = await browseProject()
      if (result.path) {
        setPath(result.path)
        await handleOpen(result.path)
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setBrowsing(false)
    }
  }

  const handleRemoveRecent = async (e, projectPath) => {
    e.stopPropagation()
    await removeRecentProject(projectPath)
    setRecent(prev => prev.filter(r => r.path !== projectPath))
  }

  return (
    <div className="project-opener">
      <h1>⬡ mAistro</h1>
      <p>Open a project directory to begin</p>
      <div className="input-row">
        <input
          type="text"
          placeholder="/path/to/project"
          value={path}
          onChange={e => setPath(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleOpen()}
        />
        <button className="primary" onClick={() => handleOpen()} disabled={opening}>
          {opening ? '...' : 'Open'}
        </button>
        <button onClick={handleBrowse} disabled={browsing || opening}>
          {browsing ? '...' : 'Browse'}
        </button>
      </div>
      {error && <p className="project-error">{error}</p>}

      {recent.length > 0 && (
        <div className="recent-section">
          <p className="recent-section-header">Recent projects</p>
          {recent.map(r => (
            <div
              key={r.path}
              className="recent-project"
              onClick={() => handleOpen(r.path)}
            >
              <div className="recent-project-info">
                <div className="recent-project-name">{r.name}</div>
                <div className="recent-project-path">{r.path}</div>
              </div>
              <button
                className="small recent-project-remove"
                onClick={(e) => handleRemoveRecent(e, r.path)}
                title="Remove from recent"
              >✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
