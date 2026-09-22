# Setting up GitHub: the App + webhook walkthrough

This is the recommended production setup, written as a checklist. At the end:

- GitHub **pushes** every issue, comment and PR event to your machine the
  moment it happens (no polling latency);
- **every repository on your account is covered automatically** — including
  ones you create later — with a single app-level webhook, nothing to
  configure per repository;
- the backbone acts as its **own bot identity** on GitHub, not as you.

Time: ~15 minutes, once. Every step ends with a checkpoint so you know it
worked before moving on. (If you just want to try the backbone without any
of this: run `gh auth token | backbone secrets set GITHUB_TOKEN`, then
`backbone service restart` — that enables polling every 60 seconds without
a public endpoint. Terminal delivery still waits for agent readiness.)

## 0. Prerequisites

- The CLI installed **with the `github-app` extra** (App auth needs it):
  `uv tool install "agent-backbone[github-app]"`.
  Missing it fails at startup with a message naming the extra; `backbone
  doctor` checks it too.
- The backbone initialised and running: `backbone init && backbone up --detach`.
- `backbone doctor` reports the API up on `127.0.0.1:7120`.
- The tunnel commands below use Homebrew on macOS. On Linux, install the
  tunnel client with its provider’s Linux instructions.
- A GitHub account. A domain on Cloudflare is ideal but **not required** —
  step 1 has an ngrok variant.

## 1. A stable public URL for the webhook

Give GitHub a stable public URL: `https://<host>/webhooks/github`. The tunnel
forwards requests from that URL to the local origin
`http://127.0.0.1:7120/webhooks/github`. Pick **one** tunnel below:

### 1a. Cloudflare Tunnel (with a domain on Cloudflare)

```bash
brew install cloudflared
cloudflared tunnel login                # browser opens; pick your zone
cloudflared tunnel create backbone
cloudflared tunnel route dns backbone hooks.example.com   # creates the DNS record for you
```

Write `~/.cloudflared/config.yml` — note the `path` rule: only the webhook
endpoint is exposed, everything else on the tunnel 404s:

```yaml
tunnel: <TUNNEL-ID>                                   # printed by `tunnel create`
credentials-file: /Users/you/.cloudflared/<TUNNEL-ID>.json
ingress:
  - hostname: hooks.example.com
    path: ^/webhooks/github$
    service: http://127.0.0.1:7120
  - service: http_status:404
```

Test in the foreground first:

```bash
cloudflared tunnel run backbone
```

> **Checkpoint** — from any network:
> `curl -i -X POST https://hooks.example.com/webhooks/github` → **HTTP 403**
> (that is the backbone answering "no signature"; Cloudflare errors would be
> 530). `curl https://hooks.example.com/health` → **404** (path filter).
> If curl says *could not resolve host*, your local DNS cached an earlier
> failure — wait a minute or test with `dig @1.1.1.1 hooks.example.com`.

For persistence, follow Cloudflare's service instructions for
[macOS](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/as-a-service/macos/)
or [Linux](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/as-a-service/linux/).
On macOS, a login agent uses `~/.cloudflared/`; a boot daemon uses
`/etc/cloudflared/`. Place the configuration and credentials in the location
for the service you choose, and verify the credentials path in `config.yml`.

> **Checkpoint** — after stopping the foreground tunnel, confirm
> `cloudflared tunnel info backbone` still lists the service's connector and
> repeat both URL checks above. Consult the service log if it does not.

### 1b. ngrok (no domain needed — fine for testing and evaluation)

The free plan supplies an assigned development domain. Check
[ngrok's current limits](https://ngrok.com/docs/pricing-limits/free-plan-limits)
and use the domain shown for your account:

```bash
brew install ngrok
ngrok config add-authtoken <token>        # from dashboard.ngrok.com
ngrok http 7120
```

Append `/webhooks/github` to the HTTPS forwarding URL ngrok prints.

> **Checkpoint** — `curl -i -X POST <url>/webhooks/github` should reach the
> backbone's signature check once its webhook secret is configured.

This simple command forwards the whole API, so routes other than the webhook
are also reachable at that hostname. The API key still applies. Use the
Cloudflare path-filtered example if you only want the webhook exposed. Keep the
ngrok process running while receiving events.

## 2. Create the GitHub App

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**
(on your personal account; an organisation works the same way).

| Field | Value |
|---|---|
| Name | anything unique, e.g. `yourname-backbone` — comments will appear as `yourname-backbone[bot]` |
| Homepage URL | anything, e.g. this repository |
| **Webhook → Active** | ✓ |
| **Webhook URL** | `https://<your host>/webhooks/github` — **include the `/webhooks/github` path**; a bare hostname is the most common mistake (you'll see 530/404 in the delivery log) |
| **Webhook secret** | generate one: `openssl rand -hex 32` — keep it, step 4 needs the exact same string |
| **Repository permissions** | *Issues: Read and write* · *Pull requests: Read and write* (Metadata: Read-only is added automatically) |
| **Subscribe to events** | *Issues* · *Issue comment* · *Pull request* · *Pull request review* |
| Where can this app be installed | Only on this account |

Create the app, then on its **General** page:

- note the **App ID** (a number near the top);
- **Private keys → Generate a private key** — a `.pem` file downloads.

## 3. Install the app on your account

App page → **Install App** (left sidebar) → your account → **All
repositories** → Install.

*All repositories* is the point of the whole exercise: any repo you create
next year is covered with zero further setup. It is safe — the backbone
ignores events from repositories no agent owns or watches.

> **Checkpoint** — the app's **Advanced** tab shows a `ping` and an
> `installation` delivery. They may show ✗ if the backbone isn't configured
> yet — that's fine, keep going.

## 4. Point the backbone at the app

```bash
mv ~/Downloads/<app-name>.*.private-key.pem ~/.local/share/agent-backbone/github-app.pem
chmod 600 ~/.local/share/agent-backbone/github-app.pem
```

In `~/.local/share/agent-backbone/.env` (create lines, don't uncomment the
template comments halfway):

```bash
GITHUB_APP_ID=<the number from step 2>
GITHUB_APP_PRIVATE_KEY_PATH=/Users/you/.local/share/agent-backbone/github-app.pem
GITHUB_WEBHOOK_SECRET=<the exact secret from step 2>
# remove or comment out GITHUB_TOKEN — a token takes precedence over the App
```

Restart and check (use `backbone service restart` if installed as a login service):

```bash
backbone down && backbone up --detach
backbone doctor        # → ✓ GitHub credentials found — intake: webhook
```

> **Checkpoint** — the backbone can read GitHub as the app. For the curl
> examples, set `BACKBONE_API_KEY` in your shell to the key in the file printed
> by `backbone secrets path`; the CLI reads it automatically, curl does not.
> ```bash
> curl -s -H "Authorization: Bearer $BACKBONE_API_KEY" \
>   "http://127.0.0.1:7120/api/issues?repo=you/some-repo" | head -c 200
> ```
> returns JSON, not a 5xx.

## 5. Prove the round trip

Start an agent in some repository, then open a test issue:

```bash
cd ~/code/some-repo && backbone agent start
gh issue create -R you/some-repo --title "[task] webhook test (safe to close)" --body "test"
```

After GitHub delivers the event:

```bash
curl -s -H "Authorization: Bearer $BACKBONE_API_KEY" "http://127.0.0.1:7120/api/events?limit=3"
```

shows an `issue_opened` event. Once the agent is ready, its terminal
(`tmux attach -t some-repo`) shows the `[via:github issue:N] New issue…`
message. Comment on the issue → the agent gets the comment. Close it →
the agent gets its next issue. Done.

## Troubleshooting

Everything you need is in the app's **Advanced → Recent Deliveries** page
(each delivery shows the response code, and has a **Redeliver** button) and
in the backbone's `GET /api/events`.

| Symptom in the delivery log | Cause | Fix |
|---|---|---|
| `530` | webhook URL hostname wrong / no tunnel route for it | fix the URL in the app's Webhook settings; check `cloudflared tunnel info` |
| `404` | URL missing the `/webhooks/github` path (or path filter typo) | add the path |
| `403 Invalid signature` | the secret in the app form ≠ `GITHUB_WEBHOOK_SECRET` in `.env` | re-paste one of them so they're byte-identical, redeliver |
| `couldn't connect` | tunnel/ngrok not running, or backbone down | `cloudflared tunnel info`, `backbone status` |
| `200` but nothing happens | event's repository has no owning/watching agent | `backbone agent list` shows registered repositories and watches |
| Events arrive but only every ~60 s | intake is still `poll` (no `GITHUB_WEBHOOK_SECRET` at restart) | `backbone doctor` → intake; check `.env`, restart |

Missed events while the backbone or tunnel was down are not lost: on
startup the backbone runs one poll over every tracked repository
(`github.backfill_on_start`) and the events table deduplicates, so a
webhook replay plus backfill never double-delivers.

## The fallback: token + per-repository webhooks

Without an App, GitHub can only attach token-manageable webhooks **per
repository** (personal accounts have no account-wide webhook). If you must:
Repo → Settings → Webhooks → Add webhook, same URL/secret, content type
`application/json`, events *Issues / Issue comments / Pull requests / Pull
request reviews* — for each repository. This is tedious by design of GitHub's permission model,
which is why the App path above is the default recommendation.
