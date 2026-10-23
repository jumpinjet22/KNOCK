import { describe, expect, it } from "vitest"
import type { DoorData } from "./api"
import { cameraLabel, describeDoor, doorKeyFromName, emptyDoor, findConflict } from "./doors"

function door(overrides: Partial<DoorData> = {}): DoorData {
  return { ...emptyDoor(), key: "front", name: "Front door", ...overrides }
}

describe("doorKeyFromName", () => {
  it("makes a lowercase, dash-separated key", () => {
    expect(doorKeyFromName("Front Door!", [])).toBe("front-door")
    expect(doorKeyFromName("  Side   gate  ", [])).toBe("side-gate")
  })

  it("only ever produces characters the server accepts for a key", () => {
    for (const name of ["../../etc/passwd", "a/b\\c", "é cam", "Front door 🚪", "x".repeat(200)]) {
      const key = doorKeyFromName(name, [])
      expect(key).toMatch(/^[a-z0-9-]{1,64}$/)
    }
  })

  it("falls back to 'door' when nothing usable is left", () => {
    expect(doorKeyFromName("!!!", [])).toBe("door")
    expect(doorKeyFromName("", [])).toBe("door")
  })

  it("adds a number until the key is unique", () => {
    expect(doorKeyFromName("Front door", ["front-door"])).toBe("front-door-2")
    expect(doorKeyFromName("Front door", ["front-door", "front-door-2"])).toBe("front-door-3")
  })

  it("stays within the server's 64-character limit even after a suffix", () => {
    const long = "x".repeat(200)
    const first = doorKeyFromName(long, [])
    expect(doorKeyFromName(long, [first]).length).toBeLessThanOrEqual(64)
  })
})

describe("emptyDoor", () => {
  it("has no triggers or devices, and a fresh object each time", () => {
    const a = emptyDoor()
    expect(a.triggers).toEqual({ protect_cameras: [], ha_entities: [] })
    expect(a.listen_device).toBeNull()
    a.triggers.protect_cameras.push("x")
    expect(emptyDoor().triggers.protect_cameras).toEqual([])
  })
})

describe("cameraLabel", () => {
  it("uses the camera name, else the raw ID, else the fallback", () => {
    expect(cameraLabel({ c1: "Porch" }, "c1")).toBe("Porch")
    expect(cameraLabel({}, "c9")).toBe("c9")
    expect(cameraLabel({}, null)).toBe("the camera that rang")
    expect(cameraLabel({}, null, "?")).toBe("?")
  })
})

describe("describeDoor", () => {
  const cameras = { a: "Entry", b: "Driveway" }

  it("defaults every role to the trigger camera", () => {
    const text = describeDoor(door({ triggers: { protect_cameras: ["a"], ha_entities: [] } }), cameras)
    expect(text).toBe(
      "When Entry fires: listens through Entry, talks through Entry, looks through Entry.",
    )
  })

  it("shows split roles and combines triggers with 'or'", () => {
    const text = describeDoor(
      door({
        triggers: { protect_cameras: ["a"], ha_entities: ["binary_sensor.knock"] },
        speak_device: "b",
      }),
      cameras,
    )
    expect(text).toBe(
      "When Entry or binary_sensor.knock fires: listens through Entry, talks through Driveway, looks through Entry.",
    )
  })

  it("looks through the first look camera when one is set", () => {
    const text = describeDoor(
      door({ triggers: { protect_cameras: ["a"], ha_entities: [] }, look_devices: ["b"] }),
      cameras,
    )
    expect(text).toContain("looks through Driveway")
  })

  it("asks for a camera when a door has no camera trigger", () => {
    const text = describeDoor(
      door({ triggers: { protect_cameras: [], ha_entities: ["binary_sensor.knock"] } }),
      cameras,
    )
    expect(text).toContain("listens through (choose a camera)")
    expect(text).not.toContain("the camera that rang")
  })

  it("does not break on a door with no triggers at all", () => {
    expect(describeDoor(door(), cameras)).toContain("When … fires")
  })
})

describe("findConflict", () => {
  const front = door({ triggers: { protect_cameras: ["a"], ha_entities: ["binary_sensor.k"] } })

  it("is null when nothing is shared", () => {
    const other = door({ key: "side", triggers: { protect_cameras: ["b"], ha_entities: [] } })
    expect(findConflict(other, [front], {})).toBeNull()
    expect(findConflict(other, [], {})).toBeNull()
  })

  it("names the camera and the door that already uses it", () => {
    const other = door({ key: "side", triggers: { protect_cameras: ["a"], ha_entities: [] } })
    expect(findConflict(other, [front], { a: "Entry" })).toBe(
      'Entry already starts "Front door". A camera can start only one door.',
    )
  })

  it("catches a shared Home Assistant sensor", () => {
    const other = door({
      key: "side",
      triggers: { protect_cameras: [], ha_entities: ["binary_sensor.k"] },
    })
    expect(findConflict(other, [front], {})).toBe(
      'binary_sensor.k already starts "Front door". A sensor can start only one door.',
    )
  })

  it("falls back to the key when the other door has no name", () => {
    const unnamed = door({ name: "", triggers: { protect_cameras: ["a"], ha_entities: [] } })
    const other = door({ key: "side", triggers: { protect_cameras: ["a"], ha_entities: [] } })
    expect(findConflict(other, [unnamed], {})).toContain('"front"')
  })
})
