import { useEffect, useState } from "react"
import { ApiError, historyApi, type AuditEntry, type SessionState } from "../lib/api"

type TabKey = "audit" | "sessions"

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail)
  }
  return "Request failed."
}

function formatTimestamp(value: string): string {
  try {
    return new Date(value).toLocaleString()
  } catch {
    return value
  }
}

export function History() {
  const [tab, setTab] = useState<TabKey>("audit")

  return (
    <div>
      <h1 className="font-display text-2xl font-black text-ink dark:text-mist">History</h1>
      <div className="mt-4 flex gap-1 border-b border-steel/20">
        {(["audit", "sessions"] as const).map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition ${
              tab === key
                ? "border-porch text-ink dark:text-mist"
                : "border-transparent text-steel hover:text-ink dark:hover:text-mist"
            }`}
          >
            {key === "audit" ? "Audit log" : "Sessions"}
          </button>
        ))}
      </div>
      <div className="mt-6">{tab === "audit" ? <AuditTab /> : <SessionsTab />}</div>
    </div>
  )
}

function AuditTab() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    historyApi
      .audit()
      .then(setEntries)
      .catch((err) => setError(errorMessage(err)))
  }, [])

  if (error) {
    return (
      <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
        {error}
      </p>
    )
  }

  if (!entries) {
    return <p className="text-sm text-steel">Loading…</p>
  }

  if (entries.length === 0) {
    return <p className="text-sm text-steel">No audit entries yet.</p>
  }

  return (
    <div className="space-y-2">
      {entries.map((entry, index) => (
        <div
          key={index}
          className="rounded-lg border border-steel/20 bg-paper px-4 py-3 dark:bg-dusk"
        >
          <div className="flex items-center justify-between gap-4">
            <p className="text-sm text-ink dark:text-mist">{entry.text}</p>
            <span className="shrink-0 text-xs text-steel">
              {formatTimestamp(entry.timestamp)}
            </span>
          </div>
          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            <Badge label={entry.reason} tone={entry.allowed ? "neutral" : "warning"} />
            {entry.intent && <Badge label={entry.intent} tone="neutral" />}
            {entry.matched_rule_ids.map((id) => (
              <Badge key={id} label={id} tone="warning" />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function SessionsTab() {
  const [sessions, setSessions] = useState<SessionState[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  useEffect(() => {
    historyApi
      .sessions()
      .then(setSessions)
      .catch((err) => setError(errorMessage(err)))
  }, [])

  if (error) {
    return (
      <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
        {error}
      </p>
    )
  }

  if (!sessions) {
    return <p className="text-sm text-steel">Loading…</p>
  }

  if (sessions.length === 0) {
    return <p className="text-sm text-steel">No sessions yet.</p>
  }

  return (
    <div className="space-y-2">
      {sessions.map((session) => {
        const isExpanded = expanded === session.session_id
        return (
          <div
            key={session.session_id}
            className="rounded-lg border border-steel/20 bg-paper px-4 py-3 dark:bg-dusk"
          >
            <button
              type="button"
              onClick={() => setExpanded(isExpanded ? null : session.session_id)}
              className="flex w-full items-center justify-between gap-4 text-left"
            >
              <div>
                <p className="font-mono text-sm text-ink dark:text-mist">{session.session_id}</p>
                <p className="mt-0.5 text-xs text-steel">
                  {session.turn_count} turn(s) · last intent: {session.last_intent}
                  {session.escalated && " · escalated"}
                </p>
              </div>
              <span className="shrink-0 text-xs text-steel">
                {formatTimestamp(session.updated_at)}
              </span>
            </button>
            {isExpanded && (
              <div className="mt-3 space-y-1 border-t border-steel/20 pt-3">
                {session.history.length === 0 ? (
                  <p className="text-xs text-steel">No history recorded.</p>
                ) : (
                  session.history.map((line, index) => (
                    <p key={index} className="text-sm text-ink dark:text-mist">
                      {line}
                    </p>
                  ))
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function Badge({ label, tone }: { label: string; tone: "neutral" | "warning" }) {
  return (
    <span
      className={`rounded-full px-2 py-0.5 font-medium uppercase tracking-wide ${
        tone === "warning"
          ? "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
          : "bg-steel/15 text-steel"
      }`}
    >
      {label}
    </span>
  )
}
