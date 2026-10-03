import { createContext, useContext, useEffect, useState, type ReactNode } from "react"
import { trainingApi } from "./api"

interface UIModeValue {
  trainingOnly: boolean
}

// Defaults to the full UI -- if the fetch is slow, fails, or hasn't
// resolved yet, showing everything is the safe failure mode, not
// silently locking a normal deployment down to just /training.
const UIModeContext = createContext<UIModeValue>({ trainingOnly: false })

export function UIModeProvider({ children }: { children: ReactNode }) {
  const [trainingOnly, setTrainingOnly] = useState(false)

  useEffect(() => {
    trainingApi
      .uiMode()
      .then((mode) => setTrainingOnly(mode.training_only))
      .catch(() => setTrainingOnly(false))
  }, [])

  return <UIModeContext.Provider value={{ trainingOnly }}>{children}</UIModeContext.Provider>
}

export function useUIMode(): UIModeValue {
  return useContext(UIModeContext)
}
