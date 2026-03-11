import React, { useState, useEffect, useRef, useCallback } from 'react'
import {
  sendChatMessage, getChatSessions, getChatMessages, deleteChatSession,
} from '../api'

export default function Chat({ agents }) {
  const [sessions, setSessions] = useState([])
  const [activeSession, setActiveSession] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [streaming, setStreaming] = useState('')
  const [selectedAgent, setSelectedAgent] = useState(agents[0]?.id || '')
  const messagesEnd = useRef(null)

  const refreshSessions = useCallback(async () => {
    try {
      const s = await getChatSessions()
      setSessions(s)
    } catch {}
  }, [])

  useEffect(() => { refreshSessions() }, [refreshSessions])

  // Auto-scroll
  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streaming])

  const loadSession = async (session) => {
    setActiveSession(session)
    setSelectedAgent(session.agent_id)
    try {
      const msgs = await getChatMessages(session.id)
      setMessages(msgs)
    } catch {
      setMessages([])
    }
  }

  const handleSend = async () => {
    if (!input.trim() || !selectedAgent) return
    const msg = input.trim()
    setInput('')
    setSending(true)
    setStreaming('')

    // Add user message to UI immediately
    setMessages(prev => [...prev, { role: 'user', content: msg, created_at: new Date().toISOString() }])

    let fullResponse = ''
    let newSessionId = activeSession?.id || null

    try {
      await sendChatMessage(selectedAgent, msg, activeSession?.id, null, (event) => {
        if (event.type === 'text') {
          fullResponse += event.content || ''
          setStreaming(fullResponse)
        } else if (event.session_id) {
          newSessionId = event.session_id
        }
      })
    } catch (e) {
      fullResponse = `Error: ${e.message}`
    }

    // Add assistant message
    if (fullResponse) {
      setMessages(prev => [...prev, { role: 'assistant', content: fullResponse, created_at: new Date().toISOString() }])
    }
    setStreaming('')
    setSending(false)

    // Refresh sessions to pick up new session
    await refreshSessions()
    if (newSessionId && !activeSession) {
      const s = sessions.find(s => s.id === newSessionId)
      if (s) setActiveSession(s)
    }
  }

  const handleDeleteSession = async (sessionId) => {
    try {
      await deleteChatSession(sessionId)
      if (activeSession?.id === sessionId) {
        setActiveSession(null)
        setMessages([])
      }
      await refreshSessions()
    } catch {}
  }

  const handleNewChat = () => {
    setActiveSession(null)
    setMessages([])
    setStreaming('')
  }

  return (
    <>
      <div className="header-bar">
        <h1>Chat</h1>
        <div className="spacer" />
        <select
          value={selectedAgent}
          onChange={e => setSelectedAgent(e.target.value)}
          style={{ width: 150 }}
        >
          {agents.map(a => (
            <option key={a.id} value={a.id}>{a.name}</option>
          ))}
        </select>
      </div>

      <div className="chat-layout">
        {/* Session sidebar */}
        <div className="chat-sidebar">
          <div style={{ padding: 8 }}>
            <button className="small" style={{ width: '100%' }} onClick={handleNewChat}>
              + New Chat
            </button>
          </div>
          <div className="scroll-area">
            {sessions.map(s => (
              <div
                key={s.id}
                className={`chat-session-item ${activeSession?.id === s.id ? 'active' : ''}`}
                onClick={() => loadSession(s)}
              >
                <div className="chat-session-title">{s.title || 'Untitled'}</div>
                <div className="chat-session-meta">
                  {agents.find(a => a.id === s.agent_id)?.name || s.agent_id}
                </div>
              </div>
            ))}
            {sessions.length === 0 && (
              <div style={{ padding: 12, fontSize: 11, color: '#888' }}>
                No chat sessions yet
              </div>
            )}
          </div>
        </div>

        {/* Chat messages */}
        <div className="chat-main">
          <div className="chat-messages">
            {messages.length === 0 && !streaming && (
              <div className="empty-state">
                Start a conversation with an agent
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`chat-message ${m.role}`}>
                <div className="bubble">{m.content}</div>
              </div>
            ))}
            {streaming && (
              <div className="chat-message assistant">
                <div className="bubble">{streaming}<span style={{ opacity: 0.5 }}>▌</span></div>
              </div>
            )}
            <div ref={messagesEnd} />
          </div>

          <div className="chat-input-bar">
            <input
              type="text"
              placeholder={`Message ${agents.find(a => a.id === selectedAgent)?.name || 'agent'}...`}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && !e.shiftKey && handleSend()}
              disabled={sending}
            />
            <button className="primary" onClick={handleSend} disabled={sending || !input.trim()}>
              {sending ? '...' : 'Send'}
            </button>
          </div>
        </div>
      </div>
    </>
  )
}
