import { useState, useEffect, useCallback } from 'react'
import { listMcpServers, createMcpServer, updateMcpServer, deleteMcpServer, probeMcpServer } from '../api'
import HelpTip from './HelpTip'

const TIP_MCP = 'External tool servers that extend agent capabilities. Registered servers are available to dispatched agents alongside the platform\'s built-in tools. Command and args specify how to launch the server process.'

export default function McpServers() {
  const [mcpServers, setMcpServers] = useState([])
  const [loading, setLoading] = useState(true)
  const [saveMsg, setSaveMsg] = useState('')

  // MCP form
  const [mcpName, setMcpName] = useState('')
  const [mcpCommand, setMcpCommand] = useState('')
  const [mcpArgs, setMcpArgs] = useState('')
  const [mcpError, setMcpError] = useState('')

  // MCP server probes: { [name]: { status, tools, error, loading } }
  const [serverProbes, setServerProbes] = useState({})

  const load = useCallback(async () => {
    try {
      const servers = await listMcpServers()
      setMcpServers(servers)
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

  const handleToggleMcp = async (name, enabled) => {
    await updateMcpServer(name, { enabled })
    const servers = await listMcpServers()
    setMcpServers(servers)
    flash(enabled ? 'Server enabled' : 'Server disabled')
  }

  const handleDeleteMcp = async (name) => {
    await deleteMcpServer(name)
    const servers = await listMcpServers()
    setMcpServers(servers)
    setServerProbes(prev => { const next = { ...prev }; delete next[name]; return next })
    flash('Server removed')
  }

  const handleProbeMcp = async (name) => {
    setServerProbes(prev => ({ ...prev, [name]: { loading: true } }))
    try {
      const result = await probeMcpServer(name)
      setServerProbes(prev => ({ ...prev, [name]: { ...result, loading: false } }))
    } catch (e) {
      setServerProbes(prev => ({ ...prev, [name]: { status: 'error', tools: [], error: e.message, loading: false } }))
    }
  }

  if (loading) return <div className="loading">Loading...</div>

  return (
    <div className="settings-view">
      <div className="header-bar">
        <h1>MCP Servers</h1>
        <div className="spacer" />
        {saveMsg && <span className="success-text">{saveMsg}</span>}
      </div>

      <div className="settings-content">
        <div className="settings-section">
          <div className="section-head">
            <h3>Registered Servers</h3>
            <HelpTip text={TIP_MCP} />
          </div>
          {mcpServers.length === 0 && (
            <div className="muted-text">No MCP servers configured</div>
          )}
          {mcpServers.map(s => {
            const probe = serverProbes[s.name]
            return (
              <div key={s.name} className={`mcp-server-item${s.enabled ? '' : ' disabled'}`}>
                <div>
                  <input
                    type="checkbox"
                    checked={!!s.enabled}
                    onChange={e => handleToggleMcp(s.name, e.target.checked)}
                    title={s.enabled ? 'Enabled — available to jobs' : 'Disabled — unavailable to any job'}
                  />
                  <div>
                    <div className="mcp-server-name">
                      {s.name}
                      {probe && !probe.loading && (
                        <span
                          className={`mcp-health-dot ${probe.status === 'ok' ? 'healthy' : 'unhealthy'}`}
                          title={probe.status === 'ok' ? `Healthy — ${probe.tools.length} tools` : probe.error || 'Unreachable'}
                        />
                      )}
                    </div>
                    <div className="mcp-server-cmd">
                      {s.command} {s.args ? (() => { try { return JSON.parse(s.args).join(' ') } catch { return s.args } })() : ''}
                    </div>
                    {probe && !probe.loading && probe.status === 'ok' && probe.tools.length > 0 && (
                      <div className="mcp-server-tools">
                        {probe.tools.join(', ')}
                      </div>
                    )}
                    {probe && !probe.loading && probe.status === 'error' && (
                      <div className="error-text mcp-server-tools">{probe.error}</div>
                    )}
                  </div>
                </div>
                <div className="mcp-server-actions">
                  {s.enabled && (
                    <button className="small" onClick={() => handleProbeMcp(s.name)} disabled={probe?.loading}>
                      {probe?.loading ? '...' : 'Test'}
                    </button>
                  )}
                  <button className="small danger" onClick={() => handleDeleteMcp(s.name)}>Remove</button>
                </div>
              </div>
            )
          })}
          <div className="mcp-add-form">
            <div className="mcp-add-form-row">
              <div>
                <label>Name</label>
                <input value={mcpName} onChange={e => setMcpName(e.target.value)} placeholder="server-name" />
              </div>
              <div>
                <label>Command</label>
                <input value={mcpCommand} onChange={e => setMcpCommand(e.target.value)} placeholder="npx or python3" />
              </div>
            </div>
            <div className="mcp-add-form-args">
              <label>Arguments (space-separated)</label>
              <input value={mcpArgs} onChange={e => setMcpArgs(e.target.value)} placeholder="-m my_server --port 3000" />
            </div>
            {mcpError && <div className="error-text">{mcpError}</div>}
            <button onClick={handleAddMcp}>Add Server</button>
          </div>
        </div>
      </div>
    </div>
  )
}
