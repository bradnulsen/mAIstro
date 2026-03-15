/** Contextual help tooltip — attaches to a ? indicator adjacent to field labels. */
export default function HelpTip({ text }) {
  return (
    <span className="help-tip" tabIndex={0} role="note" aria-label="Help">
      ?
      <span className="help-tip-content">{text}</span>
    </span>
  )
}
