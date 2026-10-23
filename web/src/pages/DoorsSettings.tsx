import { useEffect, useMemo, useState } from "react"
import {
  ApiError,
  doorsApi,
  videoApi,
  type DoorData,
  type UnifiCameraInfo,
} from "../lib/api"

const inputClass =
  "w-full rounded-md border border-steel/30 bg-white px-3 py-2 text-sm text-ink outline-none focus:border-porch focus:ring-1 focus:ring-porch dark:border-steel/40 dark:bg-ink dark:text-mist"
const primaryButton =
  "rounded-md bg-porch px-4 py-2 text-sm font-semibold text-ink transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60"
const quietButton =
  "rounded-md px-3 py-2 text-sm font-medium text-steel transition hover:bg-steel/10 hover:text-ink dark:hover:text-mist"
const errorBox =
  "rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300"

/** "Front Door!" -> "front-door"; unique among `taken`, max 64 chars. */
function doorKeyFromName(name: string, taken: string[]): string {
  const base =
    name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 56) || "door"
  let key = base
  for (let n = 2; taken.includes(key); n += 1) {
    key = `${base}-${n}`
  }
  return key
}

function emptyDoor(): DoorData {
  return {
    key: "",
    name: "",
    triggers: { protect_cameras: [], ha_entities: [] },
    listen_device: null,
    speak_device: null,
    look_devices: [],
    call_targets: [],
  }
}

/** A friendly message when this door would share a trigger with another door. */
function findConflict(door: DoorData, others: DoorData[], cameras: Cameras): string | null {
  for (const other of others) {
    const name = other.name || other.key
    for (const id of door.triggers.protect_cameras) {
      if (other.triggers.protect_cameras.includes(id)) {
        return `${cameraLabel(cameras, id)} already starts "${name}". A camera can start only one door.`
      }
    }
    for (const entity of door.triggers.ha_entities) {
      if (other.triggers.ha_entities.includes(entity)) {
        return `${entity} already starts "${name}". A sensor can start only one door.`
      }
    }
  }
  return null
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return typeof err.detail === "string" ? err.detail : "Some values are invalid."
  }
  return "Request failed."
}

type Cameras = Record<string, string> // device id -> name

function cameraLabel(
  cameras: Cameras,
  id: string | null,
  fallback = "the camera that rang",
): string {
  if (!id) return fallback
  return cameras[id] ?? id
}

/** The plain-English version of a door, shown on the list and while editing. */
function describeDoor(door: DoorData, cameras: Cameras): string {
  const triggers = [
    ...door.triggers.protect_cameras.map((id) => cameraLabel(cameras, id)),
    ...door.triggers.ha_entities,
  ]
  const base = door.look_devices[0] ?? door.triggers.protect_cameras[0] ?? null
  // No camera trigger means no "camera that rang" to fall back to.
  const unset = base ? "the camera that rang" : "(choose a camera)"
  const listen = cameraLabel(cameras, door.listen_device ?? base, unset)
  const speak = cameraLabel(cameras, door.speak_device ?? base, unset)
  const look = cameraLabel(cameras, base, unset)
  const when = triggers.length > 0 ? triggers.join(" or ") : "…"
  return `When ${when} fires: listens through ${listen}, talks through ${speak}, looks through ${look}.`
}

export function DoorsSettings() {
  const [doors, setDoors] = useState<DoorData[] | null>(null)
  const [shadowedByEnv, setShadowedByEnv] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [cameraList, setCameraList] = useState<UnifiCameraInfo[] | null>(null)
  // null = looking at the list; otherwise the one door being edited
  // (index === -1 means a brand-new door).
  const [editing, setEditing] = useState<{ index: number; door: DoorData } | null>(null)
  const [justSaved, setJustSaved] = useState(false)

  useEffect(() => {
    doorsApi
      .get()
      .then((data) => {
        setDoors(data.doors)
        setShadowedByEnv(data.shadowed_by_env)
        setLoadError(data.error)
      })
      .catch((err) => setLoadError(errorMessage(err)))
    videoApi
      .unifiCameras()
      .then(setCameraList)
      .catch(() => setCameraList(null))
  }, [])

  const cameras: Cameras = useMemo(
    () => Object.fromEntries((cameraList ?? []).map((camera) => [camera.device_id, camera.name])),
    [cameraList],
  )

  if (doors === null) {
    return loadError ? <p className={errorBox}>{loadError}</p> : <p className="text-sm text-steel">Loading…</p>
  }

  async function persist(next: DoorData[]): Promise<void> {
    const saved = await doorsApi.update(next)
    setDoors(saved.doors)
    setJustSaved(true)
  }

  if (editing) {
    const { index, door } = editing
    return (
      <DoorEditor
        initial={door}
        isNew={index === -1}
        otherDoors={doors.filter((_, i) => i !== index)}
        cameraList={cameraList}
        cameras={cameras}
        onCancel={() => setEditing(null)}
        onSave={async (saved) => {
          const next = [...doors]
          if (index === -1) next.push(saved)
          else next[index] = saved
          await persist(next)
          setEditing(null)
        }}
        onDelete={
          index === -1
            ? undefined
            : async () => {
                await persist(doors.filter((_, i) => i !== index))
                setEditing(null)
              }
        }
      />
    )
  }

  return (
    <div>
      <h1 className="font-display text-2xl font-black text-ink dark:text-mist">Doors</h1>
      <p className="mt-1 text-sm text-steel">
        By default every camera answers on its own. Add a door when one visit should use
        different cameras for listening, talking and looking, or when a sensor and a camera
        should share one conversation.
      </p>

      {loadError && <p className={`mt-4 ${errorBox}`}>Couldn't read the saved doors: {loadError}</p>}
      {shadowedByEnv && (
        <p className="mt-4 rounded-md bg-steel/15 px-3 py-2 text-sm text-ink dark:text-mist">
          These doors come from the <span className="font-mono">KNOCK_DOORS</span> environment
          variable, so they can't be edited here. Unset it to manage doors on this page.
        </p>
      )}
      {justSaved && (
        <p className="mt-4 rounded-md bg-porch/15 px-3 py-2 text-sm text-ink dark:text-mist">
          Saved. Restart the UniFi bridge (Processes page) for the change to take effect.
        </p>
      )}

      {doors.length === 0 ? (
        <p className="mt-6 text-sm text-steel">
          No custom doors. Every camera answers on its own, as it always has.
        </p>
      ) : (
        <ul className="mt-6 space-y-3">
          {doors.map((door, index) => (
            <li
              key={door.key}
              className="flex items-start justify-between gap-4 rounded-lg border border-steel/20 p-4"
            >
              <div className="min-w-0">
                <p className="text-sm font-semibold text-ink dark:text-mist">
                  {door.name || door.key}
                </p>
                <p className="mt-1 text-sm text-steel">{describeDoor(door, cameras)}</p>
              </div>
              {!shadowedByEnv && (
                <button
                  type="button"
                  className={quietButton}
                  onClick={() => {
                    setJustSaved(false)
                    setEditing({ index, door })
                  }}
                >
                  Edit
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {!shadowedByEnv && (
        <div className="mt-6">
          <button
            type="button"
            className={primaryButton}
            onClick={() => {
              setJustSaved(false)
              setEditing({ index: -1, door: emptyDoor() })
            }}
          >
            Add a door
          </button>
        </div>
      )}
    </div>
  )
}

function CameraPicker({
  value,
  onChange,
  cameraList,
  placeholder,
}: {
  value: string | null
  onChange: (value: string | null) => void
  cameraList: UnifiCameraInfo[] | null
  placeholder: string
}) {
  if (cameraList && cameraList.length > 0) {
    return (
      <select
        value={value ?? ""}
        onChange={(event) => onChange(event.target.value || null)}
        className={inputClass}
      >
        <option value="">{placeholder}</option>
        {cameraList.map((camera) => (
          <option key={camera.device_id} value={camera.device_id}>
            {camera.name}
            {camera.is_connected ? "" : " (offline)"}
          </option>
        ))}
      </select>
    )
  }
  // The console couldn't be reached: fall back to typing a camera ID.
  return (
    <input
      type="text"
      value={value ?? ""}
      onChange={(event) => onChange(event.target.value.trim() || null)}
      placeholder={`${placeholder} (camera ID)`}
      className={inputClass}
    />
  )
}

function TextList({
  values,
  onChange,
  placeholder,
  addLabel,
}: {
  values: string[]
  onChange: (values: string[]) => void
  placeholder: string
  addLabel: string
}) {
  const [draft, setDraft] = useState("")
  function add() {
    const value = draft.trim()
    if (value && !values.includes(value)) onChange([...values, value])
    setDraft("")
  }
  return (
    <div>
      {values.length > 0 && (
        <ul className="mb-2 flex flex-wrap gap-2">
          {values.map((value) => (
            <li
              key={value}
              className="flex items-center gap-1 rounded-full bg-steel/15 py-1 pl-3 pr-1 text-sm text-ink dark:text-mist"
            >
              <span className="font-mono text-xs">{value}</span>
              <button
                type="button"
                aria-label={`Remove ${value}`}
                onClick={() => onChange(values.filter((v) => v !== value))}
                className="rounded-full px-2 text-steel hover:text-ink dark:hover:text-mist"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex gap-2">
        <input
          type="text"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault()
              add()
            }
          }}
          placeholder={placeholder}
          className={inputClass}
        />
        <button type="button" onClick={add} className={quietButton}>
          {addLabel}
        </button>
      </div>
    </div>
  )
}

function Step({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="mt-6">
      <h2 className="text-sm font-semibold text-ink dark:text-mist">{title}</h2>
      {hint && <p className="mt-1 text-xs text-steel">{hint}</p>}
      <div className="mt-2">{children}</div>
    </section>
  )
}

function DoorEditor({
  initial,
  isNew,
  otherDoors,
  cameraList,
  cameras,
  onCancel,
  onSave,
  onDelete,
}: {
  initial: DoorData
  isNew: boolean
  otherDoors: DoorData[]
  cameraList: UnifiCameraInfo[] | null
  cameras: Cameras
  onCancel: () => void
  onSave: (door: DoorData) => Promise<void>
  onDelete?: () => Promise<void>
}) {
  const [door, setDoor] = useState<DoorData>(initial)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  const mainLook = door.look_devices[0] ?? null
  const extraLooks = door.look_devices.slice(1)
  const hasCamera = door.triggers.protect_cameras.length > 0
  // With no camera trigger there is no "camera that rang" to fall back to.
  const defaultLabel = hasCamera ? "Same camera that rang" : "Choose a camera"

  function toggleCamera(deviceId: string, checked: boolean) {
    const current = door.triggers.protect_cameras
    setDoor({
      ...door,
      triggers: {
        ...door.triggers,
        protect_cameras: checked ? [...current, deviceId] : current.filter((id) => id !== deviceId),
      },
    })
  }

  function setMainLook(value: string | null) {
    // Extra "look" cameras set outside this page (KNOCK_DOORS JSON) are kept.
    const rest = extraLooks.filter((id) => id !== value)
    setDoor({ ...door, look_devices: value ? [value, ...rest] : [] })
  }

  async function run(action: () => Promise<void>) {
    setSaving(true)
    setError(null)
    try {
      await action()
    } catch (err) {
      setError(errorMessage(err))
      setSaving(false)
    }
  }

  function handleSave(event: React.FormEvent) {
    event.preventDefault()
    const name = door.name.trim()
    if (!name) {
      setError("Give the door a name.")
      return
    }
    const conflict = findConflict(door, otherDoors, cameras)
    if (conflict) {
      setError(conflict)
      return
    }
    const key = isNew
      ? doorKeyFromName(
          name,
          otherDoors.map((d) => d.key),
        )
      : door.key
    void run(() => onSave({ ...door, name, key }))
  }

  return (
    <form onSubmit={handleSave}>
      <h1 className="font-display text-2xl font-black text-ink dark:text-mist">
        {isNew ? "New door" : door.name || door.key}
      </h1>

      {error && <p className={`mt-4 ${errorBox}`}>{error}</p>}

      <Step title="Name">
        <input
          type="text"
          value={door.name}
          onChange={(event) => setDoor({ ...door, name: event.target.value })}
          placeholder="Front door"
          className={inputClass}
        />
      </Step>

      <Step
        title="What starts a visit here?"
        hint="Any of these fires the door. A camera ring or person-detect, or a sensor like a knock detector."
      >
        {cameraList && cameraList.length > 0 ? (
          <div className="space-y-2 rounded-md border border-steel/30 p-3">
            {cameraList.map((camera) => (
              <label key={camera.device_id} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  aria-label={camera.name}
                  checked={door.triggers.protect_cameras.includes(camera.device_id)}
                  onChange={(event) => toggleCamera(camera.device_id, event.target.checked)}
                  className="h-4 w-4 accent-porch"
                />
                <span className="text-ink dark:text-mist">{camera.name}</span>
              </label>
            ))}
          </div>
        ) : (
          <TextList
            values={door.triggers.protect_cameras}
            onChange={(protect_cameras) =>
              setDoor({ ...door, triggers: { ...door.triggers, protect_cameras } })
            }
            placeholder="Camera ID"
            addLabel="Add camera"
          />
        )}
        <p className="mb-1 mt-4 text-xs text-steel">Home Assistant sensors (optional)</p>
        <TextList
          values={door.triggers.ha_entities}
          onChange={(ha_entities) => setDoor({ ...door, triggers: { ...door.triggers, ha_entities } })}
          placeholder="binary_sensor.front_door_knock"
          addLabel="Add sensor"
        />
      </Step>

      <Step
        title="Which camera does what?"
        hint={
          hasCamera
            ? "Leave a choice on the first option to use the camera that triggered the visit."
            : "This door has no camera trigger, so pick a camera for each job."
        }
      >
        <div className="space-y-3">
          <label className="block text-sm text-ink dark:text-mist">
            <span className="mb-1 block text-xs text-steel">Listens through (microphone)</span>
            <CameraPicker
              value={door.listen_device}
              onChange={(listen_device) => setDoor({ ...door, listen_device })}
              cameraList={cameraList}
              placeholder={defaultLabel}
            />
          </label>
          <label className="block text-sm text-ink dark:text-mist">
            <span className="mb-1 block text-xs text-steel">Talks through (speaker)</span>
            <CameraPicker
              value={door.speak_device}
              onChange={(speak_device) => setDoor({ ...door, speak_device })}
              cameraList={cameraList}
              placeholder={defaultLabel}
            />
          </label>
          <label className="block text-sm text-ink dark:text-mist">
            <span className="mb-1 block text-xs text-steel">Looks through (photos)</span>
            <CameraPicker
              value={mainLook}
              onChange={setMainLook}
              cameraList={cameraList}
              placeholder={defaultLabel}
            />
          </label>
          {extraLooks.length > 0 && (
            <p className="text-xs text-steel">
              Also looks through {extraLooks.map((id) => cameraLabel(cameras, id)).join(", ")} (set
              outside this page; kept when you save).
            </p>
          )}
        </div>
      </Step>

      <Step
        title="Phone numbers to call for this door (optional)"
        hint="Leave empty to use the phone list from the Phone calls (SIP) settings."
      >
        <TextList
          values={door.call_targets}
          onChange={(call_targets) => setDoor({ ...door, call_targets })}
          placeholder="Extension, e.g. 101"
          addLabel="Add number"
        />
      </Step>

      <p className="mt-6 rounded-md bg-steel/10 px-3 py-2 text-sm text-ink dark:text-mist">
        {describeDoor(door, cameras)}
      </p>

      <div className="mt-6 flex flex-wrap items-center gap-3">
        <button type="submit" disabled={saving} className={primaryButton}>
          {saving ? "Saving…" : "Save door"}
        </button>
        <button type="button" onClick={onCancel} disabled={saving} className={quietButton}>
          Cancel
        </button>
        {onDelete &&
          (confirmingDelete ? (
            <span className="ml-auto flex items-center gap-2 text-sm text-ink dark:text-mist">
              Delete this door?
              <button
                type="button"
                disabled={saving}
                onClick={() => void run(onDelete)}
                className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-60"
              >
                Yes, delete
              </button>
              <button type="button" onClick={() => setConfirmingDelete(false)} className={quietButton}>
                Keep
              </button>
            </span>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmingDelete(true)}
              className={`ml-auto ${quietButton}`}
            >
              Delete door
            </button>
          ))}
      </div>
    </form>
  )
}
