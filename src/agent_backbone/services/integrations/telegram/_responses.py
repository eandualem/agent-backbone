"""Complete plain-text answers, split without dropping text or splitting emoji."""

from agent_backbone.models import AgentResponse


def response_text(record: dict) -> str:
    response = AgentResponse.model_validate(record["response"])
    author = record["agent_name"] or record["author_name"]
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


def response_chunks(record: dict) -> list[str]:
    chunks, current, units = [], [], 0
    for char in response_text(record):
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
