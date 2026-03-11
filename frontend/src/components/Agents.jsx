import React, { useState, useEffect, useCallback } from 'react'
import {
  createAgent, updateAgent, deleteAgent, getAgentArtifacts,
  dispatchAgent,
} from '../api'

export default function Agents({ agents, onRefresh }) {
  const [selected, setSelected] = useState(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')

  // Auto-select first agent
  useEffect(() => {
    if (!selected && agents.length > 0) {
      setSelected(agents[0].id)
    }
  }, [agents, selected])

  const handleCreate = async () => {
    if (!newName.trim()) return
    try {
      const agent = await createAgent(newName.trim())
      setNewName('')
      setCreating(false)
      await onRefresh()
      setSelected(agent.id)
    } catch (e) {
      alert(e.message)
    }
  }

  const activeAgent = agents.find(a => a.id === selected)

  return (
    <>
      <div className="header-bar">
        <h1>Agents</h1>
      </div>

      <div className="agents-layout">
        {/* Agent list sidebar */}
        <div className="agent-list">
          <div className="scroll-area">
            {agents.map(a => (
              <div
                key={a.id}
                className={`agent-list-item ${selected === a.id ? 'active' : ''}`}
                onClick={() => setSelected(a.id)}
              >
                <span className={`status-dot ${a.properties?.running ? 'running' : 'idle'}`} />
                <span>{a.name}</span>
              </div>
            ))}
          </div>
          <div className="agent-list-footer">
            {creating ? (
              <div style={{ display: 'flex', gap: 4 }}>
                <input
                  type="text"
                  placeholder="Agent name"
                  value={newName}
                  onChange={e => setNewName(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && handleCreate()}
                  autoFocus
                  style={{ flex: 1 }}
                />
                <button className="small primary" onClick={handleCreate}>+</button>
                <button className="small" onClick={() => setCreating(false)}>✕</button>
              </div>
            ) : (
              <button className="small" style={{ width: '100%' }} onClick={() => setCreating(true)}>
                + New Agent
              </button>
            )}
          </div>
        </div>

        {/* Agent detail */}
        <div className="agent-detail">
          {activeAgent ? (
            <AgentDetail agent={activeAgent} onRefresh={onRefresh} onDelete={() => setSelected(null)} />
          ) : (
            <div className="empty-state">Select or create an agent</div>
          )}
        </div>
      </div>
    </>
  )
}

function AgentDetail({ agent, onRefresh, onDelete }) {
  const [editing, setEditing] = useState({})
  const [saving, setSaving] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [streamOutput, setStreamOutput] = useState([])
  const [instructions, setInstructions] = useState('')
  const [artifacts, setArtifacts] = useState(null)
  const props = agent.properties || {}

  // Load artifacts
  useEffect(() => {
    getAgentArtifacts(agent.id).then(setArtifacts).catch(() => setArtifacts(null))
  }, [agent.id])

  // Reset editing state when agent changes
  useEffect(() => { setEditing({}) }, [agent.id])

  const edit = (key, value) => setEditing(prev => ({ ...prev, [key]: value }))

  const getVal = (key) => key in editing ? editing[key] : props[key]

  const handleSave = async () => {
    if (Object.keys(editing).length === 0) return
    setSaving(true)
    try {
      await updateAgent(agent.id, editing)
      setEditing({})
      await onRefresh()
    } catch (e) {
      alert(e.message)
    }
    setSaving(false)
  }

  const handleDelete = async () => {
    if (!confirm(`Delete agent "${agent.name}"?`)) return
    try {
      await deleteAgent(agent.id)
      onDelete()
      await onRefresh()
    } catch (e) {
      alert(e.message)
    }
  }

  const handleDispatch = async () => {
    setStreaming(true)
    setStreamOutput([])
    try {
      await dispatchAgent(agent.id, instructions || undefined, (event) => {
        setStreamOutput(prev => [...prev, event])
      })
    } catch (e) {
      setStreamOutput(prev => [...prev, { type: 'error', message: e.message }])
    }
    setStreaming(false)
    setInstructions('')
    await onRefresh()
  }

  const isDirty = Object.keys(editing).length > 0

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <h2 style={{ flex: 1 }}>{agent.name}</h2>
        <span style={{ fontSize: 11, color: '#888' }}>id: {agent.id}</span>
      </div>

      {/* Actions */}
      <div className="agent-actions">
        <button className="primary" onClick={handleDispatch} disabled={streaming || props.running}>
          {streaming ? 'Running...' : '▶ Run'}
        </button>
        <input
          type="text"
          placeholder="Optional instructions..."
          value={instructions}
          onChange={e => setInstructions(e.target.value)}
          style={{ flex: 1 }}
        />
      </div>

      {/* Stream output */}
      {streamOutput.length > 0 && (
        <div className="dispatch-stream">
          {streamOutput.map((evt, i) => {
            if (evt.type === 'text') return <span key={i} className="stream-text">{evt.content}</span>
            if (evt.type === 'tool_use') return <div key={i} className="stream-tool">[tool: {evt.tool}]</div>
            if (evt.type === 'error') return <div key={i} className="stream-error">Error: {evt.message}</div>
            if (evt.type === 'result') return <div key={i} className="stream-result">✓ Completed {evt.commit ? `(${evt.commit.slice(0, 8)})` : ''}</div>
            return null
          })}
        </div>
      )}

      {/* Artifacts */}
      {artifacts && (
        <div className="agent-section">
          <h3>Authored Files</h3>
          {artifacts.authored.length === 0 && <div style={{ fontSize: 11, color: '#888' }}>No authored files yet</div>}
          {artifacts.authored.map(f => (
            <div key={f} style={{ fontSize: 11, padding: '2px 0' }}>
              {f}
            </div>
          ))}

          <h3 style={{ marginTop: 12 }}>Subscriptions</h3>
          {artifacts.subscriptions.length === 0 && <div style={{ fontSize: 11, color: '#888' }}>No subscriptions</div>}
          {artifacts.subscriptions.map(f => (
            <div key={f.path} style={{ fontSize: 11, padding: '2px 0', display: 'flex', justifyContent: 'space-between' }}>
              <span>{f.path}</span>
              <span style={{ color: '#888' }}>{formatSize(f.size)}</span>
            </div>
          ))}
        </div>
      )}

      {/* Config */}
      <div className="agent-section">
        <h3>Configuration</h3>

        <div className="field-group">
          <label>Persona / System Prompt</label>
          <textarea
            rows={4}
            value={getVal('persona') || ''}
            onChange={e => edit('persona', e.target.value)}
          />
        </div>

        <div className="field-group">
          <label>Model</label>
          <select value={getVal('model') || 'sonnet'} onChange={e => edit('model', e.target.value)}>
            <option value="sonnet">Sonnet</option>
            <option value="opus">Opus</option>
            <option value="haiku">Haiku</option>
          </select>
        </div>

        <div className="field-group">
          <label>Dispatch Mode</label>
          <div className="mode-toggle">
            {['manual', 'watch', 'auto'].map(mode => (
              <button
                key={mode}
                className={getVal('mode') === mode ? 'active' : ''}
                onClick={() => edit('mode', mode)}
              >
                {mode}
              </button>
            ))}
          </div>
        </div>

        <div className="field-group">
          <label>Subscriptions (one glob per line)</label>
          <textarea
            rows={3}
            value={arrayToLines(getVal('subscriptions'))}
            onChange={e => edit('subscriptions', linesToArray(e.target.value))}
          />
        </div>

        <div className="field-group">
          <label>Allowed Tools (comma-separated)</label>
          <input
            type="text"
            value={(getVal('base_tools') || []).join(', ')}
            onChange={e => edit('base_tools', e.target.value.split(',').map(s => s.trim()).filter(Boolean))}
          />
        </div>

        <div className="field-group">
          <label>Cooldown (seconds)</label>
          <input
            type="number"
            value={getVal('cooldown_seconds') || 30}
            onChange={e => edit('cooldown_seconds', parseInt(e.target.value) || 30)}
          />
        </div>

        {isDirty && (
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <button className="primary" onClick={handleSave} disabled={saving}>
              {saving ? 'Saving...' : 'Save Changes'}
            </button>
            <button onClick={() => setEditing({})}>Cancel</button>
          </div>
        )}
      </div>

      {/* Danger zone */}
      <div className="agent-section">
        <h3>Danger Zone</h3>
        <button className="danger small" onClick={handleDelete}>Delete Agent</button>
      </div>
    </div>
  )
}

function arrayToLines(arr) {
  return Array.isArray(arr) ? arr.join('\n') : ''
}

function linesToArray(text) {
  return text.split('\n').map(s => s.trim()).filter(Boolean)
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}K`
  return `${(bytes / (1024 * 1024)).toFixed(1)}M`
}
