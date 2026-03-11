import React, { useState, useEffect, useCallback } from 'react'
import { getProject, openProject, listAgents } from './api'
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

  const handleOpen = async () => {
    if (!path.trim()) return
    setOpening(true)
    setError('')
    try {
      await openProject(path.trim())
      const p = await getProject()
      onOpen(p)
    } catch (e) {
      setError(e.message)
    } finally {
      setOpening(false)
    }
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
        <button className="primary" onClick={handleOpen} disabled={opening}>
          {opening ? '...' : 'Open'}
        </button>
      </div>
      {error && <p style={{ color: '#c44', fontSize: 12 }}>{error}</p>}
    </div>
  )
}
