import { useState, useRef } from 'react'

/** Contextual help tooltip — attaches to a ? indicator adjacent to field labels. */
export default function HelpTip({ text, linkUrl, linkLabel }) {
  const [pos, setPos] = useState(null)
  const ref = useRef(null)
  const hideTimer = useRef(null)

  const cancelHide = () => {
    if (hideTimer.current) { clearTimeout(hideTimer.current); hideTimer.current = null }
  }

  const show = () => {
    cancelHide()
    if (ref.current) {
      const r = ref.current.getBoundingClientRect()
      setPos({ top: r.top, centerX: r.left + r.width / 2 })
    }
  }

  const scheduleHide = () => {
    cancelHide()
    hideTimer.current = setTimeout(() => setPos(null), 200)
  }

  return (
    <span
      ref={ref}
      className="help-tip"
      tabIndex={0}
      role="note"
      aria-label="Help"
      onMouseEnter={show}
      onFocus={show}
      onMouseLeave={scheduleHide}
      onBlur={scheduleHide}
    >
      ?
      {pos && (
        <span
          className="help-tip-content"
          style={{ top: pos.top - 6, left: pos.centerX }}
          onMouseEnter={cancelHide}
          onMouseLeave={scheduleHide}
        >
          {text}
          {linkUrl && (
            <>
              {' '}
              <a
                href={linkUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="help-tip-link"
                onClick={e => e.stopPropagation()}
              >
                {linkLabel || linkUrl}
              </a>
            </>
          )}
        </span>
      )}
    </span>
  )
}
