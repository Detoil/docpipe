"""Thin wrapper around the google-genai SDK for batch jobs and single calls.

All batch submissions use the JSONL-file flow (upload via the Files API,
then batches.create) so one code path handles any corpus size; the input
file limit is 2GB, which the stage modules respect by chunking jobs.
"""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path

COMPLETED_STATES = {
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
}


class GeminiError(RuntimeError):
    pass


def get_client():
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise GeminiError(
            "No API key found. Set the GEMINI_API_KEY environment variable "
            "(see https://aistudio.google.com/apikey)."
        )
    from google import genai

    return genai.Client(api_key=api_key)


def write_jsonl(path: Path, requests: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for req in requests:
            f.write(json.dumps(req, ensure_ascii=False) + "\n")


def submit_batch(client, model: str, jsonl_path: Path, display_name: str) -> str:
    from google.genai import types

    uploaded = client.files.upload(
        file=str(jsonl_path),
        config=types.UploadFileConfig(display_name=display_name, mime_type="jsonl"),
    )
    job = client.batches.create(
        model=model, src=uploaded.name, config={"display_name": display_name}
    )
    return job.name


def get_job(client, job_name: str):
    return client.batches.get(name=job_name)


def job_state(job) -> str:
    state = getattr(job, "state", None)
    return getattr(state, "name", None) or str(state)


def fetch_result_lines(client, job) -> list[dict]:
    """Download and parse the JSONL result file of a succeeded batch job."""
    dest = getattr(job, "dest", None)
    file_name = getattr(dest, "file_name", None) if dest else None
    if not file_name:
        raise GeminiError(f"Batch job {job.name} has no result file")
    content = client.files.download(file=file_name).decode("utf-8")
    lines = []
    for raw in content.splitlines():
        raw = raw.strip()
        if raw:
            lines.append(json.loads(raw))
    return lines


def response_text(response: dict) -> str | None:
    """Concatenate the text parts of a GenerateContentResponse dict."""
    try:
        parts = response["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return None
    return "".join(p.get("text", "") for p in parts) or None


def parse_json_response(response: dict):
    """Parse the model's structured-output JSON; returns None on failure."""
    text = response_text(response)
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def upload_for_inference(client, path: Path) -> tuple[str, str]:
    """Upload an original file (scanned PDF, image) for the model to read.

    Returns (file_uri, mime_type). Note Files API uploads expire after 48h,
    so stages re-upload at request-build time rather than caching URIs.
    """
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    uploaded = client.files.upload(file=str(path))
    return uploaded.uri, getattr(uploaded, "mime_type", None) or mime


def generate(client, model: str, system: str, text: str, schema: dict | None = None) -> str:
    """Single synchronous (non-batch) call, used for one-off requests like
    the schema proposal and the final briefing synthesis."""
    config: dict = {"system_instruction": system}
    if schema:
        config["response_mime_type"] = "application/json"
        config["response_schema"] = schema
    response = client.models.generate_content(model=model, contents=text, config=config)
    return response.text
