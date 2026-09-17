"""The Gmail source: the search it runs and what it makes of the reply."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from agent_backbone.services.sources import build_sources
from agent_backbone.services.sources.gmail import (
    GmailSource,
    clean_text,
    parse_fetch,
    search_query,
)
from tests.conftest import make_config

_FETCH = [
    (
        b'1 (X-GM-MSGID 1990000000000000001 INTERNALDATE "17-Sep-2026 14:02:33 +0000" '
        b"BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {120}",
        b"From: Upwork <donotreply@upwork.com>\r\n"
        b"Subject: =?UTF-8?Q?New_job:_Python_scraper?=\r\nDate: x\r\n\r\n",
    ),
    b")",
    (
        b'2 (X-GM-MSGID 1990000000000000002 INTERNALDATE "17-Sep-2026 09:00:00 +0000" '
        b"BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {40}",
        b"From: a@b.c\r\nSubject: old\r\n\r\n",
    ),
    b")",
]


def test_search_query_is_gmail_syntax_bounded_to_the_window():
    since = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    assert search_query("from:upwork.com subject:job", since) == (
        f"(from:upwork.com subject:job) after:{int(since.timestamp())}"
    )


def test_clean_text_is_one_clipped_line():
    assert clean_text(" a\r\nb\t c ") == "a b c"
    assert clean_text("x" * 200).endswith("…")


def test_parse_fetch_decodes_headers_and_ids():
    items = parse_fetch(_FETCH)
    assert [item[0] for item in items] == [
        format(1990000000000000001, "x"),
        format(1990000000000000002, "x"),
    ]
    assert items[0][1] == datetime(2026, 9, 17, 14, 2, 33, tzinfo=UTC)
    assert items[0][2] == "Upwork <donotreply@upwork.com>"
    assert items[0][3] == "New job: Python scraper"


class _FakeImap:
    instances: list[_FakeImap] = []

    def __init__(self, host):
        self.host = host
        self.calls: list[tuple] = []
        self.literal = None
        _FakeImap.instances.append(self)

    def login(self, user, password):
        self.calls.append(("login", user))

    def list(self):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\All \\HasNoChildren) "/" "[Gmail]/Tous les messages"',
        ]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"3"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [b"1 2" if "upwork" in args[-1] else b""]
        return "OK", _FETCH

    def logout(self):
        self.calls.append(("logout",))


async def test_poll_runs_one_search_per_filter_and_keeps_the_window(tmp_path):
    _FakeImap.instances.clear()
    config = make_config(tmp_path, gmail_address="me@gmail.com", gmail_app_password="pw")
    source = GmailSource(config)
    assert source.enabled
    since = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    with patch("agent_backbone.services.sources.gmail.imaplib.IMAP4_SSL", _FakeImap):
        events = await source.poll(["from:upwork.com", "from:nobody"], since)
    client = _FakeImap.instances[0]
    assert ("login", "me@gmail.com") in client.calls
    assert ("select", '"[Gmail]/Tous les messages"', True) in client.calls
    searches = [c for c in client.calls if len(c) > 1 and c[1] == "SEARCH"]
    assert len(searches) == 2
    assert searches[0][2:] == ("X-GM-RAW", f'"(from:upwork.com) after:{int(since.timestamp())}"')
    assert client.calls[-1] == ("logout",)
    # The older message is inside the reply but outside the window.
    assert [event.id for event in events] == [format(1990000000000000001, "x")]
    event = events[0]
    assert event.source == "gmail"
    assert event.filters == frozenset({"from:upwork.com"})
    assert event.link.endswith(event.id)
    assert event.subject == "New job: Python scraper"


async def test_unconfigured_source_is_disabled_and_polls_nothing(tmp_path):
    config = make_config(tmp_path)
    sources = build_sources(lambda: config)
    assert sources.health() == {"gmail": "disabled"}
    assert sources.enabled == []
    assert await sources.get("gmail").poll(["from:x"], datetime.now(UTC)) == []
