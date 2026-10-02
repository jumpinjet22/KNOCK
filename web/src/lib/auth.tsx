import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react"
import { Navigate, useLocation } from "react-router-dom"
import { ApiError, authApi } from "./api"

type AuthState =
  | { status: "loading" }
  | { status: "setup_required" }
  | { status: "logged_out" }
  | { status: "logged_in"; username: string }

interface AuthContextValue {
  state: AuthState
  login: (username: string, password: string) => Promise<void>
  completeSetup: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" })

  const refresh = useCallback(async () => {
    try {
      const me = await authApi.me()
      setState({ status: "logged_in", username: me.username })
      return
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 401) {
        // Unexpected failure (network/server down) -- don't silently claim
        // setup is required, that's a different, misleading state.
        console.error("Failed to check current session", err)
      }
    }

    try {
      const status = await authApi.status()
      setState({ status: status.setup_required ? "setup_required" : "logged_out" })
    } catch (err) {
      console.error("Failed to check auth status", err)
      setState({ status: "logged_out" })
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const login = useCallback(async (username: string, password: string) => {
    const user = await authApi.login(username, password)
    setState({ status: "logged_in", username: user.username })
  }, [])

  const completeSetup = useCallback(async (username: string, password: string) => {
    const user = await authApi.setup(username, password)
    setState({ status: "logged_in", username: user.username })
  }, [])

  const logout = useCallback(async () => {
    await authApi.logout()
    setState({ status: "logged_out" })
  }, [])

  return (
    <AuthContext.Provider value={{ state, login, completeSetup, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider")
  }
  return ctx
}

/** Gates a route behind login, redirecting to /setup or /login as needed. */
export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { state } = useAuth()
  const location = useLocation()

  if (state.status === "loading") {
    return <AuthLoadingScreen />
  }
  if (state.status === "setup_required") {
    return <Navigate to="/setup" replace />
  }
  if (state.status === "logged_out") {
    return <Navigate to="/login" replace state={{ from: location }} />
  }
  return <>{children}</>
}

export function AuthLoadingScreen() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-mist dark:bg-ink">
      <p className="font-mono text-sm uppercase tracking-wide text-steel">Loading…</p>
    </div>
  )
}
