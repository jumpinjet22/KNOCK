import { useState, type ReactNode } from "react"
import { NavLink, useNavigate } from "react-router-dom"
import { useAuth } from "../lib/auth"
import { Mark } from "./Wordmark"

const NAV_LINKS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/cameras", label: "Cameras", end: false },
  { to: "/processes", label: "Processes", end: false },
  { to: "/history", label: "History", end: false },
  { to: "/training", label: "Training", end: false },
  { to: "/debug", label: "Debug", end: false },
  { to: "/settings", label: "Settings", end: false },
]

export function AppShell({ children }: { children: ReactNode }) {
  const { state, logout } = useAuth()
  const navigate = useNavigate()
  const username = state.status === "logged_in" ? state.username : null
  const [menuOpen, setMenuOpen] = useState(false)

  async function handleLogout() {
    await logout()
    navigate("/login", { replace: true })
  }

  const navLinkClassName = ({ isActive }: { isActive: boolean }) =>
    `text-sm font-medium transition ${
      isActive ? "text-porch" : "text-steel hover:text-ink dark:hover:text-mist"
    }`

  return (
    <div className="min-h-screen bg-mist dark:bg-ink">
      <header className="border-b border-steel/20 bg-paper px-4 py-3 dark:border-steel/30 dark:bg-dusk sm:px-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-6">
            <div className="flex items-center gap-2">
              <Mark className="h-7 w-7" />
              <span className="font-display text-lg font-black tracking-wide text-ink dark:text-mist">
                KNOCK
              </span>
            </div>
            <nav className="hidden items-center gap-4 md:flex">
              {NAV_LINKS.map((link) => (
                <NavLink key={link.to} to={link.to} end={link.end} className={navLinkClassName}>
                  {link.label}
                </NavLink>
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-3">
            {username && (
              <span className="hidden font-mono text-xs text-steel sm:inline">{username}</span>
            )}
            {username && (
              <button
                type="button"
                onClick={() => void handleLogout()}
                className="hidden rounded-md border border-steel/30 px-3 py-1 text-xs font-medium text-ink transition hover:border-porch hover:text-porch dark:border-steel/40 dark:text-mist md:inline-block"
              >
                Sign out
              </button>
            )}
            <button
              type="button"
              aria-label={menuOpen ? "Close menu" : "Open menu"}
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((open) => !open)}
              className="flex h-9 w-9 items-center justify-center rounded-md border border-steel/30 text-ink dark:border-steel/40 dark:text-mist md:hidden"
            >
              <span className="sr-only">{menuOpen ? "Close menu" : "Open menu"}</span>
              {menuOpen ? (
                <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M6 18 18 6M6 6l12 12"
                  />
                </svg>
              ) : (
                <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M4 6h16M4 12h16M4 18h16"
                  />
                </svg>
              )}
            </button>
          </div>
        </div>
        {menuOpen && (
          <nav className="mt-3 flex flex-col gap-3 border-t border-steel/20 pt-3 dark:border-steel/30 md:hidden">
            {NAV_LINKS.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                end={link.end}
                onClick={() => setMenuOpen(false)}
                className={navLinkClassName}
              >
                {link.label}
              </NavLink>
            ))}
            {username && (
              <div className="flex items-center justify-between border-t border-steel/20 pt-3 dark:border-steel/30">
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
          </nav>
        )}
      </header>
      <main className="mx-auto max-w-5xl px-4 py-6 sm:px-6 sm:py-8">{children}</main>
    </div>
  )
}
