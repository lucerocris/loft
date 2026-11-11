"""Local speech-to-text worker for Loft.

Runs in ~/.local/share/loft/venv (faster-whisper). Reads one request per line on
stdin and writes one JSON line {"text": ...} or {"error": ...} per request.

A request is either a plain audio file path (any format ffmpeg reads; the
desktop recorder's webm, the phone's m4a) or a JSON object:

  {"path": "/tmp/x.pcm", "raw": true, "mode": "partial" | "final"}

"raw" files are PCM16 little-endian, 16 kHz mono (the phone's live stream).
"partial" is the quick pass used for live words while someone is still
talking (greedy, no VAD); "final" is the normal pass. The file is deleted after
it is read. The model loads once, so each clip after the first takes about a
second. Nothing leaves the machine.
"""
import json
import os
import subprocess
import sys

import numpy as np

from faster_whisper import WhisperModel

MODEL = os.environ.get("LOFT_WHISPER_MODEL", "small.en")
# Words Whisper otherwise mishears; extend with LOFT_WHISPER_HINT.
HINT = os.environ.get("LOFT_WHISPER_HINT", "Claude, PR, repo, Figma, Loft, GitHub, README, API.")
model = WhisperModel(MODEL, device="cpu", compute_type="int8")
print(json.dumps({"ready": MODEL}), flush=True)


def decode(path):
    # Decode with ffmpeg (16 kHz mono float) rather than faster-whisper's PyAV
    # path, which breaks across PyAV versions.
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, dtype=np.float32)


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line) if line.startswith("{") else {"path": line}
    path = req.get("path", "")
    try:
        if req.get("raw"):
            with open(path, "rb") as f:
                audio = np.frombuffer(f.read(), dtype="<i2").astype(np.float32) / 32768.0
        else:
            audio = decode(path)
        if req.get("mode") == "partial":
            segments, _ = model.transcribe(audio, beam_size=1, vad_filter=False, without_timestamps=True,
                                           condition_on_previous_text=False, initial_prompt=HINT)
        else:
            segments, _ = model.transcribe(audio, beam_size=1, vad_filter=True,
                                           condition_on_previous_text=False, initial_prompt=HINT)
        text = " ".join(s.text.strip() for s in segments).strip()
        print(json.dumps({"text": text}), flush=True)
    except Exception as e:  # keep serving after a bad clip
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
