"""Help endpoints — the backbone describes its own capabilities to agents."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config
from agent_backbone.help import get_doc, get_topic, list_docs, list_topics

router = APIRouter(prefix="/api", tags=["help"])


@router.get("/docs")
async def docs_index():
    """The documentation pages shipped with this install, with one-line summaries."""
    return {"items": list_docs()}


@router.get("/docs/{page}")
async def docs_page(page: str):
    """One documentation page's full markdown."""
    content = get_doc(page)
    if content is None:
        known = ", ".join(p["name"] for p in list_docs())
        raise HTTPException(status_code=404, detail=f"unknown page '{page}' — try: {known}")
    return {"name": page, "content": content}


@router.get("/help")
async def help_index(config=Depends(get_config)):
    """Available help topics with one-line summaries."""
    return {"items": list_topics(config.data_dir)}


@router.get("/help/{topic}")
async def help_topic(topic: str, config=Depends(get_config)):
    """One topic's full markdown."""
    content = get_topic(topic, config.data_dir)
    if content is None:
        known = ", ".join(t["name"] for t in list_topics(config.data_dir))
        raise HTTPException(status_code=404, detail=f"unknown topic '{topic}' — try: {known}")
    return {"name": topic, "content": content}
