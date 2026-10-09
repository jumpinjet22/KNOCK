"""Debug/test-lab routes.

Thin, authenticated wrappers around providers and the orchestrator that are
already independently tested -- these routes exist so the web UI can
exercise the *real*, currently-configured hardware/models (a live camera
snapshot, a real Ollama/Whisper/Kokoro server) from the browser, rather than
only trusting that settings were entered correctly. Gated behind
`require_auth` for the same reason settings are: every panel here can hit
real cameras/microphones/credentials.
"""

from __future__ import annotations

import base64
import io
import time
import wave
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from knock.api.auth_routes import CurrentUserDep
from knock.api.settings_routes import ConfigStoreDep
from knock.config import KokoroConfig, OllamaConfig, VisionConfig, WhisperConfig
from knock.core.audit import JSONLAuditLog
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.scene import SceneContext, SceneObservation, format_scene_for_prompt
from knock.providers.llm.ollama import OllamaProvider
from knock.providers.stt.whisper import WhisperSTTProvider
from knock.providers.tts.kokoro import KokoroTTSProvider
from knock.providers.vision.ollama import OllamaVisionProvider, parse_observation
from knock.providers.vision.safety import contains_alarming_language, sanitize_description

router = APIRouter(prefix="/api/debug", tags=["debug"])


def get_audit_log() -> JSONLAuditLog:
    return JSONLAuditLog()


AuditLogDep = Annotated[JSONLAuditLog, Depends(get_audit_log)]


def _pcm_to_wav_base64(pcm: bytes, *, rate: int, width: int, channels: int) -> str:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _wav_base64_to_pcm(audio_wav_base64: str) -> tuple[bytes, int, int, int]:
    try:
        wav_bytes = base64.b64decode(audio_wav_base64)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            rate = wav_file.getframerate()
            width = wav_file.getsampwidth()
            channels = wav_file.getnchannels()
            pcm = wav_file.readframes(wav_file.getnframes())
    except (ValueError, wave.Error) as exc:
        raise HTTPException(
            status_code=400, detail=f"audio_wav_base64 is not a valid WAV file: {exc}"
        ) from exc
    return pcm, rate, width, channels


def _httpx_error_detail(exc: httpx.HTTPError) -> str:
    """`str(exc)` on an `HTTPStatusError` is just "Client error '400 Bad
    Request' for url '...'" -- it drops the response body, which is where a
    model server (Ollama, an OpenAI-compatible proxy, ...) actually explains
    *why* (e.g. a request exceeding the model's context window). Surface
    that body here instead of making the debug panel's whole point --
    seeing what actually went wrong -- useless for exactly the errors it
    exists to catch.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text.strip()
        if body:
            return f"{exc} | response body: {body[:2000]}"
    return str(exc)


# -- vision -------------------------------------------------------------------


class VisionDescribeRequest(BaseModel):
    image_base64: str
    prompt: str | None = None


class VisionDescribeResponse(BaseModel):
    raw: str
    sanitized: str
    alarming_language_detected: bool
    latency_ms: float


@router.post("/vision/describe", response_model=VisionDescribeResponse)
def debug_vision_describe(
    body: VisionDescribeRequest, current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> VisionDescribeResponse:
    try:
        image = base64.b64decode(body.image_base64)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="image_base64 is not valid base64") from exc

    config = VisionConfig.from_sources(store)
    provider = OllamaVisionProvider(config=config)
    start = time.perf_counter()
    try:
        raw = provider.describe_raw(image, body.prompt)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"vision request failed: {_httpx_error_detail(exc)}"
        ) from exc
    finally:
        provider.close()
    latency_ms = (time.perf_counter() - start) * 1000

    return VisionDescribeResponse(
        raw=raw,
        sanitized=sanitize_description(raw),
        alarming_language_detected=contains_alarming_language(raw),
        latency_ms=latency_ms,
    )


class VisionObserveRequest(BaseModel):
    image_base64: str


class VisionObserveResponse(BaseModel):
    raw: str
    # None when the model's reply couldn't be parsed as the structured
    # schema -- a real bridge would fall back to a plain describe() then.
    observation: SceneObservation | None
    parse_error: str | None = None
    # Exactly the camera-observations block the LLM prompt would get.
    prompt_block: str
    latency_ms: float


@router.post("/vision/observe", response_model=VisionObserveResponse)
def debug_vision_observe(
    body: VisionObserveRequest, current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> VisionObserveResponse:
    try:
        image = base64.b64decode(body.image_base64)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="image_base64 is not valid base64") from exc

    provider = OllamaVisionProvider(config=VisionConfig.from_sources(store))
    start = time.perf_counter()
    try:
        raw = provider.observe_raw(image)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"vision request failed: {_httpx_error_detail(exc)}"
        ) from exc
    finally:
        provider.close()
    latency_ms = (time.perf_counter() - start) * 1000

    observation: SceneObservation | None
    parse_error: str | None = None
    try:
        observation = parse_observation(raw)
    except ValueError as exc:
        observation = None
        parse_error = str(exc)

    return VisionObserveResponse(
        raw=raw,
        observation=observation,
        parse_error=parse_error,
        prompt_block=format_scene_for_prompt(
            SceneContext(observation=observation) if observation is not None else None
        ),
        latency_ms=latency_ms,
    )


# -- llm ------------------------------------------------------------------------


class LLMGenerateRequest(BaseModel):
    prompt: str


class LLMGenerateResponse(BaseModel):
    response: str
    latency_ms: float


@router.post("/llm/generate", response_model=LLMGenerateResponse)
def debug_llm_generate(
    body: LLMGenerateRequest, current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> LLMGenerateResponse:
    config = OllamaConfig.from_sources(store)
    provider = OllamaProvider(config=config)
    start = time.perf_counter()
    try:
        text = provider.generate(body.prompt)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"LLM request failed: {_httpx_error_detail(exc)}"
        ) from exc
    finally:
        provider.close()
    latency_ms = (time.perf_counter() - start) * 1000
    return LLMGenerateResponse(response=text, latency_ms=latency_ms)


# -- stt --------------------------------------------------------------------


class STTTranscribeRequest(BaseModel):
    audio_wav_base64: str


class STTTranscribeResponse(BaseModel):
    transcript: str
    latency_ms: float


@router.post("/stt/transcribe", response_model=STTTranscribeResponse)
async def debug_stt_transcribe(
    body: STTTranscribeRequest, current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> STTTranscribeResponse:
    pcm, rate, width, channels = _wav_base64_to_pcm(body.audio_wav_base64)

    config = WhisperConfig.from_sources(store)
    provider = WhisperSTTProvider(config=config)
    start = time.perf_counter()
    try:
        transcript = await provider.transcribe(pcm, rate=rate, width=width, channels=channels)
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"STT request failed: {exc}") from exc
    latency_ms = (time.perf_counter() - start) * 1000
    return STTTranscribeResponse(transcript=transcript, latency_ms=latency_ms)


# -- tts ------------------------------------------------------------------------


class TTSSynthesizeRequest(BaseModel):
    text: str


class TTSSynthesizeResponse(BaseModel):
    audio_wav_base64: str
    rate: int
    width: int
    channels: int
    latency_ms: float


@router.post("/tts/synthesize", response_model=TTSSynthesizeResponse)
async def debug_tts_synthesize(
    body: TTSSynthesizeRequest, current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> TTSSynthesizeResponse:
    config = KokoroConfig.from_sources(store)
    provider = KokoroTTSProvider(config=config)
    start = time.perf_counter()
    try:
        result = await provider.synthesize(body.text)
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"TTS request failed: {exc}") from exc
    latency_ms = (time.perf_counter() - start) * 1000

    return TTSSynthesizeResponse(
        audio_wav_base64=_pcm_to_wav_base64(
            result.audio, rate=result.rate, width=result.width, channels=result.channels
        ),
        rate=result.rate,
        width=result.width,
        channels=result.channels,
        latency_ms=latency_ms,
    )


# -- conversation simulator ---------------------------------------------------


class ConversationSimulateRequest(BaseModel):
    text: str


class ConversationSimulateResponse(BaseModel):
    decision: ResponseDecision
    intent: str
    policy_allowed: bool
    policy_reason: str
    policy_flags: list[str]
    policy_confidence: dict[str, float]
    matched_rule_ids: list[str]


@router.post("/conversation/simulate", response_model=ConversationSimulateResponse)
def debug_conversation_simulate(
    body: ConversationSimulateRequest,
    current_user: CurrentUserDep,
    *,
    store: ConfigStoreDep,
    audit_log: AuditLogDep,
) -> ConversationSimulateResponse:
    """A browser version of the CLI REPL: run one visitor line through the
    real, unmodified policy engine and orchestrator (no session persisted),
    surfacing the policy details `Orchestrator.respond()` doesn't return on
    its own -- useful for debugging why a rule did or didn't fire without
    needing real hardware.

    Recorded to the real audit log, same as the CLI's own REPL -- a
    simulated line is exactly as reviewable/trainable afterward (History,
    Training) as a real conversation, and previously wasn't: the
    orchestrator here was constructed without an audit_log at all, so
    every simulated line silently never reached audit.jsonl.
    """
    orchestrator = Orchestrator(
        llm_provider=OllamaProvider(config=OllamaConfig.from_sources(store)), audit_log=audit_log
    )
    policy_decision = orchestrator.policy.evaluate(body.text)
    event = VisitorEvent(text=body.text, timestamp=datetime.now(UTC))
    decision = orchestrator.respond(event)

    return ConversationSimulateResponse(
        decision=decision,
        intent=decision.intent or "unknown",
        policy_allowed=policy_decision.allowed,
        policy_reason=policy_decision.reason,
        policy_flags=policy_decision.flags,
        policy_confidence=policy_decision.confidence,
        matched_rule_ids=policy_decision.matched_rule_ids,
    )
