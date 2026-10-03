import { useEffect, useState } from "react"
import { historyApi } from "../lib/api"

const STAT_TILES: { key: string; label: string }[] = [
  { key: "soliciting", label: "Solicitors turned away" },
  { key: "religious_soliciting", label: "Religious canvassers turned away" },
  { key: "political_soliciting", label: "Political canvassers turned away" },
  { key: "delivery", label: "Deliveries" },
  { key: "delivery_signature_required", label: "Signature-required deliveries" },
  { key: "unknown", label: "Unrecognized visitors" },
]

export function Dashboard() {
  const [counts, setCounts] = useState<Record<string, number> | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    historyApi
      .stats()
      .then((stats) => setCounts(stats.counts))
      .catch(() => setError("Failed to load stats."))
  }, [])

  return (
    <div>
      <h1 className="font-display text-3xl font-black text-ink dark:text-mist">Dashboard</h1>
      <p className="mt-2 max-w-prose text-sm text-steel">
        A running tally of who's come to the door, from the audit log.
      </p>

      {error && (
        <p className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}

      <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {STAT_TILES.map((tile) => (
          <div
            key={tile.key}
            className="rounded-lg border border-steel/20 bg-paper px-5 py-4 dark:bg-dusk"
          >
            <p className="font-display text-3xl font-black text-ink dark:text-mist">
              {counts ? (counts[tile.key] ?? 0) : "—"}
            </p>
            <p className="mt-1 text-sm text-steel">{tile.label}</p>
          </div>
        ))}
      </div>
    </div>
  )
}
