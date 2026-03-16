import { useState, useEffect, useCallback, useRef } from 'react'
import Markdown from 'react-markdown'
import {
  createTask, updateTask, deleteTask, getTaskSubscriptions,
  dispatchTask, listMcpServers,
} from '../api'
import HelpTip from './HelpTip'

const TIPS = {
  subscriptions: 'Glob patterns, one per line. * matches files in one directory; ** matches across directories recursively. Patterns serve two purposes: they determine which commits trigger this task (watch), and they inject matching files as context into every dispatch prompt.',
  schedule: 'Five-field cron: minute hour day-of-month month day-of-week. Supports ranges (1-5), lists (0,15,30), steps (*/10), and wildcards (*). Examples: */30 * * * * (every 30 min), 0 9 * * 1-5 (weekdays at 9am). The first evaluation after setting a schedule establishes a baseline — it does not fire immediately.',
  allowedTools: 'Comma-separated tool names the agent can use (e.g. Read, Edit, Bash, Write). When set, the platform computes the complement and hides all other tools from the agent. Leave empty to use the default tool set.',
  requireApproval: 'When enabled, automated triggers (commit-watch, schedule, dependency) produce dispatches that wait for manual approval before executing. Manual dispatches bypass this gate.',
  coalesceDispatches: 'When enabled, the task will never have more than one pending dispatch. Any new trigger merges into the existing pending dispatch instead of creating a new queue entry. Useful for tasks that should catch up in one run rather than queuing redundant work.',
  dependencies: 'This task auto-dispatches when all selected upstream tasks complete successfully. Timed-out, failed, or cancelled dispatches do not trigger dependents.',
  timeout: 'Maximum execution time in seconds. The platform gracefully terminates the agent when reached, then force-kills if it does not exit. Timed-out dispatches do not trigger downstream dependencies. Set to 0 for no limit.',
  model: 'Opus: highest capability, slowest, most expensive. Sonnet: balanced capability and speed. Haiku: fastest, cheapest, best for simple or high-frequency tasks.',
  mcpServers: 'External MCP servers to connect to this task\'s agent. Servers must first be registered in Settings. When enabled, the agent can use tools provided by these servers alongside the platform\'s built-in tools.',
}

export default function Tasks({ tasks, onRefresh, onNavigate }) {
  const [selected, setSelected] = useState(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [createError, setCreateError] = useState('')
  const [search, setSearch] = useState('')
  const searchRef = useRef(null)

  const filteredTasks = tasks.filter(t => {
    if (!search.trim()) return true
    const q = search.toLowerCase()
    const props = t.properties || {}
    const fields = [
      t.name,
      t.id,
      props.description,
      props.instructions,
      props.model,
      Array.isArray(props.subscriptions) ? props.subscriptions.join(' ') : '',
      Array.isArray(props.depends_on) ? props.depends_on.join(' ') : '',
      props.schedule,
    ]
    return fields.some(f => f && f.toLowerCase().includes(q))
  })

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
  // When search is active and the selected task is filtered out, don't show its detail —
  // the task isn't visible in the list so showing it in the panel is confusing.
  const detailVisible = !search.trim() || !!filteredTasks.find(t => t.id === selected)

  return (
    <>
      <div className="header-bar">
        <h1>Tasks</h1>
      </div>

      <div className="tasks-layout">
        <div className="task-list">
          <div className="task-search">
            <input
              ref={searchRef}
              type="text"
              placeholder="Filter tasks..."
              value={search}
              onChange={e => setSearch(e.target.value)}
              onKeyDown={e => { if (e.key === 'Escape') { setSearch(''); e.currentTarget.blur() } }}
            />
            {search && (
              <button className="task-search-clear" onClick={() => { setSearch(''); searchRef.current?.focus() }}>✕</button>
            )}
          </div>
          <div className="scroll-area">
            {filteredTasks.length === 0 && search && (
              <div className="muted-text" style={{ padding: '12px' }}>No matches</div>
            )}
            {filteredTasks.map(t => (
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
                {createError && <span className="error-text">{createError}</span>}
              </div>
            ) : (
              <button className="small" style={{ width: '100%' }} onClick={() => setCreating(true)}>
                + New Task
              </button>
            )}
          </div>
        </div>

        <div className="task-detail">
          {detailVisible && activeTask ? (
            <TaskDetail key={activeTask.id} task={activeTask} allTasks={tasks} onRefresh={onRefresh} onDelete={() => setSelected(null)} onNavigate={onNavigate} />
          ) : (
            <div className="empty-state">Select or create a task</div>
          )}
        </div>
      </div>
    </>
  )
}

function TaskDetail({ task, allTasks, onRefresh, onDelete, onNavigate }) {
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
  const [activeTab, setActiveTab] = useState('definition')
  const [availableMcpServers, setAvailableMcpServers] = useState([])
  const props = task.properties || {}

  const refreshSubs = useCallback(() => {
    getTaskSubscriptions(task.id).then(setSubs).catch(() => setSubs(null))
  }, [task.id])

  useEffect(() => { refreshSubs() }, [refreshSubs])

  useEffect(() => {
    listMcpServers().then(setAvailableMcpServers).catch(() => setAvailableMcpServers([]))
  }, [])

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
          {saveError && <span className="error-text" style={{ flex: 1 }}>{saveError}</span>}
          <button onClick={() => { setEditing({}); setSaveError('') }}>Discard</button>
          <button className="primary" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: props.description ? 4 : 12 }}>
        <h2 style={{ flex: 1 }}>{task.name}</h2>
        <span className="muted-text">id: {task.id}</span>
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
          placeholder="Optional context... (↵ to queue)"
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
          {dispatching
            ? <><span className="tool-spinner" style={{ marginRight: 5 }} />Queuing</>
            : props.running
            ? <><span className="pulse-dot" style={{ marginRight: 4 }}>●</span>Running</>
            : '▶ Queue'}
        </button>
      </div>

      {lastDispatchId && (
        <div className="dispatch-queued">
          <span>✓ Queued as dispatch #{lastDispatchId}</span>
          {onNavigate && (
            <button className="small" onClick={() => onNavigate('queue')}>View in Queue →</button>
          )}
        </div>
      )}
      {dispatchError && (
        <div className="error-text" style={{ marginBottom: 8 }}>{dispatchError}</div>
      )}

      {/* Tabbed sections */}
      <div className="task-tabs">
        <button
          className={`task-tab-btn${activeTab === 'definition' ? ' active' : ''}`}
          onClick={() => setActiveTab('definition')}
        >
          Task Configuration
        </button>
        <button
          className={`task-tab-btn${activeTab === 'triggers' ? ' active' : ''}`}
          onClick={() => setActiveTab('triggers')}
        >
          Triggers
        </button>
      </div>

      {activeTab === 'definition' && (
        <div className="task-tab-panel">
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
                <div className="label-row">
                  <label>Allowed Tools</label>
                  <HelpTip text={TIPS.allowedTools} />
                </div>
                <input
                  type="text"
                  value={(getVal('allowed_tools') || []).join(', ')}
                  onChange={e => edit('allowed_tools', e.target.value.split(',').map(s => s.trim()).filter(Boolean))}
                  placeholder="Comma-separated tool names..."
                />
              </div>

              <div className="field-group">
                <div className="label-row">
                  <label>Model</label>
                  <HelpTip text={TIPS.model} />
                </div>
                <select value={getVal('model') || 'sonnet'} onChange={e => edit('model', e.target.value)}>
                  <option value="sonnet">Sonnet</option>
                  <option value="opus">Opus</option>
                  <option value="haiku">Haiku</option>
                </select>
              </div>

              <div className="field-group">
                <div className="label-row">
                  <label>Timeout (seconds)</label>
                  <HelpTip text={TIPS.timeout} />
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <input
                    type="number"
                    value={getVal('timeout') ?? 900}
                    onChange={e => edit('timeout', parseInt(e.target.value) || 0)}
                    min={0}
                    step={60}
                    style={{ width: 100 }}
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

              {availableMcpServers.length > 0 && (
                <div className="field-group">
                  <div className="label-row">
                    <label>MCP Servers</label>
                    <HelpTip text={TIPS.mcpServers} />
                  </div>
                  <div className="checkbox-list">
                    {availableMcpServers.map(s => {
                      const enabled = getVal('mcp_servers') || []
                      const checked = enabled.includes(s.name)
                      return (
                        <label key={s.name} className="checkbox-label" style={{ fontSize: 12 }}>
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => {
                              const next = checked ? enabled.filter(n => n !== s.name) : [...enabled, s.name]
                              edit('mcp_servers', next)
                            }}
                          />
                          {s.name}
                        </label>
                      )
                    })}
                  </div>
                </div>
              )}
            </div>

            <div className="task-def-right">
              <div className="field-group">
                <label>Instructions</label>
                {editingInstructions || !getVal('instructions') ? (
                  <textarea
                    value={getVal('instructions') || ''}
                    onChange={e => edit('instructions', e.target.value)}
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
      )}

      {activeTab === 'triggers' && (
        <div className="task-tab-panel">
          <div className="field-group trigger-options">
            <div className="label-row">
              <label className="checkbox-label" style={{ whiteSpace: 'nowrap' }}>
                <input
                  type="checkbox"
                  checked={getVal('require_approval') || false}
                  onChange={e => edit('require_approval', e.target.checked)}
                />
                Require approval for automatic dispatches
              </label>
              <HelpTip text={TIPS.requireApproval} />
            </div>
            <div className="label-row">
              <label className="checkbox-label" style={{ whiteSpace: 'nowrap' }}>
                <input
                  type="checkbox"
                  checked={getVal('coalesce_dispatches') || false}
                  onChange={e => edit('coalesce_dispatches', e.target.checked)}
                />
                Coalesce pending dispatches
              </label>
              <HelpTip text={TIPS.coalesceDispatches} />
            </div>
          </div>

          <div className="field-group">
            <div className="label-row">
              <label>Dependencies (runs after these tasks complete)</label>
              <HelpTip text={TIPS.dependencies} />
            </div>
            <div className="checkbox-list">
              {allTasks.filter(t => t.id !== task.id).map(t => {
                const deps = getVal('depends_on') || []
                const checked = deps.includes(t.id)
                return (
                  <label key={t.id} className="checkbox-label" style={{ fontSize: 12 }}>
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => {
                        const next = checked ? deps.filter(d => d !== t.id) : [...deps, t.id]
                        edit('depends_on', next)
                      }}
                    />
                    {t.name}
                  </label>
                )
              })}
              {allTasks.length <= 1 && (
                <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>No other tasks to depend on</span>
              )}
            </div>
          </div>

          <div className="field-group">
            <div className="label-row">
              <label>Schedule (cron)</label>
              <HelpTip text={TIPS.schedule} />
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <input
                type="text"
                value={getVal('schedule') || ''}
                onChange={e => edit('schedule', e.target.value)}
                placeholder="e.g. */30 * * * *  or  0 9 * * 1-5"
                style={{ flex: 1 }}
              />
              {getVal('schedule') && (
                <span style={{ fontSize: 11, color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
                  {describeCron(getVal('schedule'))}
                </span>
              )}
            </div>
          </div>

          <div className="field-group">
            <div className="label-row">
              <label>Subscriptions</label>
              <HelpTip text={TIPS.subscriptions} />
            </div>
            <AutoTextarea
              value={'subscriptions' in editing ? editing.subscriptions : arrayToLines(props.subscriptions)}
              onChange={e => edit('subscriptions', e.target.value)}
              placeholder={"e.g. backend/**/*.py\nfrontend/src/**/*.jsx"}
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
                    <div key={f.path} className="resolved-file-item">
                      <span>{f.path}</span>
                      <span style={{ color: 'var(--text-muted)' }}>{formatSize(f.size)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Danger zone */}
      <div className="task-section danger">
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
        {deleteError && <div className="error-text" style={{ marginTop: 6 }}>{deleteError}</div>}
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
