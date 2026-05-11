import { useState, useEffect, useCallback, useRef } from 'react'
import { getProject, openProject, browseProject, getRecentProjects, removeRecentProject, listJobs, enqueueTrigger, getQueueSettings, setQueueSettings, queueAll, shelveAll, getGovernorStatus, getClaudeStatus } from './api'
import Tasks from './components/Tasks'
import Queue from './components/Queue'
import Governor from './components/Governor'
import Files from './components/Files'
import Settings from './components/Settings'
import McpServers from './components/McpServers'
import Dashboard from './components/Dashboard'

const VIEWS = { tasks: 'tasks', queue: 'queue', files: 'files', mcp: 'mcp', dashboard: 'dashboard', governor: 'governor', settings: 'settings' }

export default function App() {
  const [project, setProject] = useState(null)
  const [loading, setLoading] = useState(true)
  const [view, setView] = useState(VIEWS.queue)
  const [jobs, setJobs] = useState([])
  const [autoQueue, setAutoQueue] = useState(false)
  const [governorBadge, setGovernorBadge] = useState(0)

  // Load auto-queue setting at app level
  useEffect(() => {
    if (project) getQueueSettings().then(s => setAutoQueue(s.auto_dispatch)).catch(() => {})
  }, [project])

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

  // Poll governor status for badge count
  useEffect(() => {
    if (!project) return
    const poll = () => {
      getGovernorStatus()
        .then(s => setGovernorBadge((s.unread_threads || 0) + (s.pending_proposals || 0)))
        .catch(() => {})
    }
    poll()
    const interval = setInterval(poll, 10000)
    return () => clearInterval(interval)
  }, [project])

  if (loading) return <div className="loading">Loading...</div>
  if (!project) return <ProjectOpener onOpen={setProject} />

  return (
    <div className="app-shell" data-auto-dispatch={autoQueue || undefined} data-any-running={jobs.some(j => j.properties?.running) || undefined}>
      <nav className="rail">
        <div className="rail-logo" onClick={() => { setProject(null); setJobs([]) }} title="Switch project">⬡</div>
        <button
          className={`rail-icon ${view === VIEWS.queue ? 'active' : ''}`}
          onClick={() => setView(VIEWS.queue)}
          title="Dispatch"
        >▶</button>
        <button
          className={`rail-icon ${view === VIEWS.dashboard ? 'active' : ''}`}
          onClick={() => setView(VIEWS.dashboard)}
          title="Activity"
        >⊞</button>
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
        <button
          className={`rail-icon ${view === VIEWS.mcp ? 'active' : ''}`}
          onClick={() => setView(VIEWS.mcp)}
          title="MCP Servers"
        >⧈</button>
        <button
          className={`rail-icon ${view === VIEWS.governor ? 'active' : ''}`}
          onClick={() => setView(VIEWS.governor)}
          title="Governor"
        >
          ⚑
          {governorBadge > 0 && <span className="governor-badge">{governorBadge}</span>}
        </button>
        <div className="rail-spacer" />
        <button
          className={`rail-icon ${view === VIEWS.settings ? 'active' : ''}`}
          onClick={() => setView(VIEWS.settings)}
          title="Settings"
        >&#9881;</button>
      </nav>

      <div className="main-area">
        <CommandBar project={project} jobs={jobs} onNavigate={setView} refreshJobs={refreshJobs} autoQueue={autoQueue} setAutoQueue={setAutoQueue} />

        {view === VIEWS.tasks && <Tasks jobs={jobs} onRefresh={refreshJobs} />}
        {view === VIEWS.queue && <Queue />}
        {view === VIEWS.files && <Files />}
        {view === VIEWS.mcp && <McpServers />}
        {view === VIEWS.dashboard && <Dashboard />}
        {view === VIEWS.governor && <Governor onBadgeChange={setGovernorBadge} />}
        {view === VIEWS.settings && <Settings />}
      </div>
    </div>
  )
}

function CommandBar({ project, jobs, onNavigate, refreshJobs, autoQueue, setAutoQueue }) {
  const [popout, setPopout] = useState(null) // job id
  const [context, setContext] = useState('')
  const [dispatching, setDispatching] = useState(false)
  const [claudeStatus, setClaudeStatus] = useState(null) // null until first poll, then {installed, path}
  const popoutRef = useRef(null)

  // Probe the Claude CLI on mount + every 60s. Installation status changes rarely;
  // the poll exists so re-installing the CLI without refreshing the tab still updates.
  useEffect(() => {
    let cancelled = false
    const poll = () => {
      getClaudeStatus()
        .then(s => { if (!cancelled) setClaudeStatus(s) })
        .catch(() => {})
    }
    poll()
    const interval = setInterval(poll, 60000)
    return () => { cancelled = true; clearInterval(interval) }
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

  const handleDispatch = async (jobId) => {
    setDispatching(true)
    try {
      await enqueueTrigger(jobId, context || undefined)
      setPopout(null)
      setContext('')
      refreshJobs()
      window.dispatchEvent(new Event('queue-refresh'))
    } catch {}
    setDispatching(false)
  }

  const handleQueueAll = async () => {
    await queueAll().catch(() => {})
    refreshJobs()
    window.dispatchEvent(new Event('queue-refresh'))
  }

  const handleShelveAll = async () => {
    await shelveAll().catch(() => {})
    refreshJobs()
    window.dispatchEvent(new Event('queue-refresh'))
  }

  const hasPending = jobs.some(j => (j.properties?.pending_count || 0) > 0)
  const hasQueued = jobs.some(j => (j.properties?.queued_count || 0) > 0)

  return (
    <div className="command-bar">
      <div className="command-bar-brand">
        <span className="command-bar-brand-name">
          mA<span className="brand-orange">i</span>str<span className="brand-orange">o</span>
        </span>
        {project && (
          <span className="command-bar-brand-project" title={project.path}>
            {project.name}
          </span>
        )}
      </div>
      <div className="command-bar-jobs">
        {jobs.map(j => {
          const p = j.properties || {}
          const state = p.running ? 'active' : (p.queued_count > 0 ? 'queued' : (p.pending_count > 0 ? 'pending' : 'idle'))
          const counts = []
          if (p.running) counts.push('running')
          if (p.queued_count > 0) counts.push(`${p.queued_count} queued`)
          if (p.pending_count > 0) counts.push(`${p.pending_count} pending`)
          const subtitle = counts.length > 0 ? counts.join(', ') : ''
          return (
            <div key={j.id} className="command-bar-indicator-wrap" ref={popout === j.id ? popoutRef : undefined}>
              <button
                className={`command-bar-indicator ${state}`}
                onClick={() => { setPopout(popout === j.id ? null : j.id); setContext('') }}
                title={`${j.name} — ${state}${subtitle ? ` (${subtitle})` : ''}`}
              >
                <span className={`command-bar-dot ${state}`} />
                <span className="command-bar-name">{j.name}</span>
                {subtitle && <span className={`command-bar-count ${state}`}>{subtitle}</span>}
              </button>
              {popout === j.id && (
                <div
                  className="dispatch-modal-backdrop"
                  onMouseDown={e => { if (e.target === e.currentTarget) { setPopout(null); setContext('') } }}
                >
                  <div
                    className="dispatch-modal"
                    ref={popoutRef}
                    onKeyDown={e => {
                      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !dispatching) {
                        e.preventDefault()
                        handleDispatch(j.id)
                      } else if (e.key === 'Escape') {
                        setPopout(null); setContext('')
                      }
                    }}
                  >
                    <div className="dispatch-modal-header">
                      <span className="dispatch-modal-title">Dispatch — {j.name}</span>
                      <button
                        className="dispatch-modal-close"
                        onClick={() => { setPopout(null); setContext('') }}
                        title="Close (Esc)"
                      >✕</button>
                    </div>
                    <div className="dispatch-modal-body">
                      <textarea
                        className="dispatch-modal-context"
                        value={context}
                        onChange={e => setContext(e.target.value)}
                        placeholder="Context (optional) — what should this job work on?"
                        autoFocus
                      />
                    </div>
                    <div className="dispatch-modal-footer">
                      <span className="dispatch-modal-hint">Ctrl+Enter to dispatch · Esc to close</span>
                      <div className="dispatch-modal-actions">
                        <button onClick={() => { setPopout(null); setContext('') }}>Cancel</button>
                        <button
                          className="primary"
                          onClick={() => handleDispatch(j.id)}
                          disabled={dispatching}
                        >
                          {dispatching ? 'Dispatching…' : '▶ Dispatch'}
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )
        })}
        {jobs.length === 0 && <span className="muted-text">No jobs configured</span>}
      </div>

      <div className="command-bar-actions">
        {claudeStatus && (
          <div
            className={`command-bar-pill ${claudeStatus.installed ? 'ok' : 'warn'}`}
            title={
              claudeStatus.installed
                ? `Claude CLI: ${claudeStatus.path}`
                : "Claude CLI not found on PATH. Install from https://claude.ai/code — agents can't dispatch without it."
            }
          >
            <span className={`command-bar-pill-dot ${claudeStatus.installed ? 'ok' : 'warn'}`} />
            <span>claude</span>
          </div>
        )}
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
      <h1>⬡ mA<span className="title-highlight">i</span>str<span className="title-highlight">o</span></h1>
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
