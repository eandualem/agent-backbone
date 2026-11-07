"""Run the basic profile flow for real in a disposable home: no accounts, no model calls.

Needs tmux. Runtimes are inert stand-in scripts, so signing in and running a model are
not covered here. Everything lives under one temporary directory and is removed.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

PORT = 7998
INERT = """#!/bin/sh
case "$1" in --version|-V|version) echo "99.0.0"; exit 0;; esac
env > "$PWD/runtime.env"
printf '> '
exec sleep 600
"""


class Home:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        for name in ("claude", "codex", "opencode"):
            (self.bin / name).write_text(INERT)
            (self.bin / name).chmod(0o755)
        self.data = root / ".local/share/agent-backbone"
        tmux = Path(shutil.which("tmux")).parent
        self.env = {
            "PATH": f"{self.bin}:{tmux}:/usr/bin:/bin",
            "HOME": str(root),
            "TMUX_TMPDIR": str(root),
            "BACKBONE_PORT": str(PORT),
        }
        self.sockets: list[str] = []

    def cli(self, *args, check=True):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "agent_backbone.cli", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if check and result.returncode:
            raise SystemExit(f"backbone {' '.join(args)}: {result.stdout}{result.stderr}")
        return result

    def apply(self, request_id, action, **fields):
        path = self.root / f"{request_id}.json"
        path.write_text(
            json.dumps({"schema_version": 1, "request_id": request_id, "action": action, **fields})
        )
        receipt = json.loads(self.cli("profiles", "apply", "--file", str(path), "--json").stdout)
        assert receipt["status"] == "succeeded", receipt
        return receipt

    def lifecycle(self, request_id, action, receipt):
        return self.apply(
            request_id,
            action,
            profile_id=receipt["profile_id"],
            expected_installation_id=receipt["installation_id"],
            expected_lifecycle_id=receipt["lifecycle_id"],
        )

    def show(self, profile_id):
        return json.loads(self.cli("profiles", "show", profile_id, "--json").stdout)

    def up(self):
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "agent_backbone.cli", "up"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
                return process
            except OSError:
                time.sleep(0.2)
        raise SystemExit("backbone up did not listen")

    def key(self, path):
        for line in Path(path).read_text().splitlines():
            if line.startswith("BACKBONE_API_KEY="):
                return line.split("=", 1)[1]
        raise SystemExit("no credential")

    def bound(self, record):
        binding = record["binding"]
        return (
            httpx.Client(
                transport=httpx.HTTPTransport(uds=binding["endpoint"]["path"]),
                base_url="http://profile",
                trust_env=False,
                timeout=30,
            ),
            {
                "Authorization": "Bearer " + self.key(binding["credential_reference"]["path"]),
                "X-Backbone-Profile-Id": record["profile_id"],
                "X-Backbone-Installation-Id": binding["installation_id"],
                "X-Backbone-Lifecycle-Id": binding["lifecycle_id"],
            },
        )

    def first_agent(self, record, name, directory, runtime="codex"):
        directory.mkdir()
        request = self.root / f"setup-{record['profile_id']}-{name}.json"
        request.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "request_id": f"setup-{record['profile_id']}-{name}",
                    "action": "first_agent",
                    "profile_id": record["profile_id"],
                    "expected_installation_id": record["binding"]["installation_id"],
                    "expected_lifecycle_id": record["binding"]["lifecycle_id"],
                    "hooks": [runtime],
                    "agent": {
                        "name": name,
                        "directory": str(directory),
                        "create_directory": False,
                        "runtime": runtime,
                        "model": None,
                        "brief": {"template": "orchestrator-v1"},
                    },
                    "start_greeting": False,
                }
            )
        )
        result = self.cli("setup", "--file", str(request), "--jsonl", check=False)
        lines = result.stdout
        check(f"{runtime} first agent is ready", agent_ready(lines), (lines + result.stderr)[-900:])
        env = {}
        for line in (directory / "runtime.env").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                env[key] = value
        return env

    def sessions(self, socket_path):
        result = subprocess.run(
            ["tmux", "-S", socket_path, "list-sessions", "-F", "#{session_name}"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        return set(result.stdout.split())

    def cleanup(self, processes):
        # Providers from `profiles apply` are detached, so find them by their home.
        subprocess.run(
            ["pkill", "-TERM", "-f", "--", f"--registry {self.root}"], capture_output=True
        )
        for path in self.sockets:
            subprocess.run(["tmux", "-S", path, "kill-server"], env=self.env, capture_output=True)
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=30)


def agent_ready(lines: str) -> bool:
    """The setup result says the CLI started and stayed running."""
    results = [json.loads(line) for line in lines.splitlines() if '"code"' in line]
    return bool(results) and (results[-1]["code"], results[-1]["data"].get("ready")) == (
        "agent_ready",
        "ready",
    )


def check(label, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + label + (f": {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


def fresh_install() -> tuple[list[str], object]:
    """A fresh installer run; a new profile must offer the same first agent."""
    root = Path(tempfile.mkdtemp(prefix="bbf", dir="/tmp")).resolve()
    home = Home(root)
    process = None
    try:

        def setup(request_id, action, **fields):
            path = root / f"{request_id}.json"
            common = {"schema_version": 1, "request_id": request_id, "action": action}
            path.write_text(json.dumps({**common, "data_dir": str(home.data), **fields}))
            return home.cli("setup", "--file", str(path), "--jsonl", check=False).stdout

        ready = setup("install", "initialize", service="none", starter_profile="core-v1")
        check("fresh setup initializes", '"status": "completed"' in ready, ready[-300:])
        process = home.up()
        listed = json.loads(home.cli("profiles", "list", "--json").stdout)["items"]
        check("a fresh install is Default too", [i["kind"] for i in listed] == ["legacy_default"])
        project = root / "guide"
        project.mkdir()
        lines = setup(
            "guide",
            "first_agent",
            hooks=["codex"],
            agent={
                "name": "guide",
                "directory": str(project),
                "create_directory": False,
                "runtime": "codex",
                "model": None,
                "brief": {"template": "orchestrator-v1"},
            },
            start_greeting=False,
        )
        check("fresh install's first agent is ready", agent_ready(lines), lines[-300:])
        operator = {"Authorization": "Bearer " + home.key(home.data / ".env")}
        policy = httpx.get(
            f"http://127.0.0.1:{PORT}/api/config/agents.shared_policy",
            headers=operator,
            trust_env=False,
            timeout=30,
        ).json()["value"]
        return sorted(p.name for p in (project / ".agents/skills").iterdir()), policy
    finally:
        home.cleanup([process] if process else [])
        shutil.rmtree(root, ignore_errors=True)


def symlinked_data_dir() -> None:
    """A data directory reached through a link serves, instead of refusing every request."""
    root = Path(tempfile.mkdtemp(prefix="bbl", dir="/tmp")).resolve()
    home = Home(root)
    process = None
    try:
        home.data = root / "real-data"
        home.data.mkdir()
        (root / "linked-data").symlink_to(home.data)
        home.env["BACKBONE_DATA_DIR"] = str(root / "linked-data")
        home.cli("init")
        process = home.up()
        operator = {"Authorization": "Bearer " + home.key(home.data / ".env")}
        answer = httpx.get(
            f"http://127.0.0.1:{PORT}/api/agents", headers=operator, trust_env=False, timeout=30
        )
        check("a symlinked data directory serves", answer.status_code == 200, answer.text[:200])
    finally:
        home.cleanup([process] if process else [])
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    symlinked_data_dir()
    fresh_skills, fresh_policy = fresh_install()
    # Under /tmp: a pytest or scratch path can exceed the Unix socket length limit.
    root = Path(tempfile.mkdtemp(prefix="bbp", dir="/tmp")).resolve()
    home = Home(root)
    processes = []
    try:
        absent = home.cli("profiles", "list", "--json", check=False)
        check("no registry is registry_absent", "registry_absent" in absent.stdout)
        refused = home.cli("up", check=False)
        check("nothing runs without a profile", refused.returncode == 1, refused.stderr[-200:])
        detached = home.cli("up", "--detach", check=False)
        check("a detached start refuses too", detached.returncode == 1, detached.stdout[-200:])
        check("a refused start creates nothing", not home.data.exists())
        started = home.cli("agent", "start", "ghost", "--dir", str(root), check=False)
        check("the CLI starts nothing without a profile", started.returncode != 0)
        check("the refused CLI creates nothing", not home.data.exists())
        home.cli("init")
        first = home.up()
        processes.append(first)
        operator = {"Authorization": "Bearer " + home.key(home.data / ".env")}
        tcp = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", trust_env=False, timeout=30)
        check("install answers", tcp.get("/api/agents", headers=operator).status_code == 200)
        listed = json.loads(home.cli("profiles", "list", "--json").stdout)["items"]
        check(
            "a new install is the Default profile",
            [item["kind"] for item in listed] == ["legacy_default"],
        )

        default = {"profile_id": listed[0]["profile_id"]}
        first.terminate()
        first.wait(timeout=60)
        restarted = home.up()
        processes.append(restarted)
        record = home.show(default["profile_id"])
        home.sockets.append(record["binding"]["tmux"]["socket_path"])
        check("Default survives a restart", record["kind"] == "legacy_default")
        check(
            "unbound clients still work on Default",
            tcp.get("/api/agents", headers=operator).status_code == 200,
        )
        client, headers = home.bound(record)
        check(
            "Default answers bound on its socket",
            client.get("/api/profile", headers=headers).json()["profile_id"]
            == default["profile_id"],
        )

        for cwd in (None, str(root)):
            default_shell = {"schema_version": 1, "shell_id": "0123456789ab", "cwd": cwd}
            opened = client.post("/api/profile/shells", headers=headers, json=default_shell)
            check("Default opens a shell", opened.status_code == 200, str(opened.status_code))
        signing = client.post(
            "/api/profile/accounts/sign-in",
            headers=headers,
            json={"schema_version": 1, "runtime": "codex"},
        )
        check("Default keeps its CLI sign-ins", signing.status_code == 422)

        created, records = {}, {}
        for name in ("A", "B"):
            created[name] = home.apply(
                f"create-{name}", "create", display_name=name, baseline_version="core-v1"
            )
            home.lifecycle(f"start-{name}", "start", created[name])
            records[name] = home.show(created[name]["profile_id"])
            home.sockets.append(records[name]["binding"]["tmux"]["socket_path"])
        clients = {name: home.bound(records[name]) for name in records}
        for name, (http, bound) in clients.items():
            check(f"{name} answers", http.get("/api/profile", headers=bound).status_code == 200)
        a, b = clients["A"], clients["B"]
        swarms = a[0].get("/api/swarms", headers=a[1])
        log = Path(records["A"]["binding"]["credential_reference"]["path"]).parent / "state"
        check(
            "a profile with no sessions is not an error",
            swarms.status_code == 200
            and "Could not query" not in (log / "provider.log").read_text(),
        )
        check(
            "A's key does not open B",
            b[0]
            .get("/api/profile", headers={**b[1], "Authorization": a[1]["Authorization"]})
            .status_code
            == 401,
        )
        check(
            "A's tuple does not open B",
            b[0]
            .get("/api/profile", headers={**a[1], "Authorization": b[1]["Authorization"]})
            .status_code
            == 409,
        )
        check(
            "isolated profile refuses unbound",
            a[0].get("/api/agents", headers={"Authorization": a[1]["Authorization"]}).status_code
            == 409,
        )

        env_a = home.first_agent(records["A"], "worker", root / "project-a")
        check("an isolated agent can run backbone", shutil.which("backbone", path=env_a["PATH"]))
        linked = root / "project-a" / ".agents" / "skills" / "backbone-documents"
        check("the first agent received the starter skills", linked.exists(), str(linked))
        skills = sorted(p.name for p in linked.parent.iterdir())
        check("same skills as a fresh install", skills == fresh_skills, f"{skills} {fresh_skills}")
        policy = a[0].get("/api/config/agents.shared_policy", headers=a[1]).json()["value"]
        check("same policies as a fresh install", policy == fresh_policy, str(policy))
        folder = root / "my-skill"
        folder.mkdir()
        (folder / "SKILL.md").write_text("---\nname: my-skill\ndescription: mine\n---\n# Mine\n")
        copied = a[0].post(
            "/api/skills", headers=a[1], json={"path": str(folder), "tags": ["all"], "mode": "copy"}
        )
        check(
            "a skill copies in from any folder and stays there",
            copied.status_code == 200 and (folder / "SKILL.md").exists(),
            copied.text[:200],
        )
        (folder / "link").symlink_to(home.data / ".env")
        linked_copy = a[0].post(
            "/api/skills",
            headers=a[1],
            json={"path": str(folder), "name": "other", "mode": "copy"},
        )
        check("a skill folder with a link is refused", linked_copy.status_code == 422)
        document = a[0].post(
            "/api/documents",
            headers=a[1],
            json={"author": "operator", "request_id": "mine", "title": "Mine", "html": "<p>x</p>"},
        )
        check(
            "a person publishes a document as operator",
            document.status_code == 201 and document.json()["record"]["agent_name"] is None,
            document.text[:200],
        )
        env_b = home.first_agent(records["B"], "worker", root / "project-b", "claude")
        env_c = home.first_agent(records["A"], "helper", root / "project-c", "opencode")
        socket_a, socket_b = (records[n]["binding"]["tmux"]["socket_path"] for n in "AB")
        check(
            "same agent name runs in both",
            "worker" in home.sessions(socket_a) and "worker" in home.sessions(socket_b),
        )
        check(
            "no agent on Default's server",
            "worker" not in home.sessions(record["binding"]["tmux"]["socket_path"]),
        )
        root_a, root_b = (
            Path(records[n]["binding"]["credential_reference"]["path"]).parent for n in "AB"
        )

        def inside(env, root, *keys):
            return env["HOME"] == str(root / "home") and all(
                Path(env[key]).is_relative_to(root) for key in keys
            )

        check(
            "each CLI's HOME and store are its profile's",
            inside(env_a, root_a, "CODEX_HOME")
            and inside(env_b, root_b, "CLAUDE_CONFIG_DIR")
            and inside(env_c, root_a, "XDG_CONFIG_HOME", "XDG_DATA_HOME"),
        )
        ports = {env_a["BACKBONE_PORT"], env_b["BACKBONE_PORT"], str(PORT)}
        check("separate ports", len(ports) == 3)
        check(
            "no host variables", "BACKBONE_DATA_DIR" not in env_a and "SSH_AUTH_SOCK" not in env_a
        )

        def names(client):
            body = client[0].get("/api/agents", headers=client[1]).json()
            return [item["name"] for item in body["items"]]

        check(
            "each profile lists only its own agents",
            sorted(names(a)) == ["helper", "worker"] and names(b) == ["worker"],
        )
        registration = {"Authorization": "Bearer " + env_a["BACKBONE_API_KEY"]}
        own = httpx.get(
            f"http://127.0.0.1:{env_a['BACKBONE_PORT']}/api/reports",
            headers=registration,
            trust_env=False,
            timeout=30,
        )
        other = httpx.get(
            f"http://127.0.0.1:{env_b['BACKBONE_PORT']}/api/reports",
            headers=registration,
            trust_env=False,
            timeout=30,
        )
        check(
            "agent credential works on its own provider",
            own.status_code == 200,
            str(own.status_code),
        )
        check(
            "agent credential fails on the other",
            other.status_code in {401, 403},
            str(other.status_code),
        )

        shell = {"schema_version": 1, "shell_id": "0123456789ab", "cwd": None}
        first = a[0].post("/api/profile/shells", headers=a[1], json=shell).json()
        again = a[0].post("/api/profile/shells", headers=a[1], json=shell).json()
        check("shell is idempotent", (first["created"], again["created"]) == (True, False))
        answer = root / "shell-cli.txt"
        subprocess.run(
            [
                "tmux",
                "-S",
                socket_a,
                "send-keys",
                "-t",
                first["session"],
                f"backbone agent list > {answer} 2>&1; echo done >> {answer}",
                "Enter",
            ],
            env=home.env,
            check=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "done" not in (
            answer.read_text() if answer.exists() else ""
        ):
            time.sleep(0.2)
        said = answer.read_text() if answer.exists() else ""
        check("the CLI in a profile shell reaches that profile", "worker" in said, said[-300:])
        signin = a[0].post(
            "/api/profile/accounts/sign-in",
            headers=a[1],
            json={"schema_version": 1, "runtime": "codex"},
        )
        check(
            "sign-in session opens",
            signin.status_code == 200 and signin.json()["session"] in home.sessions(socket_a),
        )

        home.lifecycle("stop-A", "stop", created["A"])
        refused = False
        try:
            a[0].get("/api/profile", headers=a[1])
        except httpx.TransportError:
            refused = True
        check("stopped A refuses", refused)
        check("B unaffected", b[0].get("/api/profile", headers=b[1]).status_code == 200)
        check("Default unaffected", tcp.get("/api/agents", headers=operator).status_code == 200)
        # A caller's blocked signals reach the provider; stop-A-end proves it still stops.
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            home.lifecycle("start-A-again", "start", created["A"])
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
        clients["A"] = home.bound(home.show(created["A"]["profile_id"]))
        identity = clients["A"][0].get("/api/profile", headers=clients["A"][1]).json()
        check(
            "restart keeps the lifecycle", identity["lifecycle_id"] == created["A"]["lifecycle_id"]
        )
        check("agent session survived the restart", "worker" in home.sessions(socket_a))

        for name in ("A", "B"):
            home.lifecycle(f"stop-{name}-end", "stop", created[name])
        print("profile smoke passed")
        return 0
    finally:
        home.cleanup(processes)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    # Scratch home only: refuse to run from inside a managed agent's selection.
    for variable in list(os.environ):
        if variable.startswith("BACKBONE_"):
            del os.environ[variable]
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    sys.exit(main())
