"""Provider reservations persist without adopting or activating an installation."""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import text

from agent_backbone.config import sqlite_url
from agent_backbone.services.database import BackboneDB, ProfileIdentity


async def test_unreserved_read_has_no_identity_or_signing_effect(db):
    assert await db.profile_identity.read() is None
    async with db.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM signing_install")) == 0


async def test_reservation_preserves_signing_audience_across_restart(tmp_path):
    url = sqlite_url(tmp_path / "provider.db")
    async with BackboneDB.connect(url) as db:
        audience = await db.signing.audience()
        identity = ProfileIdentity("profile-a", audience, "lifecycle-a1")
        assert await db.profile_identity.reserve(identity) == identity
    async with BackboneDB.connect(url) as db:
        assert await db.profile_identity.read() == identity
        assert await db.profile_identity.reserve(identity) == identity
        assert await db.signing.audience() == audience


@pytest.mark.parametrize("field", ["profile_id", "installation_id", "lifecycle_id"])
async def test_conflicting_reservation_never_overwrites(db, field):
    identity = ProfileIdentity("profile-a", await db.signing.audience(), "lifecycle-a1")
    await db.profile_identity.reserve(identity)
    with pytest.raises(ValueError, match="reservation does not match"):
        await db.profile_identity.reserve(replace(identity, **{field: "different"}))
    assert await db.profile_identity.read() == identity
    assert await db.signing.audience() == identity.installation_id


async def test_reservation_requires_existing_matching_audience(db):
    identity = ProfileIdentity("profile-a", "other-installation", "lifecycle-a1")
    with pytest.raises(ValueError, match="reservation does not match"):
        await db.profile_identity.reserve(identity)
    assert await db.profile_identity.read() is None
    async with db.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM signing_install")) == 0
    audience = await db.signing.audience()
    with pytest.raises(ValueError, match="reservation does not match"):
        await db.profile_identity.reserve(identity)
    assert await db.profile_identity.read() is None
    assert await db.signing.audience() == audience


@pytest.mark.parametrize("same_input", [True, False])
async def test_concurrent_file_reservations_are_atomic(tmp_path, same_input):
    url = sqlite_url(tmp_path / "provider.db")
    async with BackboneDB.connect(url) as first, BackboneDB.connect(url) as second:
        identity = ProfileIdentity("profile-a", await first.signing.audience(), "lifecycle-a1")
        competing = identity if same_input else replace(identity, profile_id="profile-b")
        results = await asyncio.gather(
            first.profile_identity.reserve(identity),
            second.profile_identity.reserve(competing),
            return_exceptions=True,
        )
        if same_input:
            assert results == [identity, identity]
        else:
            assert sum(isinstance(result, ValueError) for result in results) == 1
        stored = await first.profile_identity.read()
        assert stored in (identity, competing)
        assert all(result == stored for result in results if not isinstance(result, Exception))
        assert await second.profile_identity.read() == stored


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE profile_identity SET schema_version = 2",
        "UPDATE profile_identity SET profile_id = ''",
        "UPDATE signing_install SET audience = 'replaced'",
        "DELETE FROM signing_install",
    ],
)
async def test_invalid_persisted_identity_is_not_repaired(db, statement):
    identity = ProfileIdentity("profile-a", await db.signing.audience(), "lifecycle-a1")
    await db.profile_identity.reserve(identity)
    async with db.engine.begin() as conn:
        await conn.execute(text(statement))
    with pytest.raises(ValueError):
        await db.profile_identity.read()
    with pytest.raises(ValueError):
        await db.profile_identity.reserve(identity)


async def test_old_installation_schema_repair_does_not_adopt(tmp_path):
    url = sqlite_url(tmp_path / "provider.db")
    async with BackboneDB.connect(url) as db:
        audience = await db.signing.audience()
        async with db.engine.begin() as conn:
            await conn.execute(text("DROP TABLE profile_identity"))
            await conn.execute(text("UPDATE alembic_version SET version_num = 'old-revision'"))
    async with BackboneDB.connect(url) as db:
        assert await db.profile_identity.read() is None
        assert await db.signing.audience() == audience


@pytest.mark.parametrize("invalid", ["", "two words", "line\nbreak", "\x00", "\x7f", "é", None])
def test_invalid_identity_is_rejected_before_reservation(invalid):
    with pytest.raises(ValueError, match="Invalid profile identity"):
        ProfileIdentity(invalid, "installation-a", "lifecycle-a1")
