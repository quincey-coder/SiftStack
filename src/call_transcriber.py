"""Transcribe SmrtPhone call recordings and classify each call.

Feeds the three call-coaching skills we already ship (cold-call-coach,
lead-manager-coach, closer-coach) with real transcripts instead of hand-typed
notes.

WHY AN AUDIO MODEL AND NOT WHISPER-THEN-CLAUDE: the grading rubrics score
TONALITY — pace, energy, hesitation, silence — which a text transcript destroys.
An audio-native model hears the call, so the delivery notes are observed rather
than inferred from wording. That is the whole reason this step is not a plain
speech-to-text call.

ROUTING: Anthropic models do not accept audio input, so this cannot go through
``llm_client.chat_json``'s Anthropic path. It uses OpenRouter, which our fork
ALREADY configures (``OPENROUTER_API_KEY`` / ``OPENROUTER_BASE_URL`` in config,
plus an OpenRouter backend in ``llm_client``) — so this adds a capability, not a
second vendor. The model is overridable via ``CALL_AUDIO_MODEL``; it must be
audio-capable (Gemini Flash is the cheap default, roughly $0.002 per audio
minute).

Two passes per call:
  1. AUDIO  -> speaker-labelled transcript with bracketed delivery notes.
  2. TEXT   -> strict JSON classification (call_type, pipeline stage, whether a
               seller actually spoke, one-line summary) so a grader never wastes
               a rubric pass on a voicemail or a wrong number.

CLI:
    python src/call_transcriber.py                 # everything in calls_to_review.json
    python src/call_transcriber.py --limit 5       # cheap smoke test
    python src/call_transcriber.py --call-id 151893352
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
from pathlib import Path

import requests

import config

logger = logging.getLogger(__name__)

OUT_DIR = config.OUTPUT_DIR / "call_coaching"
REC_DIR = OUT_DIR / "recordings"
TRANSCRIPT_DIR = OUT_DIR / "transcripts"

AUDIO_MODEL = getattr(config, "CALL_AUDIO_MODEL", "google/gemini-2.5-flash")
REQUEST_TIMEOUT = 300

TRANSCRIBE_PROMPT = """\
Transcribe this real estate sales phone call.

Rules:
- Label every turn as REP: or PROSPECT:. If a third party joins, label them.
- After a turn, add bracketed DELIVERY notes ONLY when they are audible and
  meaningful: [rushed], [long pause 4s], [flat tone], [talks over prospect],
  [warm], [defensive], [hesitant]. Do not invent notes you cannot hear.
- Transcribe what is actually said, including filler and false starts. Do not
  clean up or paraphrase the rep to sound better.
- If the audio is a voicemail greeting, transcribe it and nothing else.
- If there is no intelligible speech, output exactly: NO_SPEECH

Output the transcript only, no preamble."""

CLASSIFY_PROMPT = """\
Classify this real estate sales call transcript. Return ONLY a JSON object:

{
  "call_type": "conversation" | "voicemail" | "no_contact" | "wrong_number" | "dead_air",
  "pipeline": "cold_call" | "lead_management" | "closing" | "unknown",
  "has_seller_dialogue": true/false,
  "summary": "one sentence, factual",
  "gradeable": true/false
}

"gradeable" is true ONLY when a human actually spoke with the rep long enough to
score against a rubric. A voicemail, wrong number or dead air is NOT gradeable.

Transcript:
"""


class TranscriptionError(RuntimeError):
    pass


def _openrouter_key() -> str:
    key = getattr(config, "OPENROUTER_API_KEY", "")
    if not key:
        raise TranscriptionError(
            "OPENROUTER_API_KEY is not set. Call transcription needs an audio-capable "
            "model, which Anthropic does not provide — see this module's docstring.")
    return key


def _post(payload: dict) -> str:
    base = getattr(config, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    resp = requests.post(
        f"{base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {_openrouter_key()}",
                 "Content-Type": "application/json"},
        json=payload, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise TranscriptionError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise TranscriptionError(f"Unexpected OpenRouter response: {str(data)[:300]}") from exc


def transcribe_audio(mp3_path: str | Path, model: str = "") -> str:
    """One audio pass: MP3 -> speaker-labelled transcript with delivery notes."""
    mp3_path = Path(mp3_path)
    encoded = base64.b64encode(mp3_path.read_bytes()).decode()
    payload = {
        "model": model or AUDIO_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": TRANSCRIBE_PROMPT},
                {"type": "input_audio",
                 "input_audio": {"data": encoded, "format": "mp3"}},
            ],
        }],
    }
    return _post(payload).strip()


def classify(transcript: str, model: str = "") -> dict:
    """Second pass: strict JSON triage so graders skip ungradeable calls."""
    payload = {
        "model": model or AUDIO_MODEL,
        "messages": [{"role": "user", "content": CLASSIFY_PROMPT + transcript[:20000]}],
        "response_format": {"type": "json_object"},
    }
    raw = _post(payload).strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    try:
        return json.loads(raw)
    except ValueError:
        logger.warning("Classification returned non-JSON; marking unknown")
        return {"call_type": "unknown", "pipeline": "unknown",
                "has_seller_dialogue": False, "summary": "", "gradeable": False}


def transcribe_call(call: dict, force: bool = False) -> dict | None:
    """Transcribe + classify one call. Returns the enriched record."""
    call_id = call.get("call_id")
    mp3 = REC_DIR / f"{call_id}.mp3"
    if not mp3.exists():
        logger.warning("No recording on disk for call %s — skipping", call_id)
        return None

    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    out = TRANSCRIPT_DIR / f"{call_id}.json"
    if out.exists() and not force:
        return json.loads(out.read_text(encoding="utf-8"))

    try:
        transcript = transcribe_audio(mp3)
    except TranscriptionError as exc:
        logger.error("Transcription failed for call %s: %s", call_id, exc)
        return None

    if transcript.strip() == "NO_SPEECH":
        record = {**call, "transcript": "", "call_type": "dead_air",
                  "pipeline": "unknown", "has_seller_dialogue": False,
                  "summary": "no intelligible speech", "gradeable": False}
    else:
        record = {**call, "transcript": transcript, **classify(transcript)}

    out.write_text(json.dumps(record, indent=2), encoding="utf-8")
    logger.info("Call %s: %s / %s / gradeable=%s", call_id,
                record.get("call_type"), record.get("pipeline"), record.get("gradeable"))
    return record


def transcribe_batch(limit: int = 0, call_id: str = "", force: bool = False) -> list[dict]:
    source = OUT_DIR / "calls_to_review.json"
    if not source.exists():
        raise TranscriptionError(
            f"{source} not found. Run: python src/smrtphone.py pull --min-seconds 60")
    calls = json.loads(source.read_text(encoding="utf-8"))
    if call_id:
        calls = [c for c in calls if str(c.get("call_id")) == str(call_id)]
    if limit:
        calls = calls[:limit]

    done = [r for r in (transcribe_call(c, force) for c in calls) if r]
    gradeable = sum(1 for r in done if r.get("gradeable"))
    logger.info("Transcribed %d/%d call(s); %d gradeable", len(done), len(calls), gradeable)
    (OUT_DIR / "transcribed.json").write_text(json.dumps(done, indent=2), encoding="utf-8")
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--call-id", default="")
    parser.add_argument("--force", action="store_true", help="re-transcribe cached calls")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    transcribe_batch(args.limit, args.call_id, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
