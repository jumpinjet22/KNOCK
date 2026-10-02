import { useState } from "react"
import {
  ApiError,
  debugApi,
  type ConversationSimulateResult,
  type LLMGenerateResult,
  type STTTranscribeResult,
  type TTSSynthesizeResult,
  type VisionDescribeResult,
} from "../lib/api"

type TabKey = "vision" | "llm" | "stt" | "tts" | "conversation"

const TABS: { key: TabKey; label: string }[] = [
  { key: "vision", label: "Vision" },
  { key: "llm", label: "LLM" },
  { key: "stt", label: "Speech-to-text" },
  { key: "tts", label: "Text-to-speech" },
  { key: "conversation", label: "Conversation simulator" },
]

const inputClass =
  "w-full rounded-md border border-steel/30 bg-white px-3 py-2 text-sm text-ink outline-none focus:border-porch focus:ring-1 focus:ring-porch dark:border-steel/40 dark:bg-ink dark:text-mist"

const buttonClass =
  "rounded-md bg-porch px-4 py-2 text-sm font-semibold text-ink transition hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-60"

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail)
  }
  return "Request failed."
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = reader.result as string
      resolve(result.slice(result.indexOf(",") + 1))
    }
    reader.onerror = () => reject(reader.error ?? new Error("failed to read file"))
    reader.readAsDataURL(file)
  })
}

export function Debug() {
  const [tab, setTab] = useState<TabKey>("vision")

  return (
    <div className="flex gap-8">
      <nav className="w-52 shrink-0">
        <ul className="space-y-1">
          {TABS.map((item) => (
            <li key={item.key}>
              <button
                type="button"
                onClick={() => setTab(item.key)}
                className={`w-full rounded-md px-3 py-2 text-left text-sm font-medium transition ${
                  tab === item.key
                    ? "bg-porch/15 text-ink dark:text-mist"
                    : "text-steel hover:bg-steel/10 hover:text-ink dark:hover:text-mist"
                }`}
              >
                {item.label}
              </button>
            </li>
          ))}
        </ul>
      </nav>
      <div className="min-w-0 flex-1">
        {tab === "vision" && <VisionPanel />}
        {tab === "llm" && <LLMPanel />}
        {tab === "stt" && <STTPanel />}
        {tab === "tts" && <TTSPanel />}
        {tab === "conversation" && <ConversationPanel />}
      </div>
    </div>
  )
}

function PanelHeading({ title, description }: { title: string; description: string }) {
  return (
    <div className="mb-6">
      <h1 className="font-display text-2xl font-black text-ink dark:text-mist">{title}</h1>
      <p className="mt-1 text-sm text-steel">{description}</p>
    </div>
  )
}

function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <p className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
      {message}
    </p>
  )
}

function VisionPanel() {
  const [imagePreview, setImagePreview] = useState<string | null>(null)
  const [imageBase64, setImageBase64] = useState<string | null>(null)
  const [prompt, setPrompt] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<VisionDescribeResult | null>(null)

  async function handleFile(file: File | undefined) {
    if (!file) return
    setImagePreview(URL.createObjectURL(file))
    setImageBase64(await fileToBase64(file))
    setResult(null)
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    if (!imageBase64) return
    setLoading(true)
    setError(null)
    try {
      setResult(await debugApi.describeVision(imageBase64, prompt || undefined))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <PanelHeading
        title="Vision"
        description="Send a snapshot to the configured vision model and compare its raw output against the safety-filtered version a visitor would actually hear."
      />
      <form onSubmit={(event) => void handleSubmit(event)} className="space-y-4">
        <input
          type="file"
          accept="image/*"
          onChange={(event) => void handleFile(event.target.files?.[0])}
          className={inputClass}
        />
        {imagePreview && (
          <img src={imagePreview} alt="Selected snapshot" className="max-h-64 rounded-md border border-steel/20" />
        )}
        <input
          type="text"
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder="Prompt override (optional — uses the configured default prompt otherwise)"
          className={inputClass}
        />
        <button type="submit" disabled={!imageBase64 || loading} className={buttonClass}>
          {loading ? "Describing…" : "Describe image"}
        </button>
      </form>
      <ErrorBanner message={error} />
      {result && (
        <div className="mt-6 space-y-4">
          <ResultField label="Raw model output" value={result.raw} />
          <ResultField label="Sanitized (what a visitor would hear)" value={result.sanitized} />
          <p className="text-sm text-steel">
            Alarming language detected:{" "}
            <span className={result.alarming_language_detected ? "font-semibold text-red-600 dark:text-red-400" : ""}>
              {result.alarming_language_detected ? "yes" : "no"}
            </span>
            {" · "}
            {result.latency_ms.toFixed(0)} ms
          </p>
        </div>
      )}
    </div>
  )
}

function LLMPanel() {
  const [prompt, setPrompt] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<LLMGenerateResult | null>(null)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    if (!prompt.trim()) return
    setLoading(true)
    setError(null)
    try {
      setResult(await debugApi.generateLLM(prompt))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <PanelHeading
        title="LLM"
        description="Send a raw prompt straight to the configured Ollama model, bypassing the conversation policy entirely."
      />
      <form onSubmit={(event) => void handleSubmit(event)} className="space-y-4">
        <textarea
          rows={4}
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder="Type a prompt…"
          className={inputClass}
        />
        <button type="submit" disabled={!prompt.trim() || loading} className={buttonClass}>
          {loading ? "Generating…" : "Generate"}
        </button>
      </form>
      <ErrorBanner message={error} />
      {result && (
        <div className="mt-6 space-y-2">
          <ResultField label="Response" value={result.response} />
          <p className="text-sm text-steel">{result.latency_ms.toFixed(0)} ms</p>
        </div>
      )}
    </div>
  )
}

function STTPanel() {
  const [fileName, setFileName] = useState<string | null>(null)
  const [audioBase64, setAudioBase64] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<STTTranscribeResult | null>(null)

  async function handleFile(file: File | undefined) {
    if (!file) return
    setFileName(file.name)
    setAudioBase64(await fileToBase64(file))
    setResult(null)
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    if (!audioBase64) return
    setLoading(true)
    setError(null)
    try {
      setResult(await debugApi.transcribeSTT(audioBase64))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <PanelHeading
        title="Speech-to-text"
        description="Upload a WAV recording and send it to the configured Whisper server — the same path a real doorbell visit uses."
      />
      <form onSubmit={(event) => void handleSubmit(event)} className="space-y-4">
        <input
          type="file"
          accept="audio/wav,.wav"
          onChange={(event) => void handleFile(event.target.files?.[0])}
          className={inputClass}
        />
        {fileName && <p className="text-sm text-steel">Selected: {fileName}</p>}
        <button type="submit" disabled={!audioBase64 || loading} className={buttonClass}>
          {loading ? "Transcribing…" : "Transcribe"}
        </button>
      </form>
      <ErrorBanner message={error} />
      {result && (
        <div className="mt-6 space-y-2">
          <ResultField label="Transcript" value={result.transcript} />
          <p className="text-sm text-steel">{result.latency_ms.toFixed(0)} ms</p>
        </div>
      )}
    </div>
  )
}

function TTSPanel() {
  const [text, setText] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<TTSSynthesizeResult | null>(null)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    if (!text.trim()) return
    setLoading(true)
    setError(null)
    try {
      setResult(await debugApi.synthesizeTTS(text))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <PanelHeading
        title="Text-to-speech"
        description="Synthesize text with the configured Kokoro voice and play it back right here."
      />
      <form onSubmit={(event) => void handleSubmit(event)} className="space-y-4">
        <textarea
          rows={3}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Type what the voice should say…"
          className={inputClass}
        />
        <button type="submit" disabled={!text.trim() || loading} className={buttonClass}>
          {loading ? "Synthesizing…" : "Synthesize"}
        </button>
      </form>
      <ErrorBanner message={error} />
      {result && (
        <div className="mt-6 space-y-2">
          <audio controls src={`data:audio/wav;base64,${result.audio_wav_base64}`} className="w-full" />
          <p className="text-sm text-steel">
            {result.rate} Hz · {result.channels === 1 ? "mono" : `${result.channels}ch`} ·{" "}
            {result.latency_ms.toFixed(0)} ms
          </p>
        </div>
      )}
    </div>
  )
}

function ConversationPanel() {
  const [text, setText] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ConversationSimulateResult | null>(null)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    if (!text.trim()) return
    setLoading(true)
    setError(null)
    try {
      setResult(await debugApi.simulateConversation(text))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <PanelHeading
        title="Conversation simulator"
        description="Type what a visitor might say and see exactly how the policy engine and orchestrator respond — a browser version of the CLI, no hardware required."
      />
      <form onSubmit={(event) => void handleSubmit(event)} className="space-y-4">
        <input
          type="text"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="e.g. I have a package for you"
          className={inputClass}
        />
        <button type="submit" disabled={!text.trim() || loading} className={buttonClass}>
          {loading ? "Simulating…" : "Simulate"}
        </button>
      </form>
      <ErrorBanner message={error} />
      {result && (
        <div className="mt-6 space-y-4">
          <ResultField label="Response text" value={result.decision.text} />
          <div className="grid grid-cols-2 gap-4 text-sm">
            <Fact label="Reason" value={result.decision.reason} />
            <Fact label="Escalate" value={result.decision.escalate ? "yes" : "no"} />
            <Fact label="Intent" value={result.intent} />
            <Fact label="Policy allowed" value={result.policy_allowed ? "yes" : "no"} />
            <Fact label="Matched rules" value={result.matched_rule_ids.join(", ") || "none"} />
            <Fact label="Flags" value={result.policy_flags.join(", ") || "none"} />
          </div>
          {Object.keys(result.policy_confidence).length > 0 && (
            <ResultField
              label="Confidence scores"
              value={Object.entries(result.policy_confidence)
                .map(([flag, score]) => `${flag}: ${score.toFixed(2)}`)
                .join("\n")}
            />
          )}
        </div>
      )}
    </div>
  )
}

function ResultField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="mb-1 text-xs font-medium uppercase tracking-wide text-steel">{label}</p>
      <pre className="whitespace-pre-wrap rounded-md border border-steel/20 bg-white px-3 py-2 text-sm text-ink dark:bg-dusk dark:text-mist">
        {value}
      </pre>
    </div>
  )
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-steel">{label}: </span>
      <span className="font-medium text-ink dark:text-mist">{value}</span>
    </div>
  )
}
