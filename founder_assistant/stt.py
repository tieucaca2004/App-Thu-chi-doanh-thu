"""Speech-to-text. Pluggable: any Whisper-compatible `/audio/transcriptions` endpoint.

If STT is not configured the voice file is still stored and the Founder is asked to type,
so no voice message is ever silently dropped.
"""
from __future__ import annotations

from typing import Protocol

import httpx


class Transcriber(Protocol):
    def transcribe(self, audio: bytes, filename: str, mime: str | None) -> str: ...


class STTUnavailable(RuntimeError):
    pass


class WhisperHTTPTranscriber:
    def __init__(self, url: str, api_key: str = "", model: str = "whisper-1", timeout: float = 120.0):
        self.url, self.api_key, self.model, self.timeout = url, api_key, model, timeout

    def transcribe(self, audio: bytes, filename: str, mime: str | None) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        files = {"file": (filename, audio, mime or "application/octet-stream")}
        data = {"model": self.model, "language": "vi", "response_format": "json"}
        r = httpx.post(self.url, headers=headers, files=files, data=data, timeout=self.timeout)
        r.raise_for_status()
        text = (r.json().get("text") or "").strip()
        if not text:
            raise STTUnavailable("Bản chép giọng nói rỗng.")
        return text


class NoTranscriber:
    def transcribe(self, audio: bytes, filename: str, mime: str | None) -> str:
        raise STTUnavailable("Chưa cấu hình speech-to-text (STT_URL).")
