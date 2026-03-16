import { useState, useEffect, useCallback, useRef } from 'react'
import { getProject, openProject, browseProject, getRecentProjects, removeRecentProject, listJobs } from './api'
import Feed from './components/Feed'
import Tasks from './components/Tasks'
import Queue from './components/Queue'
import Chat from './components/Chat'
import Files from './components/Files'
import Settings from './components/Settings'

const VIEWS = { feed: 'feed', tasks: 'tasks', queue: 'queue', files: 'files', settings: 'settings' }

export default function App() {
  const [project, setProject] = useState(null)
  const [loading, setLoading] = useState(true)
  const [view, setView] = useState(VIEWS.queue)
  const [jobs, setJobs] = useState([])
  const [chatOpen, setChatOpen] = useState(false)
  const [chatWidth, setChatWidth] = useState(380)
  const chatTrayRef = useRef(null)

  useEffect(() => {
    getProject()
      .then(p => { if (p.loaded) setProject(p) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const refreshJobs = useCallback(async () => {
    try {
      const list = await listJobs()
      setJobs(list)
    } catch {}
  }, [])

  useEffect(() => {
    if (project) refreshJobs()
  }, [project, refreshJobs])

  useEffect(() => {
    if (!project) return
    const interval = setInterval(refreshJobs, 5000)
    return () => clearInterval(interval)
  }, [project, refreshJobs])

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
        <div className="rail-logo" onClick={() => { setProject(null); setJobs([]) }} title="Switch project">⬡</div>
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
          title="Jobs"
        >◉</button>
        <button
          className={`rail-icon ${view === VIEWS.files ? 'active' : ''}`}
          onClick={() => setView(VIEWS.files)}
          title="Files"
        >&#9783;</button>
        <div className="rail-spacer" />
        <button
          className={`rail-icon ${view === VIEWS.settings ? 'active' : ''}`}
          onClick={() => setView(VIEWS.settings)}
          title="Settings"
        >&#9881;</button>
      </nav>

      <div className="main-area">
        <div className="status-bar">
          {jobs.map(j => j.properties?.running ? (
            <div
              key={j.id}
              className="status-chip running"
              onClick={() => setView(VIEWS.queue)}
              title="View in Dispatch"
              style={{ cursor: 'pointer' }}
            >
              <span className="status-dot running" />
              {j.name}
            </div>
          ) : (
            <span key={j.id} className="status-idle-task">{j.name}</span>
          ))}
          {jobs.length === 0 && <span style={{ color: 'var(--text-muted)' }}>No jobs configured</span>}
        </div>

        {view === VIEWS.feed && <Feed />}
        {view === VIEWS.tasks && <Tasks jobs={jobs} onRefresh={refreshJobs} onNavigate={setView} />}
        {view === VIEWS.queue && <Queue />}
        {view === VIEWS.files && <Files />}
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
      <p style={{ color: 'var(--text-muted)' }}>Open a project directory to begin</p>
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
      {error && <p style={{ color: 'var(--danger)', fontSize: 12 }}>{error}</p>}

      {recent.length > 0 && (
        <div style={{ marginTop: 24 }}>
          <p style={{ color: 'var(--text-muted)', fontSize: 12, marginBottom: 8 }}>Recent projects</p>
          {recent.map(r => (
            <div
              key={r.path}
              className="recent-project"
              onClick={() => handleOpen(r.path)}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 'bold', fontSize: 13 }}>{r.name}</div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.path}</div>
              </div>
              <button
                className="small"
                onClick={(e) => handleRemoveRecent(e, r.path)}
                title="Remove from recent"
                style={{ opacity: 0.5, fontSize: 10 }}
              >✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
