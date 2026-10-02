import { useEffect, useState } from "react"
import {
  ApiError,
  settingsApi,
  videoApi,
  type FrigateCameraInfo,
  type UnifiCameraInfo,
} from "../lib/api"

const SNAPSHOT_REFRESH_MS = 2000

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail)
  }
  return "Request failed."
}

function SnapshotTile({
  title,
  subtitle,
  snapshotUrl,
}: {
  title: string
  subtitle?: string
  snapshotUrl: string
}) {
  const [cacheBust, setCacheBust] = useState(() => Date.now())

  useEffect(() => {
    const interval = setInterval(() => setCacheBust(Date.now()), SNAPSHOT_REFRESH_MS)
    return () => clearInterval(interval)
  }, [])

  return (
    <div className="overflow-hidden rounded-lg border border-steel/20 bg-paper dark:bg-dusk">
      <img
        src={`${snapshotUrl}?t=${cacheBust}`}
        alt={title}
        className="aspect-video w-full bg-ink object-cover"
      />
      <div className="px-3 py-2">
        <p className="text-sm font-medium text-ink dark:text-mist">{title}</p>
        {subtitle && <p className="text-xs text-steel">{subtitle}</p>}
      </div>
    </div>
  )
}

function UnifiSection() {
  const [cameras, setCameras] = useState<UnifiCameraInfo[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    videoApi
      .unifiCameras()
      .then(setCameras)
      .catch((err) => setError(errorMessage(err)))
  }, [])

  return (
    <section>
      <h2 className="font-display text-lg font-black text-ink dark:text-mist">UniFi Protect</h2>
      <p className="mt-1 text-sm text-steel">
        Polled snapshots, refreshed every {SNAPSHOT_REFRESH_MS / 1000}s.
      </p>
      {error && (
        <p className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}
      {cameras && cameras.length === 0 && !error && (
        <p className="mt-3 text-sm text-steel">No cameras found.</p>
      )}
      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {cameras?.map((camera) => (
          <SnapshotTile
            key={camera.device_id}
            title={camera.name}
            subtitle={camera.is_connected ? "Connected" : "Disconnected"}
            snapshotUrl={videoApi.unifiSnapshotUrl(camera.device_id)}
          />
        ))}
      </div>
    </section>
  )
}

function FrigateSection() {
  const [cameras, setCameras] = useState<FrigateCameraInfo[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [frigateUrl, setFrigateUrl] = useState<string | null>(null)

  useEffect(() => {
    videoApi
      .frigateCameras()
      .then(setCameras)
      .catch((err) => setError(errorMessage(err)))

    settingsApi
      .get("frigate")
      .then((settings) => {
        const host = settings.fields.find((f) => f.name === "http_host")?.value
        const port = settings.fields.find((f) => f.name === "http_port")?.value
        if (typeof host === "string" && typeof port === "number") {
          setFrigateUrl(`http://${host}:${port}/`)
        }
      })
      .catch(() => setFrigateUrl(null))
  }, [])

  return (
    <section className="mt-10">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="font-display text-lg font-black text-ink dark:text-mist">Frigate</h2>
          <p className="mt-1 text-sm text-steel">
            Polled snapshots here; open Frigate's own UI for a true live view.
          </p>
        </div>
        {frigateUrl && (
          <a
            href={frigateUrl}
            target="_blank"
            rel="noreferrer"
            className="rounded-md border border-steel/30 px-3 py-1.5 text-xs font-medium text-ink transition hover:border-porch hover:text-porch dark:border-steel/40 dark:text-mist"
          >
            Open Frigate UI →
          </a>
        )}
      </div>
      {error && (
        <p className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}
      {cameras && cameras.length === 0 && !error && (
        <p className="mt-3 text-sm text-steel">No cameras found in Frigate's config.</p>
      )}
      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {cameras?.map((camera) => (
          <SnapshotTile
            key={camera.name}
            title={camera.name}
            snapshotUrl={videoApi.frigateSnapshotUrl(camera.name)}
          />
        ))}
      </div>
    </section>
  )
}

export function Cameras() {
  return (
    <div>
      <h1 className="font-display text-2xl font-black text-ink dark:text-mist">Cameras</h1>
      <p className="mt-1 text-sm text-steel">
        A live-ish view of what each integration can currently see.
      </p>
      <div className="mt-6">
        <UnifiSection />
        <FrigateSection />
      </div>
    </div>
  )
}
