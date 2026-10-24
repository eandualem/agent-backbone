# Responses — make a direct answer easy to find

When your answer is ready, explicitly save the question, answer and resulting
action or remaining decision. Continue working normally; no pause is required.

```bash
backbone respond --example
backbone respond --file answer.json --validate
backbone respond --file answer.json
backbone responses --agent app
backbone responses show 123
backbone respond --file revised-answer.json --supersedes 123
```

The file contains plain-text `question`, `answer` and `action`. Limits are 500,
6,000 and 1,000 characters respectively. Use `--schema` for details, `--file -`
for stdin and `--json` for a full receipt. Your agent session supplies the author;
outside it, use `--agent NAME`. Retry the same file and key after a failed request.
The default key hashes the content and correction ID.

The full response goes to your existing authorized Telegram topic with durable
text retries and optional existing audio. A publication receipt means stored;
`telegram_delivery=sent` means Telegram accepted all chunks. Audio failure cannot
block the text. Progress reports remain separate. Swarm participants send findings
to their initiating repository agent instead of publishing human-facing responses.

Use `--supersedes ID` only for an explicit correction to your own retained response;
it preserves the original. Responses are retained using the existing delivery
retention setting, with the latest per author kept. Read `backbone docs responses`
for paging, delivery limits, corrections, audio and API details.
