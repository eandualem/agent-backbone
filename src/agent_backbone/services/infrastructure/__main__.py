"""CLI entry point: python -m agent_backbone.services.infrastructure <command> [args]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_backbone.services.infrastructure",
        description="Backbone infrastructure management CLI",
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # Backbone services
    sub.add_parser("start-backbone", help="Start all backbone services")
    sub.add_parser("stop-backbone", help="Stop all backbone services")
    sub.add_parser("restart-backbone", help="Restart all backbone services")

    # Individual services
    sub.add_parser("start-gateway", help="Start gateway server")
    sub.add_parser("stop-gateway", help="Stop gateway server")
    sub.add_parser("restart-gateway", help="Restart gateway server")
    sub.add_parser("start-telegram", help="Start Telegram bot")
    sub.add_parser("stop-telegram", help="Stop Telegram bot")

    # Agent management
    sa = sub.add_parser("start-agent", help="Start a single agent session")
    sa.add_argument("name", help="Agent name")
    sa.add_argument("--cli", default="claude", help="CLI binary (claude, cursor, gemini, codex)")
    sa.add_argument("--model", default=None, help="Model override")

    st = sub.add_parser("stop-agent", help="Stop a single agent session")
    st.add_argument("name", help="Agent name")

    # Group operations
    sub.add_parser("start-orchestrators", help="Start all orchestrator agents")
    sub.add_parser("start-arclio", help="Start Arclio coding agents")
    sub.add_parser("start-arclio-all", help="Start all Arclio coding agents")
    sub.add_parser("start-loveble", help="Start Loveble coding agents")
    sub.add_parser("start-wf", help="Start WF coding agents")
    sub.add_parser("start-all", help="Start all agents")
    sub.add_parser("stop-all", help="Stop all agent sessions (pauses scheduled startups)")
    sub.add_parser("stop-agents", help="Stop all agent sessions (alias for stop-all)")
    sub.add_parser("stop-everything", help="Stop all agents + all backbone services")

    # Info
    sub.add_parser("list", help="List available agents")
    sub.add_parser("status", help="Show infrastructure status")

    # Internal
    sub.add_parser("run-telegram-bot", help="Run Telegram bot (internal, blocks)")

    return parser


async def _dispatch(args: argparse.Namespace) -> None:
    """Dispatch a CLI command to the appropriate function."""
    from agent_backbone.config import BackboneConfig

    config = BackboneConfig.from_toml()

    from agent_backbone.services.infrastructure import _agents, _backbone, _status

    cmd = args.command

    # Backbone services
    if cmd == "start-backbone":
        ok = await _backbone.start_backbone(config)
        sys.exit(0 if ok else 1)
    elif cmd == "stop-backbone":
        await _backbone.stop_backbone(config)
    elif cmd == "restart-backbone":
        ok = await _backbone.restart_backbone(config)
        sys.exit(0 if ok else 1)

    # Individual services
    elif cmd == "start-gateway":
        ok = await _backbone.start_gateway(config)
        sys.exit(0 if ok else 1)
    elif cmd == "stop-gateway":
        await _backbone.stop_gateway(config)
    elif cmd == "restart-gateway":
        ok = await _backbone.restart_gateway(config)
        sys.exit(0 if ok else 1)
    elif cmd == "start-telegram":
        ok = await _backbone.start_telegram(config)
        sys.exit(0 if ok else 1)
    elif cmd == "stop-telegram":
        await _backbone.stop_telegram(config)

    # Agent management
    elif cmd == "start-agent":
        ok = await _agents.start_agent(args.name, config, cli=args.cli, model=args.model)
        sys.exit(0 if ok else 1)
    elif cmd == "stop-agent":
        ok = await _agents.stop_agent(args.name)
        sys.exit(0 if ok else 1)

    # Group operations
    elif cmd == "start-orchestrators":
        await _agents.start_orchestrators(config)
    elif cmd == "start-arclio":
        await _agents.start_org("Arclio", config)
    elif cmd == "start-arclio-all":
        await _agents.start_org("Arclio", config)
    elif cmd == "start-loveble":
        await _agents.start_org("Loveble", config)
    elif cmd == "start-wf":
        await _agents.start_org("WF", config)
    elif cmd == "start-all":
        await _agents.start_all(config)
    elif cmd in ("stop-all", "stop-agents"):
        await _agents.stop_all_agents(config)
    elif cmd == "stop-everything":
        await _agents.stop_all_agents(config)
        await _backbone.stop_backbone(config)

    # Info
    elif cmd == "list":
        print(_agents.list_agents(config))
    elif cmd == "status":
        print(await _status.show_status(config))

    # Internal
    elif cmd == "run-telegram-bot":
        await _run_telegram_bot(config)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


async def _run_telegram_bot(config) -> None:
    """Run the Telegram bot directly (blocking). Used as tmux session command."""
    from agent_backbone.services.telegram.interface import TelegramService

    bot = TelegramService(config)
    await bot.run()


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    asyncio.run(_dispatch(args))


if __name__ == "__main__":
    main()
