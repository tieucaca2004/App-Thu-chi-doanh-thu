"""BUG-006: STT timeout / retry / error state / format handling (mocked HTTP; real STT still BLOCKED)."""
import httpx
import pytest

from founder_assistant.stt import STTUnavailable, WhisperHTTPTranscriber


def make(responses, converter=None):
    seen = []

    def handler(request):
        seen.append(request)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    t = WhisperHTTPTranscriber("https://stt.example/v1/audio/transcriptions", "k", client=httpx.Client(
        transport=httpx.MockTransport(handler)), sleep=lambda s: None,
        converter=converter or (lambda audio, ext: b"WAV" + audio))
    return t, seen


def test_success_sends_vietnamese_and_returns_text():
    t, seen = make([httpx.Response(200, json={"text": " Hôm nay mua năm ký thịt heo "})])
    assert t.transcribe(b"aud", "v.m4a", "audio/mp4") == "Hôm nay mua năm ký thịt heo"
    body = seen[0].content
    assert b'name="language"\r\n\r\nvi' in body and b'filename="v.m4a"' in body


def test_retries_transient_errors_then_succeeds():
    t, seen = make([httpx.ConnectTimeout("t"), httpx.Response(503), httpx.Response(200, json={"text": "ok"})])
    assert t.transcribe(b"a", "v.mp3", None) == "ok" and len(seen) == 3


def test_gives_up_after_retries():
    t, seen = make([httpx.Response(500)] * 3)
    with pytest.raises(STTUnavailable, match="3 lần"):
        t.transcribe(b"a", "v.mp3", None)


def test_client_error_not_retried():
    t, seen = make([httpx.Response(401, text="bad key")])
    with pytest.raises(STTUnavailable, match="401"):
        t.transcribe(b"a", "v.mp3", None)
    assert len(seen) == 1


def test_empty_transcript_is_an_error_not_empty_data():
    t, _ = make([httpx.Response(200, json={"text": "  "})])
    with pytest.raises(STTUnavailable):
        t.transcribe(b"a", "v.mp3", None)


def test_zalo_aac_is_transcoded_to_wav():
    t, seen = make([httpx.Response(200, json={"text": "x"})])
    t.transcribe(b"aac-bytes", "voice-3.aac", "audio/aac")
    assert b'filename="voice-3.wav"' in seen[0].content and b"WAVaac-bytes" in seen[0].content


def test_missing_ffmpeg_is_a_clear_error(monkeypatch):
    from founder_assistant import stt
    monkeypatch.setattr(stt.shutil, "which", lambda name: None)
    t = WhisperHTTPTranscriber("https://x", sleep=lambda s: None)
    with pytest.raises(STTUnavailable, match="ffmpeg"):
        t.transcribe(b"a", "v.amr", "audio/amr")


# BUG-013 (found by running real ffmpeg): error text must be ffmpeg's actual error, not its version banner.
# These run the REAL ffmpeg binary when installed; skipped otherwise (never faked).
needs_ffmpeg = pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="ffmpeg not installed")


@needs_ffmpeg
def test_real_ffmpeg_error_is_readable():
    from founder_assistant.stt import to_wav
    with pytest.raises(STTUnavailable) as e:
        to_wav(b"not audio at all", ".aac")
    assert "libpostproc" not in str(e.value) and "Copyright" not in str(e.value)
    assert "Invalid data" in str(e.value) or "Error" in str(e.value)


@needs_ffmpeg
def test_real_ffmpeg_aac_to_wav(tmp_path):
    import io, subprocess, wave
    from founder_assistant.stt import to_wav
    src = tmp_path / "gen.aac"  # generated tone, NOT a Zalo/Founder recording
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:a", "aac", str(src)], check=True)
    w = wave.open(io.BytesIO(to_wav(src.read_bytes(), ".aac")))
    assert (w.getframerate(), w.getnchannels()) == (16000, 1)
