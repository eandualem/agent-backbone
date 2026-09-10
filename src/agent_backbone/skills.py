"""The shared skills store — one copy of every skill, materialised per agent.

A skill is a directory with a ``SKILL.md`` (the Agent Skills format that
Claude Code, Codex and OpenCode all read). The store — ``skills.store``,
``~/skills`` by default — holds the only copy. Each skill names the tags of
the agents that should have it in its frontmatter, under the spec's
``metadata`` slot::

    ---
    name: backend-module-pattern
    description: …
    metadata:
      backbone-tags: coder python
    ---

At launch an agent receives every skill whose tags intersect its own
(``all`` reaches everyone; ``agent:NAME`` reaches one agent), as symlinks in
the directory its runtime actually reads — ``.claude/skills`` for Claude
Code, ``.agents/skills`` for Codex and OpenCode (measured 2026-09-10; see
``docs/skills.md``). Links are the only thing the backbone creates inside a
repository, every one is recorded in a manifest, and only manifest entries
are ever removed: a real directory with the same name is the repository's
own skill and wins. The link names are kept out of ``git status`` through a
backbone-owned block in ``.git/info/exclude``, never a tracked file.

Standard library only: this module is a leaf like ``templates``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

TAGS_KEY = "backbone-tags"
"""The ``metadata`` key carrying a skill's tags (space-separated)."""
ALL_TAG = "all"
"""The tag that reaches every agent."""
AGENT_TAG_PREFIX = "agent:"
"""``agent:NAME`` reaches exactly that agent, whatever its tags."""

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?!-)){0,62}$")
"""Spec skill names: lowercase, digits, single hyphens, no leading/trailing hyphen."""
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,99}$")

EXCLUDE_BEGIN = "# backbone-skills begin (managed by agent-backbone; edits inside are replaced)"
EXCLUDE_END = "# backbone-skills end"


@dataclass(frozen=True)
class Skill:
    """One store entry as read from disk; ``error`` explains an invalid one."""

    name: str
    path: Path
    description: str = ""
    tags: tuple[str, ...] = ()
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.error is None


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_frontmatter(text: str) -> tuple[list[str], str] | None:
    """``(frontmatter lines, rest)`` or None when the file has no frontmatter."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index], "".join(lines[index + 1 :])
    return None


def _parse_frontmatter(lines: list[str]) -> dict[str, object]:
    """A deliberately small YAML subset: ``key: value`` scalars (plain, quoted
    or ``|``/``>`` blocks) and one level of nested ``key:`` mappings.

    That covers every SKILL.md in the wild that the spec allows; anything
    stranger is read as opaque text and left untouched by ``write_tags``.
    """
    result: dict[str, object] = {}
    index = 0
    while index < len(lines):
        raw = lines[index].rstrip("\n")
        index += 1
        if not raw.strip() or raw.lstrip().startswith("#") or raw.startswith((" ", "\t")):
            continue
        key, sep, value = raw.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if value in ("|", ">", "|-", ">-"):
            block: list[str] = []
            while index < len(lines) and (
                lines[index].startswith((" ", "\t")) or not lines[index].strip()
            ):
                block.append(lines[index].strip())
                index += 1
            joiner = "\n" if value.startswith("|") else " "
            result[key] = joiner.join(part for part in block if part).strip()
        elif value:
            result[key] = _unquote(value)
        else:
            nested: dict[str, str] = {}
            while index < len(lines) and (
                lines[index].startswith((" ", "\t")) or not lines[index].strip()
            ):
                inner = lines[index].strip()
                index += 1
                if not inner or inner.startswith("#"):
                    continue
                inner_key, inner_sep, inner_value = inner.partition(":")
                if inner_sep:
                    nested[inner_key.strip()] = _unquote(inner_value)
            result[key] = nested
    return result


def parse_tags(value: object) -> tuple[str, ...]:
    """Tags from the frontmatter value: a space/comma-separated string or a list."""
    if isinstance(value, str):
        parts = re.split(r"[,\s]+", value.strip("[] "))
    elif isinstance(value, (list, tuple)):
        parts = [str(part) for part in value]
    else:
        parts = []
    return tuple(dict.fromkeys(part.strip().lower() for part in parts if part.strip()))


def validate_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    for tag in tags:
        if not TAG_RE.match(tag):
            raise ValueError(f"invalid tag {tag!r}: lowercase letters, digits, and : . _ -")
    return tags


def parse_skill(path: Path) -> Skill:
    """Read one skill directory (or the file inside it) without trusting it."""
    path = Path(path)
    name = path.name
    skill_file = path / "SKILL.md"
    if not path.is_dir():
        return Skill(name, path, error="not a directory")
    if not skill_file.is_file():
        return Skill(name, path, error="no SKILL.md")
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Skill(name, path, error=f"unreadable SKILL.md: {exc}")
    parts = _split_frontmatter(text)
    if parts is None:
        return Skill(name, path, error="SKILL.md has no frontmatter")
    meta = _parse_frontmatter(parts[0])
    declared = meta.get("name")
    description = meta.get("description")
    metadata = meta.get("metadata")
    tags = parse_tags(metadata.get(TAGS_KEY)) if isinstance(metadata, dict) else ()
    if not NAME_RE.match(name):
        return Skill(name, path, error="directory name is not a valid skill name")
    if declared != name:
        return Skill(name, path, error=f"frontmatter name {declared!r} does not match directory")
    if not isinstance(description, str) or not description.strip():
        return Skill(name, path, error="frontmatter description is missing")
    try:
        validate_tags(tags)
    except ValueError as exc:
        return Skill(name, path, error=str(exc))
    return Skill(name, path, description=description.strip(), tags=tags)


def read_store(store: Path) -> list[Skill]:
    """Every entry of the store, valid or not, sorted by name."""
    store = Path(store)
    if not store.is_dir():
        return []
    entries = [
        parse_skill(child)
        for child in sorted(store.iterdir())
        if not child.name.startswith(".") and (child.is_dir() or child.is_symlink())
    ]
    return entries


def select_skills(skills: list[Skill], tags: tuple[str, ...], agent_name: str) -> list[Skill]:
    """The valid skills an agent with ``tags`` receives, in store order."""
    mine = {tag.lower() for tag in tags} | {ALL_TAG, f"{AGENT_TAG_PREFIX}{agent_name}"}
    return [skill for skill in skills if skill.valid and set(skill.tags) & mine]


def write_tags(skill_dir: Path, tags: tuple[str, ...]) -> None:
    """Set ``metadata.backbone-tags`` in ``SKILL.md``, leaving every other line as it is."""
    validate_tags(tags)
    skill_file = Path(skill_dir) / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")
    parts = _split_frontmatter(text)
    if parts is None:
        raise ValueError(f"{skill_file} has no frontmatter")
    front, body = parts
    value = " ".join(tags)
    tag_line = f"  {TAGS_KEY}: {json.dumps(value)}\n" if tags else None
    out: list[str] = []
    index = 0
    handled = False
    while index < len(front):
        line = front[index]
        key, separator, inline = line.partition(":")
        if key.strip() == "metadata" and not line.startswith((" ", "\t")):
            if separator and inline.strip():
                raise ValueError(
                    "metadata is an inline mapping; write it as an indented block first"
                )
            out.append("metadata:\n")
            index += 1
            while index < len(front) and (
                front[index].startswith((" ", "\t")) or not front[index].strip()
            ):
                inner = front[index]
                index += 1
                if inner.strip().split(":", 1)[0].strip() == TAGS_KEY:
                    continue
                if inner.strip():
                    out.append(inner)
            if tag_line:
                out.append(tag_line)
            handled = True
            # An emptied mapping would be invalid YAML; drop the key instead.
            if out[-1] == "metadata:\n":
                out.pop()
            continue
        out.append(line)
        index += 1
    if not handled and tag_line:
        out.append("metadata:\n")
        out.append(tag_line)
    skill_file.write_text("---\n" + "".join(out) + "---\n" + body, encoding="utf-8")


def add_skill(
    store: Path,
    source: Path,
    *,
    name: str | None = None,
    tags: tuple[str, ...] = (),
    replace: bool = False,
) -> Skill:
    """Move a skill directory into the store and tag it.

    A move, never a copy: the source directory is gone afterwards, so the
    store holds the only instance. ``name`` renames on the way in (the
    frontmatter ``name`` is rewritten to match, as the spec requires).
    """
    store = Path(store)
    source = Path(source).expanduser().resolve()
    if not source.is_dir() or not (source / "SKILL.md").is_file():
        raise ValueError(f"{source} is not a skill directory (no SKILL.md)")
    if store.exists() and store.resolve() in source.parents:
        raise ValueError(f"{source} is already in the store")
    target_name = name or source.name
    if not NAME_RE.match(target_name):
        raise ValueError(f"invalid skill name {target_name!r}")
    validate_tags(tags)
    target = store / target_name
    if (target.exists() or target.is_symlink()) and not replace:
        raise ValueError(f"skill {target_name!r} already exists in the store (use --replace)")
    if replace and not tags and target.is_dir():
        # A replace without tags is an update of the content, not a re-tag.
        tags = parse_skill(target).tags
    # Everything that can be checked before touching the filesystem is
    # checked here: a rejected skill leaves the source where it was.
    draft = parse_skill(source)
    # The name is rewritten on the way in, so a mismatched or invalid one is
    # tolerated only when the caller renames; everything else is refused now.
    renaming = name is not None
    tolerated = draft.error is None or (
        draft.error.startswith("frontmatter name")
        or (renaming and draft.error == "directory name is not a valid skill name")
    )
    if not tolerated:
        raise ValueError(f"{source.name}: {draft.error}")
    _split_frontmatter_or_raise(source / "SKILL.md")
    store.mkdir(parents=True, exist_ok=True)
    displaced = store / f".replaced-{target_name}"
    if displaced.exists() or displaced.is_symlink():
        _remove(displaced)
    if target.exists() or target.is_symlink():
        os.replace(target, displaced) if not target.is_symlink() else target.rename(displaced)
    try:
        shutil.move(str(source), str(target))
    except OSError:
        if displaced.exists() or displaced.is_symlink():
            displaced.rename(target)
        raise
    try:
        _rewrite_name(target, target_name)
        write_tags(target, tags)
        skill = parse_skill(target)
        if not skill.valid:
            raise ValueError(f"{target_name}: {skill.error}")
    except (OSError, ValueError):
        # Put both parties back: the source to its origin, the old entry to its name.
        shutil.move(str(target), str(source))
        if displaced.exists() or displaced.is_symlink():
            displaced.rename(target)
        raise
    if displaced.exists() or displaced.is_symlink():
        _remove(displaced)
    return skill


def _split_frontmatter_or_raise(skill_file: Path) -> None:
    if _split_frontmatter(skill_file.read_text(encoding="utf-8")) is None:
        raise ValueError(f"{skill_file} has no frontmatter")


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _rewrite_name(skill_dir: Path, name: str) -> None:
    skill_file = skill_dir / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")
    parts = _split_frontmatter(text)
    if parts is None:
        raise ValueError(f"{skill_file} has no frontmatter")
    front, body = parts
    out = [f"name: {name}\n" if line.split(":", 1)[0].strip() == "name" else line for line in front]
    if not any(line.split(":", 1)[0].strip() == "name" for line in front):
        out.insert(0, f"name: {name}\n")
    skill_file.write_text("---\n" + "".join(out) + "---\n" + body, encoding="utf-8")


# --- Materialisation ---------------------------------------------------------


@dataclass
class Materialization:
    """What one launch did inside the repository."""

    linked: list[str] = field(default_factory=list)
    """``<dir>/<name>`` links present after the run (created or kept)."""
    removed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    """Selected skills the repository already owns under that name."""
    broken: list[str] = field(default_factory=list)
    """Links that do not resolve to a SKILL.md after the run."""

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.broken


def manifest_path(data_dir: Path, agent_name: str) -> Path:
    return Path(data_dir) / "skills" / "materialized" / f"{agent_name}.json"


def _read_manifest(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    links = data.get("links") if isinstance(data, dict) else None
    return [str(item) for item in links] if isinstance(links, list) else []


def _write_manifest(path: Path, repo_dir: Path, links: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not links:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_suffix(".json.tmp")
    body = {"repo": str(repo_dir), "links": sorted(links)}
    tmp.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _links_sharing_git_dir(manifest_dir: Path, git_dir: Path) -> set[str]:
    """Every manifest's links whose repository uses ``git_dir`` — several
    agents can share one checkout, and worktrees share one ``info/exclude``."""
    links: set[str] = set()
    if not manifest_dir.is_dir():
        return links
    for path in manifest_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("repo"), str):
            continue
        other = _common_git_dir(Path(data["repo"]))
        if other is not None and other == git_dir:
            links.update(str(item) for item in data.get("links", []) if isinstance(item, str))
    return links


def _points_into(link: Path, store: Path) -> bool:
    """Whether ``link`` is a symlink whose target lies inside the store."""
    if not link.is_symlink():
        return False
    try:
        target = Path(os.readlink(link))
    except OSError:
        return False
    if not target.is_absolute():
        target = link.parent / target
    try:
        return store.resolve() in Path(os.path.normpath(target)).resolve().parents or (
            Path(os.path.normpath(target)).resolve().parent == store.resolve()
        )
    except OSError:
        return False


def materialize(
    store: Path,
    repo_dir: Path,
    dirs: tuple[str, ...],
    selected: list[Skill],
    manifest: Path,
) -> Materialization:
    """Bring ``<repo>/<dir>/<name>`` links in line with ``selected``.

    Links are created for selected skills, links recorded in the manifest
    but no longer selected are removed (only while they still point into
    the store), and nothing else in those directories is touched.
    """
    store = Path(store).expanduser()
    repo_dir = Path(repo_dir)
    result = Materialization()
    previous = set(_read_manifest(manifest))
    wanted = {f"{directory}/{skill.name}": skill for directory in dirs for skill in selected}
    for rel in sorted(previous - set(wanted)):
        link = repo_dir / rel
        if _points_into(link, store):
            link.unlink()
            result.removed.append(rel)
    kept: list[str] = []
    for rel, skill in sorted(wanted.items()):
        link = repo_dir / rel
        target = store / skill.name
        if link.is_symlink():
            if _points_into(link, store):
                if os.readlink(link) != str(target):
                    link.unlink()
                    link.symlink_to(target, target_is_directory=True)
            else:
                result.conflicts.append(f"{rel} is a link the repository owns")
                continue
        elif link.exists():
            result.conflicts.append(f"{rel} is the repository's own skill")
            continue
        else:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=True)
        if (link / "SKILL.md").is_file():
            kept.append(rel)
            result.linked.append(rel)
        else:
            result.broken.append(f"{rel} -> {target} has no SKILL.md")
            kept.append(rel)
    _write_manifest(manifest, repo_dir, kept)
    git_dir = _common_git_dir(repo_dir)
    if git_dir is not None:
        _update_exclude(git_dir, _links_sharing_git_dir(manifest.parent, git_dir))
    return result


def _git_dir(repo_dir: Path) -> Path | None:
    """The ``.git`` directory of a checkout or worktree, or None."""
    dot_git = repo_dir / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            line = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            path = Path(line.partition(":")[2].strip())
            path = path if path.is_absolute() else repo_dir / path
            return path if path.is_dir() else None
    return None


def _common_git_dir(repo_dir: Path) -> Path | None:
    """The git directory whose ``info/exclude`` governs ``repo_dir`` — a
    worktree's is its parent checkout's."""
    git_dir = _git_dir(repo_dir)
    if git_dir is None:
        return None
    common = git_dir / "commondir"
    if common.is_file():
        try:
            rel = common.read_text(encoding="utf-8").strip()
            git_dir = Path(os.path.normpath(git_dir / rel)) if rel else git_dir
        except OSError:
            pass
    try:
        return git_dir.resolve()
    except OSError:
        return git_dir


def _update_exclude(git_dir: Path, links: set[str]) -> None:
    """Rewrite the backbone-owned block of ``<git_dir>/info/exclude``.

    ``info/exclude`` is git's per-clone ignore list: never committed, never
    shared, so the repository's own files are untouched. Only the block
    between the two markers is replaced.
    """
    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    except OSError:
        return
    kept: list[str] = []
    inside = False
    for line in existing:
        if line.strip() == EXCLUDE_BEGIN:
            inside = True
            continue
        if line.strip() == EXCLUDE_END:
            inside = False
            continue
        if not inside:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    block = [EXCLUDE_BEGIN, *(f"/{rel}" for rel in sorted(links)), EXCLUDE_END] if links else []
    if not block and not any(line.strip() == EXCLUDE_BEGIN for line in existing):
        return
    lines = kept + ([""] if kept and block else []) + block
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    except OSError:
        return


def is_git_repository(store: Path) -> bool:
    return (Path(store) / ".git").exists()


async def commit_store(store: Path, message: str) -> bool:
    """Commit every change in the store, initialising it as a git repository first.

    The store's history is the audit trail for a skill other agents will
    load: who changed what, and when — so history starts with the first
    tool operation, not when someone remembers to ``git init``. Returns
    whether a commit was made.
    """
    from agent_backbone.git import run_git

    store = Path(store)
    if not store.is_dir():
        return False
    if not is_git_repository(store):
        rc, _, _ = await run_git(store, "init", "-q")
        if rc != 0:
            return False
    rc, _, _ = await run_git(store, "add", "-A")
    if rc != 0:
        return False
    rc, _, _ = await run_git(store, "diff", "--cached", "--quiet")
    if rc == 0:
        return False
    rc, _, _ = await run_git(store, "commit", "-q", "-m", message)
    return rc == 0
