import type { ReactNode } from "react"
import { Wordmark } from "./Wordmark"

/** Shared centered-card chrome for the Login and First-run Setup pages. */
export function AuthCard({
  eyebrow,
  children,
}: {
  eyebrow: string
  children: ReactNode
}) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-mist px-4 py-12 dark:bg-ink">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center gap-3">
          <Wordmark className="h-9 w-auto" />
          <p className="font-mono text-xs uppercase tracking-widest text-steel">{eyebrow}</p>
        </div>
        <div className="rounded-lg border border-steel/20 bg-paper p-6 shadow-sm dark:border-steel/30 dark:bg-dusk">
          {children}
        </div>
      </div>
    </div>
  )
}

export function FormField({
  label,
  type,
  value,
  onChange,
  autoFocus,
  autoComplete,
  minLength,
  helpText,
}: {
  label: string
  type: "text" | "password"
  value: string
  onChange: (value: string) => void
  autoFocus?: boolean
  autoComplete?: string
  minLength?: number
  helpText?: string
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-ink dark:text-mist">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoFocus={autoFocus}
        autoComplete={autoComplete}
        minLength={minLength}
        required
        className="w-full rounded-md border border-steel/30 bg-white px-3 py-2 text-sm text-ink outline-none focus:border-porch focus:ring-1 focus:ring-porch dark:border-steel/40 dark:bg-ink dark:text-mist"
      />
      {helpText && <span className="mt-1 block text-xs text-steel">{helpText}</span>}
    </label>
  )
}

export function SubmitButton({ children, disabled }: { children: ReactNode; disabled?: boolean }) {
  return (
    <button
      type="submit"
      disabled={disabled}
      className="w-full rounded-md bg-porch px-4 py-2 text-sm font-semibold text-ink transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60"
    >
      {children}
    </button>
  )
}

export function FormError({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <p className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
      {message}
    </p>
  )
}
