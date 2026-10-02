import { useCallback, useEffect, useRef, useState } from "react"
import {
  ApiError,
  BRIDGE_LABELS,
  supervisorApi,
  type BridgeState,
} from "../lib/api"

const BRIDGE_ORDER = ["mqtt", "frigate", "homeassistant", "unifi"]

const STATUS_STYLES: Record<string, string> = {
  running: "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300",
  starting: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  stopped: "bg-steel/15 text-steel",
  crashed: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail)
  }
  return "Request failed."
}

export function Processes() {
  const [bridges, setBridges] = useState<BridgeState[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setBridges(await supervisorApi.list())
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    void refresh()
    const interval = setInterval(() => void refresh(), 2000)
    return () => clearInterval(interval)
  }, [refresh])

  return (
    <div>
      <h1 className="font-display text-2xl font-black text-ink dark:text-mist">Processes</h1>
      <p className="mt-1 text-sm text-steel">
        Start, stop, and restart each bridge, and watch its log output live.
      </p>

      {error && (
        <p className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}

      <div className="mt-6 space-y-3">
        {(bridges ?? BRIDGE_ORDER.map((name) => ({ name, status: "stopped" as const, restart_count: 0, pid: null }))).map(
          (bridge) => (
            <BridgeCard
              key={bridge.name}
              bridge={bridge}
              expanded={expanded === bridge.name}
              onToggleExpand={() =>
                setExpanded((current) => (current === bridge.name ? null : bridge.name))
              }
              onChanged={refresh}
              onError={setError}
            />
          ),
        )}
      </div>
    </div>
  )
}

function BridgeCard({
  bridge,
  expanded,
  onToggleExpand,
  onChanged,
  onError,
}: {
  bridge: BridgeState
  expanded: boolean
  onToggleExpand: () => void
  onChanged: () => void
  onError: (message: string) => void
}) {
  const [busy, setBusy] = useState(false)

  async function run(action: "start" | "stop" | "restart") {
    setBusy(true)
    try {
      await supervisorApi[action](bridge.name)
      onChanged()
    } catch (err) {
      onError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-lg border border-steel/20 bg-paper p-4 dark:bg-dusk">
      <div className="flex items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-medium text-ink dark:text-mist">
              {BRIDGE_LABELS[bridge.name] ?? bridge.name}
            </span>
            <span
              className={`rounded-full px-2 py-0.5 text-xs font-medium uppercase tracking-wide ${
                STATUS_STYLES[bridge.status] ?? "bg-steel/15 text-steel"
              }`}
            >
              {bridge.status}
            </span>
          </div>
          <p className="mt-1 text-xs text-steel">
            {bridge.pid !== null ? `pid ${bridge.pid}` : "not running"}
            {bridge.restart_count > 0 && ` · ${bridge.restart_count} restart(s)`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={busy || bridge.status === "running" || bridge.status === "starting"}
            onClick={() => void run("start")}
            className="rounded-md border border-steel/30 px-3 py-1.5 text-xs font-medium text-ink transition hover:border-porch hover:text-porch disabled:cursor-not-allowed disabled:opacity-40 dark:border-steel/40 dark:text-mist"
          >
            Start
          </button>
          <button
            type="button"
            disabled={busy || bridge.status === "stopped" || bridge.status === "crashed"}
            onClick={() => void run("stop")}
            className="rounded-md border border-steel/30 px-3 py-1.5 text-xs font-medium text-ink transition hover:border-porch hover:text-porch disabled:cursor-not-allowed disabled:opacity-40 dark:border-steel/40 dark:text-mist"
          >
            Stop
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void run("restart")}
            className="rounded-md border border-steel/30 px-3 py-1.5 text-xs font-medium text-ink transition hover:border-porch hover:text-porch disabled:cursor-not-allowed disabled:opacity-40 dark:border-steel/40 dark:text-mist"
          >
            Restart
          </button>
          <button
            type="button"
            onClick={onToggleExpand}
            className="rounded-md px-3 py-1.5 text-xs font-medium text-steel transition hover:text-ink dark:hover:text-mist"
          >
            {expanded ? "Hide logs" : "View logs"}
          </button>
        </div>
      </div>
      {expanded && <LogTail name={bridge.name} />}
    </div>
  )
}

function LogTail({ name }: { name: string }) {
  const [lines, setLines] = useState<string[]>([])
  const cursor = useRef(0)
  const scrollRef = useRef<HTMLPreElement | null>(null)

  useEffect(() => {
    let cancelled = false
    setLines([])
    cursor.current = 0

    async function poll() {
      try {
        const result = await supervisorApi.logs(name, cursor.current)
        if (cancelled) return
        if (result.lines.length > 0) {
          setLines((current) => [...current, ...result.lines].slice(-500))
        }
        cursor.current = result.next_after
      } catch {
        // best-effort -- a transient failure just skips this tick
      }
    }

    void poll()
    const interval = setInterval(() => void poll(), 1500)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [name])

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
