"""``backbone`` command-line interface.

backbone init                        create the data directory, .env and database
backbone secrets set|list|path       tokens in <data_dir>/.env (the only secrets file read)
backbone up [--detach]               run the backbone (API + scheduler + integrations)
backbone down                        stop a detached backbone
backbone status                      agents, sessions, repositories and health
backbone doctor                      check tmux, runtimes, credentials
backbone runtimes                    supported CLIs, installed or not, example model ids
backbone service install|status      start the backbone at login (launchd / systemd --user)
backbone config list|get|set|unset   settings (stored in the database)
backbone agent start [--dir D]       discover + start an agent (waits for its prompt)
backbone agent list|stop|inspect|set|watch|unwatch|forget
backbone swarm create|list|status|disband   coordinator+members on one issue
backbone tell <agent> <msg>          deliver a message to an agent (or a swarm)
backbone reply <text>                answer the humans on their channel (Telegram topic, …)
backbone hooks install claude        install the state-reporting hooks

Commands talk to the running backbone API when it is up and fall back to the
database directly when it is not.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from agent_backbone.cli.agents import cmd_agent, cmd_hooks, cmd_reply, cmd_tell
from agent_backbone.cli.server import cmd_config, cmd_down, cmd_status, cmd_up
from agent_backbone.cli.service import cmd_service
from agent_backbone.cli.setup import cmd_doctor, cmd_init, cmd_runtimes, cmd_secrets
from agent_backbone.cli.swarms import cmd_help, cmd_swarm
from agent_backbone.config import RUNTIMES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backbone", description=__doc__.split("\n\n")[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the data directory, .env and database")
    p.add_argument(
        "--data-dir",
        default=None,
        help="data directory (default: $BACKBONE_DATA_DIR or ~/.local/share/agent-backbone)",
    )
    p.add_argument("--force", action="store_true", help="regenerate .env")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="check the environment and configuration")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("runtimes", help="supported runtimes, whether installed, example model ids")
    p.set_defaults(func=cmd_runtimes)

    p = sub.add_parser("service", help="start the backbone at login (launchd / systemd --user)")
    svc = p.add_subparsers(dest="service_command", required=True)
    svc.add_parser("install", help="install and start the login service")
    svc.add_parser("uninstall", help="stop and remove the login service")
    svc.add_parser("status", help="running | installed | not installed")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser(
        "secrets", help="tokens in <data_dir>/.env — the only secrets file the backbone reads"
    )
    ssec = p.add_subparsers(dest="secrets_command", required=True)
    pset = ssec.add_parser(
        "set", help="set a value (prompted when omitted, so it stays out of history)"
    )
    pset.add_argument("key", metavar="KEY", help="e.g. TELEGRAM_TOKEN, GITHUB_TOKEN")
    pset.add_argument("value", nargs="?", default=None, metavar="VALUE")
    punset = ssec.add_parser("unset", help="remove a value")
    punset.add_argument("key", metavar="KEY")
    ssec.add_parser("list", help="which secrets are set (names only)")
    ssec.add_parser("path", help="print the .env path")
    p.set_defaults(func=cmd_secrets)

    p = sub.add_parser("up", help="run the backbone")
    p.add_argument("--detach", "-d", action="store_true", help="run inside a tmux session")
    p.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("down", help="stop a detached backbone")
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("status", help="show agents, repositories, sessions and health")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("config", help="settings (stored in the database)")
    csub = p.add_subparsers(dest="config_command", required=True)
    csub.add_parser("list", help="show every setting")
    pc = csub.add_parser("get", help="show one setting")
    pc.add_argument("key")
    pc = csub.add_parser("set", help="change a setting (JSON values accepted)")
    pc.add_argument("key")
    pc.add_argument("value")
    pc = csub.add_parser("unset", help="reset a setting to its default")
    pc.add_argument("key")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("agent", help="manage agents")
    asub = p.add_subparsers(dest="agent_command", required=True)
    asub.add_parser("list", help="list known agents")
    ps = asub.add_parser("start", help="start agents (discovers a new one from a directory)")
    ps.add_argument(
        "names",
        nargs="*",
        default=[],
        metavar="NAME",
        help="agent name(s); default: discover from the current directory",
    )
    ps.add_argument(
        "--dir", default=None, help="project directory (default: cwd when no name is given)"
    )
    ps.add_argument(
        "--runtime",
        default=None,
        help=f"{' | '.join(RUNTIMES)} "
        "(default: agents.default_runtime, recorded on the agent afterwards)",
    )
    ps.add_argument(
        "--model",
        default=None,
        help="model passed to the runtime CLI (e.g. opus, sonnet, or a full model id); "
        "recorded on the agent and reused by later starts",
    )
    ps.add_argument("--resume", action="store_true", help="resume the runtime's last conversation")
    ps.add_argument(
        "--watch",
        action="append",
        default=None,
        metavar="OWNER/REPO",
        help="also watch this repository (repeatable)",
    )
    ps.add_argument(
        "--no-wait",
        action="store_true",
        help="return immediately instead of waiting for the prompt",
    )
    pst = asub.add_parser("stop", help="stop agent sessions")
    pst.add_argument("names", nargs="+", metavar="NAME")
    pi = asub.add_parser("inspect", help="show state, delivery readiness and the evidence")
    pi.add_argument("name")
    pi.add_argument("--json", action="store_true")
    pa = asub.add_parser(
        "approve", help="answer the permission prompt an agent's runtime is showing"
    )
    pa.add_argument("name")
    pa.add_argument(
        "--from",
        dest="sender",
        default=os.environ.get("BACKBONE_AGENT") or os.environ.get("USER", "cli"),
        help="who is approving, for the audit trail (default: $BACKBONE_AGENT or $USER)",
    )
    pse = asub.add_parser(
        "set", help="change agent fields: runtime=… model=… repo=… dir=… description=…"
    )
    pse.add_argument("name")
    pse.add_argument("assignments", nargs="+", metavar="key=value")
    pw = asub.add_parser("watch", help="watch repositories (NAME optional inside an agent session)")
    pw.add_argument("targets", nargs="+", metavar="[NAME] OWNER/REPO")
    pu = asub.add_parser(
        "unwatch", help="stop watching repositories (NAME optional inside an agent session)"
    )
    pu.add_argument("targets", nargs="+", metavar="[NAME] OWNER/REPO")
    pf = asub.add_parser("forget", help="remove an agent from the backbone")
    pf.add_argument("name")
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("hooks", help="install runtime hooks that report agent state")
    hsub = p.add_subparsers(dest="hooks_command", required=True)
    for name, help_text in (
        ("install", "add the hooks to the runtime's settings"),
        ("uninstall", "remove the hooks"),
    ):
        hp = hsub.add_parser(name, help=help_text)
        hp.add_argument("runtime", choices=["claude"], help="runtime to configure")
        hp.add_argument(
            "--dir",
            default=None,
            help="project directory (writes .claude/settings.json there); default: global",
        )
    p.set_defaults(func=cmd_hooks)

    p = sub.add_parser("swarm", help="run a coordinator+members swarm on an issue")
    ssub = p.add_subparsers(dest="swarm_command", required=True)
    psc = ssub.add_parser("create", help="create and start a swarm on an existing issue")
    psc.add_argument("name", help="swarm name (lowercase, digits, dashes)")
    psc.add_argument("--issue", required=True, metavar="OWNER/REPO#N", help="the issue to work")
    psc.add_argument(
        "--member",
        action="append",
        metavar="ROLE[*N][@RUNTIME[/MODEL]]",
        help="roster entry, repeatable — quote specs with a count so the shell "
        "does not glob the * (e.g. 'scout*3@claude/sonnet', coder@codex); "
        "a coordinator@claude is added when none is given",
    )
    psc.add_argument(
        "--initiator",
        default=None,
        help="agent initiating the swarm (default: $BACKBONE_AGENT)",
    )
    ssub.add_parser("list", help="all swarms with members")
    pss = ssub.add_parser("status", help="one swarm's roster and state")
    pss.add_argument("name", nargs="?", default=None)
    psd = ssub.add_parser("disband", help="stop members, remove the worktree, keep the branch")
    psd.add_argument("name")
    p.set_defaults(func=cmd_swarm)

    p = sub.add_parser("help", help="capability help for agents (topics: swarms, messaging, …)")
    p.add_argument("topic", nargs="?", default=None)
    p.set_defaults(func=cmd_help)

    p = sub.add_parser("tell", help="deliver a message to an agent (via the running API)")
    p.add_argument("agent")
    p.add_argument("message", nargs="+")
    p.add_argument(
        "--from",
        dest="sender",
        # Inside an agent session the sender is the agent, not the human account.
        default=os.environ.get("BACKBONE_AGENT") or os.environ.get("USER", "cli"),
        help="sender label for the provenance envelope (default: $BACKBONE_AGENT, then $USER)",
    )
    p.add_argument("--priority", action="store_true", help="deliver even while someone is typing")
    p.set_defaults(func=cmd_tell)

    p = sub.add_parser(
        "reply", help="answer the humans on their channel (e.g. the agent's Telegram topic)"
    )
    p.add_argument("text", nargs="+")
    p.add_argument(
        "--agent",
        default=None,
        help="which agent is answering (default: $BACKBONE_AGENT inside an agent session)",
    )
    p.set_defaults(func=cmd_reply)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
