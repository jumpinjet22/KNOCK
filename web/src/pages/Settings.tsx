import { useCallback, useEffect, useState } from "react"
import { Navigate, useNavigate, useParams } from "react-router-dom"
import { SettingsFieldInput } from "../components/SettingsFieldInput"
import { ApiError, SETTINGS_SECTIONS, settingsApi, type SectionSettings } from "../lib/api"

const ACRONYMS = new Set(["api", "url", "ssl", "mqtt", "id", "http", "rtsp"])

function humanizeFieldName(name: string): string {
  return name
    .split("_")
    .map((word) =>
      ACRONYMS.has(word) ? word.toUpperCase() : word.charAt(0).toUpperCase() + word.slice(1),
    )
    .join(" ")
}

export function Settings() {
  const { section } = useParams<{ section: string }>()

  if (!section) {
    return <Navigate to={`/settings/${SETTINGS_SECTIONS[0].key}`} replace />
  }

  return (
    <div className="flex gap-8">
      <nav className="w-48 shrink-0">
        <ul className="space-y-1">
          {SETTINGS_SECTIONS.map((item) => (
            <SectionNavLink key={item.key} itemKey={item.key} label={item.label} active={item.key === section} />
          ))}
        </ul>
      </nav>
      <div className="min-w-0 flex-1">
        <SettingsSectionForm key={section} section={section} />
      </div>
    </div>
  )
}

function SectionNavLink({
  itemKey,
  label,
  active,
}: {
  itemKey: string
  label: string
  active: boolean
}) {
  const navigate = useNavigate()
  return (
    <li>
      <button
        type="button"
        onClick={() => navigate(`/settings/${itemKey}`)}
        className={`w-full rounded-md px-3 py-2 text-left text-sm font-medium transition ${
          active
            ? "bg-porch/15 text-ink dark:text-mist"
            : "text-steel hover:bg-steel/10 hover:text-ink dark:hover:text-mist"
        }`}
      >
        {label}
      </button>
    </li>
  )
}

function SettingsSectionForm({ section }: { section: string }) {
  const [settings, setSettings] = useState<SectionSettings | null>(null)
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [voiceOptions, setVoiceOptions] = useState<string[] | undefined>(undefined)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await settingsApi.get(section)
      setSettings(data)
      const initial: Record<string, unknown> = {}
      for (const field of data.fields) {
        initial[field.name] = field.type === "secret" ? "" : field.value
      }
      setValues(initial)
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : "Failed to load settings.")
    } finally {
      setLoading(false)
    }
  }, [section])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (section !== "kokoro") return
    settingsApi
      .kokoroVoices()
      .then((result) => setVoiceOptions(result.error ? undefined : result.voices))
      .catch(() => setVoiceOptions(undefined))
  }, [section])

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const payload: Record<string, unknown> = {}
      for (const field of settings?.fields ?? []) {
        if (field.type === "secret") {
          if (typeof values[field.name] === "string" && values[field.name] !== "") {
            payload[field.name] = values[field.name]
          }
          continue
        }
        payload[field.name] = values[field.name]
      }
      const updated = await settingsApi.update(section, payload)
      setSettings(updated)
      const refreshed: Record<string, unknown> = {}
      for (const field of updated.fields) {
        refreshed[field.name] = field.type === "secret" ? "" : field.value
      }
      setValues(refreshed)
      setSaved(true)
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        setError("One or more values are invalid. Check the field types and try again.")
      } else {
        setError("Failed to save settings.")
      }
    } finally {
      setSaving(false)
    }
  }

  const label = SETTINGS_SECTIONS.find((item) => item.key === section)?.label ?? section

  if (loading) {
    return <p className="text-sm text-steel">Loading…</p>
  }

  if (!settings) {
    return <p className="text-sm text-red-600 dark:text-red-400">{error ?? "Not found."}</p>
  }

  return (
    <form onSubmit={(event) => void handleSubmit(event)}>
      <h1 className="font-display text-2xl font-black text-ink dark:text-mist">{label}</h1>

      {error && (
        <p className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}
      {saved && !error && (
        <p className="mt-4 rounded-md bg-porch/15 px-3 py-2 text-sm text-ink dark:text-mist">
          Saved.
        </p>
      )}

      <div className="mt-6 space-y-5">
        {settings.fields.map((field) => (
          <div key={field.name}>
            <div className="mb-1 flex items-center gap-2">
              <span className="text-sm font-medium text-ink dark:text-mist">
                {humanizeFieldName(field.name)}
              </span>
              {field.shadowed_by_env && (
                <span className="rounded-full bg-steel/15 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide text-steel">
                  set via env var
                </span>
              )}
            </div>
            <SettingsFieldInput
              field={field}
              value={values[field.name]}
              onChange={(value) => setValues((prev) => ({ ...prev, [field.name]: value }))}
              voiceOptions={field.name === "voice" ? voiceOptions : undefined}
            />
            {field.shadowed_by_env && (
              <p className="mt-1 text-xs text-steel">
                An environment variable is currently overriding this value. Saving here still
                updates what's stored, but it won't take effect until that variable is unset.
              </p>
            )}
          </div>
        ))}
      </div>

      <div className="mt-6">
        <button
          type="submit"
          disabled={saving}
          className="rounded-md bg-porch px-4 py-2 text-sm font-semibold text-ink transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {saving ? "Saving…" : "Save changes"}
        </button>
      </div>
    </form>
  )
}
