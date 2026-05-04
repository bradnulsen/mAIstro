import { useState, useEffect, useCallback, useRef } from 'react'
import {
  listMcpServers, createMcpServer, updateMcpServer, deleteMcpServer, probeMcpServer,
  previewMcpBundle, installMcpBundle, cancelMcpBundleStaging, beaconCancelStaging,
} from '../api'
import HelpTip from './HelpTip'

const TIP_MCP = 'External tool servers that extend agent capabilities. Registered servers are available to dispatched agents alongside the platform\'s built-in tools. Command and args specify how to launch the server process. Drop a .mcpb bundle anywhere on this panel, or click Import bundle.'

/** Parse "KEY=VALUE\n..." into {KEY: VALUE} or null on bad format */
function parseEnvString(str) {
  if (!str || !str.trim()) return {}
  const env = {}
  for (const line of str.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const eq = trimmed.indexOf('=')
    if (eq <= 0) return null
    env[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1).trim()
  }
  return env
}

export default function McpServers() {
  const [mcpServers, setMcpServers] = useState([])
  const [loading, setLoading] = useState(true)
  const [saveMsg, setSaveMsg] = useState('')

  // MCP form
  const [mcpName, setMcpName] = useState('')
  const [mcpCommand, setMcpCommand] = useState('')
  const [mcpArgs, setMcpArgs] = useState('')
  const [mcpEnv, setMcpEnv] = useState('')
  const [mcpError, setMcpError] = useState('')

  // Edit state: { name, command, args, env }
  const [editing, setEditing] = useState(null)

  // MCP server probes: { [name]: { status, tools, error, loading } }
  const [serverProbes, setServerProbes] = useState({})
  const [activeProbeServer, setActiveProbeServer] = useState(null)

  // Bundle import state. `staging` holds the response from /preview while the
  // operator fills in user_config (or while overwrite is being confirmed).
  // `bundleError` is shown inline on the import form; `dropActive` toggles
  // the highlight while a file drag is over the window.
  const [staging, setStaging] = useState(null)
  const [userConfigValues, setUserConfigValues] = useState({})
  const [bundleError, setBundleError] = useState('')
  const [bundleBusy, setBundleBusy] = useState(false)
  const [dropActive, setDropActive] = useState(false)
  const fileInputRef = useRef(null)
  const stagingIdRef = useRef(null)
  const panelRef = useRef(null)

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

  // Keep stagingIdRef synced with state so the unmount/beforeunload cleanup
  // can read the current id without depending on stale closures.
  useEffect(() => {
    stagingIdRef.current = staging?.staging_id || null
  }, [staging])

  // Best-effort cleanup if the operator closes the tab mid-import. The server
  // also reaps by TTL, but sendBeacon is faster and more honest. On unmount
  // (e.g. navigating to another view) cancel synchronously.
  useEffect(() => {
    const onUnload = () => beaconCancelStaging(stagingIdRef.current)
    window.addEventListener('beforeunload', onUnload)
    return () => {
      window.removeEventListener('beforeunload', onUnload)
      const id = stagingIdRef.current
      if (id) cancelMcpBundleStaging(id).catch(() => {})
    }
  }, [])

  // Window-level drag highlight. relatedTarget === null on dragleave is the
  // only reliable signal for "the drag left the window" (depth-counting via
  // element boundaries breaks when the cursor moves between child elements).
  useEffect(() => {
    const dragHasFiles = (ev) => {
      const types = ev.dataTransfer?.types
      if (!types) return false
      for (const t of types) if (t === 'Files') return true
      return false
    }
    const onEnter = (ev) => { if (dragHasFiles(ev)) setDropActive(true) }
    const onLeave = (ev) => { if (ev.relatedTarget === null) setDropActive(false) }
    // Suppress the browser's default file-open behavior when dropping outside the panel.
    const onWindowOver = (ev) => { if (dragHasFiles(ev)) ev.preventDefault() }
    const onWindowDrop = (ev) => {
      if (dragHasFiles(ev)) ev.preventDefault()
      setDropActive(false)
    }
    document.addEventListener('dragenter', onEnter)
    document.addEventListener('dragleave', onLeave)
    document.addEventListener('dragover', onWindowOver)
    document.addEventListener('drop', onWindowDrop)
    return () => {
      document.removeEventListener('dragenter', onEnter)
      document.removeEventListener('dragleave', onLeave)
      document.removeEventListener('dragover', onWindowOver)
      document.removeEventListener('drop', onWindowDrop)
    }
  }, [])

  // ── Bundle import flow ────────────────────────────────────

  const handleBundlePreview = async (file) => {
    if (!file) return
    if (!/\.mcpb$/i.test(file.name)) {
      setBundleError(`Not a .mcpb bundle: ${file.name}`)
      return
    }
    // If a previous import is still active, cancel it before starting a new one.
    if (stagingIdRef.current) {
      cancelMcpBundleStaging(stagingIdRef.current).catch(() => {})
    }

    setBundleError('')
    setBundleBusy(true)
    try {
      const result = await previewMcpBundle(file)
      setStaging(result)
      // Seed user_config values from the schema's defaults so the form has
      // sensible starting state on render.
      const defaults = {}
      const props = result.user_config_schema?.properties || {}
      for (const [k, p] of Object.entries(props)) {
        if (p.default !== undefined) defaults[k] = p.default
      }
      setUserConfigValues(defaults)

      // No user_config → install immediately.
      const hasFields = Object.keys(props).length > 0
      if (!hasFields) {
        await doInstall(result, {}, false)
      }
    } catch (e) {
      setBundleError(e.message)
    } finally {
      setBundleBusy(false)
    }
  }

  const doInstall = async (current, values, overwrite) => {
    if (!current) return
    setBundleBusy(true)
    setBundleError('')
    try {
      const result = await installMcpBundle({
        stagingId: current.staging_id,
        name: current.manifest.name,
        userConfig: values,
        overwrite,
      })
      if (result.conflict) {
        const ok = window.confirm(
          `Server "${current.manifest.name}" already exists. Overwrite it?`
        )
        if (!ok) {
          setBundleError('Install cancelled.')
          return
        }
        await doInstall(current, values, true)
        return
      }
      // Success.
      setStaging(null)
      setUserConfigValues({})
      const servers = await listMcpServers()
      setMcpServers(servers)
      flash(`Installed "${result.name}" from bundle`)
    } catch (e) {
      setBundleError(e.message)
    } finally {
      setBundleBusy(false)
    }
  }

  const handleBundleSubmit = (ev) => {
    ev.preventDefault()
    if (!staging) return
    // Validate required fields.
    const required = staging.user_config_schema?.required || []
    for (const field of required) {
      const v = userConfigValues[field]
      if (v === undefined || v === null || v === '') {
        setBundleError(`"${field}" is required`)
        return
      }
    }
    doInstall(staging, userConfigValues, false)
  }

  const handleBundleCancel = async () => {
    const id = staging?.staging_id
    setStaging(null)
    setUserConfigValues({})
    setBundleError('')
    if (id) await cancelMcpBundleStaging(id).catch(() => {})
  }

  const triggerFilePicker = () => {
    if (fileInputRef.current) {
      fileInputRef.current.value = ''  // re-selecting the same file should still fire 'change'
      fileInputRef.current.click()
    }
  }

  // Panel-level drop handlers — only the panel root accepts the drop;
  // window-level handlers above suppress browser default on misses.
  const onPanelDragOver = (ev) => {
    if (ev.dataTransfer?.types && Array.from(ev.dataTransfer.types).includes('Files')) {
      ev.preventDefault()
    }
  }
  const onPanelDrop = (ev) => {
    ev.preventDefault()
    setDropActive(false)
    const file = ev.dataTransfer?.files?.[0]
    if (file) handleBundlePreview(file)
  }

  const handleAddMcp = async () => {
    if (!mcpName.trim() || !mcpCommand.trim()) {
      setMcpError('Name and command are required')
      return
    }
    const env = parseEnvString(mcpEnv)
    if (env === null) {
      setMcpError('Invalid env format — use KEY=VALUE, one per line')
      return
    }
    setMcpError('')
    const args = mcpArgs.trim() ? mcpArgs.trim().split(/\s+/) : []
    try {
      await createMcpServer(mcpName.trim(), mcpCommand.trim(), args, env)
    } catch (e) {
      setMcpError(e.message?.includes('UNIQUE') ? `Server "${mcpName.trim()}" already exists` : e.message)
      return
    }
    setMcpName('')
    setMcpCommand('')
    setMcpArgs('')
    setMcpEnv('')
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

  const startEdit = (s) => {
    const args = s.args ? (() => { try { return JSON.parse(s.args).join('\n') } catch { return s.args } })() : ''
    const env = s.env ? (() => { try { return Object.entries(JSON.parse(s.env)).map(([k, v]) => `${k}=${v}`).join('\n') } catch { return '' } })() : ''
    setEditing({ name: s.name, command: s.command, args, env })
  }

  const handleSaveEdit = async () => {
    if (!editing) return
    const env = parseEnvString(editing.env)
    if (env === null) {
      setMcpError('Invalid env format — use KEY=VALUE, one per line')
      return
    }
    setMcpError('')
    const args = editing.args.trim() ? editing.args.trim().split(/\s+/) : []
    await updateMcpServer(editing.name, { command: editing.command, args, env })
    setEditing(null)
    const servers = await listMcpServers()
    setMcpServers(servers)
    flash('Server updated')
  }

  const handleProbeMcp = async (name) => {
    setActiveProbeServer(name)
    setServerProbes(prev => ({ ...prev, [name]: { loading: true } }))
    try {
      const result = await probeMcpServer(name)
      setServerProbes(prev => ({ ...prev, [name]: { ...result, loading: false } }))
    } catch (e) {
      setServerProbes(prev => ({ ...prev, [name]: { status: 'error', tools: [], error: e.message, loading: false } }))
    }
  }

  if (loading) return <div className="loading">Loading...</div>

  const activeProbe = activeProbeServer ? serverProbes[activeProbeServer] : null
  const activeServer = activeProbeServer ? mcpServers.find(s => s.name === activeProbeServer) : null

  return (
    <div className="settings-view">
      <div className="header-bar">
        <h1>MCP Servers</h1>
        <HelpTip text={TIP_MCP} />
        <div className="spacer" />
        {saveMsg && <span className="success-text">{saveMsg}</span>}
      </div>
      <div className="mcp-layout">
        <div
          className={`mcp-config-panel${dropActive ? ' is-drop-target' : ''}`}
          ref={panelRef}
          onDragOver={onPanelDragOver}
          onDrop={onPanelDrop}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept=".mcpb"
            style={{ display: 'none' }}
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) handleBundlePreview(file)
            }}
          />
          <div className="mcp-import-row">
            <button onClick={triggerFilePicker} disabled={bundleBusy}>
              {bundleBusy && !staging ? 'Importing…' : 'Import bundle (.mcpb)'}
            </button>
            <span className="muted-text">…or drop a .mcpb file anywhere on this panel</span>
          </div>
          {staging && (
            <div className="mcp-bundle-form">
              <div className="mcp-bundle-header">
                <strong>{staging.manifest.display_name || staging.manifest.name}</strong>
                <span className="muted-text"> v{staging.manifest.version}</span>
              </div>
              {staging.manifest.description && (
                <div className="mcp-bundle-description">{staging.manifest.description}</div>
              )}
              {staging.user_config_schema && Object.keys(staging.user_config_schema.properties || {}).length > 0 ? (
                <form onSubmit={handleBundleSubmit}>
                  {Object.entries(staging.user_config_schema.properties).map(([fieldName, prop]) => {
                    const required = (staging.user_config_schema.required || []).includes(fieldName)
                    const value = userConfigValues[fieldName] ?? (prop.default ?? '')
                    return (
                      <div key={fieldName} className="mcp-bundle-field">
                        <label>
                          {fieldName}
                          {required && <span className="required-marker"> *</span>}
                        </label>
                        {prop.description && <div className="muted-text">{prop.description}</div>}
                        {prop.type === 'boolean' ? (
                          <input
                            type="checkbox"
                            checked={!!value}
                            onChange={(e) => setUserConfigValues({ ...userConfigValues, [fieldName]: e.target.checked })}
                          />
                        ) : (
                          <input
                            type={prop.sensitive ? 'password' : (prop.type === 'number' || prop.type === 'integer' ? 'number' : 'text')}
                            value={value === undefined || value === null ? '' : value}
                            onChange={(e) => {
                              let v = e.target.value
                              if (prop.type === 'integer') v = v === '' ? '' : parseInt(v, 10)
                              else if (prop.type === 'number') v = v === '' ? '' : parseFloat(v)
                              setUserConfigValues({ ...userConfigValues, [fieldName]: v })
                            }}
                          />
                        )}
                      </div>
                    )
                  })}
                  {bundleError && <div className="error-text">{bundleError}</div>}
                  <div className="mcp-bundle-actions">
                    <button type="submit" disabled={bundleBusy}>{bundleBusy ? 'Installing…' : 'Install'}</button>
                    <button type="button" onClick={handleBundleCancel} disabled={bundleBusy}>Cancel</button>
                  </div>
                </form>
              ) : (
                bundleError && <div className="error-text">{bundleError}</div>
              )}
            </div>
          )}
          {!staging && bundleError && <div className="error-text">{bundleError}</div>}
          {mcpServers.length === 0 && (
            <div className="muted-text">No MCP servers configured</div>
          )}
          {mcpServers.map(s => {
            const probe = serverProbes[s.name]
            const isActive = activeProbeServer === s.name
            const isEditing = editing?.name === s.name
            return (
              <div key={s.name} className={`mcp-server-item${s.enabled ? '' : ' disabled'}${isActive ? ' active' : ''}`}>
                <div>
                  <input
                    type="checkbox"
                    checked={!!s.enabled}
                    onChange={e => handleToggleMcp(s.name, e.target.checked)}
                    title={s.enabled ? 'Enabled — available to jobs' : 'Disabled — unavailable to any job'}
                  />
                  <div className="mcp-server-content">
                    <div className="mcp-server-name">
                      {s.name}
                      {probe && !probe.loading && (
                        <span
                          className={`mcp-health-dot ${probe.status === 'ok' ? 'healthy' : 'unhealthy'}`}
                          title={probe.status === 'ok' ? `Healthy — ${probe.tools.length} tools` : probe.error || 'Unreachable'}
                        />
                      )}
                    </div>
                    {!isEditing && (() => { try { const e = JSON.parse(s.env || '{}'); const n = Object.keys(e).length; return n > 0 ? <div className="mcp-server-env-count">{n} env var{n > 1 ? 's' : ''}</div> : null } catch { return null } })()}
                    {isEditing ? (
                      <div className="mcp-edit-form">
                        <div className="mcp-edit-row">
                          <label>Command</label>
                          <input value={editing.command} onChange={e => setEditing({ ...editing, command: e.target.value })} onKeyDown={e => e.key === 'Enter' && handleSaveEdit()} />
                        </div>
                        <div className="mcp-edit-row">
                          <label>Arguments</label>
                          <textarea
                            value={editing.args}
                            onChange={e => setEditing({ ...editing, args: e.target.value })}
                            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSaveEdit() } }}
                            rows={2}
                          />
                        </div>
                        <div className="mcp-edit-row">
                          <label>Environment</label>
                          <textarea
                            value={editing.env}
                            onChange={e => setEditing({ ...editing, env: e.target.value })}
                            placeholder={'API_KEY=sk-...\nDATABASE_URL=...'}
                            rows={2}
                            className="env-textarea"
                          />
                        </div>
                        {mcpError && <div className="error-text">{mcpError}</div>}
                        <div className="mcp-edit-actions">
                          <button className="small" onClick={handleSaveEdit}>Save</button>
                          <button className="small" onClick={() => { setEditing(null); setMcpError('') }}>Cancel</button>
                        </div>
                      </div>
                    ) : (
                      <div className="mcp-server-cmd">
                        {s.command} {s.args ? (() => { try { return JSON.parse(s.args).join(' ') } catch { return s.args } })() : ''}
                      </div>
                    )}
                  </div>
                </div>
                <div className="mcp-server-actions">
                  {!isEditing && (
                    <button className="small" onClick={() => startEdit(s)}>Edit</button>
                  )}
                  {s.enabled && !isEditing && (
                    <button className="small" onClick={() => handleProbeMcp(s.name)} disabled={probe?.loading}>
                      {probe?.loading ? '...' : 'Test'}
                    </button>
                  )}
                  {!isEditing && (
                    <button className="small danger" onClick={() => handleDeleteMcp(s.name)}>Remove</button>
                  )}
                </div>
              </div>
            )
          })}
          <div className="mcp-add-form">
            <div className="mcp-add-form-row">
              <div>
                <label>Name</label>
                <input value={mcpName} onChange={e => setMcpName(e.target.value)} placeholder="server-name" onKeyDown={e => e.key === 'Enter' && handleAddMcp()} />
              </div>
              <div>
                <label>Command</label>
                <input value={mcpCommand} onChange={e => setMcpCommand(e.target.value)} placeholder="npx or python3" onKeyDown={e => e.key === 'Enter' && handleAddMcp()} />
              </div>
            </div>
            <div className="mcp-add-form-args">
              <label>Arguments</label>
              <textarea
                value={mcpArgs}
                onChange={e => setMcpArgs(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleAddMcp() } }}
                placeholder={`-m my_server\n--port 3000\n--verbose`}
                rows={2}
              />
            </div>
            <div className="mcp-add-form-args">
              <label>Environment Variables</label>
              <textarea
                value={mcpEnv}
                onChange={e => setMcpEnv(e.target.value)}
                placeholder={'API_KEY=sk-...\nDATABASE_URL=...'}
                rows={2}
                className="env-textarea"
              />
            </div>
            {mcpError && <div className="error-text">{mcpError}</div>}
            <button onClick={handleAddMcp}>Add Server</button>
          </div>
        </div>
        <div className="mcp-results-panel">
          {!activeProbeServer && (
            <div className="mcp-results-empty">
              <div className="muted-text">Click <strong>Test</strong> on a server to inspect its tools and connectivity.</div>
            </div>
          )}
          {activeProbeServer && activeProbe?.loading && (
            <div className="mcp-results-body">
              <div className="mcp-results-server-name">{activeProbeServer}</div>
              <div className="muted-text">Testing...</div>
            </div>
          )}
          {activeProbeServer && activeProbe && !activeProbe.loading && (
            <div className="mcp-results-body">
              <div className="mcp-results-server-name">
                {activeProbeServer}
                <span className={`mcp-health-dot ${activeProbe.status === 'ok' ? 'healthy' : 'unhealthy'}`} />
              </div>
              {activeServer && (
                <div className="mcp-results-cmd">
                  {activeServer.command} {activeServer.args ? (() => { try { return JSON.parse(activeServer.args).join(' ') } catch { return activeServer.args } })() : ''}
                </div>
              )}
              {activeProbe.status === 'error' && (
                <div className="error-text mcp-results-error">{activeProbe.error || 'Unreachable'}</div>
              )}
              {activeProbe.status === 'ok' && (
                <>
                  <div className="mcp-results-tools-label">
                    {activeProbe.tools.length} {activeProbe.tools.length === 1 ? 'tool' : 'tools'} available
                  </div>
                  {activeProbe.tools.length > 0 && (
                    <ul className="mcp-results-tools-list">
                      {activeProbe.tools.map(t => <li key={t}>{t}</li>)}
                    </ul>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
