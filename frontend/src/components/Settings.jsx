import { useState, useEffect, useCallback } from 'react'
import { getProject, getConfig, setConfig, getQueueSettings, setQueueSettings } from '../api'
import HelpTip from './HelpTip'

const TIPS = {
  autoDispatch: 'When enabled, new tasks skip the Pending column and go directly to Queued — the worker processes them automatically. When disabled, new tasks land in Pending for review. Drag tasks between columns to promote or demote them.',
  model: 'Opus: highest capability, slowest, most expensive. Sonnet: balanced capability and speed. Haiku: fastest, cheapest, best for simple or high-frequency tasks.',
  timeout: 'Maximum execution time in seconds applied to tasks that have no task-level override. Set to 0 or leave blank for no limit.',
}

export default function Settings() {
  const [project, setProject] = useState(null)
  const [config, setConfigState] = useState({})
  const [queueSettings, setQueueSettingsState] = useState({ auto_dispatch: false })
  const [loading, setLoading] = useState(true)
  const [saveMsg, setSaveMsg] = useState('')

  // Editable state
  const [defaultModel, setDefaultModel] = useState('sonnet')
  const [defaultTimeout, setDefaultTimeout] = useState('')

  const load = useCallback(async () => {
    try {
      const [p, cfg, qs] = await Promise.all([
        getProject(), getConfig(), getQueueSettings(),
      ])
      setProject(p)
      setConfigState(cfg)
      setQueueSettingsState(qs)
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

  if (loading) return <div className="loading">Loading settings...</div>

  return (
    <div className="settings-view">
      <div className="header-bar">
        <h1>Settings</h1>
        <div className="spacer" />
        {saveMsg && <span className="success-text">{saveMsg}</span>}
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
            <div className="label-row">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={queueSettings.auto_dispatch}
                  onChange={e => handleAutoDispatch(e.target.checked)}
                />
                Auto-queue new tasks
              </label>
              <HelpTip text={TIPS.autoDispatch} />
            </div>
          </div>
        </div>

        {/* Default Model */}
        <div className="settings-section">
          <h3>Default Model</h3>
          <div className="settings-field settings-save-row">
            <div>
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
          <div className="settings-field settings-save-row">
            <div>
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

      </div>
    </div>
  )
}
