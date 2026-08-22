#!/usr/bin/env bash
# External transcription hook for DUCTOR_TRANSCRIBE_COMMAND (#66).
#
# Contract: the audio path arrives as the LAST argument; print JSON
# {"transcript": "..."} on stdout, non-zero exit on failure so the caller
# falls through to the built-in strategies.
#
# Backend: Groq whisper-large-v3 (fast, cheap, solid on Russian).
# Key: GROQ_API_KEY from the ductor home .env (injected into every CLI execution).
set -uo pipefail

AUDIO="${!#}"

[ -n "${GROQ_API_KEY:-}" ] || { echo "GROQ_API_KEY unset" >&2; exit 1; }
[ -f "$AUDIO" ] || { echo "audio file not found: $AUDIO" >&2; exit 1; }

# Groq rejects some Telegram .ogg/opus containers; normalise to 16 kHz mono wav.
TMP_WAV="$(mktemp --suffix=.wav)"
trap 'rm -f "$TMP_WAV"' EXIT
if ! ffmpeg -nostdin -loglevel error -y -i "$AUDIO" -ar 16000 -ac 1 "$TMP_WAV" 2>/dev/null; then
    echo "ffmpeg failed to decode $AUDIO" >&2
    exit 1
fi

RESPONSE="$(curl -sS --max-time 240 \
    -X POST https://api.groq.com/openai/v1/audio/transcriptions \
    -H "Authorization: Bearer ${GROQ_API_KEY}" \
    -F "file=@${TMP_WAV}" \
    -F "model=whisper-large-v3" \
    -F "response_format=json")" || { echo "curl to Groq failed" >&2; exit 1; }

python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    print("Groq returned non-JSON: " + raw[:300], file=sys.stderr)
    sys.exit(1)
if "error" in data:
    print("Groq error: " + json.dumps(data["error"])[:300], file=sys.stderr)
    sys.exit(1)
text = (data.get("text") or "").strip()
if not text:
    print("Groq returned empty transcript", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"transcript": text, "method": "groq/whisper-large-v3"}, ensure_ascii=False))
' <<< "$RESPONSE"
