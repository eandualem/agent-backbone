"""The package description PyPI shows: the README with its links made absolute."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_package_description_has_no_relative_links():
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    # A static `readme` builds without error but skips the hook below.
    assert "readme" in pyproject["project"]["dynamic"] and "readme" not in pyproject["project"]
    hook = pyproject["tool"]["hatch"]["metadata"]["hooks"]["fancy-pypi-readme"]
    text = (_ROOT / "README.md").read_text()
    for sub in hook["substitutions"]:
        text = re.sub(sub["pattern"], sub["replacement"], text)
    targets = re.findall(r'(?:\]\(|src="|href=")([^)"]*)', text)
    assert targets
    assert [t for t in targets if not t.startswith(("https://", "#"))] == []
    images = re.findall(r'src="([^"]*)"', text)
    assert all(i.startswith("https://raw.githubusercontent.com/") for i in images)
