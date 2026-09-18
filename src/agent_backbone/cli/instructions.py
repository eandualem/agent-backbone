"""Find, inspect and edit the Markdown supplied when an agent starts."""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_backbone.cli import _common
from agent_backbone.cli.presentation import note, print_record, print_table
from agent_backbone.config import validate_setting
from agent_backbone.fs import atomic_write_text
from agent_backbone.services.agents import instruction_preview
from agent_backbone.templates import (
    append_policies,
    list_templates,
    read_template,
    template_path,
    template_source,
)


def add_instruction_commands(sub) -> None:
    parser = sub.add_parser(
        "templates", aliases=["instructions"], help="find, edit and preview injected instructions"
    )
    commands = parser.add_subparsers(dest="instructions_command", required=True)
    p = commands.add_parser("list", help="show sources and global/tag assignments")
    p.add_argument("--json", action="store_true")
    p = commands.add_parser(
        "init", help="create editable copies without overwriting existing files"
    )
    p.add_argument("names", nargs="*", metavar="TEMPLATE")
    for verb, description in (
        ("show", "print base or a policy's content"),
        ("path", "print the editable path for base or a policy"),
        ("edit", "edit base or a policy using $VISUAL/$EDITOR"),
    ):
        p = commands.add_parser(verb, help=description)
        p.add_argument(
            "name",
            nargs="?" if verb == "path" else None,
            help="base, swarm:ROLE, or policy:NAME; path alone prints the directory",
        )
    p = commands.add_parser(
        "preview", help="effective content and sources for an agent's next start"
    )
    p.add_argument("agent")
    p.add_argument("--json", action="store_true")
    p = commands.add_parser("use", help="show or replace the global or tag policy assignment")
    p.add_argument("policies", nargs="*", metavar="POLICY")
    p.add_argument("--tag", help="assign to this tag instead of all agents")
    p.add_argument("--clear", action="store_true", help="remove the assignment")
    p = commands.add_parser("validate", help="check effective instructions for known agents")
    p.add_argument("agent", nargs="?")
    parser.set_defaults(func=cmd_instructions)


def edit_file(path: Path, initial: str, *, source: Path | None = None) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise ValueError(
            "set $VISUAL or $EDITOR, or edit the path printed by `templates path NAME`"
        )
    before = path.read_text() if path.exists() else None
    inherited = source.read_text() if source and source != path and source.exists() else None
    # The live file is unchanged on cancellation, failure or concurrent edit.
    with tempfile.TemporaryDirectory(prefix="backbone-instructions-") as directory:
        draft = Path(directory) / path.name
        draft.write_text(before if before is not None else initial)
        if subprocess.call([*shlex.split(editor), str(draft)]) != 0:
            raise ValueError("editor exited unsuccessfully; instructions were not saved")
        text = draft.read_text()
        if not text.strip():
            raise ValueError("empty instructions were not saved")
        if (path.read_text() if path.exists() else None) != before:
            raise ValueError("instructions changed during editing; nothing was overwritten")
        if inherited is not None and source.read_text() != inherited:
            raise ValueError("source instructions changed during editing; nothing was overwritten")
        atomic_write_text(path, text)


async def _instructions(args: argparse.Namespace) -> int:
    sub = args.instructions_command
    # A read-only look at the database: `templates.dir` names the directory.
    config = await _common.read_config()
    dirs = config.template_dirs
    if sub in ("show", "path", "edit"):
        if sub == "path" and args.name is None:
            print(dirs.editable)
            return 0
        path = template_path(dirs, args.name)
        source = template_source(args.name, dirs)
        if sub == "path":
            print(path)
        elif sub == "show":
            print(read_template(args.name, dirs), end="")
        else:
            initial = source.read_text() if source.is_file() else f"# {args.name}\n\n"
            edit_file(path, initial, source=source)
            when = "new swarms" if args.name.startswith("swarm:") else "the next fresh start"
            print(f"Saved {path}. Applies to {when}.")
            if args.name != "base" and not args.name.startswith("swarm:"):
                print(
                    f"Assign: backbone templates use {args.name.removeprefix('policy:')} "
                    "[--tag TAG]"
                )
        return 0
    if sub == "init":
        names = args.names or [row["name"] for row in list_templates(dirs)]
        # Validate every name before writing any file.
        targets = [(name, template_path(dirs, name)) for name in names]
        for name, target in targets:
            if target.exists() or target.is_symlink():
                print(f"Kept {target}")
                continue
            text = read_template(name, dirs)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with target.open("x") as stream:
                    stream.write(text)
            except FileExistsError:
                print(f"Kept {target} (created concurrently)")
            else:
                print(f"Created {target}")
        print("Editable copies are preserved on upgrades. Preview before a fresh start.")
        return 0
    if sub == "list":
        entries = list_templates(dirs)
        view = {
            "directory": str(dirs.editable),
            "injection_enabled": config.launch.inject_brief,
            "global": list(config.launch.shared_policy),
            "tags": {tag: list(names) for tag, names in config.launch.tag_policy.items()},
            "templates": entries,
        }
        if args.json:
            _common.print_json(view)
            return 0
        where = "templates.dir" if config.templates.dir else "default; set templates.dir to move"
        note(f"Editable templates: {view['directory']} ({where})")
        if unread := dirs.unread_default_files():
            note(
                f"! {len(unread)} file(s) still under {dirs.data_dir / 'templates'} are not read; "
                f"move them into {dirs.editable}"
            )
        print_table(
            "Templates",
            ("Template", "Source", "Override"),
            [
                (
                    entry["name"],
                    entry["source"],
                    "legacy override" if entry["legacy"] else "current",
                )
                for entry in entries
            ],
        )
        print_table(
            "Assignments (global first, then matching tags alphabetically)",
            ("Scope", "Policies"),
            [
                ("global", ", ".join(config.launch.shared_policy) or "none"),
                *(
                    (f"tag {tag}", ", ".join(names) or "none")
                    for tag, names in sorted(config.launch.tag_policy.items())
                ),
            ],
        )
        note(
            "Inspect: backbone templates show NAME\nEdit: backbone templates edit NAME\n"
            "Preview: backbone templates preview AGENT"
        )
        return 0
    if sub == "use":
        scope = "tag " + args.tag if args.tag is not None else "global"
        if args.policies and args.clear:
            raise ValueError("pass policy names or --clear, not both")
        if not args.policies and not args.clear:
            # No names is a read: clearing an assignment takes an explicit --clear.
            current = (
                config.launch.tag_policy.get(args.tag, ())
                if args.tag is not None
                else config.launch.shared_policy
            )
            print(f"{scope}: {', '.join(current) or 'none'}")
            tag_arg = f" --tag {args.tag}" if args.tag else ""
            note(f"Clear: backbone templates use --clear{tag_arg}")
            return 0
        args.policies = [name.removeprefix("policy:") for name in args.policies]
        validate_setting("agents.shared_policy", args.policies)
        append_policies("", dirs, tuple(args.policies))
        if args.tag is not None:
            validate_setting("agents.tag_policy", {args.tag: args.policies})
            value = {tag: list(names) for tag, names in config.launch.tag_policy.items()}
            if args.policies:
                value[args.tag] = args.policies
            else:
                value.pop(args.tag, None)
            key = "agents.tag_policy"
        else:
            key, value = "agents.shared_policy", args.policies
        validate_setting(key, value)
        if await _common.api_up(config):
            response = await _common.api(
                config, "PUT", f"/api/config/{key}", json_body={"value": value}
            )
            if not response or response[0] != 200:
                raise ValueError(
                    f"could not update assignment: {response[1] if response else 'API unreachable'}"
                )
        else:
            async with _common.Direct(config) as direct:
                await direct.store.set_setting(key, value)
        print(f"{scope}: {', '.join(args.policies) or 'none'}")
        return 0
    specs = [config.agents.get(args.agent)] if args.agent else list(config.agents)
    if any(spec is None for spec in specs):
        raise ValueError(f"unknown agent '{args.agent}'")
    if sub == "validate":
        errors = []
        if not args.agent:
            try:
                append_policies(
                    "", dirs, config.launch.policy_names(tuple(config.launch.tag_policy))
                )
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
            for entry in list_templates(dirs):
                try:
                    read_template(entry["name"], dirs)
                except (OSError, ValueError) as exc:
                    errors.append(str(exc))
        for spec in specs:
            try:
                instruction_preview(spec, config)
                print(f"{spec.name}: valid")
            except (OSError, ValueError) as exc:
                errors.append(f"{spec.name}: {exc}")
        for error in errors:
            print(error, file=sys.stderr)
        if not errors:
            print("Instructions valid.")
        return 1 if errors else 0
    preview = instruction_preview(specs[0], config)
    if args.json:
        _common.print_json(preview)
    else:
        print_record(
            f"Instructions for {preview['name']} (next start)",
            [
                ("CLI", preview["runtime"]),
                ("Mode", preview["mode"]),
            ],
        )
        print_table(
            "Sources",
            ("State", "Scope", "Path"),
            [
                ("included" if source["applied"] else "skipped", source["scope"], source["path"])
                for source in preview["sources"]
            ],
        )
        for notice in preview["notices"]:
            note(notice)
        print("\n--- Effective instructions ---\n")
        print(preview["content"])
    return 0


def cmd_instructions(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_instructions(args))
    except (OSError, ValueError) as exc:
        print(f"templates: {exc}", file=sys.stderr)
        return 1
