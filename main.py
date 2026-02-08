#!/usr/bin/env python3
"""
Voice Ingest — Watch, Transcribe, Webhook
A microservice that watches for audio files, transcribes them via STT,
and sends structured transcripts to an OpenClaw webhook for AI processing.
"""
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

# ── Config ──────────────────────────────────────────────────────────────────

WATCH_DIR = Path(os.getenv("WATCH_DIR", "/data/voice"))
STT_URL = os.getenv("STT_URL", "")
STT_MODEL = os.getenv("STT_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
OPENCLAW_URL = os.getenv("OPENCLAW_URL", "")
OPENCLAW_HOOK_TOKEN = os.getenv("OPENCLAW_HOOK_TOKEN", "")
OPENCLAW_HOOK_PATH = os.getenv("OPENCLAW_HOOK_PATH", "/hooks/agent")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))
DELETE_AFTER = os.getenv("DELETE_AFTER_TRANSCRIBE", "true").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
AUDIO_EXTENSIONS = {".m4a", ".ogg", ".wav", ".mp3", ".opus", ".flac", ".wma"}

# Minimum file age in seconds before processing (avoids partial syncs)
MIN_FILE_AGE = int(os.getenv("MIN_FILE_AGE", "5"))

# State file to track processed files (survives restarts)
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/state/processed.json"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("voice-ingest")


# ── State Management ────────────────────────────────────────────────────────

def load_state() -> set:
    """Load set of already-processed filenames."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return set(data.get("processed", []))
        except (json.JSONDecodeError, KeyError):
            return set()
    return set()


def save_state(processed: set):
    """Persist processed filenames."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "processed": sorted(processed),
        "updated": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


# ── STT ─────────────────────────────────────────────────────────────────────

def transcribe(audio_path: Path) -> dict | None:
    """
    Send audio to STT endpoint.
    Returns {"text": "...", "duration": ...} or None on failure.
    """
    try:
        with open(audio_path, "rb") as f:
            resp = requests.post(
                STT_URL,
                files={"file": (audio_path.name, f)},
                data={"model": STT_MODEL},
                timeout=120,
            )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("text", "").strip()
        if not text:
            log.warning("Empty transcript for %s", audio_path.name)
            return None
        return {
            "text": text,
            "duration": data.get("duration"),
        }
    except requests.RequestException as e:
        log.error("STT request failed for %s: %s", audio_path.name, e)
        return None
    except (json.JSONDecodeError, ValueError) as e:
        log.error("STT response parse error for %s: %s", audio_path.name, e)
        return None


# ── Webhook ─────────────────────────────────────────────────────────────────

def send_to_openclaw(transcript: str, metadata: dict):
    """Send transcript to OpenClaw /hooks/agent endpoint."""
    webhook_url = f"{OPENCLAW_URL.rstrip('/')}{OPENCLAW_HOOK_PATH}"

    # Build the message for Tim
    message = (
        f"Voice note received from Derek. Process this and ALWAYS save something to the Obsidian vault.\n\n"
        f"**Transcript:**\n> {transcript}\n\n"
        f"**Metadata:**\n"
        f"- Audio file: {metadata.get('audio_file', 'unknown')}\n"
        f"- Recorded: {metadata.get('recorded_at', 'unknown')}\n"
        f"- Duration: {metadata.get('duration', 'unknown')}\n\n"
        f"**CRITICAL RULE: Every voice note MUST result in a vault write.** "
        f"Derek records voice notes because he's away from his desk. If he took the time to record it, it matters. "
        f"Never just answer a question — always save the information to the appropriate vault note too.\n\n"
        f"**Instructions:**\n"
        f"1. Classify the intent (new-knowledge, update-existing, action-item, person-context, question, random-thought, decision)\n"
        f"2. Search the Obsidian vault (/home/node/obsidian-vault) for related existing notes\n"
        f"3. ALWAYS route to vault: create new notes, append to existing, extract tasks to TASKS.md, update person notes\n"
        f"4. Save a raw transcript note to Inbox/Voice/ with proper frontmatter (type, status, intent, topics, actions_taken)\n"
        f"5. If it's a question, answer it AND save the answer to the relevant vault note\n"
        f"6. If it's a reminder/task, add it to TASKS.md or the relevant project checklist\n"
        f"7. Send a brief summary to Derek via Discord (channel: 1466806583714644063) confirming what was saved"
    )

    payload = {
        "message": message,
        "name": "VoiceIngest",
        "sessionKey": f"hook:voice:{metadata.get('audio_file', 'unknown')}",
        "wakeMode": "now",
        "deliver": True,
        "channel": "discord",
        "to": "1466806583714644063",
        "timeoutSeconds": 120,
    }

    try:
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {OPENCLAW_HOOK_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if resp.status_code in (200, 202):
            log.info("Webhook sent successfully for %s", metadata.get("audio_file"))
            return True
        else:
            log.error("Webhook failed (%d): %s", resp.status_code, resp.text[:200])
            return False
    except requests.RequestException as e:
        log.error("Webhook request failed: %s", e)
        return False


# ── Processing ──────────────────────────────────────────────────────────────

def process_file(audio_path: Path, processed: set) -> bool:
    """Process a single audio file through the pipeline."""
    filename = audio_path.name

    if filename in processed:
        return False

    # Check file age (avoid processing partially-synced files)
    file_age = time.time() - audio_path.stat().st_mtime
    if file_age < MIN_FILE_AGE:
        log.debug("Skipping %s (too new, age=%.1fs)", filename, file_age)
        return False

    log.info("Processing: %s (%.1f KB)", filename, audio_path.stat().st_size / 1024)

    # Stage 1: Transcribe
    result = transcribe(audio_path)
    if not result:
        log.error("Transcription failed for %s — skipping", filename)
        return False

    log.info("Transcript: %s", result["text"][:100])

    # Build metadata
    stat = audio_path.stat()
    recorded_at = datetime.fromtimestamp(stat.st_mtime).astimezone()
    metadata = {
        "audio_file": filename,
        "recorded_at": recorded_at.isoformat(),
        "duration": f"{result.get('duration', 'unknown')}s" if result.get("duration") else "unknown",
        "file_size": stat.st_size,
    }

    # Stage 2: Send to OpenClaw webhook
    success = send_to_openclaw(result["text"], metadata)

    if success:
        # Mark as processed
        processed.add(filename)
        save_state(processed)

        # Delete audio file if configured
        if DELETE_AFTER:
            try:
                audio_path.unlink()
                log.info("Deleted: %s", filename)
            except OSError as e:
                log.warning("Failed to delete %s: %s", filename, e)

        return True
    else:
        log.error("Webhook delivery failed for %s — will retry next cycle", filename)
        return False


# ── File Watcher ────────────────────────────────────────────────────────────

class AudioFileHandler(FileSystemEventHandler):
    """Watches for new audio files and processes them."""

    def __init__(self, processed: set):
        super().__init__()
        self.processed = processed

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() in AUDIO_EXTENSIONS:
            # Wait for file to finish writing
            time.sleep(MIN_FILE_AGE)
            if path.exists():
                process_file(path, self.processed)

    def on_moved(self, event):
        """Handle files moved into the watch directory."""
        if event.is_directory:
            return
        path = Path(event.dest_path)
        if path.suffix.lower() in AUDIO_EXTENSIONS:
            time.sleep(MIN_FILE_AGE)
            if path.exists():
                process_file(path, self.processed)


# ── Main ────────────────────────────────────────────────────────────────────

def process_existing(processed: set):
    """Process any existing files in the watch directory."""
    count = 0
    for path in sorted(WATCH_DIR.iterdir()):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            if process_file(path, processed):
                count += 1
    if count:
        log.info("Processed %d existing file(s)", count)


def main():
    # Validate config
    if not STT_URL:
        log.error("STT_URL is required")
        sys.exit(1)
    if not OPENCLAW_URL:
        log.error("OPENCLAW_URL is required")
        sys.exit(1)
    if not OPENCLAW_HOOK_TOKEN:
        log.error("OPENCLAW_HOOK_TOKEN is required")
        sys.exit(1)

    WATCH_DIR.mkdir(parents=True, exist_ok=True)

    log.info("Voice Ingest starting")
    log.info("  Watch dir: %s", WATCH_DIR)
    log.info("  STT: %s (model: %s)", STT_URL, STT_MODEL)
    log.info("  OpenClaw: %s%s", OPENCLAW_URL, OPENCLAW_HOOK_PATH)
    log.info("  Delete after transcribe: %s", DELETE_AFTER)

    processed = load_state()
    log.info("  Previously processed: %d files", len(processed))

    # Process any existing files first
    process_existing(processed)

    # Set up file watcher
    handler = AudioFileHandler(processed)
    try:
        observer = Observer()
        log.info("Using inotify file watcher")
    except Exception:
        observer = PollingObserver(timeout=POLL_INTERVAL)
        log.info("Using polling observer (interval: %ds)", POLL_INTERVAL)

    # Use PollingObserver for network filesystems (CephFS/NFS)
    # inotify doesn't work across different mount points on shared storage
    observer = PollingObserver(timeout=POLL_INTERVAL)
    log.info("Using polling observer (interval: %ds) — required for CephFS/NFS", POLL_INTERVAL)

    observer.schedule(handler, str(WATCH_DIR), recursive=False)
    observer.start()

    # Graceful shutdown
    def shutdown(signum, frame):
        log.info("Shutting down...")
        observer.stop()
        observer.join()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("Watching for audio files... (Ctrl+C to stop)")
    try:
        while True:
            # Also poll manually in the main loop as a safety net
            time.sleep(POLL_INTERVAL)
            process_existing(processed)
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
