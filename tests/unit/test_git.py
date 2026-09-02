"""Tests for the git helpers."""

from __future__ import annotations

import pytest

from agent_backbone.git import parse_github_remote


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/app", "acme/app"),
        ("https://github.com/acme/app.git", "acme/app"),
        ("https://user@github.com/acme/app.git", "acme/app"),
        ("git@github.com:acme/app.git", "acme/app"),
        ("ssh://git@github.com/acme/app", "acme/app"),
        ("https://github.com/acme/app/", "acme/app"),
        # only github.com itself: lookalike hosts must not become "acme/app"
        ("https://evilgithub.com/acme/app", ""),
        ("https://github.com.evil.example/acme/app", ""),
        ("git@gitlab.com:acme/app.git", ""),
        ("https://example.com/github.com/acme/app", ""),
        ("", ""),
    ],
)
def test_parse_github_remote(url, expected):
    assert parse_github_remote(url) == expected
