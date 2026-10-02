/**
 * Thin fetch wrapper for the KNOCK API.
 *
 * Handles the two things every authenticated call needs and a hand-written
 * fetch() would forget somewhere: `credentials: "include"` (so the
 * httpOnly session cookie actually gets sent) and the CSRF double-submit
 * header (the backend's `starlette-csrf` middleware rejects any unsafe
 * request made while a session cookie is present unless it's echoed back).
 */

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `request failed with status ${status}`)
    this.status = status
    this.detail = detail
  }
}

function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match ? decodeURIComponent(match[1]) : null
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE"
  body?: unknown
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET"
  const headers: Record<string, string> = {}

  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json"
  }
  if (method !== "GET") {
    const csrfToken = readCookie("csrftoken")
    if (csrfToken) {
      headers["x-csrftoken"] = csrfToken
    }
  }

  const response = await fetch(path, {
    method,
    headers,
    credentials: "include",
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  })

  if (!response.ok) {
    let detail: unknown
    try {
      const data = await response.json()
      detail = data.detail ?? data
    } catch {
      detail = response.statusText
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

export interface AuthStatus {
  setup_required: boolean
}

export interface CurrentUser {
  username: string
}

export const authApi = {
  status: () => apiFetch<AuthStatus>("/api/auth/status"),
  setup: (username: string, password: string) =>
    apiFetch<CurrentUser>("/api/auth/setup", { method: "POST", body: { username, password } }),
  login: (username: string, password: string) =>
    apiFetch<CurrentUser>("/api/auth/login", { method: "POST", body: { username, password } }),
  logout: () => apiFetch<void>("/api/auth/logout", { method: "POST" }),
  me: () => apiFetch<CurrentUser>("/api/auth/me"),
}

export type SettingsFieldType =
  | "string"
  | "text"
  | "int"
  | "float"
  | "bool"
  | "list_string"
  | "secret"

export interface SettingsField {
  name: string
  type: SettingsFieldType
  value: unknown
  has_value: boolean | null
  shadowed_by_env: boolean
}

export interface SectionSettings {
  section: string
  fields: SettingsField[]
}

export interface KokoroVoices {
  voices: string[]
  error: string | null
}

export const SETTINGS_SECTIONS: { key: string; label: string }[] = [
  { key: "ollama", label: "Ollama (LLM)" },
  { key: "vision", label: "Vision" },
  { key: "whisper", label: "Whisper (STT)" },
  { key: "kokoro", label: "Kokoro (TTS)" },
  { key: "mqtt", label: "MQTT" },
  { key: "frigate", label: "Frigate" },
  { key: "homeassistant", label: "Home Assistant" },
  { key: "unifi", label: "UniFi Protect" },
]

export const settingsApi = {
  get: (section: string) => apiFetch<SectionSettings>(`/api/settings/${section}`),
  update: (section: string, values: Record<string, unknown>) =>
    apiFetch<SectionSettings>(`/api/settings/${section}`, { method: "PUT", body: { values } }),
  kokoroVoices: () => apiFetch<KokoroVoices>("/api/settings/kokoro/voices"),
}

export interface VisionDescribeResult {
  raw: string
  sanitized: string
  alarming_language_detected: boolean
  latency_ms: number
}

export interface LLMGenerateResult {
  response: string
  latency_ms: number
}

export interface STTTranscribeResult {
  transcript: string
  latency_ms: number
}

export interface TTSSynthesizeResult {
  audio_wav_base64: string
  rate: number
  width: number
  channels: number
  latency_ms: number
}

export interface ResponseDecision {
  text: string
  safe: boolean
  escalate: boolean
  reason: string
}

export interface ConversationSimulateResult {
  decision: ResponseDecision
  intent: string
  policy_allowed: boolean
  policy_reason: string
  policy_flags: string[]
  policy_confidence: Record<string, number>
  matched_rule_ids: string[]
}

export const debugApi = {
  describeVision: (imageBase64: string, prompt?: string) =>
    apiFetch<VisionDescribeResult>("/api/debug/vision/describe", {
      method: "POST",
      body: { image_base64: imageBase64, prompt: prompt || null },
    }),
  generateLLM: (prompt: string) =>
    apiFetch<LLMGenerateResult>("/api/debug/llm/generate", { method: "POST", body: { prompt } }),
  transcribeSTT: (audioWavBase64: string) =>
    apiFetch<STTTranscribeResult>("/api/debug/stt/transcribe", {
      method: "POST",
      body: { audio_wav_base64: audioWavBase64 },
    }),
  synthesizeTTS: (text: string) =>
    apiFetch<TTSSynthesizeResult>("/api/debug/tts/synthesize", {
      method: "POST",
      body: { text },
    }),
  simulateConversation: (text: string) =>
    apiFetch<ConversationSimulateResult>("/api/debug/conversation/simulate", {
      method: "POST",
      body: { text },
    }),
}
