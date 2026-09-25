/**
 * One typed answer control for one portal question.
 *
 * The queue surfaces (Assisted Apply, the User Input Needed queue) used to
 * render a free-text box for *every* question — a checkbox consent question
 * arrived as "Your answer for this field (never a password or code)", which is
 * both wrong and confusing. The field's own type is the contract here:
 *
 *   checkbox → a tick box ("true"/"false" — an explicit *no* is an answer);
 *   radio    → the portal's own option labels, one dot each;
 *   select   → the portal's own option labels in a picker (never free text);
 *   date     → a date picker (the value format a date input expects);
 *   textarea → a multi-line box;
 *   anything else (text/email/tel/number/url/…) → a matching single-line input.
 *
 * A `select` with no observed options falls back to a Yes/No picker rather
 * than a text box — the only binary guess worth making, and only when the
 * portal revealed no options at all. `password` is never rendered as a value
 * control: secrets are typed in the browser, never here.
 */
export type AnswerFieldSpec = {
  name: string
  label?: string
  type?: string
  required?: boolean
  options?: string[]
}

/** The control kind this field should get. Exported for tests. */
export function controlKind(field: AnswerFieldSpec): 'checkbox' | 'radio' | 'select' | 'date' | 'textarea' | 'text' | 'secret' {
  const type = String(field.type || 'text').toLowerCase().trim()
  const options = (field.options || []).map(o => String(o).trim()).filter(Boolean)
  if (type === 'password') return 'secret'
  if (type === 'checkbox') return 'checkbox'
  if (type === 'radio') return options.length ? 'radio' : 'select'
  if (type === 'select' || type === 'select-one') return 'select'
  if (type === 'date') return 'date'
  if (type === 'textarea') return 'textarea'
  if (options.length && type !== 'text') return 'select'
  return 'text'
}

/** Options for a select control: the portal's own, or the Yes/No fallback. */
export function selectOptions(field: AnswerFieldSpec): string[] {
  const options = (field.options || []).map(o => String(o).trim()).filter(Boolean)
  return options.length ? options : ['Yes', 'No']
}

/** The value of a control that has not been touched yet. */
export function initialAnswerValue(field: AnswerFieldSpec): string {
  return controlKind(field) === 'checkbox' ? 'false' : ''
}

/**
 * Build the answers payload for `complete`. Checkboxes always carry their
 * state ("true"/"false" — unticked is the answer "no", not an absence); every
 * other control contributes only a non-empty value. Fields whose control was
 * never shown are not in the payload at all — the API refuses secret-looking
 * keys (`422 restricted_field`), and a handoff step closes with a note only.
 */
export function collectAnswers(
  fields: AnswerFieldSpec[],
  values: Record<string, string>,
): Record<string, string> {
  const out: Record<string, string> = {}
  for (const field of fields || []) {
    const kind = controlKind(field)
    if (kind === 'secret') continue // passwords/codes are typed in the browser, never here
    const value = values[field.name]
    if (kind === 'checkbox') {
      if (value === 'true' || value === 'false') out[field.name] = value
      continue
    }
    const text = String(value ?? '').trim()
    if (text) out[field.name] = text
  }
  return out
}

type Props = {
  field: AnswerFieldSpec
  value: string
  onChange: (value: string) => void
  /** Accessible name override (the queue item's own label scheme). */
  ariaLabel?: string
  className?: string
  disabled?: boolean
}

const BASE_INPUT = 'w-full border rounded-xl px-3 py-2 min-h-[40px] text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700 focus:outline-none focus:ring-2 focus:ring-blue-500'

export default function AnswerField({ field, value, onChange, ariaLabel, className, disabled }: Props) {
  const kind = controlKind(field)
  const label = field.label || field.name
  const requiredMark = field.required ? ' *' : ''

  if (kind === 'secret') {
    // A password/code/captcha control is never answered here — the browser
    // handoff is the only safe surface. This branch exists so a mis-routed
    // action degrades to advice instead of a text box that would 422.
    return (
      <div className="text-[11px] mono text-amber-700 dark:text-amber-300" data-testid="answer-secret-refused">
        {label}: enter this in the browser window — it is never typed here.
      </div>
    )
  }

  if (kind === 'checkbox') {
    return (
      <label className={`flex items-start gap-2 text-sm ${className || ''}`} data-testid={`answer-${field.name}`}>
        <input
          type="checkbox"
          className="mt-0.5"
          checked={value === 'true'}
          disabled={disabled}
          onChange={e => onChange(e.target.checked ? 'true' : 'false')}
          aria-label={ariaLabel || `${label}${requiredMark}`}
        />
        <span>{label}{requiredMark}</span>
      </label>
    )
  }

  if (kind === 'radio') {
    const options = selectOptions(field)
    return (
      <fieldset className={`space-y-1 ${className || ''}`} data-testid={`answer-${field.name}`}>
        <legend className="text-xs mono">{label}{requiredMark}</legend>
        {options.map(option => (
          <label key={option} className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name={`${field.name}-choice`}
              checked={value === option}
              disabled={disabled}
              onChange={() => onChange(option)}
              aria-label={option}
            />
            <span>{option}</span>
          </label>
        ))}
      </fieldset>
    )
  }

  if (kind === 'select') {
    const options = selectOptions(field)
    return (
      <div className={className} data-testid={`answer-${field.name}`}>
        <label className="text-xs mono">{label}{requiredMark}</label>
        <select
          className={`${BASE_INPUT} mt-1`}
          value={value}
          disabled={disabled}
          onChange={e => onChange(e.target.value)}
          aria-label={ariaLabel || label}
        >
          <option value="">Choose…</option>
          {options.map(option => <option key={option} value={option}>{option}</option>)}
        </select>
      </div>
    )
  }

  if (kind === 'textarea') {
    return (
      <div className={className} data-testid={`answer-${field.name}`}>
        <label className="text-xs mono">{label}{requiredMark}</label>
        <textarea
          className={`${BASE_INPUT} mt-1`}
          rows={3}
          value={value}
          disabled={disabled}
          onChange={e => onChange(e.target.value)}
          aria-label={ariaLabel || label}
          placeholder={label}
        />
      </div>
    )
  }

  const inputType = kind === 'date' ? 'date' : 'text'
  return (
    <input
      type={inputType}
      data-testid={`answer-${field.name}`}
      className={className || BASE_INPUT}
      value={value}
      disabled={disabled}
      onChange={e => onChange(e.target.value)}
      aria-label={ariaLabel || `Answer for ${label}`}
      placeholder={`${label}${requiredMark} (never a password or code)`}
    />
  )
}
