"""The provenance envelope Backbone writes before a message it delivers.

The marker ``owner-confirmed:<id>`` is written only from a verified
confirmation (docs/owner-confirmed-messages.md), and any body line that looks
like an envelope is shown as quoted, so no text can pass for one.
"""

from __future__ import annotations

import unicodedata

RELAY_LABEL = "(signed relay, not owner-confirmed)"
STEER_LABEL = "(steer for your current task)"


# Letters that render as nothing (Hangul fillers); NFKC keeps them.
_BLANK_LETTERS = frozenset("\u115f\u1160\u3164\uffa0")


def _invisible(c: str) -> bool:
    """Control, format, unassigned and private characters, marks that
    combine with nothing, and blank letters: none shows before ``[via:``."""
    category = unicodedata.category(c)
    return category[0] == "C" or category in {"Mn", "Me"} or c in _BLANK_LETTERS


def _starts_like_envelope(line: str) -> bool:
    """Compared after NFKC, without invisible characters, so ``\u200b[via:``,
    ``\u034f[via:`` or a fullwidth ``［via:`` counts too."""
    folded = unicodedata.normalize("NFKC", line)
    visible = "".join(c for c in folded if not _invisible(c))
    return visible.lstrip().casefold().startswith("[via:")


def quote_envelope_lines(text: str) -> str:
    """Prefix ``[quoted] `` to every line that starts like an envelope."""
    return "".join(
        "[quoted] " + line if _starts_like_envelope(line) else line
        for line in text.splitlines(keepends=True)
    )


def envelope(
    sender: str,
    text: str,
    *,
    confirmation_id: str | None = None,
    relay: bool = False,
    steer: bool = False,
) -> str:
    """``[via:backbone from:<sender>[ owner-confirmed:<id>]] [labels] <text>``.

    ``relay`` marks a signed request that the owner didn't confirm."""
    head = f"[via:backbone from:{sender}"
    if confirmation_id:
        head += f" owner-confirmed:{confirmation_id}"
    parts = [head + "]"]
    if steer:
        parts.append(STEER_LABEL)
    if relay and not confirmation_id:
        parts.append(RELAY_LABEL)
    parts.append(quote_envelope_lines(text))
    return " ".join(parts)
