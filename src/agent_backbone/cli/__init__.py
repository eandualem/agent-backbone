"""``backbone`` command-line interface.

backbone init                        create the data directory, .env and database
backbone secrets set|list|path       tokens in <data_dir>/.env (the only secrets file read)
backbone up [--detach]               run the backbone (API + scheduler + integrations)
backbone down                        stop a detached backbone
backbone status                      agents, sessions, repositories and health
backbone doctor                      check tmux, runtimes, credentials
backbone diagnostics                 recorded problems, grouped without message content
backbone report --file FILE          publish a short structured progress report
backbone updates [--agent NAME]      read reports, with --history or show ID for depth
backbone runtimes                    supported CLIs, installed or not, example model ids
backbone service install|uninstall|status   start the backbone at login (launchd / systemd --user)
backbone config list|get|set|unset   settings (stored in the database)
backbone agent start [--dir D]       discover + start an agent (waits for its prompt)
backbone agent list|stop|inspect|set|watch|unwatch|forget
backbone swarm create|list|status|disband   coordinator+members on one issue
backbone tell <agent> <msg>          deliver a message to an agent (or a swarm)
backbone reply <text>                answer the humans on their channel (Telegram topic, …)
backbone hooks install claude        install the state-reporting hooks
backbone help [topic]                capability playbooks for agents (setup, agents, messaging, …)
backbone docs [page]                 the documentation shipped with this install

Commands talk to the running backbone API when it is up and fall back to the
database directly when it is not.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from agent_backbone import __version__
from agent_backbone.cli.agents import cmd_agent, cmd_hooks, cmd_inbox, cmd_reply, cmd_tell
from agent_backbone.cli.completion import cmd_completion, complete
from agent_backbone.cli.diagnostics import add_diagnostics_parser
from agent_backbone.cli.instructions import add_instruction_commands
from agent_backbone.cli.reports import add_report_parsers
from agent_backbone.cli.server import cmd_config, cmd_down, cmd_up
from agent_backbone.cli.service import cmd_service
from agent_backbone.cli.setup import cmd_doctor, cmd_init, cmd_runtimes, cmd_secrets
from agent_backbone.cli.skills import add_skill_commands, expand_shorthand
from agent_backbone.cli.status import add_status_options, cmd_status
from agent_backbone.cli.swarms import cmd_docs, cmd_help, cmd_swarm
from agent_backbone.cli.upgrade import cmd_upgrade
from agent_backbone.config import RUNTIMES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backbone",
        description="Start agents, follow their work and connect their sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Start here:\n  backbone agent start --attach\n  backbone status --watch\n"
        "\nRecall a command: backbone help agent start\n"
        "Capability playbooks: backbone help\n"
        "Enable Tab completion: backbone completion zsh --install (also bash/fish)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"backbone {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, title="commands", metavar="COMMAND")

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

    p = sub.add_parser("completion", help="enable Tab completion (see docs cli)")
    p.add_argument("shell", choices=["bash", "zsh", "fish"])
    install = p.add_mutually_exclusive_group()
    install.add_argument(
        "--install", action="store_true", help="persist setup in your shell config"
    )
    install.add_argument("--uninstall", action="store_true", help="remove the managed setup block")
    p.add_argument("--rc-file", metavar="PATH", help="use a different shell startup file")
    p.add_argument(
        "--suggestions", action="store_true", help="add inline hints using zsh-autosuggestions"
    )
    p.set_defaults(func=cmd_completion)

    p = sub.add_parser("service", help="start the backbone at login (launchd / systemd --user)")
    svc = p.add_subparsers(dest="service_command", required=True)
    svc.add_parser("install", help="install and start the login service")
    svc.add_parser("uninstall", help="stop and remove the login service")
    svc.add_parser("restart", help="restart the login service")
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

    p = sub.add_parser(
        "upgrade", help="upgrade the package and restart the backbone; agents are untouched"
    )
    p.add_argument("--check", action="store_true", help="report versions only")
    p.add_argument("--no-restart", action="store_true", help="upgrade without restarting")
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser("status", help="show agent state, current work, swarms and health")
    add_status_options(p)
    p.set_defaults(func=cmd_status)

    add_diagnostics_parser(sub)
    add_instruction_commands(sub)
    add_skill_commands(sub)
    add_report_parsers(sub)

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
    pl = asub.add_parser("list", help="list known agents")
    pl.add_argument("--tag", help="show agents with this tag")
    pl.add_argument("--json", action="store_true")
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
        "(default: saved runtime; agents.default_runtime for a new agent)",
    )
    ps.add_argument(
        "--model",
        default=None,
        help="model passed to the runtime CLI (e.g. opus, sonnet, or a full model id), "
        "optionally `model:effort` (e.g. gpt-6-astra:high) for runtimes with an effort "
        "setting; recorded on the agent and reused by later starts (`backbone runtimes`)",
    )
    conversation = ps.add_mutually_exclusive_group()
    conversation.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=False,
        help="continue the conversation the backbone last saw for this agent "
        "(the runtime's latest when none is saved); same as `backbone agent resume`",
    )
    conversation.add_argument(
        "--fresh",
        dest="resume",
        action="store_false",
        default=False,
        help="start a new conversation (the default); keep the agent's saved runtime and model",
    )
    ps.add_argument(
        "--always-on",
        action="store_true",
        help="start every agent marked always_on (after a reboot: `--always-on --resume`)",
    )
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
    ps.add_argument("--attach", action="store_true", help="attach to this agent after starting")
    pr = asub.add_parser("resume", help="resume a known agent's previous conversation")
    pr.add_argument("names", nargs="+", metavar="NAME")
    pr.add_argument("--attach", action="store_true", help="attach after resuming")
    pr.add_argument("--no-wait", action="store_true")
    pr.set_defaults(resume=True, dir=None, runtime=None, model=None, watch=None, group=True)
    pa = asub.add_parser("attach", help="open an agent session; detach with Ctrl-b d")
    pa.add_argument("name")
    pa.add_argument("--read-only", action="store_true", help="watch without sending input")
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
    pad = asub.add_parser("deny", help="refuse the permission prompt an agent's runtime is showing")
    pad.add_argument("name")
    pad.add_argument(
        "--from",
        dest="sender",
        default=os.environ.get("BACKBONE_AGENT") or os.environ.get("USER", "cli"),
        help="who is refusing, for the audit trail (default: $BACKBONE_AGENT or $USER)",
    )
    pse = asub.add_parser(
        "set",
        help=(
            "change agent fields: runtime=… model=… repo=… dir=… description=… "
            "always_on=true unattended=true"
        ),
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
    for verb in ("tag", "untag"):
        pt = asub.add_parser(verb, help=f"{verb} an agent for grouping and shared instructions")
        pt.add_argument("name")
        pt.add_argument("tags", nargs="+", metavar="TAG")
    pr = asub.add_parser(
        "rename", help="rename a stopped agent, preserving configuration and history"
    )
    pr.add_argument("name")
    pr.add_argument("new_name")
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("hooks", help="install runtime hooks that report agent state")
    hsub = p.add_subparsers(dest="hooks_command", required=True)
    for name, help_text in (
        ("install", "add the hooks to the runtime's settings"),
        ("uninstall", "remove the hooks"),
    ):
        hp = hsub.add_parser(name, help=help_text)
        hp.add_argument(
            "runtime", choices=["claude", "codex", "gemini"], help="runtime to configure"
        )
        hp.add_argument(
            "--dir",
            default=None,
            help="project directory (writes the runtime's project settings there); "
            "default: the user's global settings",
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
        metavar="ROLE[*N][@RUNTIME[/MODEL[:EFFORT]]]",
        help="roster entry, repeatable — quote specs with a count so the shell "
        "does not glob the * (e.g. 'scout*3@claude/sonnet', coder@codex); the model "
        "may carry an effort as model:effort (e.g. coordinator@codex/gpt-6-astra:high); "
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
    add_status_options(pss)
    psd = ssub.add_parser("disband", help="stop members, remove the worktree, keep the branch")
    psd.add_argument("name")
    p.set_defaults(func=cmd_swarm)

    p = sub.add_parser("help", help="capability playbooks for agents (setup, swarms, messaging, …)")
    p.add_argument("topic", nargs="?", default=None)
    p.add_argument("path", nargs="*", help="optional subcommands, e.g. help agent start")
    p.set_defaults(func=cmd_help)

    usage = sub.add_parser(
        "usage", help="quick guide: start agents, read reports, rename and find help"
    )
    usage.set_defaults(func=cmd_docs, page="usage")
    p = sub.add_parser("docs", help="the documentation shipped with this install")
    p.add_argument("page", nargs="?", default=None)
    p.set_defaults(func=cmd_docs)

    p = sub.add_parser(
        "inbox", help="read corrections at a safe checkpoint; acknowledge after applying"
    )
    p.add_argument("--agent", default=os.environ.get("BACKBONE_AGENT"))
    p.add_argument("--ack", nargs="+", metavar="TOKEN", default=[])
    p.set_defaults(func=cmd_inbox)

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
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_complete"]:
        words = argv[2:] if argv[1:2] == ["--"] else argv[1:]
        sys.exit(complete(parser, words))
    args = parser.parse_args(expand_shorthand(argv))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
