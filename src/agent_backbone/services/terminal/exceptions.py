"""Terminal service exceptions."""

from agent_backbone.base import BackboneError


class TmuxError(BackboneError):
    category = "tmux"
    severity = "medium"
    retry_allowed = True
