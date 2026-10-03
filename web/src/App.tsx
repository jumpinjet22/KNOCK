import { Navigate, Route, Routes } from "react-router-dom"
import { AppShell } from "./components/AppShell"
import { AuthProvider, ProtectedRoute, useAuth } from "./lib/auth"
import { Cameras } from "./pages/Cameras"
import { Dashboard } from "./pages/Dashboard"
import { Debug } from "./pages/Debug"
import { FirstRunSetup } from "./pages/FirstRunSetup"
import { History } from "./pages/History"
import { Login } from "./pages/Login"
import { Processes } from "./pages/Processes"
import { Settings } from "./pages/Settings"
import { Training } from "./pages/Training"

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
        <Route
          path="/debug"
          element={
            <ProtectedRoute>
              <AppShell>
                <Debug />
              </AppShell>
            </ProtectedRoute>
          }
        />
        <Route
          path="/processes"
          element={
            <ProtectedRoute>
              <AppShell>
                <Processes />
              </AppShell>
            </ProtectedRoute>
          }
        />
        <Route
          path="/cameras"
          element={
            <ProtectedRoute>
              <AppShell>
                <Cameras />
              </AppShell>
            </ProtectedRoute>
          }
        />
        <Route
          path="/history"
          element={
            <ProtectedRoute>
              <AppShell>
                <History />
              </AppShell>
            </ProtectedRoute>
          }
        />
        <Route
          path="/training"
          element={
            <ProtectedRoute>
              <AppShell>
                <Training />
              </AppShell>
            </ProtectedRoute>
          }
        />
      </Routes>
    </AuthProvider>
  )
}
