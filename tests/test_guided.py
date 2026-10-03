import json
import os

from docpipe import guided
from docpipe.config import Project


def filled_settings(folder: str, key: str = "AIzaSyFakeKey1234567890", goal_lines=None):
    """Simulate a user typing answers into the template under each heading."""
    goal_lines = goal_lines or ["Brief a new director.", "Ignore marketing material."]
    out = []
    for line in guided.SETTINGS_TEMPLATE.splitlines():
        out.append(line)
        if line.startswith("==== 1"):
            out.append(f'  "{folder}"  ')  # Windows "Copy as path" adds quotes
        elif line.startswith("==== 2"):
            out.append(key)
        elif line.startswith("==== 3"):
            out.extend(goal_lines)
    return "\n".join(out)


def test_parse_filled_settings(tmp_path):
    text = filled_settings(r"C:\Users\Alex\Company Drive")
    values, errors = guided.parse_settings(text)
    assert errors == []
    assert values["folder"] == r"C:\Users\Alex\Company Drive"  # quotes stripped
    assert values["api_key"] == "AIzaSyFakeKey1234567890"
    assert "Ignore marketing material." in values["goal"]


def test_parse_untouched_template_reports_all_sections():
    values, errors = guided.parse_settings(guided.SETTINGS_TEMPLATE)
    assert len(errors) == 3


def test_parse_rejects_bad_api_key():
    text = filled_settings("/tmp", key="not a key")
    _, errors = guided.parse_settings(text)
    assert any("API key" in e for e in errors)


def test_first_run_creates_settings_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    messages = []
    guided.run(tmp_path / "proj", echo=messages.append)
    settings = tmp_path / guided.SETTINGS_FILENAME
    assert settings.exists()
    assert settings.read_text(encoding="utf-8") == guided.SETTINGS_TEMPLATE
    assert any("Welcome" in m for m in messages)
    assert not (tmp_path / "proj").exists()  # no project until settings are filled


def test_run_reports_problems_in_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / guided.SETTINGS_FILENAME).write_text(guided.SETTINGS_TEMPLATE, encoding="utf-8")
    messages = []
    guided.run(tmp_path / "proj", echo=messages.append)
    assert any("needs attention" in m for m in messages)
    assert not (tmp_path / "proj").exists()


def test_run_creates_project_from_settings(tmp_path, monkeypatch):
    src = tmp_path / "docs"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (tmp_path / guided.SETTINGS_FILENAME).write_text(
        filled_settings(str(src)), encoding="utf-8"
    )
    ran = []
    monkeypatch.setattr(guided, "_run_pipeline", lambda proj, echo, ask: ran.append(proj))

    guided.run(tmp_path / "proj", echo=lambda m: None)

    assert len(ran) == 1
    proj = Project(tmp_path / "proj")
    assert proj.exists()
    cfg_data = json.loads(proj.config_path.read_text())
    assert cfg_data["source_dir"] == str(src.resolve())
    assert "Brief a new director." in cfg_data["prompt"]
    assert os.environ["GEMINI_API_KEY"] == "AIzaSyFakeKey1234567890"


def test_rerun_updates_changed_goal(tmp_path, monkeypatch):
    src = tmp_path / "docs"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / guided.SETTINGS_FILENAME
    monkeypatch.setattr(guided, "_run_pipeline", lambda proj, echo, ask: None)

    settings.write_text(filled_settings(str(src)), encoding="utf-8")
    guided.run(tmp_path / "proj", echo=lambda m: None)

    settings.write_text(
        filled_settings(str(src), goal_lines=["A different goal."]), encoding="utf-8"
    )
    guided.run(tmp_path / "proj", echo=lambda m: None)

    proj = Project(tmp_path / "proj")
    assert json.loads(proj.config_path.read_text())["prompt"] == "A different goal."
