import { type ReactNode } from "react"
import { NavLink, useNavigate } from "react-router-dom"
import { useAuth } from "../lib/auth"
import { Mark } from "./Wordmark"

const NAV_LINKS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/processes", label: "Processes", end: false },
  { to: "/debug", label: "Debug", end: false },
  { to: "/settings", label: "Settings", end: false },
]

export function AppShell({ children }: { children: ReactNode }) {
  const { state, logout } = useAuth()
  const navigate = useNavigate()
  const username = state.status === "logged_in" ? state.username : null

  async function handleLogout() {
    await logout()
    navigate("/login", { replace: true })
  }

  return (
    <div className="min-h-screen bg-mist dark:bg-ink">
      <header className="flex items-center justify-between border-b border-steel/20 bg-paper px-6 py-3 dark:border-steel/30 dark:bg-dusk">
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-2">
            <Mark className="h-7 w-7" />
            <span className="font-display text-lg font-black tracking-wide text-ink dark:text-mist">
              KNOCK
            </span>
          </div>
          <nav className="flex items-center gap-4">
            {NAV_LINKS.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                end={link.end}
                className={({ isActive }) =>
                  `text-sm font-medium transition ${
                    isActive
                      ? "text-porch"
                      : "text-steel hover:text-ink dark:hover:text-mist"
                  }`
                }
              >
                {link.label}
              </NavLink>
            ))}
          </nav>
        </div>
        {username && (
          <div className="flex items-center gap-3">
            <span className="font-mono text-xs text-steel">{username}</span>
            <button
              type="button"
              onClick={() => void handleLogout()}
              className="rounded-md border border-steel/30 px-3 py-1 text-xs font-medium text-ink transition hover:border-porch hover:text-porch dark:border-steel/40 dark:text-mist"
            >
              Sign out
            </button>
          </div>
        )}
      </header>
      <main className="mx-auto max-w-5xl px-6 py-8">{children}</main>
    </div>
  )
}
