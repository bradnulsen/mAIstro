import { useState, useEffect, useLayoutEffect, useRef, useCallback } from 'react'
import Markdown from 'react-markdown'
import {
  sendChatMessage, getChatSessions, getChatMessages, deleteChatSession,
  getChatSessionStatus,
} from '../api'
import { mdBreaks, formatDate } from '../util'

export default function Chat() {
  const [sessions, setSessions] = useState([])
  const [activeSession, setActiveSession] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [streaming, setStreaming] = useState('')
  const [thinking, setThinking] = useState('')
  const [toolStatus, setToolStatus] = useState(null)
  const messagesEnd = useRef(null)
  const inputRef = useRef(null)
  const abortRef = useRef(null)
  // Track session ID across async operations (avoids stale closure issues)
  const activeSessionRef = useRef(null)
  // Cancel signal for poll loops — set to true to stop any active poll
  const pollCancelledRef = useRef(false)

  const refreshSessions = useCallback(async () => {
    try {
      const s = await getChatSessions()
      setSessions(s)
      return s
    } catch {}
    return []
  }, [])

  useEffect(() => { refreshSessions() }, [refreshSessions])

  // Auto-scroll
  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streaming, thinking, toolStatus])

  // Reset textarea height when input is cleared programmatically (e.g. after send)
  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 150) + 'px'
  }, [input])

  // On mount: check if the most recent session is still processing (e.g. we navigated away)
  useEffect(() => {
    pollCancelledRef.current = false
    ;(async () => {
      const sessions = await refreshSessions()
      if (pollCancelledRef.current || sessions.length === 0) return
      const latest = sessions[0]
      try {
        const { processing } = await getChatSessionStatus(latest.id)
        if (pollCancelledRef.current) return
        if (processing) {
          // Session is still being processed by backend — load messages and show indicator
          setActiveSession(latest)
          activeSessionRef.current = latest
          const msgs = await getChatMessages(latest.id)
          if (!pollCancelledRef.current) {
            setMessages(msgs)
            setToolStatus('processing...')
            setSending(true)
            _pollForCompletion(latest.id)
          }
        }
      } catch {}
    })()
    return () => { pollCancelledRef.current = true }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Poll for completion of an active background task
  const _pollForCompletion = useCallback(async (sessionId) => {
    pollCancelledRef.current = false
    const poll = async () => {
      if (pollCancelledRef.current) return
      try {
        const { processing } = await getChatSessionStatus(sessionId)
        if (pollCancelledRef.current) return
        if (!processing) {
          // Done — reload messages from DB
          const msgs = await getChatMessages(sessionId)
          setMessages(msgs)
          setStreaming('')
          setToolStatus(null)
          setSending(false)
          refreshSessions()
          return
        }
        // Still processing — reload messages (might have partial saves) and keep polling
        setTimeout(poll, 2000)
      } catch {
        if (!pollCancelledRef.current) setTimeout(poll, 3000)
      }
    }
    setTimeout(poll, 2000)
  }, [refreshSessions])

  const loadSession = async (session) => {
    // Cancel any in-progress stream or poll
    if (abortRef.current) {
      abortRef.current()
      abortRef.current = null
    }
    pollCancelledRef.current = true
    setActiveSession(session)
    activeSessionRef.current = session
    setStreaming('')
    setToolStatus(null)
    setSending(false)
    try {
      const msgs = await getChatMessages(session.id)
      setMessages(msgs)
      // Check if this session is still processing
      const { processing } = await getChatSessionStatus(session.id)
      if (processing) {
        setToolStatus('processing...')
        setSending(true)
        _pollForCompletion(session.id)
      }
    } catch {
      setMessages([])
    }
  }

  const handleSend = async () => {
    if (!input.trim()) return
    const msg = input.trim()
    setInput('')
    setSending(true)
    setStreaming('')
    setThinking('')
    setToolStatus(null)

    // Add user message to UI immediately
    setMessages(prev => [...prev, { role: 'user', content: msg, created_at: new Date().toISOString() }])

    let newSessionId = null
    let streamingText = ''

    try {
      let thinkingAccum = ''
      const { abort, done } = sendChatMessage(msg, activeSession?.id, null, (event) => {
        if (event.type === 'thinking') {
          thinkingAccum += event.content || ''
          setThinking(thinkingAccum)
          setToolStatus(null)
        } else if (event.type === 'text') {
          if (thinkingAccum) {
            thinkingAccum = ''
            setThinking('')
          }
          streamingText += event.content || ''
          setStreaming(streamingText)
          setToolStatus(null)
        } else if (event.type === 'tool_use') {
          if (thinkingAccum) {
            thinkingAccum = ''
            setThinking('')
          }
          setToolStatus(event.tool || 'working...')
        } else if (event.session_id) {
          newSessionId = event.session_id
        }
      })
      abortRef.current = abort
      await done
    } catch (e) {
      if (e.name === 'AbortError') return
    }

    // Always reload from DB — single source of truth
    const sid = newSessionId || activeSession?.id
    if (sid) {
      try {
        const msgs = await getChatMessages(sid)
        setMessages(msgs)
      } catch {}
    }
    setStreaming('')
    setThinking('')
    setToolStatus(null)
    setSending(false)
    abortRef.current = null

    // Refresh sessions and switch to new one if created
    const updated = await getChatSessions().catch(() => [])
    setSessions(updated)
    if (newSessionId && !activeSessionRef.current) {
      const found = updated.find(s => s.id === newSessionId)
      if (found) {
        setActiveSession(found)
        activeSessionRef.current = found
      }
    }
  }

  const handleDeleteSession = async (sessionId) => {
    try {
      await deleteChatSession(sessionId)
      if (activeSession?.id === sessionId) {
        setActiveSession(null)
        activeSessionRef.current = null
        setMessages([])
      }
      await refreshSessions()
    } catch {}
  }

  const handleNewChat = () => {
    if (abortRef.current) {
      abortRef.current()
      abortRef.current = null
    }
    pollCancelledRef.current = true
    setActiveSession(null)
    activeSessionRef.current = null
    setMessages([])
    setStreaming('')
    setThinking('')
    setToolStatus(null)
    setSending(false)
  }

  return (
    <div className="chat-layout">
      {/* Session list */}
      <div className="chat-sidebar">
        <div style={{ padding: 6, display: 'flex', gap: 4 }}>
          <button className="small" style={{ flex: 1 }} onClick={handleNewChat}>+ New</button>
        </div>
        {sessions.length > 0 && (
          <div className="scroll-area">
            {sessions.map(s => (
              <div
                key={s.id}
                className={`chat-session-item ${activeSession?.id === s.id ? 'active' : ''}`}
                onClick={() => loadSession(s)}
              >
                <div className="chat-session-title">{s.title || 'Untitled'}</div>
                <div className="chat-session-meta">
                  <span>{formatDate(s.created_at, true)}</span>
                  <button
                    className="chat-session-delete"
                    onClick={(e) => { e.stopPropagation(); handleDeleteSession(s.id) }}
                    title="Delete session"
                  >✕</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Chat messages */}
      <div className="chat-main">
        <div className="chat-messages">
          {messages.length === 0 && !streaming && !toolStatus && (
            <div className="empty-state">
              Ask mAistro about your project, tasks, or dispatches
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`chat-message ${m.role}`}>
              <div className={`bubble${m.role === 'assistant' ? ' md-content' : ''}`}>
                {m.role === 'assistant' ? <Markdown>{mdBreaks(m.content)}</Markdown> : m.content}
              </div>
            </div>
          ))}
          {thinking && (
            <div className="chat-message assistant">
              <div className="bubble thinking-bubble">
                <span className="thinking-label">reasoning</span>
                <div className="thinking-content">{thinking}</div>
              </div>
            </div>
          )}
          {streaming && (
            <div className="chat-message assistant">
              <div className="bubble md-content">
                <Markdown>{mdBreaks(streaming)}</Markdown>
                <span className="streaming-cursor">▌</span>
              </div>
            </div>
          )}
          <div ref={messagesEnd} />
        </div>

        {sending && (
          <div className="chat-status-strip">
            <span className="tool-spinner" />
            {toolStatus
              ? toolStatus.endsWith('...') ? toolStatus : `${toolStatus}...`
              : 'thinking...'}
          </div>
        )}

        <div className="chat-input-bar">
          <textarea
            ref={inputRef}
            placeholder="Ask mAistro..."
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                handleSend()
              }
            }}
            disabled={sending}
            rows={1}
          />
          <button className="primary" onClick={handleSend} disabled={sending || !input.trim()}>
            {sending ? <span className="tool-spinner" /> : 'Send'}
          </button>
        </div>
      </div>
    </div>
  )
}

