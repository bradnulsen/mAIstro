import React, { useState, useEffect, useCallback } from 'react'
import { getProject, openProject, browseProject, getRecentProjects, removeRecentProject, listAgents } from './api'
import Feed from './components/Feed'
import Agents from './components/Agents'
import Chat from './components/Chat'

const VIEWS = { feed: 'feed', agents: 'agents', chat: 'chat' }

export default function App() {
  const [project, setProject] = useState(null)
  const [loading, setLoading] = useState(true)
  const [view, setView] = useState(VIEWS.feed)
  const [agents, setAgents] = useState([])

  // Check for loaded project on mount
  useEffect(() => {
    getProject()
      .then(p => { if (p.loaded) setProject(p) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const refreshAgents = useCallback(async () => {
    try {
      const list = await listAgents()
      setAgents(list)
    } catch {}
  }, [])

  useEffect(() => {
    if (project) refreshAgents()
  }, [project, refreshAgents])

  if (loading) return <div className="loading">Loading...</div>
  if (!project) return <ProjectOpener onOpen={setProject} />

  return (
    <div className="app-shell">
      {/* Rail */}
      <nav className="rail">
        <div className="rail-logo">⬡</div>
        <button
          className={`rail-icon ${view === VIEWS.feed ? 'active' : ''}`}
          onClick={() => setView(VIEWS.feed)}
          title="Feed"
        >☰</button>
        <button
          className={`rail-icon ${view === VIEWS.agents ? 'active' : ''}`}
          onClick={() => setView(VIEWS.agents)}
          title="Agents"
        >◉</button>
        <button
          className={`rail-icon ${view === VIEWS.chat ? 'active' : ''}`}
          onClick={() => setView(VIEWS.chat)}
          title="Chat"
        >💬</button>
        <div className="rail-spacer" />
        <button className="rail-icon" title="Settings">⚙</button>
      </nav>

      {/* Main */}
      <div className="main-area">
        {/* Status bar */}
        <div className="status-bar">
          {agents.map(a => (
            <div key={a.id} className="status-chip">
              <span className={`status-dot ${a.properties?.running ? 'running' : 'idle'}`} />
              {a.name}
            </div>
          ))}
          {agents.length === 0 && <span style={{ color: '#888' }}>No agents configured</span>}
        </div>

        {/* View */}
        {view === VIEWS.feed && <Feed agents={agents} />}
        {view === VIEWS.agents && <Agents agents={agents} onRefresh={refreshAgents} />}
        {view === VIEWS.chat && <Chat agents={agents} />}
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
      <p style={{ color: '#888' }}>Open a project directory to begin</p>
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
      {error && <p style={{ color: '#c44', fontSize: 12 }}>{error}</p>}

      {recent.length > 0 && (
        <div style={{ marginTop: 24 }}>
          <p style={{ color: '#888', fontSize: 12, marginBottom: 8 }}>Recent projects</p>
          {recent.map(r => (
            <div
              key={r.path}
              className="recent-project"
              onClick={() => handleOpen(r.path)}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 'bold', fontSize: 13 }}>{r.name}</div>
                <div style={{ fontSize: 11, color: '#888', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.path}</div>
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
