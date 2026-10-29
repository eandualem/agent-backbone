---
name: backbone-reports
description: Record verified task progress, blockers and next steps as Backbone Reports during authorized work.
metadata:
  backbone-tags: all
---

Read `backbone help reports` and the current `backbone report --schema`. A Report
is authored progress, not an inferred task state or proof that work is complete.

State the current goal, verified progress, concrete blocker if any and next action.
Use the supported fields and real evidence links. Distinguish implementation,
checks, review, merge, deployment and live acceptance. Do not mark the task complete
while required work remains. Retain the returned record identity and verify the
stored result. Follow the installed correction/revision mechanism when correcting
an existing report; do not assume arbitrary editing or deletion exists.
