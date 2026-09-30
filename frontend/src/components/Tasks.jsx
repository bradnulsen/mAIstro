import { useState, useEffect, useCallback, useRef } from 'react'
import Markdown from 'react-markdown'
import {
  createJob, updateJob, deleteJob, getJobSubscriptions,
  listMcpServers, getToolInventory, reorderJobs,
  listTemplates, saveTemplate, updateTemplate,
  listLearnings, createLearning, updateLearning, deleteLearning, reorderLearnings,
} from '../api'
import HelpTip from './HelpTip'

const TIPS = {
  subscriptions: 'Glob patterns, one per line. * matches files in one directory; ** matches across directories recursively. Patterns serve two purposes: they determine which commits trigger this job (watch), and they inject matching files as context into every task prompt.',
  schedule: 'Five-field cron: minute hour day-of-month month day-of-week. Supports ranges (1-5), lists (0,15,30), steps (*/10), and wildcards (*). Examples: */30 * * * * (every 30 min), 0 9 * * 1-5 (weekdays at 9am). The first evaluation after setting a schedule establishes a baseline — it does not fire immediately.',
  allowedTools: 'CLI tools the agent can use, selected from the platform\'s discovered tool inventory. When a subset is selected, the platform computes the complement and hides all other tools from the agent. All checked = default (no restrictions).',
  requireApproval: 'When enabled, automated triggers (commit-watch, schedule, cascade) produce tasks that wait for manual approval before executing. Manual tasks bypass this gate.',
  coalesceTasks: 'When enabled, the job will never have more than one pending task. Any new trigger coalesces into the existing pending task instead of creating a new queue entry. Useful for jobs that should catch up in one run rather than queuing redundant work.',
  cascadesFrom: 'Upstream jobs that trigger this job on completion. When any selected upstream job completes successfully, a task is enqueued for this job. Timed-out, failed, or cancelled tasks do not trigger cascades.',
  timeout: 'Maximum execution time in seconds. The platform gracefully terminates the agent when reached, then force-kills if it does not exit. Timed-out tasks do not trigger downstream cascades. Set to 0 for no limit.',
  maxTurns: 'Maximum agent reasoning turns per task. One turn is one cycle of context → reasoning → output (text or tool call). Hitting the limit transitions the task to exhausted (distinct from completed or failed). Lower = tighter scope but more exhaustions; higher = more headroom but more cost.',
  model: 'Fable: most capable, slowest, most expensive. Opus: high capability. Sonnet: balanced capability and speed. Haiku: fastest, cheapest, best for simple or high-frequency jobs.',
  mcpServers: 'External MCP servers to connect to this job\'s agent. Servers must first be registered in Settings. When enabled, the agent can use tools provided by these servers alongside the platform\'s built-in tools.',
  allowedInternalTools: 'Internal MCP tools the agent can access. When a subset is selected, only listed tools are presented by the internal server. All checked = default (no restrictions). Use this to create read-only jobs or restrict dispatch capabilities.',
  allowedDispatchTargets: 'Jobs this agent can dispatch via the dispatch_task tool. When none are selected, the agent cannot dispatch other jobs. Self-dispatch is always prohibited.',
  learnings: 'Discrete, individually-toggleable rules and notes. The agent queries them on demand via list_learnings (returns id + summary) and read_learnings (returns full body). Each row needs a one-sentence summary so the agent can scan breadth-first before deep-diving. Disabled rows are hidden from the agent. Bounded by the max_learnings job property.',
  allowLearningSelfModification: 'When enabled, the agent can add, edit, and delete its own learnings via internal MCP tools — closing the feedback loop "what did I learn this run that should change me next time?". The agent can only modify learnings it itself authored (source=agent); operator-authored rows are never agent-writable. Read tools (list_learnings / read_learnings) are always available regardless of this setting.',
  allowSelfRequeue: 'When enabled, the agent can call requeue_self to queue a follow-up task on this same job (fresh worktree, fresh conversation). Use cases: agent is blocked needing operator input, or finished a discrete chunk that should run as its own dispatch. Throttled — 5 consecutive self-requeues force the next into pending so a runaway agent stops auto-dispatching.',
  autoContinue: 'When enabled, the platform automatically queues an auto_continue task after the agent ends in exhausted (hit max_turns) or timed_out. The continuation inherits the prior worktree+branch and runs a fresh conversation. Throttled — 2 consecutive auto_continues force the next into pending, since back-to-back overflows usually mean the job is misconfigured rather than making progress.',
}

export default function Tasks({ jobs, onRefresh }) {
  const [selected, setSelected] = useState(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [createError, setCreateError] = useState('')
  const [selectedTemplate, setSelectedTemplate] = useState(null)
  const [templates, setTemplates] = useState([])
  const [search, setSearch] = useState('')
  const searchRef = useRef(null)
  const [dragIdx, setDragIdx] = useState(null)
  const [dragOverIdx, setDragOverIdx] = useState(null)

  const loadTemplates = useCallback(() => {
    listTemplates().then(setTemplates).catch(() => setTemplates([]))
  }, [])
  useEffect(() => { loadTemplates() }, [loadTemplates])

  const filteredJobs = jobs.filter(j => {
    if (!search.trim()) return true
    const q = search.toLowerCase()
    const props = j.properties || {}
    const fields = [
      j.name,
      j.id,
      props.summary,
      props.description,
      props.model,
      Array.isArray(props.subscriptions) ? props.subscriptions.join(' ') : '',
      Array.isArray(props.cascades_from) ? props.cascades_from.join(' ') : '',
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
      const props = selectedTemplate ? selectedTemplate.properties : undefined
      const job = await createJob(newName.trim(), props)
      setNewName('')
      setSelectedTemplate(null)
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
                {templates.length > 0 && (
                  <select
                    value={selectedTemplate ? selectedTemplate.id : ''}
                    onChange={e => {
                      const t = templates.find(t => t.id === Number(e.target.value))
                      setSelectedTemplate(t || null)
                      if (t && !newName.trim()) setNewName(t.name)
                    }}
                  >
                    <option value="">Blank job</option>
                    {templates.map(t => (
                      <option key={t.id} value={t.id}>Template: {t.name}</option>
                    ))}
                  </select>
                )}
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
                  <button className="small" onClick={() => { setCreating(false); setCreateError(''); setSelectedTemplate(null) }}>✕</button>
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
            <JobDetail key={activeJob.id} job={activeJob} allJobs={jobs} onRefresh={onRefresh} onDelete={() => setSelected(null)}
              templates={templates} onTemplatesChanged={loadTemplates} />
          ) : (
            <div className="empty-state">Select or create a job</div>
          )}
        </div>
      </div>
    </>
  )
}

const PORTABLE_PROPS = new Set([
  'summary', 'description', 'model', 'allowed_tools', 'allowed_internal_tools',
  'timeout', 'max_turns', 'coalesce_tasks', 'require_approval', 'schedule',
])

function JobDetail({ job, allJobs, onRefresh, onDelete, templates = [], onTemplatesChanged }) {
  const [editing, setEditing] = useState({})
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [subs, setSubs] = useState(null)
  const [subsOpen, setSubsOpen] = useState(false)

  const [editingSummary, setEditingSummary] = useState(false)
  const [editingInstructions, setEditingInstructions] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const [activeTab, setActiveTab] = useState('definition')
  const [availableMcpServers, setAvailableMcpServers] = useState([])
  const [toolInventory, setToolInventory] = useState({ cli_native: [], internal_mcp: [], external_servers: {} })
  const instructionsRef = useRef(null)
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
    setEditingSummary(false)
    setEditingInstructions(false)
    setSaveError('')
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
      setEditingSummary(false)
      setEditingInstructions(false)
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

  const isDirty = Object.keys(editing).length > 0

  return (
    <div>
      {isDirty && (
        <div className="task-save-bar">
          <span className="task-save-bar-label">Unsaved changes</span>
          {saveError && <span className="error-text">{saveError}</span>}
          <button onClick={() => { setEditing({}); setSaveError(''); setEditingSummary(false); setEditingInstructions(false) }}>Discard</button>
          <button className="primary" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      )}

      <div className="job-header">
        <h2
          contentEditable
          suppressContentEditableWarning
          onBlur={e => {
            const val = e.target.textContent.trim()
            if (val && val !== job.name) edit('name', val)
          }}
          onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur() } }}
        >{job.name}</h2>
        <span className="muted-text">{job.slug}</span>
      </div>
      {editingSummary || ('summary' in editing) || !getVal('summary') ? (
        <AutoTextarea
          className="job-summary-inline"
          value={getVal('summary') || ''}
          onChange={e => edit('summary', e.target.value)}
          onBlur={() => { if (getVal('summary') && !('summary' in editing)) setEditingSummary(false) }}
          placeholder="Add a summary..."
          maxHeight={80}
          minRows={1}
          autoFocus={editingSummary}
        />
      ) : (
        <div className="job-summary-display md-content" onClick={() => setEditingSummary(true)}>
          <Markdown>{getVal('summary')}</Markdown>
        </div>
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
          className={`task-tab-btn${activeTab === 'learnings' ? ' active' : ''}`}
          onClick={() => setActiveTab('learnings')}
        >
          Learnings
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
            <div className="label-row">
              <label>Model</label>
              <HelpTip text={TIPS.model} />
            </div>
            <select value={getVal('model') || 'sonnet'} onChange={e => edit('model', e.target.value)}>
              <option value="sonnet">Sonnet</option>
              <option value="opus">Opus</option>
              <option value="fable">Fable</option>
              <option value="haiku">Haiku</option>
            </select>
          </div>

          <div className="field-group field-group--grow">
            <label>Description</label>
            <div className="instructions-stack">
              <textarea
                ref={instructionsRef}
                value={getVal('description') || ''}
                onChange={e => edit('description', e.target.value)}
                onFocus={() => setEditingInstructions(true)}
                onBlur={() => setEditingInstructions(false)}
                placeholder="Detailed description of what this job should do..."
                className="instructions-textarea"
              />
              {getVal('description') && (
                <div
                  className={`instructions-preview md-content${editingInstructions ? ' hidden' : ''}`}
                  onClick={() => instructionsRef.current?.focus()}
                >
                  <Markdown>{getVal('description')}</Markdown>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {activeTab === 'learnings' && (
        <div className="task-tab-panel">
          <div className="learnings-anchor">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={getVal('allow_learning_self_modification') || false}
                onChange={e => edit('allow_learning_self_modification', e.target.checked)}
              />
              Allow agent self-modification of learnings
            </label>
            <HelpTip text={TIPS.allowLearningSelfModification} />
          </div>

          <LearningsList jobId={job.id} />
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

          <div className="field-group">
            <div className="label-row">
              <label>Max turns</label>
              <HelpTip text={TIPS.maxTurns} />
            </div>
            <div className="field-row">
              <input
                type="number"
                value={getVal('max_turns') ?? 100}
                onChange={e => edit('max_turns', parseInt(e.target.value) || 1)}
                min={1}
                step={10}
                className="timeout-input"
              />
              <span className="muted-text">turns</span>
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
            <div className="label-row">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={getVal('allow_self_requeue') || false}
                  onChange={e => edit('allow_self_requeue', e.target.checked)}
                />
                Allow agent self-requeue
              </label>
              <HelpTip text={TIPS.allowSelfRequeue} />
            </div>
            <div className="label-row">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={getVal('auto_continue') || false}
                  onChange={e => edit('auto_continue', e.target.checked)}
                />
                Auto-continue on exhaustion / timeout
              </label>
              <HelpTip text={TIPS.autoContinue} />
            </div>
          </div>

          <div className="field-group">
            <div className="label-row">
              <label>Cascades from (upstream jobs that trigger this job)</label>
              <HelpTip text={TIPS.cascadesFrom} />
            </div>
            <div className="checkbox-list">
              {allJobs.filter(j => j.id !== job.id).map(j => {
                const upstreams = getVal('cascades_from') || []
                const checked = upstreams.includes(j.id)
                return (
                  <label key={j.id} className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => {
                        const next = checked ? upstreams.filter(u => u !== j.id) : [...upstreams, j.id]
                        edit('cascades_from', next)
                      }}
                    />
                    {j.name}
                  </label>
                )
              })}
              {allJobs.length <= 1 && (
                <span className="muted-text">No other jobs to cascade from</span>
              )}
            </div>
          </div>

          <div className="field-group">
            <div className="label-row">
              <label>Schedule (cron)</label>
              <HelpTip text={TIPS.schedule} linkUrl="https://crontab.guru/" linkLabel="crontab.guru" />
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
                  const checked = allowed.length === 0 ? true : allowed.includes(tool)
                  return (
                    <label key={tool} className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => {
                          // Materialize the full list on first toggle if stored as empty (legacy "all" default)
                          const current = allowed.length === 0 ? [...toolInventory.cli_native] : [...allowed]
                          const next = checked ? current.filter(t => t !== tool) : [...current, tool]
                          edit('allowed_tools', next)
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
                  const checked = allowed.length === 0 ? true : allowed.includes(tool)
                  return (
                    <label key={tool} className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => {
                          const current = allowed.length === 0 ? [...toolInventory.internal_mcp] : [...allowed]
                          const next = checked ? current.filter(t => t !== tool) : [...current, tool]
                          edit('allowed_internal_tools', next)
                        }}
                      />
                      {tool}
                    </label>
                  )
                })}
              </div>
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
                  No enabled MCP servers. Register and enable servers in the MCP Servers view.
                </span>
              </div>
            )
          })()}
        </div>
      )}

      {activeTab === 'definition' && (
        <>
          <TemplateSave job={job} templates={templates} onTemplatesChanged={onTemplatesChanged} />

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
        </>
      )}
    </div>
  )
}

function LearningsList({ jobId }) {
  const [items, setItems] = useState(null)
  const [error, setError] = useState('')
  const [adding, setAdding] = useState(false)
  const [newSummary, setNewSummary] = useState('')
  const [newBody, setNewBody] = useState('')
  const [editingId, setEditingId] = useState(null)
  const [editSummary, setEditSummary] = useState('')
  const [editBody, setEditBody] = useState('')
  const [confirmDeleteId, setConfirmDeleteId] = useState(null)
  const [dragIdx, setDragIdx] = useState(null)
  const [dragOverIdx, setDragOverIdx] = useState(null)

  const reload = useCallback(() => {
    listLearnings(jobId).then(setItems).catch(e => setError(e.message))
  }, [jobId])
  useEffect(() => { reload() }, [reload])
  useEffect(() => { setConfirmDeleteId(null); setEditingId(null) }, [jobId])

  const handleAdd = async () => {
    const summary = newSummary.trim()
    const body = newBody.trim()
    if (!summary || !body) return
    try {
      await createLearning(jobId, { summary, body })
      setNewSummary('')
      setNewBody('')
      setAdding(false)
      reload()
    } catch (e) { setError(e.message) }
  }

  const handleSaveEdit = async (id) => {
    const summary = editSummary.trim()
    const body = editBody.trim()
    if (!summary || !body) { setEditingId(null); return }
    try {
      await updateLearning(id, { summary, body })
      setEditingId(null)
      reload()
    } catch (e) { setError(e.message) }
  }

  const handleToggle = async (item) => {
    try {
      await updateLearning(item.id, { enabled: !item.enabled })
      reload()
    } catch (e) { setError(e.message) }
  }

  const handleDelete = async (id) => {
    try {
      await deleteLearning(id)
      setConfirmDeleteId(null)
      reload()
    } catch (e) { setError(e.message) }
  }

  const handleDragEnd = async () => {
    if (dragIdx !== null && dragOverIdx !== null && dragIdx !== dragOverIdx && items) {
      const ids = items.map(l => l.id)
      const [moved] = ids.splice(dragIdx, 1)
      ids.splice(dragOverIdx, 0, moved)
      try { await reorderLearnings(jobId, ids); reload() } catch (e) { setError(e.message) }
    }
    setDragIdx(null)
    setDragOverIdx(null)
  }

  return (
    <div className="field-group learnings-section">
      <div className="label-row">
        <label>Learnings</label>
        <HelpTip text={TIPS.learnings} />
      </div>
      {error && <div className="error-text">{error}</div>}
      <div className="learnings-list">
        {items === null && <span className="muted-text">Loading...</span>}
        {items && items.length === 0 && !adding && (
          <span className="muted-text">No learnings. Add a rule, example, or constraint worth toggling on its own.</span>
        )}
        {items && items.map((l, i) => (
          <div
            key={l.id}
            className={`learning-item${l.enabled ? '' : ' disabled'}${dragOverIdx === i && dragIdx !== i ? ' drag-over' : ''}${dragIdx === i ? ' dragging' : ''}`}
            draggable={editingId !== l.id}
            onDragStart={e => { setDragIdx(i); e.dataTransfer.effectAllowed = 'move' }}
            onDragOver={e => { e.preventDefault(); setDragOverIdx(i) }}
            onDragEnd={handleDragEnd}
          >
            <span className="learning-handle" title="Drag to reorder">⋮⋮</span>
            <input
              type="checkbox"
              checked={l.enabled}
              onChange={() => handleToggle(l)}
              title={l.enabled ? 'Enabled — included in prompt' : 'Disabled — excluded from prompt'}
            />
            {editingId === l.id ? (
              <div className="learning-edit">
                <input
                  className="learning-summary learning-summary-edit"
                  type="text"
                  value={editSummary}
                  onChange={e => setEditSummary(e.target.value)}
                  placeholder="One-sentence summary"
                  maxLength={120}
                  onKeyDown={e => {
                    if (e.key === 'Escape') setEditingId(null)
                    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleSaveEdit(l.id) }
                  }}
                  autoFocus
                />
                <textarea
                  className="learning-body learning-body-edit"
                  value={editBody}
                  onChange={e => setEditBody(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Escape') setEditingId(null)
                    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleSaveEdit(l.id) }
                  }}
                  rows={Math.max(2, editBody.split('\n').length)}
                />
                <div className="learning-new-actions">
                  <button className="small primary" onClick={() => handleSaveEdit(l.id)}>Save</button>
                  <button className="small" onClick={() => setEditingId(null)}>Cancel</button>
                </div>
              </div>
            ) : (
              <div
                className="learning-content"
                onClick={() => { setEditingId(l.id); setEditSummary(l.summary || ''); setEditBody(l.body) }}
                title="Click to edit"
              >
                <div className="learning-summary">{l.summary || <span className="muted-text">(no summary)</span>}</div>
                <div className="learning-body">{l.body}</div>
              </div>
            )}
            <span className={`learning-source learning-source-${l.source}`}>{l.source}</span>
            {confirmDeleteId === l.id ? (
              <div className="learning-delete-confirm">
                <button className="small danger" onClick={() => handleDelete(l.id)}>Yes</button>
                <button className="small" onClick={() => setConfirmDeleteId(null)}>No</button>
              </div>
            ) : (
              <button
                className="small learning-delete"
                onClick={() => setConfirmDeleteId(l.id)}
                title="Delete learning"
              >✕</button>
            )}
          </div>
        ))}
        {adding && (
          <div className="learning-item learning-item-new">
            <div className="learning-edit">
              <input
                className="learning-summary learning-summary-edit"
                type="text"
                value={newSummary}
                onChange={e => setNewSummary(e.target.value)}
                placeholder="One-sentence summary (the index entry)"
                maxLength={120}
                onKeyDown={e => {
                  if (e.key === 'Escape') { setAdding(false); setNewSummary(''); setNewBody('') }
                  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleAdd() }
                }}
                autoFocus
              />
              <textarea
                className="learning-body learning-body-edit"
                value={newBody}
                onChange={e => setNewBody(e.target.value)}
                placeholder="The full rule, example, or constraint..."
                rows={Math.max(2, newBody.split('\n').length)}
                onKeyDown={e => {
                  if (e.key === 'Escape') { setAdding(false); setNewSummary(''); setNewBody('') }
                  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleAdd() }
                }}
              />
              <div className="learning-new-actions">
                <button className="small primary" onClick={handleAdd} disabled={!newSummary.trim() || !newBody.trim()}>Add</button>
                <button className="small" onClick={() => { setAdding(false); setNewSummary(''); setNewBody('') }}>Cancel</button>
              </div>
            </div>
          </div>
        )}
      </div>
      {!adding && (
        <button className="small" onClick={() => setAdding(true)}>+ Add learning</button>
      )}
    </div>
  )
}

function TemplateSave({ job, templates, onTemplatesChanged }) {
  const [targetId, setTargetId] = useState('')  // '' = save as new
  const [msg, setMsg] = useState('')

  const handleSave = async () => {
    const portable = {}
    const props = job.properties || {}
    for (const k of PORTABLE_PROPS) {
      if (props[k] !== undefined) portable[k] = props[k]
    }
    try {
      if (targetId) {
        await updateTemplate(Number(targetId), job.name, portable)
        setMsg('Template updated')
      } else {
        await saveTemplate(job.name, portable)
        setMsg('Template saved')
      }
      if (onTemplatesChanged) onTemplatesChanged()
    } catch (e) {
      setMsg(`Error: ${e.message}`)
    }
    setTimeout(() => setMsg(''), 2000)
  }

  return (
    <div className="task-section">
      <h3>Template</h3>
      <div className="action-row">
        <select value={targetId} onChange={e => setTargetId(e.target.value)}>
          <option value="">Save as new template</option>
          {templates.map(t => (
            <option key={t.id} value={t.id}>Overwrite: {t.name}</option>
          ))}
        </select>
        <button className="small" onClick={handleSave}>Save</button>
        {msg && <span className={msg.startsWith('Error') ? 'error-text' : 'success-text'}>{msg}</span>}
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
