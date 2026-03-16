import { useState, useEffect, useCallback } from 'react'
import {
  getProject, getConfig, setConfig, getQueueSettings, setQueueSettings,
  listMcpServers, createMcpServer, deleteMcpServer,
} from '../api'
import HelpTip from './HelpTip'

const TIPS = {
  autoDispatch: 'When enabled, the background worker automatically pulls and executes pending dispatches. When disabled, dispatches remain pending until you manually trigger processing from the Queue view.',
  mcpServers: 'External tool servers that extend agent capabilities. Registered servers are available to dispatched agents alongside the platform\'s built-in tools. Command and args specify how to launch the server process.',
  model: 'Opus: highest capability, slowest, most expensive. Sonnet: balanced capability and speed. Haiku: fastest, cheapest, best for simple or high-frequency tasks.',
  timeout: 'Maximum execution time in seconds applied to tasks that have no task-level override. Set to 0 or leave blank for no limit.',
}

export default function Settings() {
  const [project, setProject] = useState(null)
  const [config, setConfigState] = useState({})
  const [queueSettings, setQueueSettingsState] = useState({ auto_dispatch: false })
  const [mcpServers, setMcpServers] = useState([])
  const [loading, setLoading] = useState(true)
  const [saveMsg, setSaveMsg] = useState('')

  // Editable state
  const [defaultModel, setDefaultModel] = useState('sonnet')
  const [defaultTimeout, setDefaultTimeout] = useState('')

  // MCP form
  const [mcpName, setMcpName] = useState('')
  const [mcpCommand, setMcpCommand] = useState('')
  const [mcpArgs, setMcpArgs] = useState('')
  const [mcpError, setMcpError] = useState('')

  const load = useCallback(async () => {
    try {
      const [p, cfg, qs, servers] = await Promise.all([
        getProject(), getConfig(), getQueueSettings(), listMcpServers(),
      ])
      setProject(p)
      setConfigState(cfg)
      setQueueSettingsState(qs)
      setMcpServers(servers)
      setDefaultModel(cfg.default_model || 'sonnet')
      setDefaultTimeout(cfg.default_timeout || '')
    } catch {
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const flash = (msg) => {
    setSaveMsg(msg)
    setTimeout(() => setSaveMsg(''), 2000)
  }

  const handleAutoDispatch = async (val) => {
    await setQueueSettings({ auto_dispatch: val })
    setQueueSettingsState({ auto_dispatch: val })
    flash('Saved')
  }

  const handleSaveModel = async () => {
    await setConfig('default_model', defaultModel)
    flash('Model saved')
  }

  const handleSaveTimeout = async () => {
    await setConfig('default_timeout', defaultTimeout)
    flash('Timeout saved')
  }

  const handleAddMcp = async () => {
    if (!mcpName.trim() || !mcpCommand.trim()) {
      setMcpError('Name and command are required')
      return
    }
    setMcpError('')
    const args = mcpArgs.trim() ? mcpArgs.trim().split(/\s+/) : []
    await createMcpServer(mcpName.trim(), mcpCommand.trim(), args, {})
    setMcpName('')
    setMcpCommand('')
    setMcpArgs('')
    const servers = await listMcpServers()
    setMcpServers(servers)
    flash('Server added')
  }

  const handleDeleteMcp = async (name) => {
    await deleteMcpServer(name)
    const servers = await listMcpServers()
    setMcpServers(servers)
    flash('Server removed')
  }

  if (loading) return <div className="loading">Loading settings...</div>

  return (
    <div className="settings-view">
      <div className="header-bar">
        <h1>Settings</h1>
        <div className="spacer" />
        {saveMsg && <span style={{ fontSize: 11, color: 'var(--success)' }}>{saveMsg}</span>}
      </div>

      <div className="settings-content">
        {/* Project Info */}
        <div className="settings-section">
          <h3>Project</h3>
          <div className="settings-field">
            <label>Path</label>
            <div className="settings-value">{project?.path || '—'}</div>
          </div>
          <div className="settings-field">
            <label>Name</label>
            <div className="settings-value">{project?.name || '—'}</div>
          </div>
        </div>

        {/* Queue Behavior */}
        <div className="settings-section">
          <h3>Queue Behavior</h3>
          <div className="settings-field">
            <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={queueSettings.auto_dispatch}
                  onChange={e => handleAutoDispatch(e.target.checked)}
                  style={{ width: 'auto' }}
                />
                Auto-dispatch queued items
              </label>
              <HelpTip text={TIPS.autoDispatch} />
            </div>
          </div>
        </div>

        {/* Default Model */}
        <div className="settings-section">
          <h3>Default Model</h3>
          <div className="settings-field" style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
            <div style={{ flex: 1 }}>
              <div className="label-row">
                <label>Model</label>
                <HelpTip text={TIPS.model} />
              </div>
              <select value={defaultModel} onChange={e => setDefaultModel(e.target.value)}>
                <option value="sonnet">sonnet</option>
                <option value="opus">opus</option>
                <option value="haiku">haiku</option>
              </select>
            </div>
            <button onClick={handleSaveModel}>Save</button>
          </div>
        </div>

        {/* Default Timeout */}
        <div className="settings-section">
          <h3>Default Timeout</h3>
          <div className="settings-field" style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
            <div style={{ flex: 1 }}>
              <label>Seconds (blank = no timeout)</label>
              <input
                type="number"
                value={defaultTimeout}
                onChange={e => setDefaultTimeout(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleSaveTimeout()}
                placeholder="e.g. 300"
              />
            </div>
            <button onClick={handleSaveTimeout}>Save</button>
          </div>
        </div>

        {/* MCP Servers */}
        <div className="settings-section">
          <div className="section-head">
            <h3>MCP Servers</h3>
            <HelpTip text={TIPS.mcpServers} />
          </div>
          {mcpServers.length === 0 && (
            <div style={{ color: 'var(--text-muted)', fontSize: 12, marginBottom: 8 }}>
              No MCP servers configured
            </div>
          )}
          {mcpServers.map(s => (
            <div key={s.name} className="mcp-server-item">
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 'bold', fontSize: 12 }}>{s.name}</div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                  {s.command} {s.args ? (() => { try { return JSON.parse(s.args).join(' ') } catch { return s.args } })() : ''}
                </div>
              </div>
              <button className="small danger" onClick={() => handleDeleteMcp(s.name)}>Remove</button>
            </div>
          ))}
          <div className="mcp-add-form">
            <div style={{ display: 'flex', gap: 8 }}>
              <div style={{ flex: 1 }}>
                <label>Name</label>
                <input value={mcpName} onChange={e => setMcpName(e.target.value)} placeholder="server-name" />
              </div>
              <div style={{ flex: 2 }}>
                <label>Command</label>
                <input value={mcpCommand} onChange={e => setMcpCommand(e.target.value)} placeholder="npx or python3" />
              </div>
            </div>
            <div style={{ marginTop: 6 }}>
              <label>Arguments (space-separated)</label>
              <input value={mcpArgs} onChange={e => setMcpArgs(e.target.value)} placeholder="-m my_server --port 3000" />
            </div>
            {mcpError && <div style={{ color: 'var(--danger)', fontSize: 11, marginTop: 4 }}>{mcpError}</div>}
            <button style={{ marginTop: 8 }} onClick={handleAddMcp}>Add Server</button>
          </div>
        </div>
      </div>
    </div>
  )
}
