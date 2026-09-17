"""Gmail as a source — read-only IMAP with Gmail's own search syntax.

Subscriptions are written in Gmail search syntax (``from:``, ``subject:``,
``list:``, ``label:``, ``newer_than:`` …) and Gmail evaluates them: the
backbone sends each distinct filter as one ``X-GM-RAW`` search over the
account's *All Mail* folder, bounded to the poll window, then fetches only
the headers of what matched. Bodies are never read; the mailbox is opened
read-only (``EXAMINE``, ``BODY.PEEK``), so nothing is marked read.

Credential: an **app password** (Google account → Security → 2-Step
Verification → App passwords) in the data-dir ``.env`` as ``GMAIL_ADDRESS``
and ``GMAIL_APP_PASSWORD``. Standard-library ``imaplib`` — no Google Cloud
project, no OAuth client. Without the two variables the source is disabled.
"""

from __future__ import annotations

import asyncio
import email.header
import email.parser
import imaplib
import logging
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime

from agent_backbone.config import BackboneConfig
from agent_backbone.services.sources.base import Source, SourceEvent

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
_FETCH_PARTS = "(X-GM-MSGID INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
_MSGID_RE = re.compile(rb"X-GM-MSGID (\d+)")
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')
_FETCH_CHUNK = 50
"""UIDs per FETCH command; every match in the window is fetched, in chunks."""
_TEXT_LIMIT = 120
_TIMEOUT = 30
"""Seconds per IMAP socket operation: a stalled server must not hang the poll thread."""


def message_link(message_id: str) -> str:
    return f"https://mail.google.com/mail/#all/{message_id}"


def search_query(filter_text: str, since: datetime) -> str:
    """The Gmail search the source runs for one subscription filter."""
    return f"({filter_text}) after:{int(since.timestamp())}"


def _quoted(query: str) -> str:
    return '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'


def clean_text(value: str | None) -> str:
    """One printable line, clipped — sender and subject are untrusted text.

    Control and format characters (escape sequences, zero-width marks,
    bidirectional overrides) are dropped, not just whitespace."""
    kept = "".join(
        ch for ch in (value or "") if ch.isspace() or not unicodedata.category(ch).startswith("C")
    )
    text = " ".join(kept.split())
    return text[:_TEXT_LIMIT] + ("…" if len(text) > _TEXT_LIMIT else "")


def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:
        return raw


def parse_fetch(response: list) -> list[tuple[str, datetime, str, str]]:
    """``(message id, internal date, sender, subject)`` per message in a FETCH reply."""
    items: list[tuple[str, datetime, str, str]] = []
    for part in response:
        if not isinstance(part, tuple) or len(part) < 2:
            continue
        meta, headers = part[0], part[1]
        msgid = _MSGID_RE.search(meta)
        when = _INTERNALDATE_RE.search(meta)
        if msgid is None or when is None:
            continue
        try:
            received_at = datetime.strptime(
                when.group(1).decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z"
            ).astimezone(UTC)
        except (UnicodeDecodeError, ValueError):
            continue
        message = email.parser.BytesHeaderParser().parsebytes(headers)
        items.append(
            (
                format(int(msgid.group(1)), "x"),
                received_at,
                clean_text(_decode_header(message.get("From"))),
                clean_text(_decode_header(message.get("Subject"))),
            )
        )
    return items


class GmailSource(Source):
    name = "gmail"

    @property
    def enabled(self) -> bool:
        return self.config.gmail_ready

    async def poll(self, filters: Sequence[str], since: datetime) -> list[SourceEvent]:
        config = self.config
        if not config.gmail_ready or not filters:
            return []
        return await asyncio.to_thread(self._poll, list(filters), since, config)

    # -- blocking IMAP work, run in a thread --------------------------------

    def _poll(self, filters: list[str], since: datetime, config: BackboneConfig):
        client = imaplib.IMAP4_SSL(IMAP_HOST, timeout=_TIMEOUT)
        try:
            client.login(config.gmail_address, config.gmail_app_password)
            client.select(self._all_mail(client), readonly=True)
            found: dict[str, tuple[datetime, str, str, set[str]]] = {}
            failed = 0
            for filter_text in filters:
                try:
                    matches = self._search(client, filter_text, since)
                except Exception as exc:
                    # One agent's unusable filter must not starve the others.
                    failed += 1
                    log.warning("Gmail search failed for filter %r: %s", filter_text, exc)
                    continue
                for msgid, received_at, sender, subject in matches:
                    if received_at < since:
                        continue  # the search window is a hint; the cursor is the rule
                    entry = found.setdefault(msgid, (received_at, sender, subject, set()))
                    entry[3].add(filter_text)
            if failed and failed == len(filters):
                raise RuntimeError("every Gmail search failed")
        finally:
            try:
                client.logout()
            except Exception:
                log.debug("Gmail logout failed (ignored)")
        return sorted(
            (
                SourceEvent(
                    source=self.name,
                    id=msgid,
                    sender=sender,
                    subject=subject,
                    received_at=received_at,
                    link=message_link(msgid),
                    filters=frozenset(matched),
                )
                for msgid, (received_at, sender, subject, matched) in found.items()
            ),
            key=lambda event: (event.received_at, event.id),
        )

    @staticmethod
    def _all_mail(client: imaplib.IMAP4) -> str:
        """The account's *All Mail* folder (its name is localised); else INBOX.

        Returned as the LIST reply spells it (quoted when it has spaces):
        ``imaplib`` sends command arguments verbatim.
        """
        status, boxes = client.list()
        if status == "OK":
            for box in boxes or ():
                match = _LIST_RE.match(box) if isinstance(box, bytes) else None
                if match and rb"\All" in match.group("flags").split():
                    return match.group("name").decode("utf-8", "replace")
        return "INBOX"

    @staticmethod
    def _search(client: imaplib.IMAP4, filter_text: str, since: datetime):
        query = search_query(filter_text, since)
        try:
            query.encode("ascii")
        except UnicodeEncodeError:
            client.literal = query.encode("utf-8")
            status, data = client.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
        else:
            status, data = client.uid("SEARCH", "X-GM-RAW", _quoted(query))
        if status != "OK":
            raise RuntimeError(f"Gmail search failed for a filter: {status}")
        uids = (data[0] or b"").split()
        items: list[tuple[str, datetime, str, str]] = []
        for offset in range(0, len(uids), _FETCH_CHUNK):
            chunk = b",".join(uids[offset : offset + _FETCH_CHUNK])
            status, response = client.uid("FETCH", chunk.decode(), _FETCH_PARTS)
            if status != "OK":
                raise RuntimeError(f"Gmail fetch failed: {status}")
            items.extend(parse_fetch(response))
        return items
