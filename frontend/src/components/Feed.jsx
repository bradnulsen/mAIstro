import { useState, useEffect, useCallback } from 'react'
import { getFeed, getGitDiff } from '../api'
import { formatDate, TRIGGER_ICONS } from '../util'

export default function Feed() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [diff, setDiff] = useState('')
  const [loadingDiff, setLoadingDiff] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const feed = await getFeed({ limit: 100 })
      setItems(feed)
    } catch {}
    setLoading(false)
  }, [])

  useEffect(() => { refresh() }, [refresh])

  // Auto-refresh every 5s
  useEffect(() => {
    const interval = setInterval(refresh, 5000)
    return () => clearInterval(interval)
  }, [refresh])

  const selectItem = async (item) => {
    setSelected(item)
    setDiff('')
    setLoadingDiff(true)
    try {
      const d = await getGitDiff(item.hash)
      setDiff(d.diff || '')
    } catch {
      setDiff('')
    }
    setLoadingDiff(false)
  }

  return (
    <>
      <div className="header-bar">
        <h1>Activity Feed</h1>
        <div className="spacer" />
        <button className="small" onClick={refresh}>↻</button>
      </div>

      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {/* Feed list */}
        <div className="feed-list" style={{ flex: 1 }}>
          {loading && <div className="loading">Loading feed...</div>}
          {!loading && items.length === 0 && (
            <div className="empty-state">No commits yet. Create a task and run it.</div>
          )}
          {items.map(item => (
            <div
              key={item.hash}
              className={`feed-item ${selected?.hash === item.hash ? 'active' : ''}`}
              onClick={() => selectItem(item)}
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
              </div>
              {(item.files?.length > 0 || item.insertions > 0 || item.deletions > 0) && (
                <div className="feed-stats">
                  <span className="feed-stat-files">{item.files?.length || 0} {item.files?.length === 1 ? 'file' : 'files'}</span>
                  {(item.insertions > 0 || item.deletions > 0) && (
                    <span className="feed-stat-lines">
                      {item.insertions > 0 && <span className="feed-stat-add">+{item.insertions}</span>}
                      {item.deletions > 0 && <span className="feed-stat-del">-{item.deletions}</span>}
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>

        {/* Detail panel */}
        {selected && (
          <div className="detail-panel">
            <div className="detail-panel-header">
              <h3>Commit Detail</h3>
              <button className="small" onClick={() => setSelected(null)}>✕</button>
            </div>
            <div style={{ fontSize: 11, color: '#888', marginBottom: 4, flexShrink: 0 }}>
              {selected.hash?.slice(0, 8)} by {selected.author}
            </div>
            <div style={{ fontSize: 12, marginBottom: 12, fontWeight: 'bold', flexShrink: 0 }}>
              {selected.message}
            </div>
            {selected.files && (
              <div style={{ marginBottom: 12, flexShrink: 0 }}>
                <label>Changed Files</label>
                {selected.files.map(f => (
                  <div key={f} style={{ fontSize: 11, padding: '1px 0' }}>{f}</div>
                ))}
              </div>
            )}
            {loadingDiff && (
              <div style={{ fontSize: 11, color: '#888', padding: '8px 0' }}>Loading diff...</div>
            )}
            {diff && (
              <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
                <label style={{ flexShrink: 0 }}>Diff</label>
                <pre className="diff-view">
                  {diff.split('\n').map((line, i) => (
                    <div key={i} className={
                      line.startsWith('+') ? 'diff-add' :
                      line.startsWith('-') ? 'diff-del' :
                      line.startsWith('@@') ? 'diff-hunk' : ''
                    }>{line}</div>
                  ))}
                </pre>
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}

