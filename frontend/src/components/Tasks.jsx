import { useState, useEffect, useCallback, useRef } from 'react'
import Markdown from 'react-markdown'
import {
  createJob, updateJob, deleteJob, getJobSubscriptions,
  enqueueTask, listMcpServers, getToolInventory, reorderJobs,
} from '../api'
import HelpTip from './HelpTip'

const TIPS = {
  subscriptions: 'Glob patterns, one per line. * matches files in one directory; ** matches across directories recursively. Patterns serve two purposes: they determine which commits trigger this job (watch), and they inject matching files as context into every task prompt.',
  schedule: 'Five-field cron: minute hour day-of-month month day-of-week. Supports ranges (1-5), lists (0,15,30), steps (*/10), and wildcards (*). Examples: */30 * * * * (every 30 min), 0 9 * * 1-5 (weekdays at 9am). The first evaluation after setting a schedule establishes a baseline — it does not fire immediately.',
  allowedTools: 'CLI tools the agent can use, selected from the platform\'s discovered tool inventory. When a subset is selected, the platform computes the complement and hides all other tools from the agent. All checked = default (no restrictions).',
  requireApproval: 'When enabled, automated triggers (commit-watch, schedule, dependency) produce tasks that wait for manual approval before executing. Manual tasks bypass this gate.',
  coalesceTasks: 'When enabled, the job will never have more than one pending task. Any new trigger coalesces into the existing pending task instead of creating a new queue entry. Useful for jobs that should catch up in one run rather than queuing redundant work.',
  dependencies: 'This job auto-dispatches when all selected upstream jobs complete successfully. Timed-out, failed, or cancelled tasks do not trigger dependents.',
  timeout: 'Maximum execution time in seconds. The platform gracefully terminates the agent when reached, then force-kills if it does not exit. Timed-out tasks do not trigger downstream dependencies. Set to 0 for no limit.',
  model: 'Opus: highest capability, slowest, most expensive. Sonnet: balanced capability and speed. Haiku: fastest, cheapest, best for simple or high-frequency jobs.',
  mcpServers: 'External MCP servers to connect to this job\'s agent. Servers must first be registered in Settings. When enabled, the agent can use tools provided by these servers alongside the platform\'s built-in tools.',
  allowedInternalTools: 'Internal MCP tools the agent can access. When a subset is selected, only listed tools are presented by the internal server. All checked = default (no restrictions). Use this to create read-only jobs or restrict dispatch capabilities.',
  allowedDispatchTargets: 'Jobs this agent can dispatch via the dispatch_task tool. When none are selected, the agent cannot dispatch other jobs. Self-dispatch is always prohibited.',
}

export default function Tasks({ jobs, onRefresh, onNavigate }) {
  const [selected, setSelected] = useState(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [createError, setCreateError] = useState('')
  const [search, setSearch] = useState('')
  const searchRef = useRef(null)
  const [dragIdx, setDragIdx] = useState(null)
  const [dragOverIdx, setDragOverIdx] = useState(null)

  const filteredJobs = jobs.filter(j => {
    if (!search.trim()) return true
    const q = search.toLowerCase()
    const props = j.properties || {}
    const fields = [
      j.name,
      j.id,
      props.description,
      props.instructions,
      props.model,
      Array.isArray(props.subscriptions) ? props.subscriptions.join(' ') : '',
      Array.isArray(props.depends_on) ? props.depends_on.join(' ') : '',
      props.schedule,
    ]
    return fields.some(f => f && f.toLowerCase().includes(q))
  })

  const canDrag = !search.trim() // disable drag during search

  const handleDragEnd = async () => {
    if (dragIdx !== null && dragOverIdx !== null && dragIdx !== dragOverIdx) {
      const ids = jobs.map(j => j.id)
      const [moved] = ids.splice(dragIdx, 1)
      ids.splice(dragOverIdx, 0, moved)
      try { await reorderJobs(ids); await onRefresh() } catch {}
    }
    setDragIdx(null)
    setDragOverIdx(null)
  }

  useEffect(() => {
    if (!selected && jobs.length > 0) {
      setSelected(jobs[0].id)
    }
  }, [jobs, selected])

  const handleCreate = async () => {
    if (!newName.trim()) return
    setCreateError('')
    try {
      const job = await createJob(newName.trim())
      setNewName('')
      setCreating(false)
      await onRefresh()
      setSelected(job.id)
    } catch (e) {
      setCreateError(e.message)
    }
  }

  const activeJob = jobs.find(j => j.id === selected)
  // When search is active and the selected job is filtered out, don't show its detail —
  // the job isn't visible in the list so showing it in the panel is confusing.
  const detailVisible = !search.trim() || !!filteredJobs.find(j => j.id === selected)

  return (
    <>
      <div className="header-bar">
        <h1>Jobs</h1>
      </div>

      <div className="tasks-layout">
        <div className="task-list">
          <div className="task-search">
            <input
              ref={searchRef}
              type="text"
              placeholder="Filter jobs..."
              value={search}
              onChange={e => setSearch(e.target.value)}
              onKeyDown={e => { if (e.key === 'Escape') { setSearch(''); e.currentTarget.blur() } }}
            />
            {search && (
              <button className="task-search-clear" onClick={() => { setSearch(''); searchRef.current?.focus() }}>✕</button>
            )}
          </div>
          <div className="scroll-area">
            {filteredJobs.length === 0 && search && (
              <div className="muted-text" style={{ padding: 'var(--space-6)' }}>No matches</div>
            )}
            {filteredJobs.map((j, i) => (
              <div
                key={j.id}
                className={`task-list-item ${selected === j.id ? 'active' : ''}${dragOverIdx === i && dragIdx !== i ? ' drag-over' : ''}${dragIdx === i ? ' dragging' : ''}`}
                onClick={() => setSelected(j.id)}
                draggable={canDrag}
                onDragStart={e => { setDragIdx(i); e.dataTransfer.effectAllowed = 'move' }}
                onDragOver={e => { e.preventDefault(); setDragOverIdx(i) }}
                onDragEnd={handleDragEnd}
              >
                <span className={`status-dot ${j.properties?.running ? 'running' : 'idle'}`} />
                <span>{j.name}</span>
              </div>
            ))}
          </div>
          <div className="task-list-footer">
            {creating ? (
              <div className="task-create-form">
                <div className="task-create-form-row">
                  <input
                    type="text"
                    placeholder="Job name"
                    value={newName}
                    onChange={e => { setNewName(e.target.value); setCreateError('') }}
                    onKeyDown={e => e.key === 'Enter' && handleCreate()}
                    autoFocus
                  />
                  <button className="small primary" onClick={handleCreate}>+</button>
                  <button className="small" onClick={() => { setCreating(false); setCreateError('') }}>✕</button>
                </div>
                {createError && <span className="error-text">{createError}</span>}
              </div>
            ) : (
              <button className="small" onClick={() => setCreating(true)}>
                + New Job
              </button>
            )}
          </div>
        </div>

        <div className="task-detail">
          {detailVisible && activeJob ? (
            <JobDetail key={activeJob.id} job={activeJob} allJobs={jobs} onRefresh={onRefresh} onDelete={() => setSelected(null)} onNavigate={onNavigate} />
          ) : (
            <div className="empty-state">Select or create a job</div>
          )}
        </div>
      </div>
    </>
  )
}

function JobDetail({ job, allJobs, onRefresh, onDelete, onNavigate }) {
  const [editing, setEditing] = useState({})
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [dispatching, setDispatching] = useState(false)
  const [dispatchError, setDispatchError] = useState('')
  const [lastTaskId, setLastTaskId] = useState(null)
  const [context, setContext] = useState('')
  const [subs, setSubs] = useState(null)
  const [subsOpen, setSubsOpen] = useState(false)
  const [editingInstructions, setEditingInstructions] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const [activeTab, setActiveTab] = useState('definition')
  const [availableMcpServers, setAvailableMcpServers] = useState([])
  const [toolInventory, setToolInventory] = useState({ cli_native: [], internal_mcp: [], external_servers: {} })
  const props = job.properties || {}

  const refreshSubs = useCallback(() => {
    getJobSubscriptions(job.id).then(setSubs).catch(() => setSubs(null))
  }, [job.id])

  useEffect(() => { refreshSubs() }, [refreshSubs])

  useEffect(() => {
    listMcpServers().then(setAvailableMcpServers).catch(() => setAvailableMcpServers([]))
    getToolInventory().then(setToolInventory).catch(() => {})
  }, [])

  useEffect(() => {
    setEditing({})
    setEditingInstructions(false)
    setSaveError('')
    setDispatchError('')
    setConfirmDelete(false)
    setDeleteError('')
  }, [job.id])

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
      await updateJob(job.id, payload)
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
      await deleteJob(job.id)
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
      const result = await enqueueTask(job.id, context || undefined)
      setLastTaskId(result.task_id)
      setContext('')
      await onRefresh()
    } catch (e) {
      setDispatchError(e.message)
    }
    setDispatching(false)
  }

  useEffect(() => {
    if (!lastTaskId) return
    const t = setTimeout(() => setLastTaskId(null), 4000)
    return () => clearTimeout(t)
  }, [lastTaskId])

  const isDirty = Object.keys(editing).length > 0

  return (
    <div>
      {isDirty && (
        <div className="task-save-bar">
          <span className="task-save-bar-label">Unsaved changes</span>
          {saveError && <span className="error-text">{saveError}</span>}
          <button onClick={() => { setEditing({}); setSaveError('') }}>Discard</button>
          <button className="primary" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      )}

      <div className="job-header" style={{ marginBottom: props.description ? 'var(--space-2)' : 'var(--space-6)' }}>
        <h2>{job.name}</h2>
        <span className="muted-text">id: {job.id}</span>
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
          placeholder="Optional context... (↵ to dispatch)"
          maxHeight={120}
          minRows={1}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey && !dispatching) {
              e.preventDefault()
              handleDispatch()
            }
          }}
        />
        <button
          className="primary"
          onClick={handleDispatch}
          disabled={dispatching}
        >
          {dispatching
            ? <><span className="tool-spinner" />Dispatching</>
            : '▶ Dispatch'}
        </button>
      </div>

      {lastTaskId && (
        <div className="dispatch-queued">
          <span>✓ Queued as task #{lastTaskId}</span>
          {onNavigate && (
            <button className="small" onClick={() => onNavigate('queue')}>View in Queue →</button>
          )}
        </div>
      )}
      {dispatchError && (
        <div className="error-text dispatch-error">{dispatchError}</div>
      )}

      {/* Tabbed sections */}
      <div className="task-tabs">
        <button
          className={`task-tab-btn${activeTab === 'definition' ? ' active' : ''}`}
          onClick={() => setActiveTab('definition')}
        >
          Job Definition
        </button>
        <button
          className={`task-tab-btn${activeTab === 'dispatch' ? ' active' : ''}`}
          onClick={() => setActiveTab('dispatch')}
        >
          Dispatch
        </button>
        <button
          className={`task-tab-btn${activeTab === 'tools' ? ' active' : ''}`}
          onClick={() => setActiveTab('tools')}
        >
          Tools
        </button>
      </div>

      {activeTab === 'definition' && (
        <div className="task-tab-panel task-tab-panel--definition">
          <div className="field-group">
            <label>Description</label>
            <AutoTextarea
              value={getVal('description') || ''}
              onChange={e => edit('description', e.target.value)}
              placeholder="Short description for the job registry..."
              maxHeight={200}
              minRows={3}
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

          <div className="field-group field-group--grow">
            <label>Instructions</label>
            {editingInstructions || !getVal('instructions') ? (
              <textarea
                value={getVal('instructions') || ''}
                onChange={e => edit('instructions', e.target.value)}
                placeholder="Detailed instructions for what this job should do..."
                autoFocus={editingInstructions}
                className="instructions-textarea"
              />
            ) : (
              <div className="instructions-preview md-content" onClick={() => setEditingInstructions(true)}>
                <Markdown>{getVal('instructions')}</Markdown>
              </div>
            )}
          </div>
        </div>
      )}

      {activeTab === 'dispatch' && (
        <div className="task-tab-panel">
          <div className="field-group">
            <div className="label-row">
              <label>Timeout (seconds)</label>
              <HelpTip text={TIPS.timeout} />
            </div>
            <div className="field-row">
              <input
                type="number"
                value={getVal('timeout') ?? 900}
                onChange={e => edit('timeout', parseInt(e.target.value) || 0)}
                min={0}
                step={60}
                className="timeout-input"
              />
              <span className="muted-text">
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

          <div className="field-group trigger-options">
            <div className="label-row">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={getVal('require_approval') || false}
                  onChange={e => edit('require_approval', e.target.checked)}
                />
                Require approval for automatic tasks
              </label>
              <HelpTip text={TIPS.requireApproval} />
            </div>
            <div className="label-row">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={getVal('coalesce_tasks') || false}
                  onChange={e => edit('coalesce_tasks', e.target.checked)}
                />
                Coalesce pending tasks
              </label>
              <HelpTip text={TIPS.coalesceTasks} />
            </div>
          </div>

          <div className="field-group">
            <div className="label-row">
              <label>Dependencies (run these after this job completes a task)</label>
              <HelpTip text={TIPS.dependencies} />
            </div>
            <div className="checkbox-list">
              {allJobs.filter(j => j.id !== job.id).map(j => {
                const deps = getVal('depends_on') || []
                const checked = deps.includes(j.id)
                return (
                  <label key={j.id} className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => {
                        const next = checked ? deps.filter(d => d !== j.id) : [...deps, j.id]
                        edit('depends_on', next)
                      }}
                    />
                    {j.name}
                  </label>
                )
              })}
              {allJobs.length <= 1 && (
                <span className="muted-text">No other jobs to depend on</span>
              )}
            </div>
          </div>

          <div className="field-group">
            <div className="label-row">
              <label>Schedule (cron)</label>
              <HelpTip text={TIPS.schedule} />
            </div>
            <div className="field-row">
              <input
                type="text"
                value={getVal('schedule') || ''}
                onChange={e => edit('schedule', e.target.value)}
                placeholder="e.g. */30 * * * *  or  0 9 * * 1-5"
              />
              {getVal('schedule') && (
                <span className="muted-text">
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
                      <span className="file-size">{formatSize(f.size)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {activeTab === 'tools' && (
        <div className="task-tab-panel">
          <div className="field-group">
            <div className="label-row">
              <label>Allowed CLI Tools</label>
              <HelpTip text={TIPS.allowedTools} />
            </div>
            {toolInventory.cli_native.length > 0 ? (
              <div className="checkbox-list">
                {toolInventory.cli_native.map(tool => {
                  const allowed = getVal('allowed_tools') || []
                  const checked = allowed.includes(tool)
                  const allEmpty = allowed.length === 0
                  return (
                    <label key={tool} className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={allEmpty || checked}
                        onChange={() => {
                          if (allEmpty) {
                            // Switching from "all" to explicit selection — select all except this one
                            edit('allowed_tools', toolInventory.cli_native.filter(t => t !== tool))
                          } else {
                            const next = checked ? allowed.filter(t => t !== tool) : [...allowed, tool]
                            // If all tools are now selected, clear back to empty (= default "all")
                            edit('allowed_tools', next.length === toolInventory.cli_native.length ? [] : next)
                          }
                        }}
                      />
                      {tool}
                    </label>
                  )
                })}
              </div>
            ) : (
              <span className="muted-text">Loading tools...</span>
            )}
            {(getVal('allowed_tools') || []).length === 0 && toolInventory.cli_native.length > 0 && (
              <span className="muted-text">All tools enabled (default)</span>
            )}
          </div>

          {toolInventory.internal_mcp.length > 0 && (
            <div className="field-group">
              <div className="label-row">
                <label>Internal Platform Tools</label>
                <HelpTip text={TIPS.allowedInternalTools} />
              </div>
              <div className="checkbox-list">
                {toolInventory.internal_mcp.map(tool => {
                  const allowed = getVal('allowed_internal_tools') || []
                  const checked = allowed.includes(tool)
                  const allEmpty = allowed.length === 0
                  return (
                    <label key={tool} className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={allEmpty || checked}
                        onChange={() => {
                          if (allEmpty) {
                            edit('allowed_internal_tools', toolInventory.internal_mcp.filter(t => t !== tool))
                          } else {
                            const next = checked ? allowed.filter(t => t !== tool) : [...allowed, tool]
                            edit('allowed_internal_tools', next.length === toolInventory.internal_mcp.length ? [] : next)
                          }
                        }}
                      />
                      {tool}
                    </label>
                  )
                })}
              </div>
              {(getVal('allowed_internal_tools') || []).length === 0 && toolInventory.internal_mcp.length > 0 && (
                <span className="muted-text">All internal tools enabled (default)</span>
              )}
            </div>
          )}

          {allJobs.length > 1 && (
            <div className="field-group">
              <div className="label-row">
                <label>Allowed Dispatch Targets</label>
                <HelpTip text={TIPS.allowedDispatchTargets} />
              </div>
              <div className="checkbox-list">
                {allJobs.filter(j => j.id !== job.id).map(j => {
                  const targets = getVal('allowed_dispatch_targets') || []
                  const checked = targets.includes(j.id)
                  return (
                    <label key={j.id} className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => {
                          const next = checked ? targets.filter(t => t !== j.id) : [...targets, j.id]
                          edit('allowed_dispatch_targets', next)
                        }}
                      />
                      {j.name}
                    </label>
                  )
                })}
              </div>
              {(getVal('allowed_dispatch_targets') || []).length === 0 && (
                <span className="muted-text">No dispatch targets — agent cannot dispatch other jobs</span>
              )}
            </div>
          )}

          {(() => {
            const enabledServers = availableMcpServers.filter(s => s.enabled)
            const extServers = toolInventory.external_servers || {}
            return enabledServers.length > 0 ? (
              <div className="field-group">
                <div className="label-row">
                  <label>External MCP Servers</label>
                  <HelpTip text={TIPS.mcpServers} />
                </div>
                <div className="checkbox-list">
                  {enabledServers.map(s => {
                    const selected = getVal('mcp_servers') || []
                    const checked = selected.includes(s.name)
                    const probe = extServers[s.name]
                    return (
                      <div key={s.name}>
                        <label className="checkbox-label">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => {
                              const next = checked ? selected.filter(n => n !== s.name) : [...selected, s.name]
                              edit('mcp_servers', next)
                            }}
                          />
                          {s.name}
                          {probe && probe.status === 'ok' && (
                            <span className="muted-text"> ({probe.tools.length} tools)</span>
                          )}
                          {probe && probe.status === 'error' && (
                            <span className="error-text"> (unreachable)</span>
                          )}
                        </label>
                        {checked && probe && probe.status === 'ok' && probe.tools.length > 0 && (
                          <div className="mcp-server-tools">{probe.tools.join(', ')}</div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            ) : (
              <div className="field-group">
                <span className="muted-text">
                  No enabled MCP servers. Register and enable servers in Settings.
                </span>
              </div>
            )
          })()}
        </div>
      )}

      {/* Danger zone */}
      <div className="task-section danger">
        <h3>Danger Zone</h3>
        {confirmDelete ? (
          <div className="action-row">
            <span className="confirm-text">Delete "{job.name}"?</span>
            <button className="danger small" onClick={handleDelete}>Confirm</button>
            <button className="small" onClick={() => setConfirmDelete(false)}>Cancel</button>
          </div>
        ) : (
          <button className="danger small" onClick={() => setConfirmDelete(true)}>Delete Job</button>
        )}
        {deleteError && <div className="error-text">{deleteError}</div>}
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
