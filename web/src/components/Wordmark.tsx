/** The KNOCK wordmark, theme-aware via <picture> (same assets as the README/docs site). */
export function Wordmark({ className }: { className?: string }) {
  return (
    <picture>
      <source media="(prefers-color-scheme: dark)" srcSet="/brand/knock-wordmark-dark.svg" />
      <img src="/brand/knock-wordmark-light.svg" alt="KNOCK" className={className} />
    </picture>
  )
}

/** Just the mark (no wordmark text) -- for tight spaces like a nav rail. */
export function Mark({ className }: { className?: string }) {
  return (
    <picture>
      <source media="(prefers-color-scheme: dark)" srcSet="/brand/knock-mark-dark.svg" />
      <img src="/brand/knock-mark-light.svg" alt="KNOCK" className={className} />
    </picture>
  )
}
