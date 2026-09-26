"""Speech-to-text. Pluggable: any Whisper-compatible `/audio/transcriptions` endpoint
(OpenAI `whisper-1` / `gpt-4o-transcribe`, a self-hosted faster-whisper server, ...), language fixed to Vietnamese.

The original audio is stored by the pipeline before this runs; STT failures leave the message in an
error state and the Founder is asked to type, so no voice message is ever silently dropped.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Protocol

import httpx

log = logging.getLogger(__name__)

# Formats accepted by Whisper-compatible transcription endpoints. Anything else (Zalo voice notes are
# AAC/AMR) is transcoded to 16 kHz mono WAV with ffmpeg first.
ACCEPTED_EXT = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".oga", ".ogg", ".wav", ".webm"}
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class Transcriber(Protocol):
    def transcribe(self, audio: bytes, filename: str, mime: str | None) -> str: ...


class STTUnavailable(RuntimeError):
    pass


def to_wav(audio: bytes, src_ext: str, ffmpeg: str | None = None, timeout: float = 60.0) -> bytes:
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        raise STTUnavailable(f"Định dạng {src_ext} cần ffmpeg để chuyển đổi nhưng máy chủ chưa cài ffmpeg.")
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / f"in{src_ext}", Path(d) / "out.wav"
        src.write_bytes(audio)
        r = subprocess.run([ffmpeg, "-nostdin", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)],
                           capture_output=True, timeout=timeout)
        if r.returncode != 0 or not dst.exists():
            raise STTUnavailable("Không chuyển đổi được file âm thanh: " + r.stderr.decode(errors="ignore")[-300:])
        return dst.read_bytes()


class WhisperHTTPTranscriber:
    def __init__(self, url: str, api_key: str = "", model: str = "whisper-1", timeout: float = 120.0,
                 retries: int = 3, backoff: float = 2.0, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, converter: Callable[[bytes, str], bytes] = to_wav):
        self.url, self.api_key, self.model, self.timeout = url, api_key, model, timeout
        self.retries, self.backoff, self.sleep, self.converter = retries, backoff, sleep, converter
        self.client = client or httpx.Client(timeout=timeout)

    def transcribe(self, audio: bytes, filename: str, mime: str | None) -> str:
        ext = Path(filename).suffix.lower()
        if ext not in ACCEPTED_EXT:
            audio, filename, mime = self.converter(audio, ext), Path(filename).stem + ".wav", "audio/wav"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = {"model": self.model, "language": "vi", "response_format": "json"}
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                r = self.client.post(self.url, headers=headers, data=data,
                                     files={"file": (filename, audio, mime or "application/octet-stream")},
                                     timeout=self.timeout)
            except httpx.TransportError as exc:  # timeout, connection reset, DNS...
                last = exc
            else:
                if r.status_code == 200:
                    text = (r.json().get("text") or "").strip()
                    if not text:
                        raise STTUnavailable("Bản chép giọng nói rỗng (không nghe được nội dung).")
                    return text
                if r.status_code not in RETRY_STATUS:
                    raise STTUnavailable(f"STT lỗi {r.status_code}: {r.text[:200]}")
                last = STTUnavailable(f"STT lỗi {r.status_code}")
            if attempt < self.retries:
                log.warning("STT attempt %s/%s failed: %s", attempt, self.retries, last)
                self.sleep(self.backoff * attempt)
        raise STTUnavailable(f"STT không phản hồi sau {self.retries} lần thử: {last}")


class NoTranscriber:
    def transcribe(self, audio: bytes, filename: str, mime: str | None) -> str:
        raise STTUnavailable("Chưa cấu hình speech-to-text (STT_URL).")
