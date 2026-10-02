import type { SettingsField } from "../lib/api"

interface Props {
  field: SettingsField
  value: unknown
  onChange: (value: unknown) => void
  /** Kokoro's `voice` field only: live options from the Wyoming server. */
  voiceOptions?: string[]
}

const baseInputClass =
  "w-full rounded-md border border-steel/30 bg-white px-3 py-2 text-sm text-ink outline-none focus:border-porch focus:ring-1 focus:ring-porch dark:border-steel/40 dark:bg-ink dark:text-mist"

export function SettingsFieldInput({ field, value, onChange, voiceOptions }: Props) {
  if (field.name === "voice" && voiceOptions && voiceOptions.length > 0) {
    return (
      <select
        value={typeof value === "string" ? value : ""}
        onChange={(event) => onChange(event.target.value || null)}
        className={baseInputClass}
      >
        <option value="">(server default)</option>
        {voiceOptions.map((voice) => (
          <option key={voice} value={voice}>
            {voice}
          </option>
        ))}
      </select>
    )
  }

  switch (field.type) {
    case "bool":
      return (
        <label className="inline-flex items-center gap-2">
          <input
            type="checkbox"
            checked={Boolean(value)}
            onChange={(event) => onChange(event.target.checked)}
            className="h-4 w-4 accent-porch"
          />
          <span className="text-sm text-steel">{value ? "Enabled" : "Disabled"}</span>
        </label>
      )

    case "int":
    case "float":
      return (
        <input
          type="number"
          step={field.type === "float" ? "any" : 1}
          value={typeof value === "number" ? value : ""}
          onChange={(event) =>
            onChange(event.target.value === "" ? null : Number(event.target.value))
          }
          className={baseInputClass}
        />
      )

    case "text":
      return (
        <textarea
          rows={4}
          value={typeof value === "string" ? value : ""}
          onChange={(event) => onChange(event.target.value)}
          className={baseInputClass}
        />
      )

    case "list_string": {
      const asText = Array.isArray(value) ? value.join(", ") : ""
      return (
        <input
          type="text"
          value={asText}
          onChange={(event) =>
            onChange(
              event.target.value
                .split(",")
                .map((item) => item.trim())
                .filter((item) => item.length > 0),
            )
          }
          placeholder="comma, separated, values"
          className={baseInputClass}
        />
      )
    }

    case "secret":
      return (
        <input
          type="password"
          value={typeof value === "string" ? value : ""}
          onChange={(event) => onChange(event.target.value)}
          placeholder={field.has_value ? "•••••••• (unchanged)" : "Not set"}
          autoComplete="off"
          className={baseInputClass}
        />
      )

    case "string":
    default:
      return (
        <input
          type="text"
          value={typeof value === "string" ? value : ""}
          onChange={(event) => onChange(event.target.value)}
          className={baseInputClass}
        />
      )
  }
}
