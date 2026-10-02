import { browserSupportsWebAuthn, startAuthentication } from "@simplewebauthn/browser"
import { useEffect, useState, type FormEvent } from "react"
import { useLocation, useNavigate } from "react-router-dom"
import { AuthCard, FormError, FormField, SubmitButton } from "../components/AuthCard"
import { ApiError, oauthApi, webauthnApi } from "../lib/api"
import { useAuth } from "../lib/auth"

export function Login() {
  const { login, refresh } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [googleConfigured, setGoogleConfigured] = useState(false)
  const [passkeysSupported] = useState(() => window.isSecureContext && browserSupportsWebAuthn())
  const [passkeyBusy, setPasskeyBusy] = useState(false)

  useEffect(() => {
    oauthApi
      .googleStatus()
      .then((status) => setGoogleConfigured(status.configured))
      .catch(() => setGoogleConfigured(false))
  }, [])

  function goToDestination() {
    const from = (location.state as { from?: Location })?.from
    navigate(from?.pathname ?? "/", { replace: true })
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(username, password)
      goToDestination()
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

  async function handlePasskeyLogin() {
    setError(null)
    setPasskeyBusy(true)
    try {
      const optionsJSON = await webauthnApi.loginOptions()
      const credential = await startAuthentication({ optionsJSON: optionsJSON as never })
      await webauthnApi.loginVerify(credential)
      await refresh()
      goToDestination()
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        setError("No passkeys are registered yet.")
      } else if (err instanceof ApiError) {
        setError("Passkey sign-in failed.")
      } else {
        // The user likely cancelled the browser's passkey prompt.
      }
    } finally {
      setPasskeyBusy(false)
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
      {(googleConfigured || passkeysSupported) && (
        <div className="mt-4 space-y-2 border-t border-steel/20 pt-4">
          {passkeysSupported && (
            <button
              type="button"
              disabled={passkeyBusy}
              onClick={() => void handlePasskeyLogin()}
              className="flex w-full items-center justify-center rounded-md border border-steel/30 px-4 py-2 text-sm font-medium text-ink transition hover:border-porch hover:text-porch disabled:cursor-not-allowed disabled:opacity-60 dark:border-steel/40 dark:text-mist"
            >
              {passkeyBusy ? "Waiting for passkey…" : "Sign in with a passkey"}
            </button>
          )}
          {googleConfigured && (
            <a
              href={oauthApi.googleLoginUrl}
              className="flex w-full items-center justify-center rounded-md border border-steel/30 px-4 py-2 text-sm font-medium text-ink transition hover:border-porch hover:text-porch dark:border-steel/40 dark:text-mist"
            >
              Sign in with Google
            </a>
          )}
        </div>
      )}
    </AuthCard>
  )
}
