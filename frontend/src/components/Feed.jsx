import React, { useState, useEffect, useCallback } from 'react'
import { getFeed, getFeedItem, getGitDiff } from '../api'

const TRIGGER_ICONS = {
  commit: '⚡',
  agent_queue: '↗',
  manual: '→',
  auto: '⏱',
  human: '👤',
  agent: '🤖',
}

export default function Feed({ agents }) {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [diff, setDiff] = useState('')

  const refresh = useCallback(async () => {
    try {
      const feed = await getFeed({ limit: 100 })
      setItems(feed)
    } catch {}
    setLoading(false)
  }, [])

  useEffect(() => { refresh() }, [refresh])

  // Auto-refresh every 10s
  useEffect(() => {
    const interval = setInterval(refresh, 10000)
    return () => clearInterval(interval)
  }, [refresh])

  const selectItem = async (item) => {
    setSelected(item)
    try {
      const d = await getGitDiff(item.hash)
      setDiff(d.diff || '')
    } catch {
      setDiff('')
    }
  }

  return (
    <>
      <div className="header-bar">
        <h1>Activity Feed</h1>
        <div className="spacer" />
        <button className="small" onClick={refresh}>↻ Refresh</button>
      </div>

      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {/* Feed list */}
        <div className="feed-list" style={{ flex: 1 }}>
          {loading && <div className="loading">Loading feed...</div>}
          {!loading && items.length === 0 && (
            <div className="empty-state">No commits yet. Create an agent and run it.</div>
          )}
          {items.map(item => (
            <div
              key={item.hash}
              className={`feed-item ${selected?.hash === item.hash ? 'active' : ''}`}
              onClick={() => selectItem(item)}
              style={selected?.hash === item.hash ? { background: '#eee' } : {}}
            >
              <div className="feed-avatar">
                {(item.author || '?')[0].toUpperCase()}
              </div>
              <div className="feed-body">
                <div className="feed-meta">
                  <span className="feed-author">{item.author}</span>
                  <span className="feed-trigger">
                    {TRIGGER_ICONS[item.trigger] || ''}
                  </span>
                  <span>{formatDate(item.date)}</span>
                </div>
                <div className="feed-message">{item.message}</div>
                {item.files && item.files.length > 0 && (
                  <div className="feed-files">
                    {item.files.slice(0, 3).join(', ')}
                    {item.files.length > 3 && ` +${item.files.length - 3} more`}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Detail panel */}
        {selected && (
          <div style={{
            width: 400, minWidth: 400, borderLeft: '1.5px solid #222',
            overflow: 'auto', padding: 16, background: '#fff',
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
              <h3 style={{ fontSize: 13 }}>Commit Detail</h3>
              <button className="small" onClick={() => setSelected(null)}>✕</button>
            </div>
            <div style={{ fontSize: 11, color: '#888', marginBottom: 4 }}>
              {selected.hash?.slice(0, 8)} by {selected.author}
            </div>
            <div style={{ fontSize: 12, marginBottom: 12, fontWeight: 'bold' }}>
              {selected.message}
            </div>
            {selected.files && (
              <div style={{ marginBottom: 12 }}>
                <label>Changed Files</label>
                {selected.files.map(f => (
                  <div key={f} style={{ fontSize: 11, padding: '1px 0' }}>{f}</div>
                ))}
              </div>
            )}
            {diff && (
              <div>
                <label>Diff</label>
                <pre style={{
                  fontSize: 11, background: '#1a1a1a', color: '#e0e0e0',
                  padding: 8, borderRadius: 4, overflow: 'auto', maxHeight: 400,
                  whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                }}>
                  {diff}
                </pre>
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}

function formatDate(dateStr) {
  if (!dateStr) return ''
  try {
    const d = new Date(dateStr)
    const now = new Date()
    const diff = (now - d) / 1000
    if (diff < 60) return 'just now'
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
    return d.toLocaleDateString()
  } catch {
    return dateStr
  }
}
