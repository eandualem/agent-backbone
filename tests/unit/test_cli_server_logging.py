"""The backbone's log: every line timestamped in UTC, the file rotated by size."""

from __future__ import annotations

import logging
import logging.config
import re

import pytest

from agent_backbone.cli.server import log_config
from tests.conftest import make_config

_LINE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z (INFO|WARNING) [\w.]+: ")


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def test_uvicorn_and_backbone_lines_carry_utc_timestamps(tmp_path, restore_logging):
    log = tmp_path / "backbone.log"
    logging.config.dictConfig(log_config(make_config(tmp_path), log))
    access = logging.getLogger("uvicorn.access")
    access.setLevel(logging.INFO)  # uvicorn sets it from log_level
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET", "/api/agents", "1.1", 200)
    logging.getLogger("agent_backbone.services.jobs").warning("Job %s failed", "prune")
    first, second = log.read_text().splitlines()
    assert _LINE.match(first) and first.endswith(
        'uvicorn.access: 127.0.0.1:5 - "GET /api/agents HTTP/1.1" 200'
    )
    assert _LINE.match(second) and second.endswith("Job prune failed")


def test_the_log_file_rotates_at_its_size_keeping_its_backups(tmp_path, restore_logging):
    log = tmp_path / "backbone.log"
    config = make_config(tmp_path, settings={"backbone.log_max_mb": 1, "backbone.log_backups": 2})
    logging.config.dictConfig(log_config(config, log))
    for _ in range(4000):
        logging.getLogger("agent_backbone").warning("x" * 1000)
    assert sorted(path.name for path in tmp_path.glob("backbone.log*")) == [
        "backbone.log",
        "backbone.log.1",
        "backbone.log.2",
    ]
    assert log.stat().st_size <= 1024 * 1024


def test_without_a_log_file_lines_go_to_stderr(tmp_path, restore_logging, capsys):
    logging.config.dictConfig(log_config(make_config(tmp_path), None))
    logging.getLogger("agent_backbone").warning("restarting onto the new code")
    assert _LINE.match(capsys.readouterr().err)
