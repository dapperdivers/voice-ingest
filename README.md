# 🎙️ Voice Ingest

A lightweight microservice that watches a directory for voice recordings, transcribes them via a Speech-to-Text API, and sends the structured transcript to an OpenClaw webhook for AI-powered processing and vault routing.

## Architecture

```
Phone → Syncthing → [watch dir] → Voice Ingest → [STT API] → [OpenClaw webhook] → AI routes to vault
```

### What It Does
1. **Watches** a directory for new audio files (`.m4a`, `.ogg`, `.wav`, `.mp3`, `.opus`)
2. **Transcribes** via a configurable STT endpoint (Speaches/Whisper compatible)
3. **Deletes** the audio file after successful transcription
4. **Sends** structured payload to an OpenClaw `/hooks/agent` webhook for AI classification and vault routing

### What It Doesn't Do
- No AI/LLM logic — that's OpenClaw's job
- No vault manipulation — the webhook agent handles routing
- No complex dependencies — just Python + watchdog

## Configuration

All configuration via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `WATCH_DIR` | No | `/data/voice` | Directory to watch for audio files |
| `STT_URL` | Yes | — | Speech-to-text API endpoint |
| `STT_MODEL` | No | `deepdml/faster-whisper-large-v3-turbo-ct2` | STT model name |
| `OPENCLAW_URL` | Yes | — | OpenClaw gateway URL (e.g. `http://molt.ai.svc.cluster.local:18789`) |
| `OPENCLAW_HOOK_TOKEN` | Yes | — | Webhook auth token |
| `OPENCLAW_HOOK_PATH` | No | `/hooks/agent` | Webhook endpoint path |
| `POLL_INTERVAL` | No | `5` | Seconds between directory polls (fallback if inotify unavailable) |
| `DELETE_AFTER_TRANSCRIBE` | No | `true` | Delete audio file after successful transcription |
| `LOG_LEVEL` | No | `INFO` | Logging level |

## Deployment (Kubernetes)

```yaml
# See kubernetes/ directory for full manifests
env:
  WATCH_DIR: /data/voice
  STT_URL: https://speaches.chelonianlabs.com/v1/audio/transcriptions
  STT_MODEL: deepdml/faster-whisper-large-v3-turbo-ct2
  OPENCLAW_URL: http://molt.ai.svc.cluster.local:18789
  DELETE_AFTER_TRANSCRIBE: "true"
envFrom:
  - secretRef:
      name: voice-ingest-secret  # OPENCLAW_HOOK_TOKEN
```

## Local Development

```bash
pip install -r requirements.txt
export WATCH_DIR=./test-audio
export STT_URL=https://speaches.chelonianlabs.com/v1/audio/transcriptions
export OPENCLAW_URL=http://localhost:18789
export OPENCLAW_HOOK_TOKEN=your-token
python main.py
```

## License

MIT
