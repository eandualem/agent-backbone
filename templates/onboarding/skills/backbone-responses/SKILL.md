---
name: backbone-responses
description: Retain a user-facing answer or correction with Backbone Responses when the answer should remain available in the workspace.
metadata:
  backbone-tags: all
---

Read `backbone help responses` and `backbone respond --schema`. Read a prior answer
before correcting or extending it. Keep the question, answer and requested next
action grounded in the actual conversation; do not invent a user decision.

Use the supported correction link for an existing Response, preserving its topic
and attribution. Validate before submission and read back the saved record. Delivery
and audio receipts are separate from storage; do not claim the user received,
heard or accepted the answer merely because it was retained.
