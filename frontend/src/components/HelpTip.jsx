import { useState, useRef } from 'react'

/** Contextual help tooltip — attaches to a ? indicator adjacent to field labels. */
export default function HelpTip({ text }) {
  const [pos, setPos] = useState(null)
  const ref = useRef(null)

  const show = () => {
    if (ref.current) {
      const r = ref.current.getBoundingClientRect()
      setPos({ top: r.top, centerX: r.left + r.width / 2 })
    }
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
      onMouseLeave={() => setPos(null)}
      onBlur={() => setPos(null)}
    >
      ?
      {pos && (
        <span
          className="help-tip-content"
          style={{ top: pos.top - 6, left: pos.centerX }}
        >
          {text}
        </span>
      )}
    </span>
  )
}
