import { useState, useRef, useEffect } from 'react'
import { sendMessage } from '../api/client'
import ReactMarkdown from 'react-markdown'
import ResultsVisualization from './ResultsVisualization'

function ChatPanel({ bbox, onResponse }) {
  const [messages, setMessages] = useState([
    {
      role: 'system',
      content: 'Задайте вопрос о пожарах. Например: "Покажи пожары в Сибири за последние 3 дня"',
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const messagesEndRef = useRef(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSend = async () => {
    const text = input.trim()
    if (!text || loading) return

    setMessages((prev) => [...prev, { role: 'user', content: text }])
    setInput('')
    setLoading(true)

    try {
      const result = await sendMessage(text, bbox)

      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: result.response, actions: result.actions },
      ])

      // Обработка map_data от execute_python
      if (result.map_data) {
        try {
          const mapDataList = result.map_data_list || []
          mapDataList.push(result.map_data)
          onResponse?.(mapDataList)
        } catch (error) {
          console.error('Failed to process map_data:', error)
        }
      } else if (result.map_data_list && result.map_data_list.length > 0) {
        // Передаём все результаты с типами
        onResponse?.(result.map_data_list)
      }
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: `Ошибка: ${err.message}` },
      ])
    } finally {
      setLoading(false)
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="chat-panel">
      <div className="chat-messages">
        {messages.map((msg, i) => (
          <div key={i} className={`chat-message ${msg.role}`}>
            {msg.role === 'assistant' ? (
              <>
                <ReactMarkdown
                  components={{
                    table: ({node, ...props}) => (
                      <div style={{overflowX: 'auto'}}>
                        <table style={{borderCollapse: 'collapse', width: '100%', margin: '8px 0'}} {...props} />
                      </div>
                    ),
                    th: ({node, ...props}) => (
                      <th style={{border: '1px solid #444', padding: '6px 12px', backgroundColor: '#1a1a2e', textAlign: 'left'}} {...props} />
                    ),
                    td: ({node, ...props}) => (
                      <td style={{border: '1px solid #444', padding: '6px 12px'}} {...props} />
                    ),
                    h1: ({node, ...props}) => <h1 style={{fontSize: '18px', fontWeight: 'bold', margin: '12px 0 8px 0', color: '#e94560'}} {...props} />,
                    h2: ({node, ...props}) => <h2 style={{fontSize: '16px', fontWeight: 'bold', margin: '10px 0 6px 0', color: '#ff8c42'}} {...props} />,
                    h3: ({node, ...props}) => <h3 style={{fontSize: '14px', fontWeight: 'bold', margin: '8px 0 4px 0', color: '#ffd166'}} {...props} />,
                    ul: ({node, ...props}) => <ul style={{margin: '8px 0', paddingLeft: '20px'}} {...props} />,
                    ol: ({node, ...props}) => <ol style={{margin: '8px 0', paddingLeft: '20px'}} {...props} />,
                    li: ({node, ...props}) => <li style={{margin: '4px 0'}} {...props} />,
                    code: ({node, inline, ...props}) =>
                      inline ?
                        <code style={{backgroundColor: '#1a1a2e', padding: '2px 6px', borderRadius: '3px', fontSize: '13px'}} {...props} /> :
                        <code style={{display: 'block', backgroundColor: '#1a1a2e', padding: '8px', borderRadius: '4px', margin: '8px 0', fontSize: '12px', overflowX: 'auto'}} {...props} />,
                    strong: ({node, ...props}) => <strong style={{fontWeight: 'bold', color: '#fff'}} {...props} />,
                    em: ({node, ...props}) => <em style={{fontStyle: 'italic'}} {...props} />,
                  }}
                >
                  {msg.content}
                </ReactMarkdown>
                {msg.actions && msg.actions.length > 0 && (
                  <ResultsVisualization actions={msg.actions} />
                )}
              </>
            ) : (
              msg.content
            )}
          </div>
        ))}
        {loading && (
          <div className="loading">
            <div className="spinner" />
            Анализирую данные...
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>
      <div className="chat-input-area">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Спросите о пожарах..."
          rows={1}
        />
        <button
          className="btn btn-primary"
          onClick={handleSend}
          disabled={loading || !input.trim()}
        >
          →
        </button>
      </div>
    </div>
  )
}

export default ChatPanel
