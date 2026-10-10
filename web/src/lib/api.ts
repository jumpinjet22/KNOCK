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

export interface BridgeState {
  name: string
  status: "stopped" | "starting" | "running" | "crashed"
  restart_count: number
  pid: number | null
  autostart: boolean
}

export interface BridgeLogsResponse {
  lines: string[]
  next_after: number
}

export const BRIDGE_LABELS: Record<string, string> = {
  mqtt: "MQTT",
  frigate: "Frigate",
  homeassistant: "Home Assistant",
  unifi: "UniFi Protect",
}

export const supervisorApi = {
  list: () => apiFetch<BridgeState[]>("/api/supervisor"),
  get: (name: string) => apiFetch<BridgeState>(`/api/supervisor/${name}`),
  start: (name: string) => apiFetch<BridgeState>(`/api/supervisor/${name}/start`, { method: "POST" }),
  stop: (name: string) => apiFetch<BridgeState>(`/api/supervisor/${name}/stop`, { method: "POST" }),
  restart: (name: string) =>
    apiFetch<BridgeState>(`/api/supervisor/${name}/restart`, { method: "POST" }),
  setAutostart: (name: string, enabled: boolean) =>
    apiFetch<BridgeState>(`/api/supervisor/${name}/autostart`, {
      method: "PUT",
      body: { enabled },
    }),
  logs: (name: string, after = 0) =>
    apiFetch<BridgeLogsResponse>(`/api/supervisor/${name}/logs?after=${after}`),
}

export interface UnifiCameraInfo {
  device_id: string
  name: string
  is_connected: boolean
}

export interface FrigateCameraInfo {
  name: string
}

export const videoApi = {
  unifiCameras: () => apiFetch<UnifiCameraInfo[]>("/api/video/unifi/cameras"),
  unifiSnapshotUrl: (deviceId: string) => `/api/video/unifi/${deviceId}/snapshot`,
  frigateCameras: () => apiFetch<FrigateCameraInfo[]>("/api/video/frigate/cameras"),
  frigateSnapshotUrl: (cameraName: string) => `/api/video/frigate/${cameraName}/snapshot`,
}

export interface AuditEntry {
  timestamp: string
  text: string
  response_text: string
  session_id: string | null
  matched_flags: Record<string, number>
  matched_rule_ids: string[]
  allowed: boolean
  reason: string
  intent: string | null
}

export interface SessionState {
  session_id: string
  turn_count: number
  last_intent: string
  escalated: boolean
  updated_at: string
  history: string[]
}

export interface IntentStats {
  counts: Record<string, number>
}

export const historyApi = {
  audit: (limit = 200) => apiFetch<AuditEntry[]>(`/api/audit?limit=${limit}`),
  sessions: (limit = 200) => apiFetch<SessionState[]>(`/api/sessions?limit=${limit}`),
  stats: () => apiFetch<IntentStats>("/api/stats"),
}

export interface GoogleOAuthStatus {
  configured: boolean
}

export interface GoogleOAuthConfig {
  client_id: string
  has_client_secret: boolean
  allowed_email: string
  configured: boolean
}

export interface AuthentikOAuthStatus {
  configured: boolean
}

export interface AuthentikOAuthConfig {
  issuer_url: string
  client_id: string
  has_client_secret: boolean
  allowed_email: string
  configured: boolean
}

export const oauthApi = {
  googleStatus: () => apiFetch<GoogleOAuthStatus>("/api/oauth/google/status"),
  googleConfig: () => apiFetch<GoogleOAuthConfig>("/api/oauth/google/config"),
  updateGoogleConfig: (values: {
    client_id?: string
    client_secret?: string
    allowed_email?: string
  }) =>
    apiFetch<GoogleOAuthConfig>("/api/oauth/google/config", { method: "PUT", body: values }),
  googleLoginUrl: "/api/oauth/google/login",
  authentikStatus: () => apiFetch<AuthentikOAuthStatus>("/api/oauth/authentik/status"),
  authentikConfig: () => apiFetch<AuthentikOAuthConfig>("/api/oauth/authentik/config"),
  updateAuthentikConfig: (values: {
    issuer_url?: string
    client_id?: string
    client_secret?: string
    allowed_email?: string
  }) =>
    apiFetch<AuthentikOAuthConfig>("/api/oauth/authentik/config", {
      method: "PUT",
      body: values,
    }),
  authentikLoginUrl: "/api/oauth/authentik/login",
}

export interface PasskeyInfo {
  credential_id: string
  nickname: string
  created_at: string
}

export const webauthnApi = {
  registerOptions: () =>
    apiFetch<Record<string, unknown>>("/api/webauthn/register/options", { method: "POST" }),
  registerVerify: (credential: unknown, nickname: string) =>
    apiFetch<PasskeyInfo>("/api/webauthn/register/verify", {
      method: "POST",
      body: { credential, nickname },
    }),
  list: () => apiFetch<PasskeyInfo[]>("/api/webauthn"),
  remove: (credentialId: string) =>
    apiFetch<void>(`/api/webauthn/${encodeURIComponent(credentialId)}`, { method: "DELETE" }),
  loginOptions: () =>
    apiFetch<Record<string, unknown>>("/api/webauthn/login/options", { method: "POST" }),
  loginVerify: (credential: unknown) =>
    apiFetch<{ username: string }>("/api/webauthn/login/verify", {
      method: "POST",
      body: { credential },
    }),
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

export interface TrainingReview {
  status: "pending" | "approved" | "rejected"
  intent_override: string | null
  response_override: string | null
  comment: string | null
}

export interface JudgeAxisScores {
  visitor_voice: number
  category_correct: number
  safety_compliant: number
  natural_quality: number
  reason: string
  judge_model: string
}

export interface AggregatedJudgeResult {
  visitor_voice_avg: number
  category_correct_avg: number
  safety_compliant_avg: number
  natural_quality_avg: number
  voice_veto: boolean
  safety_veto: boolean
  disagreement: number
  per_judge: JudgeAxisScores[]
}

export interface CorrectionAttempt {
  attempt: number
  original_response: string
  corrected_response: string
  judge_reason: string
  rejudged: AggregatedJudgeResult | null
  accepted: boolean
}

export interface TrainingMetadata {
  judge: AggregatedJudgeResult | null
  corrections: CorrectionAttempt[]
}

export interface TrainingQueueItem {
  key: string
  entry: AuditEntry
  review: TrainingReview
  metadata: TrainingMetadata
  needs_attention: boolean
}

export interface TrainingQueuePage {
  items: TrainingQueueItem[]
  total: number
  offset: number
  limit: number
  has_more: boolean
}

export interface TrainingQueueCounts {
  pending: number
  approved: number
  rejected: number
}

export interface TrainingIntentOptions {
  intents: string[]
}

export interface UIMode {
  training_only: boolean
}

export const trainingApi = {
  uiMode: () => apiFetch<UIMode>("/api/training/ui-mode"),
  intents: () => apiFetch<TrainingIntentOptions>("/api/training/intents"),
  // Status-filtered, paginated -- see training_routes.py's get_training_queue
  // docstring for why: the old flat `limit` (over the newest raw entries,
  // filtered client-side) silently hid most of an actually-reviewed
  // history once the dataset outgrew it. offset/limit here page through
  // the full, status-filtered history instead.
  queue: (
    params: {
      status?: TrainingReview["status"]
      sort?: "newest" | "needs_attention_first"
      offset?: number
      limit?: number
    } = {},
  ) => {
    const query = new URLSearchParams()
    if (params.status) query.set("status", params.status)
    if (params.sort) query.set("sort", params.sort)
    query.set("offset", String(params.offset ?? 0))
    query.set("limit", String(params.limit ?? 25))
    return apiFetch<TrainingQueuePage>(`/api/training/queue?${query.toString()}`)
  },
  queueCounts: () => apiFetch<TrainingQueueCounts>("/api/training/queue/counts"),
  review: (
    key: string,
    body: {
      status: TrainingReview["status"]
      intent_override?: string | null
      response_override?: string | null
      comment?: string | null
    },
  ) => apiFetch<TrainingReview>(`/api/training/queue/${key}`, { method: "PUT", body }),
  // One-shot interactive correction -- lighter-weight than the batch
  // correct_training_data.py script (one corrector call, no re-judging,
  // no retry loop). Returns the suggested text only; it's never saved
  // until the reviewer explicitly Approves/Rejects afterward.
  suggestCorrection: (
    key: string,
    body: {
      corrector_model: string
      judge_models: string[]
      current_response: string
      human_note?: string | null
    },
  ) =>
    apiFetch<{ corrected_response: string; metadata: TrainingMetadata; accepted: boolean }>(
      `/api/training/queue/${key}/suggest-correction`,
      { method: "POST", body },
    ),
  exportUrl: "/api/training/export",
  exportDpoUrl: "/api/training/export/dpo",
}

export interface ScriptStatus {
  script:
    | "generate_scenarios"
    | "generate_training_data"
    | "judge_training_data"
    | "correct_training_data"
    | null
  status: "idle" | "running" | "completed" | "failed" | "stopped"
  exit_code: number | null
  started_at: number | null
}

export interface ScriptLogs {
  lines: string[]
  next_after: number
}

export const scriptsApi = {
  status: () => apiFetch<ScriptStatus>("/api/training/scripts/status"),
  logs: (after = 0) => apiFetch<ScriptLogs>(`/api/training/scripts/logs?after=${after}`),
  stop: () => apiFetch<ScriptStatus>("/api/training/scripts/stop", { method: "POST" }),
  generateScenarios: (body: { models: string[]; count_per_category?: number }) =>
    apiFetch<ScriptStatus>("/api/training/scripts/generate-scenarios", { method: "POST", body }),
  generateTrainingData: (body: { models: string[]; scenarios?: string; repeats?: number }) =>
    apiFetch<ScriptStatus>("/api/training/scripts/generate-training-data", {
      method: "POST",
      body,
    }),
  judgeTrainingData: (body: { judge_models: string[]; limit?: number }) =>
    apiFetch<ScriptStatus>("/api/training/scripts/judge-training-data", {
      method: "POST",
      body,
    }),
  correctTrainingData: (body: {
    corrector_model: string
    judge_models: string[]
    max_attempts?: number
    limit?: number
  }) =>
    apiFetch<ScriptStatus>("/api/training/scripts/correct-training-data", {
      method: "POST",
      body,
    }),
}
