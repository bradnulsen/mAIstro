import { useState, useEffect, useCallback } from 'react'
import Markdown from 'react-markdown'
import SyntaxHighlighter from 'react-syntax-highlighter/dist/esm/prism-light'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import javascript from 'react-syntax-highlighter/dist/esm/languages/prism/javascript'
import jsx from 'react-syntax-highlighter/dist/esm/languages/prism/jsx'
import typescript from 'react-syntax-highlighter/dist/esm/languages/prism/typescript'
import tsx from 'react-syntax-highlighter/dist/esm/languages/prism/tsx'
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python'
import ruby from 'react-syntax-highlighter/dist/esm/languages/prism/ruby'
import go from 'react-syntax-highlighter/dist/esm/languages/prism/go'
import rust from 'react-syntax-highlighter/dist/esm/languages/prism/rust'
import java from 'react-syntax-highlighter/dist/esm/languages/prism/java'
import c from 'react-syntax-highlighter/dist/esm/languages/prism/c'
import cpp from 'react-syntax-highlighter/dist/esm/languages/prism/cpp'
import csharp from 'react-syntax-highlighter/dist/esm/languages/prism/csharp'
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash'
import yaml from 'react-syntax-highlighter/dist/esm/languages/prism/yaml'
import toml from 'react-syntax-highlighter/dist/esm/languages/prism/toml'
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json'
import xml from 'react-syntax-highlighter/dist/esm/languages/prism/xml-doc'
import css from 'react-syntax-highlighter/dist/esm/languages/prism/css'
import scss from 'react-syntax-highlighter/dist/esm/languages/prism/scss'
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql'
import graphql from 'react-syntax-highlighter/dist/esm/languages/prism/graphql'
import docker from 'react-syntax-highlighter/dist/esm/languages/prism/docker'
import { searchFiles, readFile } from '../api'

SyntaxHighlighter.registerLanguage('javascript', javascript)
SyntaxHighlighter.registerLanguage('jsx', jsx)
SyntaxHighlighter.registerLanguage('typescript', typescript)
SyntaxHighlighter.registerLanguage('tsx', tsx)
SyntaxHighlighter.registerLanguage('python', python)
SyntaxHighlighter.registerLanguage('ruby', ruby)
SyntaxHighlighter.registerLanguage('go', go)
SyntaxHighlighter.registerLanguage('rust', rust)
SyntaxHighlighter.registerLanguage('java', java)
SyntaxHighlighter.registerLanguage('c', c)
SyntaxHighlighter.registerLanguage('cpp', cpp)
SyntaxHighlighter.registerLanguage('csharp', csharp)
SyntaxHighlighter.registerLanguage('bash', bash)
SyntaxHighlighter.registerLanguage('yaml', yaml)
SyntaxHighlighter.registerLanguage('toml', toml)
SyntaxHighlighter.registerLanguage('json', json)
SyntaxHighlighter.registerLanguage('xml', xml)
SyntaxHighlighter.registerLanguage('css', css)
SyntaxHighlighter.registerLanguage('scss', scss)
SyntaxHighlighter.registerLanguage('sql', sql)
SyntaxHighlighter.registerLanguage('graphql', graphql)
SyntaxHighlighter.registerLanguage('dockerfile', docker)


const MD_EXTENSIONS = new Set(['md', 'mdx', 'markdown'])
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'])

const EXT_TO_LANG = {
  js: 'javascript', jsx: 'jsx', ts: 'typescript', tsx: 'tsx',
  py: 'python', rb: 'ruby', go: 'go', rs: 'rust', java: 'java',
  c: 'c', cpp: 'cpp', cs: 'csharp',
  sh: 'bash', bash: 'bash', zsh: 'bash',
  yaml: 'yaml', yml: 'yaml', toml: 'toml', json: 'json', xml: 'xml',
  html: 'xml', css: 'css', scss: 'scss', sql: 'sql', graphql: 'graphql',
}

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

function detectLanguage(path) {
  const filename = path.split('/').pop().toLowerCase()
  if (filename === 'dockerfile') return 'dockerfile'
  return EXT_TO_LANG[fileExtension(path)] || null
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

        {selected && content !== null && !isMarkdown(selected.path) && (() => {
          const lang = detectLanguage(selected.path)
          return lang ? (
            <div className="files-content-body files-content-body--code">
              <SyntaxHighlighter
                language={lang}
                style={oneDark}
                customStyle={{ margin: 0, fontSize: 11, borderRadius: 0, background: 'var(--code-bg)' }}
                codeTagProps={{ style: { fontFamily: 'var(--font)' } }}
              >
                {content}
              </SyntaxHighlighter>
            </div>
          ) : (
            <div className="files-content-body">
              <pre className="files-code">{content}</pre>
            </div>
          )
        })()}
      </div>
    </div>
  )
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`
}
