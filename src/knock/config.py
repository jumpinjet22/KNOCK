import os

from pydantic import BaseModel, Field


class KnockConfig(BaseModel):
    """Application configuration with safe defaults."""

    app_name: str = "KNOCK"
    short_response_limit: int = Field(default=140, ge=20, le=500)


class OllamaConfig(BaseModel):
    """Connection settings for a local Ollama LLM server."""

    host: str = "127.0.0.1"
    port: int = 11434
    model: str = "llama3.2"
    timeout: float = 30.0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(cls) -> "OllamaConfig":
        return cls(
            host=os.environ.get("KNOCK_OLLAMA_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_OLLAMA_PORT", "11434")),
            model=os.environ.get("KNOCK_OLLAMA_MODEL", "llama3.2"),
            timeout=float(os.environ.get("KNOCK_OLLAMA_TIMEOUT", "30.0")),
        )


class WhisperConfig(BaseModel):
    """Connection settings for a Wyoming-protocol Whisper STT server."""

    host: str = "127.0.0.1"
    port: int = 10300
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "WhisperConfig":
        return cls(
            host=os.environ.get("KNOCK_WHISPER_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_WHISPER_PORT", "10300")),
            timeout=float(os.environ.get("KNOCK_WHISPER_TIMEOUT", "10.0")),
        )


class KokoroConfig(BaseModel):
    """Connection settings for a Wyoming-protocol Kokoro TTS server."""

    host: str = "127.0.0.1"
    port: int = 10200
    voice: str | None = None
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "KokoroConfig":
        return cls(
            host=os.environ.get("KNOCK_KOKORO_HOST", "127.0.0.1"),
            port=int(os.environ.get("KNOCK_KOKORO_PORT", "10200")),
            voice=os.environ.get("KNOCK_KOKORO_VOICE"),
            timeout=float(os.environ.get("KNOCK_KOKORO_TIMEOUT", "10.0")),
        )
