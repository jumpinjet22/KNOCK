import type { DoorData } from "./api"

/** "Front Door!" -> "front-door"; unique among `taken`, max 64 chars. */
export function doorKeyFromName(name: string, taken: string[]): string {
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

export function emptyDoor(): DoorData {
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

export type Cameras = Record<string, string> // device id -> name

export function cameraLabel(
  cameras: Cameras,
  id: string | null,
  fallback = "the camera that rang",
): string {
  if (!id) return fallback
  return cameras[id] ?? id
}

/** The plain-English version of a door, shown on the list and while editing. */
export function describeDoor(door: DoorData, cameras: Cameras): string {
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

/** A friendly message when this door would share a trigger with another door. */
export function findConflict(door: DoorData, others: DoorData[], cameras: Cameras): string | null {
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

