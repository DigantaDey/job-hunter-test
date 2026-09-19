import { AlertTriangle, Clock, FileQuestion, Info, Pencil, XCircle } from 'lucide-react'
import {
  eventHeadline,
  formatWhen,
  originBadge,
  relativeLabel,
  type TrackingEvent,
} from '../lib/tracking'

/**
 * The append-only timeline, rendered.
 *
 * Every row keeps three things visible at once: *what* happened (the contract's
 * own words), *when* it happened as opposed to when it was written down, and
 * *who is claiming it*. That last one is the reason this component exists
 * separately from a generic activity feed — a row our automation observed and a
 * row the user typed from memory are not the same evidence, and a correction
 * never deletes the row it corrects.
 */

const SEVERITY_ICON = {
  info: Info,
  success: Clock,
  warning: AlertTriangle,
  error: XCircle,
} as const

function SeverityIcon({ severity }: { severity: string }) {
  const Icon = (SEVERITY_ICON as Record<string, typeof Info>)[severity] || Info
  const tone = severity === 'error' ? 'text-red-500'
    : severity === 'warning' ? 'text-amber-500'
      : severity === 'success' ? 'text-emerald-500' : 'text-zinc-400'
  return <Icon className={`w-3.5 h-3.5 shrink-0 ${tone}`} aria-hidden="true" />
}

export function TimelineRow({ event }: { event: TrackingEvent }) {
  const origin = originBadge(event.origin)
  const late = event.late_reported && event.occurred_at && event.recorded_at
  return (
    <li
      className="py-2.5 px-3 flex gap-3 items-start bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800"
      data-testid={`timeline-event-${event.sequence}`}
      data-event-type={event.event_type}
      data-origin={event.origin}
      data-correction={event.is_correction ? 'true' : 'false'}
    >
      <div className="pt-0.5">
        <SeverityIcon severity={event.severity} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-[11px] mono text-zinc-400">#{event.sequence}</span>
          <span className="text-sm font-medium leading-tight">{eventHeadline(event)}</span>
          <span
            title={origin.title}
            data-testid={`origin-${event.sequence}`}
            className={`text-[10px] px-2 py-0.5 rounded-full mono ${origin.className}`}
          >
            {origin.label}
          </span>
          {event.is_correction && (
            <span className="text-[10px] px-2 py-0.5 rounded-full mono bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300 inline-flex items-center gap-1">
              <Pencil className="w-3 h-3" /> correction
            </span>
          )}
          {event.is_provisional && (
            <span className="text-[10px] px-2 py-0.5 rounded-full mono bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300 inline-flex items-center gap-1">
              <FileQuestion className="w-3 h-3" /> unconfirmed
            </span>
          )}
          {late && (
            <span
              className="text-[10px] mono text-zinc-500"
              title={`Happened ${formatWhen(event.occurred_at)}, written down ${formatWhen(event.recorded_at)}`}
            >
              reported {relativeLabel(event.occurred_at)}
            </span>
          )}
        </div>
        {event.message && (
          <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-0.5">{event.message}</div>
        )}
        {event.is_correction && event.correction_reason && (
          <div className="text-xs mt-1 p-2 rounded-lg bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 text-amber-900 dark:text-amber-200">
            <span className="mono text-[10px] uppercase tracking-wide">Why it was corrected</span>
            <div>{event.correction_reason}</div>
            {event.correction_of_event_id && (
              <div className="text-[10px] mono mt-0.5 opacity-80">
                corrects row #{event.payload?.correction_of_sequence ? String(event.payload.correction_of_sequence) : event.correction_of_event_id} — the original row is still above
              </div>
            )}
          </div>
        )}
        {event.note && !event.is_correction && (
          <div className="text-xs mt-1 p-2 rounded-lg bg-zinc-50 dark:bg-zinc-800 text-zinc-700 dark:text-zinc-300 whitespace-pre-wrap">
            {event.note}
          </div>
        )}
        <div className="text-[11px] mono text-zinc-400 mt-1 flex flex-wrap gap-x-2">
          <span title="When it happened">{formatWhen(event.occurred_at)}</span>
          {event.follow_up_at && <span title="Reminder set by this row">⏰ {formatWhen(event.follow_up_at)}</span>}
          <span title="Who recorded it">{event.actor_label}</span>
        </div>
      </div>
    </li>
  )
}

export default function TrackingTimeline({ events, empty }: {
  events: TrackingEvent[]
  empty?: string
}) {
  if (!events.length) {
    return (
      <div className="text-xs mono text-zinc-500 py-4 text-center" data-testid="timeline-empty">
        {empty || 'Nothing recorded yet.'}
      </div>
    )
  }
  return (
    <ol className="space-y-2" data-testid="tracking-timeline">
      {events.map((event) => <TimelineRow key={event.id || event.event_id} event={event} />)}
    </ol>
  )
}
