import json

import pytest

from docpipe import convert, scan
from docpipe.config import Config, Project


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "drive"
    (src / "legal" / "old").mkdir(parents=True)
    (src / "marketing").mkdir()
    (src / "legal" / "shareholders_2021.txt").write_text(
        "Shareholder register as at 2021-03-01.\nJohn Smith holds 500 ordinary shares."
    )
    # Same content at a second path: must deduplicate to one document.
    (src / "legal" / "old" / "Copy of shareholders_2021.txt").write_text(
        "Shareholder register as at 2021-03-01.\nJohn Smith holds 500 ordinary shares."
    )
    (src / "legal" / "shareholders_2023.md").write_text(
        "# Shareholder register 2023-06-10\nJohn Smith holds 800 ordinary shares."
    )
    (src / "marketing" / "flyer.txt").write_text("Buy our product! Summer promo.")
    # Junk and excluded files.
    (src / ".DS_Store").write_text("junk")
    (src / "legal" / "~$shareholders_2021.docx").write_text("lock")
    (src / "marketing" / "promo.mp4").write_text("binary-ish")
    (src / "empty.txt").write_text("")
    return src


@pytest.fixture
def project(tmp_path, source):
    proj = Project(tmp_path / "proj")
    cfg = Config(source_dir=str(source), prompt="Get a new director up to speed on the company.")
    proj.create(cfg)
    return proj


@pytest.fixture
def converted_project(project):
    scan.run(project, project.config)
    convert.run(project, project.config)
    return project


def make_response_line(key: str, payload: dict) -> dict:
    """Build a batch result line the way the Batch API returns it."""
    return {
        "key": key,
        "response": {
            "candidates": [
                {"content": {"parts": [{"text": json.dumps(payload)}], "role": "model"}}
            ]
        },
    }
