import { useState, useEffect, useCallback, useRef } from 'react'
import Markdown from 'react-markdown'
import {
  createTask, updateTask, deleteTask, getTaskSubscriptions,
  dispatchTask,
} from '../api'

export default function Tasks({ tasks, onRefresh }) {
  const [selected, setSelected] = useState(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')

  useEffect(() => {
    if (!selected && tasks.length > 0) {
      setSelected(tasks[0].id)
    }
  }, [tasks, selected])

  const handleCreate = async () => {
    if (!newName.trim()) return
    try {
      const task = await createTask(newName.trim())
      setNewName('')
      setCreating(false)
      await onRefresh()
      setSelected(task.id)
    } catch (e) {
      alert(e.message)
    }
  }

  const activeTask = tasks.find(t => t.id === selected)

  return (
    <>
      <div className="header-bar">
        <h1>Tasks</h1>
      </div>

      <div className="tasks-layout">
        <div className="task-list">
          <div className="scroll-area">
            {tasks.map(t => (
              <div
                key={t.id}
                className={`task-list-item ${selected === t.id ? 'active' : ''}`}
                onClick={() => setSelected(t.id)}
              >
                <span className={`status-dot ${t.properties?.running ? 'running' : 'idle'}`} />
                <span>{t.name}</span>
              </div>
            ))}
          </div>
          <div className="task-list-footer">
            {creating ? (
              <div style={{ display: 'flex', gap: 4 }}>
                <input
                  type="text"
                  placeholder="Task name"
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
                + New Task
              </button>
            )}
          </div>
        </div>

        <div className="task-detail">
          {activeTask ? (
            <TaskDetail task={activeTask} onRefresh={onRefresh} onDelete={() => setSelected(null)} />
          ) : (
            <div className="empty-state">Select or create a task</div>
          )}
        </div>
      </div>
    </>
  )
}

function TaskDetail({ task, onRefresh, onDelete }) {
  const [editing, setEditing] = useState({})
  const [saving, setSaving] = useState(false)
  const [dispatching, setDispatching] = useState(false)
  const [lastDispatchId, setLastDispatchId] = useState(null)
  const [context, setContext] = useState('')
  const [subs, setSubs] = useState(null)
  const [subsOpen, setSubsOpen] = useState(false)
  const [editingInstructions, setEditingInstructions] = useState(false)
  const props = task.properties || {}

  const refreshSubs = useCallback(() => {
    getTaskSubscriptions(task.id).then(setSubs).catch(() => setSubs(null))
  }, [task.id])

  useEffect(() => { refreshSubs() }, [refreshSubs])

  useEffect(() => { setEditing({}); setEditingInstructions(false) }, [task.id])

  const edit = (key, value) => setEditing(prev => ({ ...prev, [key]: value }))

  const getVal = (key) => key in editing ? editing[key] : props[key]

  const handleSave = async () => {
    if (Object.keys(editing).length === 0) return
    setSaving(true)
    const payload = { ...editing }
    if (typeof payload.subscriptions === 'string') {
      payload.subscriptions = linesToArray(payload.subscriptions)
    }
    try {
      await updateTask(task.id, payload)
      setEditing({})
      await onRefresh()
      refreshSubs()
    } catch (e) {
      alert(e.message)
    }
    setSaving(false)
  }

  const handleDelete = async () => {
    if (!confirm(`Delete task "${task.name}"?`)) return
    try {
      await deleteTask(task.id)
      onDelete()
      await onRefresh()
    } catch (e) {
      alert(e.message)
    }
  }

  const handleDispatch = async () => {
    setDispatching(true)
    try {
      const result = await dispatchTask(task.id, context || undefined)
      setLastDispatchId(result.dispatch_id)
      setContext('')
      await onRefresh()
    } catch (e) {
      alert(e.message)
    }
    setDispatching(false)
  }

  const isDirty = Object.keys(editing).length > 0

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: props.description ? 4 : 12 }}>
        <h2 style={{ flex: 1 }}>{task.name}</h2>
        <span style={{ fontSize: 11, color: '#888' }}>id: {task.id}</span>
      </div>
      {props.description && (
        <div className="task-description-display">
          <Markdown>{props.description}</Markdown>
        </div>
      )}

      {/* Actions */}
      <div className="task-actions">
        <button className="primary" onClick={handleDispatch} disabled={dispatching || props.running}>
          {dispatching ? 'Queuing...' : '▶ Queue'}
        </button>
        <input
          type="text"
          placeholder="Optional context..."
          value={context}
          onChange={e => setContext(e.target.value)}
          style={{ flex: 1 }}
        />
      </div>

      {lastDispatchId && (
        <div style={{ fontSize: 11, color: '#888', marginBottom: 8 }}>
          Queued as dispatch #{lastDispatchId}
        </div>
      )}

      {/* Task Definition — two-column layout */}
      <div className="task-section">
        <h3>Task Definition</h3>
        <div className="task-def-columns">
          <div className="task-def-left">
            <div className="field-group">
              <label>Description</label>
              <AutoTextarea
                value={getVal('description') || ''}
                onChange={e => edit('description', e.target.value)}
                placeholder="Short description for the task registry..."
                maxHeight={200}
                minRows={3}
              />
            </div>

            <div className="field-group">
              <label>Allowed Tools</label>
              <input
                type="text"
                value={(getVal('base_tools') || []).join(', ')}
                onChange={e => edit('base_tools', e.target.value.split(',').map(s => s.trim()).filter(Boolean))}
                placeholder="Comma-separated tool names..."
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
          </div>

          <div className="task-def-right">
            <div className="field-group" style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
              <label>Instructions</label>
              {editingInstructions || !getVal('instructions') ? (
                <AutoTextarea
                  value={getVal('instructions') || ''}
                  onChange={e => edit('instructions', e.target.value)}
                  onBlur={() => { if (getVal('instructions')) setEditingInstructions(false) }}
                  placeholder="Detailed instructions for what this task should do..."
                  maxHeight={400}
                  minRows={8}
                  autoFocus={editingInstructions}
                  style={{ flex: 1 }}
                />
              ) : (
                <div className="instructions-preview" onClick={() => setEditingInstructions(true)}>
                  <Markdown>{getVal('instructions')}</Markdown>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Subscriptions config */}
      <div className="task-section">
        <h3>Subscriptions</h3>

        <div className="field-group">
          <label className="checkbox-label" style={{ whiteSpace: 'nowrap' }}>
            <input
              type="checkbox"
              checked={getVal('watch_enabled') || false}
              onChange={e => edit('watch_enabled', e.target.checked)}
            />
            Auto-dispatch when matched files are committed
          </label>
        </div>

        <div className="field-group">
          <label>Glob patterns (one per line)</label>
          <AutoTextarea
            value={'subscriptions' in editing ? editing.subscriptions : arrayToLines(props.subscriptions)}
            onChange={e => edit('subscriptions', e.target.value)}
            maxHeight={150}
            minRows={2}
          />
        </div>

        {/* Resolved files (collapsible) */}
        {subs && subs.subscriptions.length > 0 && (
          <div className="field-group">
            <label
              className="collapsible-label"
              onClick={() => setSubsOpen(o => !o)}
            >
              <span className={`collapse-arrow ${subsOpen ? 'open' : ''}`}>▸</span>
              Resolved files ({subs.subscriptions.length})
            </label>
            {subsOpen && (
              <div className="resolved-files-list">
                {subs.subscriptions.map(f => (
                  <div key={f.path} style={{ fontSize: 11, padding: '2px 0', display: 'flex', justifyContent: 'space-between' }}>
                    <span>{f.path}</span>
                    <span style={{ color: '#888' }}>{formatSize(f.size)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {isDirty && (
        <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
          <button className="primary" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving...' : 'Save Changes'}
          </button>
          <button onClick={() => setEditing({})}>Cancel</button>
        </div>
      )}

      {/* Danger zone */}
      <div className="task-section">
        <h3>Danger Zone</h3>
        <button className="danger small" onClick={handleDelete}>Delete Task</button>
      </div>
    </div>
  )
}

function AutoTextarea({ value, onChange, onBlur, placeholder, maxHeight = 200, minRows = 2, autoFocus, style }) {
  const ref = useRef(null)
  const resize = () => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, maxHeight) + 'px'
    el.style.overflowY = el.scrollHeight > maxHeight ? 'auto' : 'hidden'
  }
  useEffect(() => { resize() }, [value])
  useEffect(() => { if (autoFocus && ref.current) ref.current.focus() }, [autoFocus])
  return (
    <textarea
      ref={ref}
      rows={minRows}
      value={value}
      onChange={onChange}
      onBlur={onBlur}
      onInput={resize}
      placeholder={placeholder}
      style={{ resize: 'none', ...style }}
    />
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
