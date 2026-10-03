import { useEffect, useRef, useState } from "react"
import { ApiError, scriptsApi, type ScriptStatus } from "../lib/api"

const inputClass =
  "w-full rounded-md border border-steel/30 bg-white px-3 py-2 text-sm text-ink outline-none placeholder:text-steel/50 focus:border-porch focus:ring-1 focus:ring-porch dark:border-steel/40 dark:bg-ink dark:text-mist"

const buttonClass =
  "rounded-md px-3 py-1.5 text-xs font-semibold transition disabled:cursor-not-allowed disabled:opacity-60"

const STATUS_STYLES: Record<string, string> = {
  running: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  completed: "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300",
  failed: "bg-red-100 text-red-800 dark:bg-red-950/40 dark:text-red-300",
  stopped: "bg-steel/15 text-steel",
  idle: "bg-steel/15 text-steel",
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail)
  }
  return "Request failed."
}

/** Local generation scripts, run from the browser instead of a terminal --
 * only shown in training mode (see useUIMode), since this is operational
 * tooling for a single-purpose local deployment, not something a normal
 * production KNOCK install needs.
 */
export function ScriptRunnerPanel() {
  const [status, setStatus] = useState<ScriptStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [scenarioModels, setScenarioModels] = useState("")
  const [responseModels, setResponseModels] = useState("")
  const [busy, setBusy] = useState(false)
  const [showLogs, setShowLogs] = useState(false)

  const isRunning = status?.status === "running"

  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const next = await scriptsApi.status()
        if (!cancelled) setStatus(next)
      } catch {
        // best-effort -- a transient failure just skips this tick
      }
    }
    void poll()
    const interval = setInterval(() => void poll(), 2000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  async function runGenerateScenarios() {
    const models = scenarioModels
      .split(",")
      .map((m) => m.trim())
      .filter(Boolean)
    if (models.length === 0) return
    setBusy(true)
    setError(null)
    try {
      setStatus(await scriptsApi.generateScenarios({ models }))
      setShowLogs(true)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  async function runGenerateTrainingData() {
    const models = responseModels
      .split(",")
      .map((m) => m.trim())
      .filter(Boolean)
    if (models.length === 0) return
    setBusy(true)
    setError(null)
    try {
      setStatus(await scriptsApi.generateTrainingData({ models }))
      setShowLogs(true)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    setBusy(true)
    try {
      setStatus(await scriptsApi.stop())
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mb-6 rounded-lg border border-steel/20 bg-paper px-4 py-3 dark:bg-dusk">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-sm font-semibold text-ink dark:text-mist">
          Generate synthetic training data
        </h2>
        {status && status.status !== "idle" && (
          <span
            className={`rounded-full px-2 py-0.5 text-xs font-medium uppercase tracking-wide ${
              STATUS_STYLES[status.status] ?? "bg-steel/15 text-steel"
            }`}
          >
            {status.script ? `${status.script}: ${status.status}` : status.status}
          </span>
        )}
      </div>

      {error && (
        <p className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}

      <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <label className="text-xs font-medium text-steel">
            Generate scenarios -- models (comma-separated)
          </label>
          <div className="mt-1 flex gap-2">
            <input
              type="text"
              value={scenarioModels}
              onChange={(e) => setScenarioModels(e.target.value)}
              placeholder="e.g. qwen2.5:14b,llama3.1:8b"
              disabled={isRunning}
              className={inputClass}
            />
            <button
              type="button"
              disabled={busy || isRunning || !scenarioModels.trim()}
              onClick={() => void runGenerateScenarios()}
              className={`${buttonClass} shrink-0 bg-porch text-ink hover:brightness-95`}
            >
              Run
            </button>
          </div>
        </div>

        <div>
          <label className="text-xs font-medium text-steel">
            Generate responses -- models (comma-separated)
          </label>
          <div className="mt-1 flex gap-2">
            <input
              type="text"
              value={responseModels}
              onChange={(e) => setResponseModels(e.target.value)}
              placeholder="e.g. qwen2.5:14b,llama3.1:8b"
              disabled={isRunning}
              className={inputClass}
            />
            <button
              type="button"
              disabled={busy || isRunning || !responseModels.trim()}
              onClick={() => void runGenerateTrainingData()}
              className={`${buttonClass} shrink-0 bg-porch text-ink hover:brightness-95`}
            >
              Run
            </button>
          </div>
        </div>
      </div>

      <div className="mt-3 flex items-center gap-3">
        {isRunning && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void stop()}
            className={`${buttonClass} border border-steel/30 text-ink hover:border-red-400 hover:text-red-600 dark:text-mist`}
          >
            Stop
          </button>
        )}
        <button
          type="button"
          onClick={() => setShowLogs((v) => !v)}
          className={`${buttonClass} text-steel hover:text-ink dark:hover:text-mist`}
        >
          {showLogs ? "Hide logs" : "Show logs"}
        </button>
      </div>

      {showLogs && <LogTail active={isRunning} />}
    </div>
  )
}

function LogTail({ active }: { active: boolean }) {
  const [lines, setLines] = useState<string[]>([])
  const cursor = useRef(0)
  const scrollRef = useRef<HTMLPreElement | null>(null)

  useEffect(() => {
    let cancelled = false

    async function poll() {
      try {
        const result = await scriptsApi.logs(cursor.current)
        if (cancelled) return
        if (result.lines.length > 0) {
          setLines((current) => [...current, ...result.lines].slice(-1000))
        }
        cursor.current = result.next_after
      } catch {
        // best-effort -- a transient failure just skips this tick
      }
    }

    void poll()
    const interval = setInterval(() => void poll(), active ? 1000 : 4000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [active])

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [lines])

  return (
    <pre
      ref={scrollRef}
      className="mt-3 max-h-64 overflow-y-auto rounded-md bg-ink px-3 py-2 font-mono text-xs text-mist"
    >
      {lines.length > 0 ? lines.join("\n") : "No output yet."}
    </pre>
  )
}
