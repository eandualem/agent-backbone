"""Complete plain-text answers, split without dropping text or splitting emoji."""

import unicodedata

from agent_backbone.models import AgentResponse


def response_text(record: dict) -> str:
    response = AgentResponse.model_validate(record["response"])
    # Chunk boundaries must stay stable across retries, including agent renames.
    author = record["author_name"]
    lines = [f"Response {record['id']} · {author}", record["created_at"]]
    if record.get("supersedes_id"):
        lines.append(f"Updates Response {record['supersedes_id']}")
    lines.extend(
        [
            f"\nQuestion\n{response.question}",
            f"\nAnswer\n{response.answer}",
            f"\nAction or decision\n{response.action}",
        ]
    )
    return "\n".join(lines)


def _joined_characters(text: str):
    """Keep combining marks, emoji modifiers, joiners and flag pairs together."""
    cluster, regional = "", 0
    for char in text:
        is_regional = "\U0001f1e6" <= char <= "\U0001f1ff"
        joined = (
            unicodedata.category(char).startswith("M")
            or char == "\u200d"
            or cluster.endswith("\u200d")
            or "\U0001f3fb" <= char <= "\U0001f3ff"
            or "\U000e0020" <= char <= "\U000e007f"
            or (is_regional and regional % 2 == 1)
        )
        if cluster and not joined:
            yield cluster
            cluster = ""
        cluster += char
        regional = regional + 1 if is_regional else 0
    if cluster:
        yield cluster


def response_chunks(record: dict, *, joined: bool = True) -> list[str]:
    chunks, current, units = [], [], 0
    text = response_text(record)
    characters = _joined_characters(text) if joined else text
    # An unbounded combining sequence can exceed Telegram's entire message limit.
    # Split only these oversized sequences at code points to preserve all text.
    bounded = (
        part
        for sequence in characters
        for part in (sequence if len(sequence.encode("utf-16-le")) // 2 > 3500 else [sequence])
    )
    for char in bounded:
        width = len(char.encode("utf-16-le")) // 2
        if units + width > 3500:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += width
    if current:
        chunks.append("".join(current))
    return [
        text if index == 0 else f"Response {record['id']} (continued)\n{text}"
        for index, text in enumerate(chunks)
    ]
