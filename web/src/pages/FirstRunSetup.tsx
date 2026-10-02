import { useState, type FormEvent } from "react"
import { useNavigate } from "react-router-dom"
import { AuthCard, FormError, FormField, SubmitButton } from "../components/AuthCard"
import { ApiError } from "../lib/api"
import { useAuth } from "../lib/auth"

const MIN_PASSWORD_LENGTH = 8

export function FirstRunSetup() {
  const { completeSetup } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await completeSetup(username, password)
      navigate("/", { replace: true })
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError("An admin account already exists -- sign in instead.")
      } else if (err instanceof ApiError && err.status === 422) {
        setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`)
      } else {
        setError("Something went wrong creating the account. Try again.")
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AuthCard eyebrow="Create your admin account">
      <form onSubmit={handleSubmit}>
        <FormError message={error} />
        <div className="space-y-4">
          <FormField
            label="Username"
            type="text"
            value={username}
            onChange={setUsername}
            autoFocus
            autoComplete="username"
          />
          <FormField
            label="Password"
            type="password"
            value={password}
            onChange={setPassword}
            autoComplete="new-password"
            minLength={MIN_PASSWORD_LENGTH}
            helpText={`At least ${MIN_PASSWORD_LENGTH} characters.`}
          />
        </div>
        <div className="mt-6">
          <SubmitButton disabled={submitting}>
            {submitting ? "Creating account…" : "Create account"}
          </SubmitButton>
        </div>
      </form>
    </AuthCard>
  )
}
