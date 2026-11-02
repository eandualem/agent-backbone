"""Provider-local identity reservation; activation requires registry reconciliation."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo


@dataclass(frozen=True)
class ProfileIdentity:
    """The persisted tuple, independent of names, credentials and runtime selection."""

    profile_id: str
    installation_id: str
    lifecycle_id: str

    def __post_init__(self) -> None:
        for value in (self.profile_id, self.installation_id, self.lifecycle_id):
            if (
                not isinstance(value, str)
                or not value
                or any(not 0x21 <= ord(c) <= 0x7E for c in value)
            ):
                raise ValueError("Invalid profile identity")


async def _read(conn) -> ProfileIdentity | None:
    rows = (await conn.execute(text("SELECT * FROM profile_identity"))).mappings().all()
    if not rows:
        return None
    if len(rows) != 1 or rows[0]["id"] != 1 or rows[0]["schema_version"] != 1:
        raise ValueError("Unsupported provider identity metadata")
    row = rows[0]
    identity = ProfileIdentity(row["profile_id"], row["installation_id"], row["lifecycle_id"])
    audience = await conn.scalar(text("SELECT audience FROM signing_install WHERE id = 1"))
    if audience != identity.installation_id:
        raise ValueError("Provider identity does not match the signing installation")
    return identity


class ProfileIdentityRepo(Repo):
    """Reserve once; never infer adoption or rotate an identity during a read."""

    async def read(self) -> ProfileIdentity | None:
        """Read validated metadata without allocating an identity or signing audience."""
        async with self._tx() as conn:
            return await _read(conn)

    async def reserve(self, identity: ProfileIdentity) -> ProfileIdentity:
        """Persist a control-plane reservation, or verify an exact replay.

        The caller must already have obtained this database's signing audience.
        Conflicting input never overwrites metadata. This does not adopt Default,
        make a provider ready, or grant unbound-client compatibility. Replacement
        needs an external lifecycle guard and is deliberately not an upsert here.
        """
        async with self._tx() as conn:
            # The insert is the first statement: concurrent SQLite reservations
            # serialize before reading, rather than upgrading competing snapshots.
            await conn.execute(
                text(
                    "INSERT INTO profile_identity"
                    " (id, schema_version, profile_id, installation_id, lifecycle_id)"
                    " SELECT 1, 1, :profile, :installation, :lifecycle"
                    " WHERE EXISTS (SELECT 1 FROM signing_install"
                    " WHERE id = 1 AND audience = :installation)"
                    " ON CONFLICT(id) DO NOTHING"
                ),
                {
                    "profile": identity.profile_id,
                    "installation": identity.installation_id,
                    "lifecycle": identity.lifecycle_id,
                },
            )
            stored = await _read(conn)
            if stored != identity:
                raise ValueError("Provider identity reservation does not match")
            return stored
