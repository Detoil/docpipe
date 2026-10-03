"""Project configuration and on-disk layout.

A *project* is a directory that holds everything the pipeline produces:
config.json, the SQLite database, batch request/response files, review
exports, the irrelevant-document folder and the final reports. The source
document folder is never written to.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_EXTENSIONS = [
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".csv", ".tsv", ".txt", ".md", ".markdown", ".rtf",
    ".html", ".htm", ".json", ".xml", ".eml", ".msg", ".epub",
]

IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"]

# File names that are never documents.
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

# USD per 1M tokens at *batch* rates (50% of interactive). These are estimates
# used only by `docpipe estimate`; edit config.json to match the current
# https://ai.google.dev/gemini-api/docs/pricing for your chosen models.
DEFAULT_PRICES = {
    "gemini-flash-lite-latest": {"input": 0.05, "output": 0.20},
    "gemini-flash-latest": {"input": 0.15, "output": 1.25},
}


@dataclass
class Config:
    source_dir: str = ""
    prompt: str = ""
    triage_model: str = "gemini-flash-lite-latest"
    extract_model: str = "gemini-flash-latest"
    synthesis_model: str = "gemini-flash-latest"
    include_extensions: list = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    include_images: bool = False
    max_file_mb: int = 100
    convert_timeout_seconds: int = 120
    convert_workers: int = 0  # 0 = one per CPU core, capped
    triage_excerpt_chars: int = 6000
    extract_max_chars: int = 300_000
    max_batch_requests: int = 2000
    poll_interval_seconds: int = 30
    batch_prices_per_1m_tokens: dict = field(
        default_factory=lambda: json.loads(json.dumps(DEFAULT_PRICES))
    )

    @classmethod
    def from_file(cls, path: Path) -> "Config":
        data = json.loads(Path(path).read_text())
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_file(self, path: Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n")


class Project:
    """On-disk layout of a docpipe project directory."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.config_path = self.root / "config.json"
        self.db_path = self.root / "pipeline.db"
        self.batches_dir = self.root / "batches"
        self.review_dir = self.root / "review"
        self.report_dir = self.root / "report"
        self.irrelevant_dir = self.root / "irrelevant"
        self.guidance_path = self.root / "extraction_guidance.md"
        self._config: Config | None = None

    def exists(self) -> bool:
        return self.config_path.exists()

    def create(self, config: Config) -> None:
        from . import db

        for d in (self.root, self.batches_dir, self.review_dir, self.report_dir, self.irrelevant_dir):
            d.mkdir(parents=True, exist_ok=True)
        config.to_file(self.config_path)
        db.init_db(self.db_path)
        self._config = config

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = Config.from_file(self.config_path)
        return self._config

    def save_config(self) -> None:
        if self._config is not None:
            self._config.to_file(self.config_path)

    def connect(self):
        from . import db

        return db.connect(self.db_path)
