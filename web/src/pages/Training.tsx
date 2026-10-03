import { useEffect, useMemo, useState } from "react"
import { ApiError, trainingApi, type TrainingQueueItem, type TrainingReview } from "../lib/api"

type StatusFilter = "pending" | "approved" | "rejected"

const STATUS_TABS: { key: StatusFilter; label: string }[] = [
  { key: "pending", label: "Pending review" },
  { key: "approved", label: "Approved" },
  { key: "rejected", label: "Rejected" },
]

const inputClass =
  "w-full rounded-md border border-steel/30 bg-white px-3 py-2 text-sm text-ink outline-none focus:border-porch focus:ring-1 focus:ring-porch dark:border-steel/40 dark:bg-ink dark:text-mist"

const buttonClass =
  "rounded-md px-3 py-1.5 text-xs font-semibold transition disabled:cursor-not-allowed disabled:opacity-60"

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

interface EditState {
  intent: string
  response: string
}

export function Training() {
  const [items, setItems] = useState<TrainingQueueItem[] | null>(null)
  const [intents, setIntents] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<StatusFilter>("pending")
  const [edits, setEdits] = useState<Record<string, EditState>>({})
  const [savingKey, setSavingKey] = useState<string | null>(null)

  function load() {
    Promise.all([trainingApi.queue(), trainingApi.intents()])
      .then(([queue, intentOptions]) => {
        setItems(queue)
        setIntents(intentOptions.intents)
      })
      .catch((err) => setError(errorMessage(err)))
  }

  useEffect(load, [])

  const filtered = useMemo(
    () => (items ?? []).filter((item) => item.review.status === filter),
    [items, filter],
  )

  function editFor(item: TrainingQueueItem): EditState {
    return (
      edits[item.key] ?? {
        intent: item.review.intent_override ?? item.entry.intent ?? "unknown",
        response: item.review.response_override ?? item.entry.response_text,
      }
    )
  }

  async function setStatus(item: TrainingQueueItem, status: TrainingReview["status"]) {
    const edit = editFor(item)
    setSavingKey(item.key)
    setError(null)
    try {
      const intentChanged = edit.intent !== item.entry.intent
      const responseChanged = edit.response !== item.entry.response_text
      const updated = await trainingApi.review(item.key, {
        status,
        intent_override: intentChanged ? edit.intent : null,
        response_override: responseChanged ? edit.response : null,
      })
      setItems(
        (current) =>
          current?.map((entry) => (entry.key === item.key ? { ...entry, review: updated } : entry)) ??
          current,
      )
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSavingKey(null)
    }
  }

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-2xl font-black text-ink dark:text-mist">
            Training data
          </h1>
          <p className="mt-1 text-sm text-steel">
            Review real interactions, correct anything wrong, and export an instruction-tuning
            file for LoRA fine-tuning -- built from the exact same prompts KNOCK sends the LLM in
            production.
          </p>
        </div>
        <a
          href={trainingApi.exportUrl}
          download
          className="shrink-0 rounded-md bg-porch px-4 py-2 text-sm font-semibold text-ink transition hover:brightness-95"
        >
          Export training file
        </a>
      </div>

      <div className="mt-4 flex gap-1 border-b border-steel/20">
        {STATUS_TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => setFilter(tab.key)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition ${
              filter === tab.key
                ? "border-porch text-ink dark:text-mist"
                : "border-transparent text-steel hover:text-ink dark:hover:text-mist"
            }`}
          >
            {tab.label}
            {items && (
              <span className="ml-1.5 text-xs text-steel">
                ({items.filter((item) => item.review.status === tab.key).length})
              </span>
            )}
          </button>
        ))}
      </div>

      <div className="mt-6">
        {error && (
          <p className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
            {error}
          </p>
        )}

        {!items ? (
          <p className="text-sm text-steel">Loading…</p>
        ) : filtered.length === 0 ? (
          <p className="text-sm text-steel">
            {filter === "pending" ? "Nothing waiting for review." : `No ${filter} entries yet.`}
          </p>
        ) : (
          <div className="space-y-3">
            {filtered.map((item) => {
              const edit = editFor(item)
              const saving = savingKey === item.key
              return (
                <div
                  key={item.key}
                  className="rounded-lg border border-steel/20 bg-paper px-4 py-3 dark:bg-dusk"
                >
                  <div className="flex items-start justify-between gap-4">
                    <p className="text-sm text-ink dark:text-mist">
                      <span className="font-medium text-steel">Visitor:</span> {item.entry.text}
                    </p>
                    <span className="shrink-0 text-xs text-steel">
                      {formatTimestamp(item.entry.timestamp)}
                    </span>
                  </div>

                  <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-[160px_1fr]">
                    <label className="text-xs font-medium text-steel sm:pt-2">Intent</label>
                    <select
                      value={edit.intent}
                      onChange={(e) =>
                        setEdits((current) => ({
                          ...current,
                          [item.key]: { ...edit, intent: e.target.value },
                        }))
                      }
                      className={inputClass}
                    >
                      {intents.map((name) => (
                        <option key={name} value={name}>
                          {name}
                        </option>
                      ))}
                    </select>

                    <label className="text-xs font-medium text-steel sm:pt-2">Response</label>
                    <textarea
                      value={edit.response}
                      onChange={(e) =>
                        setEdits((current) => ({
                          ...current,
                          [item.key]: { ...edit, response: e.target.value },
                        }))
                      }
                      rows={2}
                      className={inputClass}
                    />
                  </div>

                  <div className="mt-3 flex items-center gap-2">
                    <button
                      type="button"
                      disabled={saving}
                      onClick={() => void setStatus(item, "approved")}
                      className={`${buttonClass} bg-porch text-ink hover:brightness-95`}
                    >
                      Approve
                    </button>
                    <button
                      type="button"
                      disabled={saving}
                      onClick={() => void setStatus(item, "rejected")}
                      className={`${buttonClass} border border-steel/30 text-ink hover:border-red-400 hover:text-red-600 dark:text-mist`}
                    >
                      Reject
                    </button>
                    {item.review.status !== "pending" && (
                      <button
                        type="button"
                        disabled={saving}
                        onClick={() => void setStatus(item, "pending")}
                        className={`${buttonClass} text-steel hover:text-ink dark:hover:text-mist`}
                      >
                        Reset to pending
                      </button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
