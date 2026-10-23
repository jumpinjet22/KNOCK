import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError, doorsApi, videoApi, type DoorData, type DoorsResponse } from "../lib/api"
import { DoorsSettings } from "./DoorsSettings"

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>()
  return {
    ...actual,
    doorsApi: { get: vi.fn(), update: vi.fn() },
    videoApi: { ...actual.videoApi, unifiCameras: vi.fn() },
  }
})

const get = vi.mocked(doorsApi.get)
const update = vi.mocked(doorsApi.update)
const unifiCameras = vi.mocked(videoApi.unifiCameras)

const CAMERAS = [
  { device_id: "a", name: "Entry", is_connected: true },
  { device_id: "b", name: "Driveway", is_connected: true },
  { device_id: "c", name: "Garage", is_connected: false },
]

function door(overrides: Partial<DoorData> = {}): DoorData {
  return {
    key: "front-door",
    name: "Front door",
    triggers: { protect_cameras: ["a"], ha_entities: [] },
    listen_device: null,
    speak_device: null,
    look_devices: [],
    call_targets: [],
    ...overrides,
  }
}

function serve(doors: DoorData[], extra: Partial<DoorsResponse> = {}) {
  get.mockResolvedValue({ doors, shadowed_by_env: false, error: null, ...extra })
  // Echo what was saved, like the real endpoint does.
  update.mockImplementation(async (next) => ({ doors: next, shadowed_by_env: false, error: null }))
}

beforeEach(() => {
  vi.resetAllMocks()
  unifiCameras.mockResolvedValue(CAMERAS)
})

describe("the list of doors", () => {
  it("explains the default when there are no doors", async () => {
    serve([])
    render(<DoorsSettings />)

    expect(await screen.findByText(/No custom doors/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Add a door" })).toBeInTheDocument()
  })

  it("shows each door as a plain-English sentence", async () => {
    serve([door({ speak_device: "b" })])
    render(<DoorsSettings />)

    expect(await screen.findByText("Front door")).toBeInTheDocument()
    expect(
      await screen.findByText(
        "When Entry fires: listens through Entry, talks through Driveway, looks through Entry.",
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument()
  })

  it("is read-only while KNOCK_DOORS is set in the environment", async () => {
    serve([door()], { shadowed_by_env: true })
    render(<DoorsSettings />)

    expect(await screen.findByText(/can't be edited here/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Add a door" })).not.toBeInTheDocument()
  })

  it("reports saved doors that couldn't be read, without hiding the page", async () => {
    serve([], { error: "door key must be 1-64 characters" })
    render(<DoorsSettings />)

    expect(await screen.findByText(/Couldn't read the saved doors/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Add a door" })).toBeInTheDocument()
  })

  it("shows the error when the doors can't be loaded at all", async () => {
    get.mockRejectedValue(new ApiError(500, "boom"))
    render(<DoorsSettings />)

    expect(await screen.findByText("boom")).toBeInTheDocument()
  })
})

describe("adding a door", () => {
  async function openNewDoor() {
    const user = userEvent.setup()
    render(<DoorsSettings />)
    await user.click(await screen.findByRole("button", { name: "Add a door" }))
    return user
  }

  it("saves exactly what was entered, with a key made from the name", async () => {
    serve([])
    const user = await openNewDoor()

    await user.type(screen.getByPlaceholderText("Front door"), "Front door")
    await user.click(await screen.findByRole("checkbox", { name: "Entry" }))
    await user.type(screen.getByPlaceholderText("binary_sensor.front_door_knock"), "binary_sensor.k")
    await user.click(screen.getByRole("button", { name: "Add sensor" }))
    await user.selectOptions(screen.getByLabelText("Talks through (speaker)"), "b")
    await user.type(screen.getByPlaceholderText("Extension, e.g. 101"), "202")
    await user.click(screen.getByRole("button", { name: "Add number" }))
    await user.click(screen.getByRole("button", { name: "Save door" }))

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    expect(update).toHaveBeenCalledWith([
      {
        key: "front-door",
        name: "Front door",
        triggers: { protect_cameras: ["a"], ha_entities: ["binary_sensor.k"] },
        listen_device: null,
        speak_device: "b",
        look_devices: [],
        call_targets: ["202"],
      },
    ])
    expect(await screen.findByText(/Restart the UniFi bridge/)).toBeInTheDocument()
  })

  it("requires a name and does not call the server without one", async () => {
    serve([])
    const user = await openNewDoor()

    await user.click(screen.getByRole("button", { name: "Save door" }))

    expect(await screen.findByText("Give the door a name.")).toBeInTheDocument()
    expect(update).not.toHaveBeenCalled()
  })

  it("refuses a camera another door already starts, naming both", async () => {
    serve([door()])
    const user = userEvent.setup()
    render(<DoorsSettings />)
    await user.click(await screen.findByRole("button", { name: "Add a door" }))

    await user.type(screen.getByPlaceholderText("Front door"), "Side gate")
    await user.click(await screen.findByRole("checkbox", { name: "Entry" }))
    await user.click(screen.getByRole("button", { name: "Save door" }))

    expect(
      await screen.findByText('Entry already starts "Front door". A camera can start only one door.'),
    ).toBeInTheDocument()
    expect(update).not.toHaveBeenCalled()
  })

  it("gives a second door with the same name its own key", async () => {
    serve([door({ triggers: { protect_cameras: ["b"], ha_entities: [] } })])
    const user = userEvent.setup()
    render(<DoorsSettings />)
    await user.click(await screen.findByRole("button", { name: "Add a door" }))

    await user.type(screen.getByPlaceholderText("Front door"), "Front door")
    await user.click(await screen.findByRole("checkbox", { name: "Entry" }))
    await user.click(screen.getByRole("button", { name: "Save door" }))

    await waitFor(() => expect(update).toHaveBeenCalled())
    const saved = update.mock.calls[0][0]
    expect(saved.map((d) => d.key)).toEqual(["front-door", "front-door-2"])
  })

  it("shows the server's message and stays on the form when saving fails", async () => {
    serve([])
    update.mockRejectedValue(new ApiError(422, "door 'x' has no triggers"))
    const user = await openNewDoor()

    await user.type(screen.getByPlaceholderText("Front door"), "Front door")
    await user.click(screen.getByRole("button", { name: "Save door" }))

    expect(await screen.findByText("door 'x' has no triggers")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Save door" })).toBeEnabled()
    expect(screen.getByPlaceholderText("Front door")).toHaveValue("Front door")
  })

  it("asks for a camera instead of defaulting when only a sensor triggers the door", async () => {
    serve([])
    const user = await openNewDoor()

    expect(screen.getAllByRole("option", { name: "Choose a camera" })).toHaveLength(3)
    await user.click(await screen.findByRole("checkbox", { name: "Entry" }))
    expect(screen.getAllByRole("option", { name: "Same camera that rang" })).toHaveLength(3)
  })

  it("lets you type camera IDs when the console can't be reached", async () => {
    serve([])
    unifiCameras.mockRejectedValue(new ApiError(502, "console down"))
    const user = await openNewDoor()

    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument()
    await user.type(screen.getByPlaceholderText("Front door"), "Front door")
    await user.type(screen.getByPlaceholderText("Camera ID"), "cam-9")
    await user.click(screen.getByRole("button", { name: "Add camera" }))
    await user.click(screen.getByRole("button", { name: "Save door" }))

    await waitFor(() => expect(update).toHaveBeenCalled())
    expect(update.mock.calls[0][0][0].triggers.protect_cameras).toEqual(["cam-9"])
  })

  it("does not add the same sensor twice, and lets you remove one", async () => {
    serve([])
    const user = await openNewDoor()
    const input = screen.getByPlaceholderText("binary_sensor.front_door_knock")

    await user.type(input, "binary_sensor.k{Enter}")
    await user.type(input, "binary_sensor.k{Enter}")
    expect(screen.getAllByText("binary_sensor.k")).toHaveLength(1)

    await user.click(screen.getByRole("button", { name: "Remove binary_sensor.k" }))
    expect(screen.queryByText("binary_sensor.k")).not.toBeInTheDocument()
  })

  it("returns to the list on Cancel without saving", async () => {
    serve([])
    const user = await openNewDoor()

    await user.click(screen.getByRole("button", { name: "Cancel" }))

    expect(await screen.findByText(/No custom doors/)).toBeInTheDocument()
    expect(update).not.toHaveBeenCalled()
  })
})

describe("editing a door", () => {
  async function openEdit(existing: DoorData) {
    serve([existing])
    const user = userEvent.setup()
    render(<DoorsSettings />)
    await user.click(await screen.findByRole("button", { name: "Edit" }))
    return user
  }

  it("keeps the key when the name changes", async () => {
    const user = await openEdit(door())

    const name = screen.getByPlaceholderText("Front door")
    await user.clear(name)
    await user.type(name, "Main entrance")
    await user.click(screen.getByRole("button", { name: "Save door" }))

    await waitFor(() => expect(update).toHaveBeenCalled())
    const [saved] = update.mock.calls[0][0]
    expect(saved.key).toBe("front-door")
    expect(saved.name).toBe("Main entrance")
  })

  it("keeps extra look cameras set outside the page", async () => {
    const user = await openEdit(door({ look_devices: ["a", "c"] }))
    expect(screen.getByText(/Also looks through Garage/)).toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Save door" }))
    await waitFor(() => expect(update).toHaveBeenCalled())
    expect(update.mock.calls[0][0][0].look_devices).toEqual(["a", "c"])
  })

  it("puts a newly chosen main look camera first and keeps the rest", async () => {
    const user = await openEdit(door({ look_devices: ["a", "c"] }))

    await user.selectOptions(screen.getByLabelText("Looks through (photos)"), "b")
    await user.click(screen.getByRole("button", { name: "Save door" }))

    await waitFor(() => expect(update).toHaveBeenCalled())
    expect(update.mock.calls[0][0][0].look_devices).toEqual(["b", "c"])
  })

  it("only deletes after an explicit confirmation", async () => {
    const user = await openEdit(door())

    await user.click(screen.getByRole("button", { name: "Delete door" }))
    expect(update).not.toHaveBeenCalled()
    await user.click(screen.getByRole("button", { name: "Keep" }))
    expect(screen.getByRole("button", { name: "Delete door" })).toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Delete door" }))
    await user.click(screen.getByRole("button", { name: "Yes, delete" }))

    await waitFor(() => expect(update).toHaveBeenCalledWith([]))
    expect(await screen.findByText(/No custom doors/)).toBeInTheDocument()
  })

  it("only changes the door being edited when there are several", async () => {
    const side = door({
      key: "side",
      name: "Side gate",
      triggers: { protect_cameras: ["b"], ha_entities: [] },
    })
    serve([door(), side])
    const user = userEvent.setup()
    render(<DoorsSettings />)
    const rows = await screen.findAllByRole("listitem")
    await user.click(within(rows[1]).getByRole("button", { name: "Edit" }))

    const name = screen.getByPlaceholderText("Front door")
    await user.clear(name)
    await user.type(name, "Back gate")
    await user.click(screen.getByRole("button", { name: "Save door" }))

    await waitFor(() => expect(update).toHaveBeenCalled())
    const saved = update.mock.calls[0][0]
    expect(saved.map((d) => d.name)).toEqual(["Front door", "Back gate"])
    expect(saved.map((d) => d.key)).toEqual(["front-door", "side"])
  })
})
