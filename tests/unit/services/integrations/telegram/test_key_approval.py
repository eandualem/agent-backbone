"""/approve_key and /approve_owner: only the owner's account, in an allowed chat."""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

from agent_backbone import signing
from agent_backbone.config import TelegramConfig
from agent_backbone.services.integrations.telegram import TelegramService

ALLOWED_CHAT = 111
OWNER = 4242
PUB = signing.b64url_encode(b"k" * 32)


def _bot(config, db) -> TelegramService:
    return TelegramService(
        replace(config, telegram=TelegramConfig(allowed_chat_ids=(ALLOWED_CHAT,))), db=db
    )


def _update(user_id: int = OWNER, chat_id: int = ALLOWED_CHAT):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    return update


def _context(args: list[str]):
    ctx = MagicMock()
    ctx.args = args
    return ctx


def _reply(update) -> str:
    return update.message.reply_text.await_args.args[0]


async def _pending(db) -> str:
    audience = await db.signing.audience()
    fields = {
        "action": "set",
        "sender": "assistant",
        "audience": audience,
        "expected_epoch": 0,
        "new_fingerprint": signing.fingerprint(b"k" * 32),
        "request_id": "7d3c2b1a-0f9e-4d8c-b7a6-958473625140",
        "expires_at": int(time.time()) + 900,
    }
    digest = signing.transition_digest(**fields)
    await db.signing.start_transition(
        sender_key="assistant", new_public_key=PUB, digest=digest, **fields
    )
    return digest


async def test_without_an_owner_it_says_how_to_set_one(config, db):
    digest = await _pending(db)
    update = _update()
    await _bot(config, db).cmd_approve_key(update, _context([digest]))
    assert f"backbone signing owner {OWNER}" in _reply(update)
    assert await db.signing.enrollment("assistant") is None


async def test_only_the_owner_applies_a_key_change(config, db):
    await db.signing.set_owner(OWNER)
    digest = await _pending(db)
    bot = _bot(config, db)

    stranger = _update(user_id=9)
    await bot.cmd_approve_key(stranger, _context([digest]))
    assert "only by the owner" in _reply(stranger)
    elsewhere = _update(chat_id=999)
    await bot.cmd_approve_key(elsewhere, _context([digest]))
    elsewhere.message.reply_text.assert_not_awaited()
    assert await db.signing.enrollment("assistant") is None

    owner = _update()
    shown = signing.grouped(digest).upper().split()  # as copied, in groups
    await bot.cmd_approve_key(owner, _context(shown))
    assert _reply(owner).startswith("Applied for 'assistant': set")
    assert (await db.signing.enrollment("assistant"))["epoch"] == 1

    again = _update()
    await bot.cmd_approve_key(again, _context([digest]))
    assert "already applied" in _reply(again)


async def test_an_unknown_digest_changes_nothing(config, db):
    await db.signing.set_owner(OWNER)
    update = _update()
    await _bot(config, db).cmd_approve_key(update, _context(["0" * 64]))
    assert "No key change is waiting" in _reply(update)


async def test_the_current_owner_hands_approvals_over(config, db):
    await db.signing.set_owner(OWNER)
    await db.signing.set_owner(5151)  # requested; waits for the current owner
    bot = _bot(config, db)
    requester = _update(user_id=5151)
    await bot.cmd_approve_owner(requester, _context(["5151"]))
    assert "only by the owner" in _reply(requester)
    owner = _update()
    await bot.cmd_approve_owner(owner, _context(["5151"]))
    assert (await db.signing.owner())["telegram_user_id"] == 5151
