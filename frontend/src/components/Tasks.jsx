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
  const [createError, setCreateError] = useState('')

  useEffect(() => {
    if (!selected && tasks.length > 0) {
      setSelected(tasks[0].id)
    }
  }, [tasks, selected])

  const handleCreate = async () => {
    if (!newName.trim()) return
    setCreateError('')
    try {
      const task = await createTask(newName.trim())
      setNewName('')
      setCreating(false)
      await onRefresh()
      setSelected(task.id)
    } catch (e) {
      setCreateError(e.message)
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
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <div style={{ display: 'flex', gap: 4 }}>
                  <input
                    type="text"
                    placeholder="Task name"
                    value={newName}
                    onChange={e => { setNewName(e.target.value); setCreateError('') }}
                    onKeyDown={e => e.key === 'Enter' && handleCreate()}
                    autoFocus
                    style={{ flex: 1 }}
                  />
                  <button className="small primary" onClick={handleCreate}>+</button>
                  <button className="small" onClick={() => { setCreating(false); setCreateError('') }}>✕</button>
                </div>
                {createError && <span style={{ fontSize: 11, color: 'var(--danger)' }}>{createError}</span>}
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
            <TaskDetail key={activeTask.id} task={activeTask} onRefresh={onRefresh} onDelete={() => setSelected(null)} />
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
  const [saveError, setSaveError] = useState('')
  const [dispatching, setDispatching] = useState(false)
  const [dispatchError, setDispatchError] = useState('')
  const [lastDispatchId, setLastDispatchId] = useState(null)
  const [context, setContext] = useState('')
  const [subs, setSubs] = useState(null)
  const [subsOpen, setSubsOpen] = useState(false)
  const [editingInstructions, setEditingInstructions] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const props = task.properties || {}

  const refreshSubs = useCallback(() => {
    getTaskSubscriptions(task.id).then(setSubs).catch(() => setSubs(null))
  }, [task.id])

  useEffect(() => { refreshSubs() }, [refreshSubs])

  useEffect(() => {
    setEditing({})
    setEditingInstructions(false)
    setSaveError('')
    setDispatchError('')
    setConfirmDelete(false)
    setDeleteError('')
  }, [task.id])

  const edit = (key, value) => setEditing(prev => ({ ...prev, [key]: value }))

  const getVal = (key) => key in editing ? editing[key] : props[key]

  const handleSave = async () => {
    if (Object.keys(editing).length === 0) return
    setSaving(true)
    setSaveError('')
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
      setSaveError(e.message)
    }
    setSaving(false)
  }

  const handleDelete = async () => {
    setDeleteError('')
    try {
      await deleteTask(task.id)
      onDelete()
      await onRefresh()
    } catch (e) {
      setDeleteError(e.message)
      setConfirmDelete(false)
    }
  }

  const handleDispatch = async () => {
    setDispatching(true)
    setDispatchError('')
    try {
      const result = await dispatchTask(task.id, context || undefined)
      setLastDispatchId(result.dispatch_id)
      setContext('')
      await onRefresh()
    } catch (e) {
      setDispatchError(e.message)
    }
    setDispatching(false)
  }

  useEffect(() => {
    if (!lastDispatchId) return
    const t = setTimeout(() => setLastDispatchId(null), 4000)
    return () => clearTimeout(t)
  }, [lastDispatchId])

  const isDirty = Object.keys(editing).length > 0

  return (
    <div>
      {isDirty && (
        <div className="task-save-bar">
          <span className="task-save-bar-label">Unsaved changes</span>
          {saveError && <span style={{ fontSize: 11, color: 'var(--danger)', flex: 1 }}>{saveError}</span>}
          <button onClick={() => { setEditing({}); setSaveError('') }}>Discard</button>
          <button className="primary" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: props.description ? 4 : 12 }}>
        <h2 style={{ flex: 1 }}>{task.name}</h2>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>id: {task.id}</span>
      </div>
      {props.description && (
        <div className="task-description-display md-content">
          <Markdown>{props.description}</Markdown>
        </div>
      )}

      {/* Actions */}
      <div className="task-actions">
        <AutoTextarea
          value={context}
          onChange={e => setContext(e.target.value)}
          placeholder="Optional context..."
          maxHeight={120}
          minRows={1}
          style={{ flex: 1 }}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey && !dispatching && !props.running) {
              e.preventDefault()
              handleDispatch()
            }
          }}
        />
        <button
          className="primary"
          onClick={handleDispatch}
          disabled={dispatching || props.running}
          title={props.running ? 'Task is currently running' : undefined}
          style={{ alignSelf: 'flex-end' }}
        >
          {dispatching ? <><span className="tool-spinner" style={{ marginRight: 5 }} />Queuing</> : props.running ? '● Running' : '▶ Queue'}
        </button>
      </div>

      {lastDispatchId && (
        <div style={{ fontSize: 11, color: 'var(--success)', marginBottom: 8 }}>
          ✓ Queued as dispatch #{lastDispatchId}
        </div>
      )}
      {dispatchError && (
        <div style={{ fontSize: 11, color: 'var(--danger)', marginBottom: 8 }}>{dispatchError}</div>
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
            <div className="field-group">
              <label>Instructions</label>
              {editingInstructions || !getVal('instructions') ? (
                <textarea
                  value={getVal('instructions') || ''}
                  onChange={e => edit('instructions', e.target.value)}
                  onBlur={() => { if (getVal('instructions')) setEditingInstructions(false) }}
                  placeholder="Detailed instructions for what this task should do..."
                  rows={10}
                  autoFocus={editingInstructions}
                  style={{ resize: 'vertical', minHeight: 200 }}
                />
              ) : (
                <div className="instructions-preview md-content" onClick={() => setEditingInstructions(true)}>
                  <Markdown>{getVal('instructions')}</Markdown>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Triggers */}
      <div className="task-section">
        <h3>Triggers</h3>

        <div className="field-group" style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
          <label className="checkbox-label" style={{ whiteSpace: 'nowrap' }}>
            <input
              type="checkbox"
              checked={getVal('watch_enabled') || false}
              onChange={e => edit('watch_enabled', e.target.checked)}
            />
            Auto-queue on commit
          </label>
          <label className="checkbox-label" style={{
            whiteSpace: 'nowrap',
            opacity: getVal('watch_enabled') ? 1 : 0.4,
          }}>
            <input
              type="checkbox"
              checked={getVal('coalesce_dispatches') || false}
              onChange={e => edit('coalesce_dispatches', e.target.checked)}
              disabled={!getVal('watch_enabled')}
            />
            Coalesce pending dispatches
          </label>
        </div>

        <div className="field-group">
          <label>Schedule (cron)</label>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              type="text"
              value={getVal('schedule') || ''}
              onChange={e => edit('schedule', e.target.value)}
              placeholder="e.g. */30 * * * *  or  0 9 * * 1-5"
              style={{ flex: 1, fontFamily: 'monospace' }}
            />
            {getVal('schedule') && (
              <span style={{ fontSize: 11, color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
                {describeCron(getVal('schedule'))}
              </span>
            )}
          </div>
        </div>

        <div className="field-group">
          <label>Timeout (seconds)</label>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              type="number"
              value={getVal('timeout') ?? 900}
              onChange={e => edit('timeout', parseInt(e.target.value) || 0)}
              min={0}
              step={60}
              style={{ width: 100, fontFamily: 'monospace' }}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
              {(() => {
                const v = getVal('timeout') ?? 900
                if (v === 0) return 'no limit'
                const mins = Math.floor(v / 60)
                const secs = v % 60
                return secs === 0 ? `${mins}m` : `${mins}m ${secs}s`
              })()}
            </span>
          </div>
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
                    <span style={{ color: 'var(--text-muted)' }}>{formatSize(f.size)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Danger zone */}
      <div className="task-section">
        <h3>Danger Zone</h3>
        {confirmDelete ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 12 }}>Delete "{task.name}"?</span>
            <button className="danger small" onClick={handleDelete}>Confirm</button>
            <button className="small" onClick={() => setConfirmDelete(false)}>Cancel</button>
          </div>
        ) : (
          <button className="danger small" onClick={() => setConfirmDelete(true)}>Delete Task</button>
        )}
        {deleteError && <div style={{ fontSize: 11, color: 'var(--danger)', marginTop: 6 }}>{deleteError}</div>}
      </div>
    </div>
  )
}

function AutoTextarea({ value, onChange, onBlur, onKeyDown, placeholder, maxHeight = 200, minRows = 2, autoFocus, style }) {
  const ref = useRef(null)
  const resize = () => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    const contentHeight = el.scrollHeight
    el.style.height = Math.min(contentHeight, maxHeight) + 'px'
    el.style.overflowY = contentHeight > maxHeight ? 'auto' : 'hidden'
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
      onKeyDown={onKeyDown}
      onInput={resize}
      placeholder={placeholder}
      style={{ resize: 'none', maxHeight, ...style }}
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

function describeCron(expr) {
  if (!expr || !expr.trim()) return ''
  const parts = expr.trim().split(/\s+/)
  if (parts.length !== 5) return 'invalid'
  const [min, hour, dom, mon, dow] = parts
  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

  // Common patterns
  if (min.startsWith('*/') && hour === '*' && dom === '*' && mon === '*' && dow === '*')
    return `every ${min.slice(2)} min`
  if (hour.startsWith('*/') && min === '0' && dom === '*' && mon === '*' && dow === '*')
    return `every ${hour.slice(2)} hours`
  if (dom === '*' && mon === '*' && dow === '*' && !min.includes('/') && !hour.includes('/'))
    return `daily at ${hour}:${min.padStart(2, '0')}`
  if (dom === '*' && mon === '*' && dow !== '*' && !min.includes('/') && !hour.includes('/')) {
    const days = dow.split(',').map(d => {
      if (d === '1-5') return 'weekdays'
      if (d === '0,6') return 'weekends'
      return DAYS[parseInt(d)] || d
    }).join(', ')
    return `${days} at ${hour}:${min.padStart(2, '0')}`
  }
  return 'custom schedule'
}
