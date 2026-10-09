import { useCallback, useEffect, useState, type Dispatch, type SetStateAction } from "react"
import { ScriptRunnerPanel } from "../components/ScriptRunnerPanel"
import {
  ApiError,
  trainingApi,
  type AggregatedJudgeResult,
  type TrainingQueueCounts,
  type TrainingQueueItem,
  type TrainingReview,
} from "../lib/api"
import { useUIMode } from "../lib/uiMode"

const DISAGREEMENT_WARNING_THRESHOLD = 2.5
const PAGE_SIZE = 25

function AxisBadge({ label, value }: { label: string; value: number }) {
  const tone =
    value <= 3
      ? "bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-300"
      : value <= 6
        ? "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300"
        : "bg-green-100 text-green-800 dark:bg-green-950/50 dark:text-green-300"
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-medium ${tone}`}>
      {label} {value.toFixed(1)}
    </span>
  )
}

function JudgePanel({ judge }: { judge: AggregatedJudgeResult }) {
  return (
    <div className="mt-3 rounded-md border border-steel/20 bg-white/50 p-3 text-xs dark:bg-black/10">
      <p className="mb-2 font-medium text-steel">Judge ensemble</p>
      <div className="flex flex-wrap gap-1.5">
        <AxisBadge label="visitor voice" value={judge.visitor_voice_avg} />
        <AxisBadge label="category" value={judge.category_correct_avg} />
        <AxisBadge label="safety" value={judge.safety_compliant_avg} />
        <AxisBadge label="quality" value={judge.natural_quality_avg} />
      </div>
      {(judge.voice_veto || judge.safety_veto) && (
        <p className="mt-2 font-medium text-red-700 dark:text-red-300">
          {judge.voice_veto ? "Vetoed: not genuine visitor speech. " : ""}
          {judge.safety_veto ? "Vetoed: unsafe phrasing." : ""}
        </p>
      )}
      {judge.disagreement >= DISAGREEMENT_WARNING_THRESHOLD && (
        <p className="mt-2 text-amber-700 dark:text-amber-300">
          Judges disagreed on this one (spread {judge.disagreement.toFixed(1)}) -- worth a closer
          look.
        </p>
      )}
      {judge.per_judge.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-steel">
          {judge.per_judge.map((j, i) => (
            <li key={i}>
              <span className="font-medium">{j.judge_model}:</span> {j.reason}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function CorrectionDiffPanel({ item }: { item: TrainingQueueItem }) {
  if (item.metadata.corrections.length === 0) return null
  return (
    <div className="mt-3 rounded-md border border-steel/20 bg-white/50 p-3 text-xs dark:bg-black/10">
      <p className="mb-2 font-medium text-steel">Correction attempts</p>
      <div className="space-y-3">
        {item.metadata.corrections.map((attempt) => (
          <div key={attempt.attempt} className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <div>
              <p className="font-medium text-steel">
                Attempt {attempt.attempt} -- before (flagged: {attempt.judge_reason})
              </p>
              <p className="mt-0.5 text-ink dark:text-mist">{attempt.original_response}</p>
            </div>
            <div>
              <p className="font-medium text-steel">
                After {attempt.accepted ? "(accepted)" : "(still failed)"}
              </p>
              <p className="mt-0.5 text-ink dark:text-mist">{attempt.corrected_response}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

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

function EntryFields({
  item,
  edit,
  intents,
  responseRows,
  onChange,
}: {
  item: TrainingQueueItem
  edit: EditState
  intents: string[]
  responseRows: number
  onChange: (next: EditState) => void
}) {
  return (
    <>
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm text-ink dark:text-mist">
          <span className="font-medium text-steel">Visitor:</span> {item.entry.text}
        </p>
        <span className="shrink-0 text-xs text-steel">{formatTimestamp(item.entry.timestamp)}</span>
      </div>

      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-[160px_1fr]">
        <label className="text-xs font-medium text-steel sm:pt-2">Intent</label>
        <select
          value={edit.intent}
          onChange={(e) => onChange({ ...edit, intent: e.target.value })}
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
          onChange={(e) => onChange({ ...edit, response: e.target.value })}
          rows={responseRows}
          className={inputClass}
        />
      </div>

      {item.metadata.judge && <JudgePanel judge={item.metadata.judge} />}
      <CorrectionDiffPanel item={item} />
    </>
  )
}

export function Training() {
  const { trainingOnly } = useUIMode()
  const [counts, setCounts] = useState<TrainingQueueCounts | null>(null)
  const [intents, setIntents] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<StatusFilter>("pending")
  const [edits, setEdits] = useState<Record<string, EditState>>({})
  const [savingKey, setSavingKey] = useState<string | null>(null)
  // Small in-memory undo stack for the one-at-a-time triage flow below --
  // "Back" pops the most recently approved/rejected item and resets it to
  // pending. Session-only (not persisted); a page reload just loses it,
  // same as the reviewed-this-session counter. Stores the full item (not
  // just its key) so undo doesn't depend on it still being in whichever
  // page happens to be loaded.
  const [history, setHistory] = useState<TrainingQueueItem[]>([])
  const [reviewedCount, setReviewedCount] = useState(0)

  // The active tab's current page. "pending" always re-fetches at
  // offset=0 -- an approved/rejected item simply stops matching
  // status=pending, so offset=0 is always "whatever's left", no cursor
  // bookkeeping needed. approved/rejected are real paginated lists with
  // a "Load more" button, since they're browsed as a list, not
  // consumed one-at-a-time.
  const [items, setItems] = useState<TrainingQueueItem[]>([])
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)

  const loadCounts = useCallback(() => {
    trainingApi
      .queueCounts()
      .then(setCounts)
      .catch((err) => setError(errorMessage(err)))
  }, [])

  const loadPage = useCallback((status: StatusFilter, offset: number) => {
    return trainingApi.queue({ status, offset, limit: PAGE_SIZE }).then((page) => {
      setItems((current) => (offset === 0 ? page.items : [...current, ...page.items]))
      setTotal(page.total)
      setHasMore(page.has_more)
      return page
    })
  }, [])

  useEffect(() => {
    setItems([])
    setTotal(0)
    setHasMore(false)
    loadPage(filter, 0).catch((err) => setError(errorMessage(err)))
  }, [filter, loadPage])

  useEffect(() => {
    loadCounts()
    trainingApi
      .intents()
      .then((res) => setIntents(res.intents))
      .catch((err) => setError(errorMessage(err)))
  }, [loadCounts])

  function loadMore() {
    setLoadingMore(true)
    loadPage(filter, items.length)
      .catch((err) => setError(errorMessage(err)))
      .finally(() => setLoadingMore(false))
  }

  function editFor(item: TrainingQueueItem): EditState {
    const lastCorrection = item.metadata.corrections.at(-1)
    return (
      edits[item.key] ?? {
        intent: item.review.intent_override ?? item.entry.intent ?? "unknown",
        response:
          item.review.response_override ??
          lastCorrection?.corrected_response ??
          item.entry.response_text,
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
      // The item no longer matches the active tab's filter (it moved to
      // a different status) -- drop it from the current page locally
      // rather than wait on a refetch, then reconcile counts/total from
      // the server so they stay authoritative.
      const remaining = items.filter((i) => i.key !== item.key)
      setItems(remaining)
      setTotal((t) => Math.max(0, t - 1))
      loadCounts()
      // Refill the pending buffer once it runs dry -- offset=0 is always
      // correct here (nothing reviewed matches status=pending anymore),
      // so this naturally fetches whatever's left rather than needing a
      // cursor. Without this, finishing a page's worth of pending items
      // would wrongly show "All caught up" even if more exist.
      if (filter === "pending" && remaining.length === 0 && hasMore) {
        loadPage("pending", 0).catch((err) => setError(errorMessage(err)))
      }
      if (status === "approved" || status === "rejected") {
        setHistory((h) => [...h, { ...item, review: updated }])
        setReviewedCount((c) => c + 1)
      }
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSavingKey(null)
    }
  }

  async function goBack() {
    const lastItem = history[history.length - 1]
    if (!lastItem) return
    setHistory((h) => h.slice(0, -1))
    setReviewedCount((c) => Math.max(0, c - 1))
    await setStatus(lastItem, "pending")
    // Reverting back to pending while viewing the pending tab: put it
    // back at the front so it's the very next one shown, rather than
    // wherever a fresh fetch would place it.
    if (filter === "pending") {
      setItems((current) => [{ ...lastItem, review: { ...lastItem.review, status: "pending" } }, ...current])
      setTotal((t) => t + 1)
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
        <div className="flex shrink-0 gap-2">
          <a
            href={trainingApi.exportUrl}
            download
            className="rounded-md bg-porch px-4 py-2 text-sm font-semibold text-ink transition hover:brightness-95"
          >
            Export training file
          </a>
          <a
            href={trainingApi.exportDpoUrl}
            download
            className="rounded-md border border-steel/30 px-4 py-2 text-sm font-semibold text-ink transition hover:border-porch dark:text-mist"
          >
            Export DPO pairs
          </a>
        </div>
      </div>

      {trainingOnly && (
        <div className="mt-6">
          <ScriptRunnerPanel />
        </div>
      )}

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
            {counts && <span className="ml-1.5 text-xs text-steel">({counts[tab.key]})</span>}
          </button>
        ))}
      </div>

      <div className="mt-6">
        {error && (
          <p className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
            {error}
          </p>
        )}

        {filter === "pending" ? (
          <PendingTriage
            current={items[0] ?? null}
            totalRemaining={total}
            intents={intents}
            setEdits={setEdits}
            editFor={editFor}
            savingKey={savingKey}
            setStatus={setStatus}
            goBack={goBack}
            canGoBack={history.length > 0}
            reviewedCount={reviewedCount}
          />
        ) : items.length === 0 ? (
          <p className="text-sm text-steel">No {filter} entries yet.</p>
        ) : (
          <div className="space-y-3">
            {items.map((item) => {
              const edit = editFor(item)
              const saving = savingKey === item.key
              return (
                <div
                  key={item.key}
                  className="rounded-lg border border-steel/20 bg-paper px-4 py-3 dark:bg-dusk"
                >
                  <EntryFields
                    item={item}
                    edit={edit}
                    intents={intents}
                    responseRows={2}
                    onChange={(next) => setEdits((current) => ({ ...current, [item.key]: next }))}
                  />
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
                    <button
                      type="button"
                      disabled={saving}
                      onClick={() => void setStatus(item, "pending")}
                      className={`${buttonClass} text-steel hover:text-ink dark:hover:text-mist`}
                    >
                      Reset to pending
                    </button>
                  </div>
                </div>
              )
            })}
            {hasMore && (
              <button
                type="button"
                disabled={loadingMore}
                onClick={loadMore}
                className={`${buttonClass} w-full border border-steel/30 py-2 text-steel hover:border-porch hover:text-ink dark:text-mist`}
              >
                {loadingMore ? "Loading…" : `Load more (${items.length}/${total})`}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function PendingTriage({
  current,
  totalRemaining,
  intents,
  setEdits,
  editFor,
  savingKey,
  setStatus,
  goBack,
  canGoBack,
  reviewedCount,
}: {
  current: TrainingQueueItem | null
  totalRemaining: number
  intents: string[]
  setEdits: Dispatch<SetStateAction<Record<string, EditState>>>
  editFor: (item: TrainingQueueItem) => EditState
  savingKey: string | null
  setStatus: (item: TrainingQueueItem, status: TrainingReview["status"]) => Promise<void>
  goBack: () => Promise<void>
  canGoBack: boolean
  reviewedCount: number
}) {
  if (!current) {
    return (
      <div className="rounded-lg border border-steel/20 bg-paper px-6 py-10 text-center dark:bg-dusk">
        <p className="text-base font-medium text-ink dark:text-mist">All caught up!</p>
        <p className="mt-1 text-sm text-steel">Nothing waiting for review.</p>
        {reviewedCount > 0 && (
          <p className="mt-3 text-xs text-steel">{reviewedCount} reviewed this session.</p>
        )}
      </div>
    )
  }

  const edit = editFor(current)
  const saving = savingKey === current.key

  return (
    <div>
      <div className="mb-3 flex items-center justify-between text-xs text-steel">
        <span>{totalRemaining} left to review</span>
        {reviewedCount > 0 && <span>{reviewedCount} done this session</span>}
      </div>
      <div className="rounded-lg border border-steel/20 bg-paper px-5 py-5 dark:bg-dusk">
        <EntryFields
          item={current}
          edit={edit}
          intents={intents}
          responseRows={4}
          onChange={(next) => setEdits((prev) => ({ ...prev, [current.key]: next }))}
        />
        <div className="mt-4 flex items-center gap-2">
          <button
            type="button"
            disabled={saving}
            onClick={() => void setStatus(current, "approved")}
            className={`${buttonClass} bg-porch px-4 py-2 text-sm text-ink hover:brightness-95`}
          >
            Approve &amp; Next
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={() => void setStatus(current, "rejected")}
            className={`${buttonClass} border border-steel/30 px-4 py-2 text-sm text-ink hover:border-red-400 hover:text-red-600 dark:text-mist`}
          >
            Reject &amp; Next
          </button>
          <button
            type="button"
            disabled={saving || !canGoBack}
            onClick={() => void goBack()}
            className={`${buttonClass} ml-auto text-steel hover:text-ink dark:hover:text-mist`}
          >
            ← Back
          </button>
        </div>
      </div>
    </div>
  )
}
