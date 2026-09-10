"""The shared skills store: list, add, retag, preview and validate."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from agent_backbone.cli import _common
from agent_backbone.services.agents import skills_preview
from agent_backbone.skills import (
    ALL_TAG,
    add_skill,
    commit_store,
    is_git_repository,
    parse_skill,
    read_store,
    write_tags,
)

SKILL_COMMANDS = ("list", "show", "path", "add", "tag", "preview", "validate")


def expand_shorthand(argv: list[str]) -> list[str]:
    """``backbone skills NAME`` means ``backbone skills preview NAME``.

    The everyday question is "what does this agent get?", so the agent's
    name alone answers it; the verbs stay for everything else.
    """
    if (
        len(argv) >= 2
        and argv[0] == "skills"
        and argv[1] not in SKILL_COMMANDS
        and not argv[1].startswith("-")
    ):
        return [argv[0], "preview", *argv[1:]]
    return argv


def add_skill_commands(sub) -> None:
    parser = sub.add_parser("skills", help="the shared skills store and who receives what")
    commands = parser.add_subparsers(dest="skills_command", required=True)
    p = commands.add_parser("list", help="store skills, their tags and the agents they reach")
    p.add_argument("--tag", help="only skills carrying this tag")
    p.add_argument("--json", action="store_true")
    p = commands.add_parser("show", help="print a store skill's SKILL.md")
    p.add_argument("name")
    p = commands.add_parser("path", help="print the store directory, or one skill's")
    p.add_argument("name", nargs="?")
    p = commands.add_parser(
        "add", help="move a skill directory into the store and tag it (never a copy)"
    )
    p.add_argument("path", help="a directory containing SKILL.md")
    p.add_argument("--name", help="store it under this name instead of the directory's")
    p.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="TAG",
        help=f"agents with this tag receive it (repeatable; `{ALL_TAG}` = everyone)",
    )
    p.add_argument("--replace", action="store_true", help="overwrite a store skill of that name")
    p = commands.add_parser("tag", help="replace a store skill's tags; none clears them")
    p.add_argument("name")
    p.add_argument("tags", nargs="*", metavar="TAG")
    p = commands.add_parser(
        "preview", help="what an agent's next launch links, and where (`skills NAME` for short)"
    )
    p.add_argument("agent")
    p.add_argument("--json", action="store_true")
    p = commands.add_parser("validate", help="check the store and every agent's selection")
    p.add_argument("agent", nargs="?")
    parser.set_defaults(func=cmd_skills)


def _actor() -> str:
    return os.environ.get("BACKBONE_AGENT") or getpass.getuser()


def _print_preview(view: dict) -> None:
    print(f"{view['name']} / {view['runtime']} · tags: {', '.join(view['tags']) or 'none'}")
    print(f"  store: {view['store'] or 'disabled'}")
    print(f"  directories: {', '.join(view['directories']) or 'none for this runtime'}")
    for skill in view["skills"]:
        states = ", ".join(f"{d}: {s}" for d, s in skill["links"].items()) or "no directory"
        print(f"  {skill['name']} [{' '.join(skill['tags'])}] — {states}")
    if not view["skills"]:
        print("  no store skill is tagged for this agent")
    for notice in view["notices"]:
        print(f"  ! {notice}")


async def _skills(args: argparse.Namespace) -> int:
    sub = args.skills_command
    config = await _common.read_config()
    store = config.skills.store_path
    if store is None:
        raise ValueError(
            "skills.store is empty: set it with `backbone config set skills.store ~/skills`"
        )

    if sub == "path":
        print(store / args.name if args.name else store)
        return 0
    if sub == "show":
        skill = parse_skill(store / args.name)
        if not skill.valid and skill.error in ("not a directory", "no SKILL.md"):
            raise ValueError(f"unknown skill '{args.name}' ({skill.error})")
        print((store / args.name / "SKILL.md").read_text(), end="")
        return 0
    if sub == "list":
        entries = read_store(store)
        if args.tag:
            entries = [entry for entry in entries if args.tag in entry.tags]
        rows = []
        for entry in entries:
            reaches = [
                spec.name
                for spec in config.agents
                if entry.valid
                and set(entry.tags)
                & ({t.lower() for t in spec.tags} | {ALL_TAG, f"agent:{spec.name}"})
            ]
            rows.append(
                {
                    "name": entry.name,
                    "tags": list(entry.tags),
                    "description": entry.description,
                    "path": str(entry.path),
                    "error": entry.error,
                    "reaches": reaches,
                }
            )
        if args.json:
            _common.print_json(
                {"store": str(store), "git": is_git_repository(store), "items": rows}
            )
            return 0
        print(f"Store: {store}{'' if is_git_repository(store) else ' (not a git repository)'}")
        if not rows:
            print("  empty — add one: backbone skills add PATH --tag TAG")
        for row in rows:
            if row["error"]:
                print(f"  {row['name']}: INVALID — {row['error']}")
                continue
            tags = " ".join(row["tags"]) or "no tags (reaches nobody)"
            print(f"  {row['name']} [{tags}] → {', '.join(row['reaches']) or 'no agent'}")
            print(f"      {row['description'][:100]}")
        print("\nPreview an agent: backbone skills preview AGENT")
        return 0
    if sub == "add":
        body = {
            "path": str(Path(args.path).expanduser().resolve()),
            "name": args.name,
            "tags": args.tag,
            "replace": args.replace,
            "actor": _actor(),
        }
        if await _common.api_up(config):
            response = await _common.api(config, "POST", "/api/skills", json_body=body)
            if not response or response[0] != 200:
                detail = response[1] if response else "API unreachable"
                detail = detail.get("detail", detail) if isinstance(detail, dict) else detail
                raise ValueError(f"could not add skill: {detail}")
            skill = response[1]
            name, tags = skill["name"], skill["tags"]
        else:
            added = add_skill(
                store, body["path"], name=args.name, tags=tuple(args.tag), replace=args.replace
            )
            await commit_store(store, f"add {added.name} [{' '.join(added.tags)}] by {_actor()}")
            name, tags = added.name, list(added.tags)
        print(f"Added {name} to {store} with tags: {' '.join(tags) or 'none (reaches nobody)'}")
        print("Agents tagged for it receive the link at their next launch.")
        return 0
    if sub == "tag":
        if await _common.api_up(config):
            response = await _common.api(
                config,
                "PUT",
                f"/api/skills/{args.name}/tags",
                json_body={"tags": args.tags, "actor": _actor()},
            )
            if not response or response[0] != 200:
                detail = response[1] if response else "API unreachable"
                detail = detail.get("detail", detail) if isinstance(detail, dict) else detail
                raise ValueError(f"could not retag skill: {detail}")
            tags = response[1]["tags"]
        else:
            current = parse_skill(store / args.name)
            if current.error in ("not a directory", "no SKILL.md"):
                raise ValueError(f"unknown skill '{args.name}' ({current.error})")
            write_tags(store / args.name, tuple(args.tags))
            await commit_store(store, f"tag {args.name} [{' '.join(args.tags)}] by {_actor()}")
            tags = list(parse_skill(store / args.name).tags)
        print(f"{args.name}: {' '.join(tags) or 'no tags (reaches nobody)'}")
        return 0
    specs = [config.agents.get(args.agent)] if args.agent else list(config.agents)
    if any(spec is None for spec in specs):
        raise ValueError(f"unknown agent '{args.agent}'")
    if sub == "validate":
        errors = [f"{entry.name}: {entry.error}" for entry in read_store(store) if not entry.valid]
        for spec in specs:
            view = skills_preview(spec, config)
            broken = [
                f"{spec.name}: {skill['name']} {directory} {state}"
                for skill in view["skills"]
                for directory, state in skill["links"].items()
                if "broken" in state
            ]
            errors.extend(broken)
            print(
                f"{spec.name}: {len(view['skills'])} skill(s)" + (" — problems" if broken else "")
            )
        for error in errors:
            print(error, file=sys.stderr)
        if not errors:
            print("Skills valid.")
        return 1 if errors else 0
    view = skills_preview(specs[0], config)
    if args.json:
        _common.print_json(view)
    else:
        _print_preview(view)
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_skills(args))
    except (OSError, ValueError) as exc:
        print(f"skills: {exc}", file=sys.stderr)
        return 1
