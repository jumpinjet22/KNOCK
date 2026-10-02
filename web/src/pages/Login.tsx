import { useEffect, useState, type FormEvent } from "react"
import { useLocation, useNavigate } from "react-router-dom"
import { AuthCard, FormError, FormField, SubmitButton } from "../components/AuthCard"
import { ApiError, oauthApi } from "../lib/api"
import { useAuth } from "../lib/auth"

export function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [googleConfigured, setGoogleConfigured] = useState(false)

  useEffect(() => {
    oauthApi
      .googleStatus()
      .then((status) => setGoogleConfigured(status.configured))
      .catch(() => setGoogleConfigured(false))
  }, [])

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
      {googleConfigured && (
        <div className="mt-4 border-t border-steel/20 pt-4">
          <a
            href={oauthApi.googleLoginUrl}
            className="flex w-full items-center justify-center rounded-md border border-steel/30 px-4 py-2 text-sm font-medium text-ink transition hover:border-porch hover:text-porch dark:border-steel/40 dark:text-mist"
          >
            Sign in with Google
          </a>
        </div>
      )}
    </AuthCard>
  )
}
