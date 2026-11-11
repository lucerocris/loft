"""Local speech-to-text worker for Loft.

Runs in ~/.local/share/loft/venv (faster-whisper). Reads one audio file path per
line on stdin and writes one JSON line {"text": ...} or {"error": ...} per path.
The model loads once, so each clip after the first takes about a second.
Nothing leaves the machine.
"""
import json
import os
import subprocess
import sys

import numpy as np

from faster_whisper import WhisperModel

MODEL = os.environ.get("LOFT_WHISPER_MODEL", "small.en")
# Words Whisper otherwise mishears; extend with LOFT_WHISPER_HINT.
HINT = os.environ.get("LOFT_WHISPER_HINT", "n8n, Zoho, TikTok, Claude, SOP, PR, repo, Figma, Loft, Supabase, GitHub, Konsole.")
model = WhisperModel(MODEL, device="cpu", compute_type="int8")
print(json.dumps({"ready": MODEL}), flush=True)

for line in sys.stdin:
    path = line.strip()
    if not path:
        continue
    try:
        # Decode with ffmpeg (16 kHz mono float) rather than faster-whisper's PyAV
        # path, which breaks across PyAV versions.
        pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                             capture_output=True, check=True).stdout
        audio = np.frombuffer(pcm, dtype=np.float32)
        segments, _ = model.transcribe(audio, beam_size=1, vad_filter=True,
                                       condition_on_previous_text=False,
                                       initial_prompt=HINT)
        text = " ".join(s.text.strip() for s in segments).strip()
        print(json.dumps({"text": text}), flush=True)
    except Exception as e:  # keep serving after a bad clip
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
