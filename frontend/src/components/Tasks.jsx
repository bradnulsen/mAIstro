import { useState, useEffect, useCallback } from 'react'
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
  const props = task.properties || {}

  useEffect(() => {
    getTaskSubscriptions(task.id).then(setSubs).catch(() => setSubs(null))
  }, [task.id])

  useEffect(() => { setEditing({}) }, [task.id])

  const edit = (key, value) => setEditing(prev => ({ ...prev, [key]: value }))

  const getVal = (key) => key in editing ? editing[key] : props[key]

  const handleSave = async () => {
    if (Object.keys(editing).length === 0) return
    setSaving(true)
    try {
      await updateTask(task.id, editing)
      setEditing({})
      await onRefresh()
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

      {/* Description & Instructions */}
      <div className="task-section">
        <h3>Task Definition</h3>

        <div className="field-group">
          <label>Description</label>
          <input
            type="text"
            value={getVal('description') || ''}
            onChange={e => edit('description', e.target.value)}
            placeholder="Short description for the task registry..."
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
          <label>Instructions</label>
          <textarea
            rows={4}
            value={getVal('instructions') || ''}
            onChange={e => edit('instructions', e.target.value)}
            placeholder="Detailed instructions for what this task should do..."
          />
        </div>
      </div>

      {/* Subscriptions config */}
      <div className="task-section">
        <h3>Subscriptions</h3>

        <div className="field-group">
          <label>Glob patterns (one per line)</label>
          <textarea
            rows={3}
            value={arrayToLines(getVal('subscriptions'))}
            onChange={e => edit('subscriptions', linesToArray(e.target.value))}
          />
        </div>

        {/* Resolved files */}
        {subs && subs.subscriptions.length > 0 && (
          <div className="field-group">
            <label>Resolved files</label>
            {subs.subscriptions.map(f => (
              <div key={f.path} style={{ fontSize: 11, padding: '2px 0', display: 'flex', justifyContent: 'space-between' }}>
                <span>{f.path}</span>
                <span style={{ color: '#888' }}>{formatSize(f.size)}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Behavior */}
      <div className="task-section">
        <h3>Behavior</h3>

        <div className="field-group">
          <label>Watch Mode</label>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={getVal('watch_enabled') || false}
              onChange={e => edit('watch_enabled', e.target.checked)}
            />
            Auto-enqueue when subscribed files change
          </label>
        </div>

        <div className="field-group">
          <label>Allowed Tools (comma-separated)</label>
          <input
            type="text"
            value={(getVal('base_tools') || []).join(', ')}
            onChange={e => edit('base_tools', e.target.value.split(',').map(s => s.trim()).filter(Boolean))}
          />
        </div>
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
