import { useState, useEffect, useCallback } from 'react'
import Markdown from 'react-markdown'
import { searchFiles, readFile } from '../api'

const MD_EXTENSIONS = new Set(['md', 'mdx', 'markdown'])
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'])

function fileExtension(path) {
  const dot = path.lastIndexOf('.')
  return dot >= 0 ? path.slice(dot + 1).toLowerCase() : ''
}

function isMarkdown(path) {
  return MD_EXTENSIONS.has(fileExtension(path))
}

function isImage(path) {
  return IMAGE_EXTENSIONS.has(fileExtension(path))
}

export default function Files() {
  const [pattern, setPattern] = useState('**/*')
  const [inputPattern, setInputPattern] = useState('**/*')
  const [files, setFiles] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)       // { path, size }
  const [content, setContent] = useState(null)
  const [contentLoading, setContentLoading] = useState(false)
  const [contentError, setContentError] = useState(null)

  const runSearch = useCallback(async (pat) => {
    setLoading(true)
    setError(null)
    try {
      const results = await searchFiles(pat)
      setFiles(results)
    } catch (e) {
      setError(e.message)
      setFiles([])
    } finally {
      setLoading(false)
    }
  }, [])

  // Load file list on mount
  useEffect(() => {
    runSearch(pattern)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const handleSearch = (e) => {
    e.preventDefault()
    setPattern(inputPattern)
    setSelected(null)
    setContent(null)
    runSearch(inputPattern)
  }

  const handleSelectFile = async (file) => {
    if (isImage(file.path)) return  // images not viewable inline

    setSelected(file)
    setContent(null)
    setContentError(null)
    setContentLoading(true)
    try {
      const data = await readFile(file.path)
      setContent(data.content)
    } catch (e) {
      setContentError(e.message)
    } finally {
      setContentLoading(false)
    }
  }

  return (
    <div className="files-view">
      <div className="files-sidebar">
        <form className="files-search" onSubmit={handleSearch}>
          <input
            type="text"
            value={inputPattern}
            onChange={e => setInputPattern(e.target.value)}
            placeholder="glob pattern e.g. src/**/*.js"
            className="files-pattern-input"
          />
          <button type="submit" disabled={loading}>
            {loading ? '...' : 'Search'}
          </button>
        </form>

        {error && <div className="files-error">{error}</div>}

        <div className="files-list">
          {files.length === 0 && !loading && !error && (
            <div className="files-empty">No files matched</div>
          )}
          {files.map(f => (
            <div
              key={f.path}
              className={`files-item ${selected?.path === f.path ? 'active' : ''} ${isImage(f.path) ? 'no-preview' : ''}`}
              onClick={() => handleSelectFile(f)}
              title={f.path}
            >
              <span className="files-item-path">{f.path}</span>
              <span className="files-item-size">{formatSize(f.size)}</span>
            </div>
          ))}
        </div>

        {files.length > 0 && (
          <div className="files-count">{files.length} file{files.length !== 1 ? 's' : ''}</div>
        )}
      </div>

      <div className="files-content">
        {!selected && (
          <div className="files-content-empty">Select a file to view its contents</div>
        )}

        {selected && contentLoading && (
          <div className="files-content-empty">Loading...</div>
        )}

        {selected && contentError && (
          <div className="files-error">{contentError}</div>
        )}

        {selected && content !== null && (
          <div className="files-content-header">
            <span className="files-content-path">{selected.path}</span>
            <span className="files-content-meta">{formatSize(selected.size)}</span>
          </div>
        )}

        {selected && content !== null && isMarkdown(selected.path) && (
          <div className="files-content-body md-content">
            <Markdown>{content}</Markdown>
          </div>
        )}

        {selected && content !== null && !isMarkdown(selected.path) && (
          <div className="files-content-body">
            <pre className="files-code">{content}</pre>
          </div>
        )}
      </div>
    </div>
  )
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`
}
