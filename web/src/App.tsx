import { Navigate, Route, Routes } from "react-router-dom"
import { AppShell } from "./components/AppShell"
import { AuthProvider, ProtectedRoute, useAuth } from "./lib/auth"
import { Dashboard } from "./pages/Dashboard"
import { FirstRunSetup } from "./pages/FirstRunSetup"
import { Login } from "./pages/Login"
import { Settings } from "./pages/Settings"

function SetupRoute() {
  const { state } = useAuth()
  // Once an admin exists, /setup isn't valid anymore -- bounce to login
  // rather than letting someone land on a dead-end "create account" form.
  if (state.status === "logged_out") {
    return <Navigate to="/login" replace />
  }
  if (state.status === "logged_in") {
    return <Navigate to="/" replace />
  }
  return <FirstRunSetup />
}

function LoginRoute() {
  const { state } = useAuth()
  if (state.status === "setup_required") {
    return <Navigate to="/setup" replace />
  }
  if (state.status === "logged_in") {
    return <Navigate to="/" replace />
  }
  return <Login />
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/setup" element={<SetupRoute />} />
        <Route path="/login" element={<LoginRoute />} />
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <AppShell>
                <Dashboard />
              </AppShell>
            </ProtectedRoute>
          }
        />
        <Route
          path="/settings/:section?"
          element={
            <ProtectedRoute>
              <AppShell>
                <Settings />
              </AppShell>
            </ProtectedRoute>
          }
        />
      </Routes>
    </AuthProvider>
  )
}
