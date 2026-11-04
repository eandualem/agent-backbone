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
        self.data = root / "default"
        tmux = Path(shutil.which("tmux")).parent
        self.env = {
            "PATH": f"{self.bin}:{tmux}:/usr/bin:/bin",
            "HOME": str(root),
            "TMUX_TMPDIR": str(root),
            "BACKBONE_DATA_DIR": str(self.data),
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

    def first_agent(self, record, name, directory):
        directory.mkdir()
        request = self.root / f"setup-{record['profile_id']}.json"
        request.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "request_id": f"setup-{record['profile_id']}",
                    "action": "first_agent",
                    "profile_id": record["profile_id"],
                    "expected_installation_id": record["binding"]["installation_id"],
                    "expected_lifecycle_id": record["binding"]["lifecycle_id"],
                    "hooks": ["codex"],
                    "agent": {
                        "name": name,
                        "directory": str(directory),
                        "create_directory": False,
                        "runtime": "codex",
                        "model": None,
                        "brief": {"text": "Smoke"},
                    },
                    "start_greeting": False,
                }
            )
        )
        # The stand-in shows no real prompt, so readiness times out after launch.
        self.cli("setup", "--file", str(request), "--jsonl", check=False)
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
        subprocess.run(["pkill", "-TERM", "-f", f"--registry {self.root}"], capture_output=True)
        for path in self.sockets:
            subprocess.run(["tmux", "-S", path, "kill-server"], env=self.env, capture_output=True)
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=30)


def check(label, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + label + (f": {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


def main() -> int:
    # Under /tmp: a pytest or scratch path can exceed the Unix socket length limit.
    root = Path(tempfile.mkdtemp(prefix="bbp", dir="/tmp")).resolve()
    home = Home(root)
    processes = []
    try:
        home.cli("init")
        legacy = home.up()
        processes.append(legacy)
        operator = {"Authorization": "Bearer " + home.key(home.data / ".env")}
        tcp = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", trust_env=False, timeout=30)
        check("legacy install answers", tcp.get("/api/agents", headers=operator).status_code == 200)
        absent = home.cli("profiles", "list", "--json", check=False)
        check("no registry is registry_absent", "registry_absent" in absent.stdout)

        preview = json.loads(home.cli("profiles", "adoption-preview", "--json").stdout)
        default = home.apply(
            "adopt",
            "adopt_default",
            expected_installation_id=preview["installation_id"],
            adoption_token=preview["adoption_token"],
        )
        check(
            "adoption kept the installation ID",
            default["installation_id"] == preview["installation_id"],
        )
        check("adoption restarted nothing", legacy.poll() is None)

        legacy.terminate()
        legacy.wait(timeout=60)
        adopted = home.up()
        processes.append(adopted)
        record = home.show(default["profile_id"])
        home.sockets.append(record["binding"]["tmux"]["socket_path"])
        check("Default is listed as adopted", record["kind"] == "legacy_default")
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
        env_b = home.first_agent(records["B"], "worker", root / "project-b")
        socket_a, socket_b = (records[n]["binding"]["tmux"]["socket_path"] for n in "AB")
        check(
            "same agent name runs in both",
            "worker" in home.sessions(socket_a) and "worker" in home.sessions(socket_b),
        )
        check(
            "no agent on Default's server",
            "worker" not in home.sessions(record["binding"]["tmux"]["socket_path"]),
        )
        check(
            "separate HOME and CLI store",
            env_a["HOME"] != env_b["HOME"] and env_a["CODEX_HOME"] != env_b["CODEX_HOME"],
        )
        check("separate ports", env_a["BACKBONE_PORT"] != env_b["BACKBONE_PORT"] != str(PORT))
        check(
            "no host variables", "BACKBONE_DATA_DIR" not in env_a and "SSH_AUTH_SOCK" not in env_a
        )

        def names(client):
            body = client[0].get("/api/agents", headers=client[1]).json()
            return [item["name"] for item in body["items"]]

        check("each profile lists only its own agent", names(a) == ["worker"] == names(b))
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
        signin = a[0].post(
            "/api/profile/accounts/sign-in",
            headers=a[1],
            json={"schema_version": 1, "runtime": "codex"},
        )
        check(
            "sign-in session opens",
            signin.status_code == 200
            and signin.json()["session"] in home.sessions(socket_a) | {"sign-in-codex"},
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
        home.lifecycle("start-A-again", "start", created["A"])
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
