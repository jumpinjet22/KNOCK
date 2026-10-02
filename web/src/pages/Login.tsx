import { useState, type FormEvent } from "react"
import { useLocation, useNavigate } from "react-router-dom"
import { AuthCard, FormError, FormField, SubmitButton } from "../components/AuthCard"
import { ApiError } from "../lib/api"
import { useAuth } from "../lib/auth"

export function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(username, password)
      const from = (location.state as { from?: Location })?.from
      navigate(from?.pathname ?? "/", { replace: true })
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Incorrect username or password.")
      } else {
        setError("Something went wrong signing in. Try again.")
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AuthCard eyebrow="Sign in">
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
            autoComplete="current-password"
          />
        </div>
        <div className="mt-6">
          <SubmitButton disabled={submitting}>{submitting ? "Signing in…" : "Sign in"}</SubmitButton>
        </div>
      </form>
    </AuthCard>
  )
}
