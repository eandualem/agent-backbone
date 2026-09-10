"""The shared skills store: parsing, selection by tag, tagging, adding and materialising."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_backbone import skills
from agent_backbone.skills import (
    EXCLUDE_BEGIN,
    EXCLUDE_END,
    add_skill,
    manifest_path,
    materialize,
    parse_skill,
    read_store,
    select_skills,
    write_tags,
)


def make_skill(root: Path, name: str, *, tags: str | None = None, body: str = "# Body\n") -> Path:
    path = root / name
    path.mkdir(parents=True)
    meta = f"metadata:\n  backbone-tags: {tags}\n" if tags is not None else ""
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Marker skill {name}. Use when asked.\n{meta}---\n{body}"
    )
    return path


class TestParse:
    def test_reads_name_description_and_tags(self, tmp_path):
        make_skill(tmp_path, "backend-module-pattern", tags="coder python")
        skill = parse_skill(tmp_path / "backend-module-pattern")
        assert skill.valid
        assert skill.tags == ("coder", "python")
        assert skill.description.startswith("Marker skill")

    def test_block_scalar_description_and_quoted_tags(self, tmp_path):
        path = tmp_path / "research-methodology"
        path.mkdir()
        (path / "SKILL.md").write_text(
            "---\nname: research-methodology\ndescription: |\n  Line one.\n  Line two.\n"
            'metadata:\n  author: leo\n  backbone-tags: "all"\n---\n# x\n'
        )
        skill = parse_skill(path)
        assert skill.valid and skill.tags == ("all",)
        assert skill.description == "Line one.\nLine two."

    @pytest.mark.parametrize(
        ("dirname", "text", "error"),
        [
            ("ok", "", "no SKILL.md"),
            ("ok", "# no frontmatter\n", "has no frontmatter"),
            ("ok", "---\nname: other\ndescription: d\n---\n", "does not match"),
            ("ok", "---\nname: ok\n---\n", "description is missing"),
            ("Bad--Name", "---\nname: Bad--Name\ndescription: d\n---\n", "not a valid skill name"),
            (
                "ok",
                "---\nname: ok\ndescription: d\nmetadata:\n  backbone-tags: 'a b/c'\n---\n",
                "invalid tag",
            ),
        ],
    )
    def test_invalid_entries_are_reported_not_raised(self, tmp_path, dirname, text, error):
        path = tmp_path / dirname
        path.mkdir()
        if text:
            (path / "SKILL.md").write_text(text)
        skill = parse_skill(path)
        assert not skill.valid and error in skill.error

    def test_read_store_skips_hidden_and_tolerates_missing(self, tmp_path):
        assert read_store(tmp_path / "missing") == []
        make_skill(tmp_path, "a")
        (tmp_path / ".git").mkdir()
        (tmp_path / "README.md").write_text("x")
        assert [s.name for s in read_store(tmp_path)] == ["a"]


class TestSelect:
    def test_union_over_the_agents_tags_plus_all_and_own_name(self, tmp_path):
        for name, tags in (
            ("everyone", "all"),
            ("coders", "coder"),
            ("python-only", "python"),
            ("frontend", "typescript"),
            ("mine", "agent:leo"),
            ("orphan", None),
        ):
            make_skill(tmp_path, name, tags=tags)
        chosen = select_skills(read_store(tmp_path), ("coder", "python"), "leo")
        assert [s.name for s in chosen] == ["coders", "everyone", "mine", "python-only"]
        assert [s.name for s in select_skills(read_store(tmp_path), (), "ike")] == ["everyone"]

    def test_invalid_skills_are_never_selected(self, tmp_path):
        path = make_skill(tmp_path, "broken", tags="all")
        (path / "SKILL.md").write_text("no frontmatter")
        assert select_skills(read_store(tmp_path), (), "x") == []


class TestWriteTags:
    def test_sets_replaces_and_clears_without_touching_other_lines(self, tmp_path):
        path = tmp_path / "s"
        path.mkdir()
        original = "---\nname: s\ndescription: d\nmetadata:\n  author: me\n---\n# Body\n\nkeep\n"
        (path / "SKILL.md").write_text(original)
        write_tags(path, ("coder", "python"))
        text = (path / "SKILL.md").read_text()
        assert '  backbone-tags: "coder python"\n' in text and "  author: me\n" in text
        assert text.endswith("---\n# Body\n\nkeep\n")
        assert parse_skill(path).tags == ("coder", "python")
        write_tags(path, ("all",))
        assert parse_skill(path).tags == ("all",)
        assert (path / "SKILL.md").read_text().count("backbone-tags") == 1
        write_tags(path, ())
        assert parse_skill(path).tags == () and "author: me" in (path / "SKILL.md").read_text()

    def test_adds_a_metadata_block_when_there_is_none(self, tmp_path):
        path = make_skill(tmp_path, "s")
        write_tags(path, ("all",))
        assert (
            (path / "SKILL.md")
            .read_text()
            .startswith(
                "---\nname: s\ndescription: Marker skill s. Use when asked.\n"
                'metadata:\n  backbone-tags: "all"\n---\n'
            )
        )

    def test_inline_metadata_mapping_is_refused_not_destroyed(self, tmp_path):
        path = tmp_path / "s"
        path.mkdir()
        original = "---\nname: s\ndescription: d\nmetadata: {author: me}\n---\n# x\n"
        (path / "SKILL.md").write_text(original)
        with pytest.raises(ValueError, match="inline mapping"):
            write_tags(path, ("all",))
        assert (path / "SKILL.md").read_text() == original

    def test_clearing_removes_an_emptied_metadata_block(self, tmp_path):
        path = make_skill(tmp_path, "s", tags="all")
        write_tags(path, ())
        assert "metadata" not in (path / "SKILL.md").read_text()


class TestAdd:
    def test_moves_renames_tags_and_leaves_no_copy(self, tmp_path):
        source = make_skill(tmp_path / "repo" / ".claude" / "skills", "draft")
        store = tmp_path / "store"
        added = add_skill(store, source, name="final-name", tags=("coder",))
        assert added.valid and added.name == "final-name" and added.tags == ("coder",)
        assert not source.exists()
        assert (store / "final-name" / "SKILL.md").read_text().startswith("---\nname: final-name\n")

    def test_refuses_duplicates_bad_names_and_store_entries(self, tmp_path):
        store = tmp_path / "store"
        make_skill(store, "taken")
        source = make_skill(tmp_path / "src", "taken")
        with pytest.raises(ValueError, match="already exists"):
            add_skill(store, source)
        with pytest.raises(ValueError, match="invalid skill name"):
            add_skill(store, source, name="Bad Name")
        with pytest.raises(ValueError, match="already in the store"):
            add_skill(store, store / "taken")
        with pytest.raises(ValueError, match="not a skill directory"):
            add_skill(store, tmp_path / "nowhere")
        add_skill(store, source, replace=True, tags=("all",))
        assert parse_skill(store / "taken").tags == ("all",)
        assert not list(store.glob(".replaced-*"))

    def test_replace_without_tags_keeps_the_existing_ones(self, tmp_path):
        """The sandboxed update path: copy, edit, add --replace — tags survive."""
        store = tmp_path / "store"
        make_skill(store, "shared", tags="coder python", body="# v1\n")
        draft = make_skill(tmp_path / "repo" / "draft", "shared", body="# v2\n")
        updated = add_skill(store, draft, name="shared", replace=True)
        assert updated.tags == ("coder", "python")
        assert (store / "shared" / "SKILL.md").read_text().endswith("# v2\n")

    def test_a_rejected_skill_is_left_where_it_was(self, tmp_path):
        store = tmp_path / "store"
        make_skill(store, "keep", tags="coder")
        source = tmp_path / "src" / "keep"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("# no frontmatter\n")
        with pytest.raises(ValueError, match="no frontmatter"):
            add_skill(store, source, replace=True)
        assert (source / "SKILL.md").is_file()
        assert parse_skill(store / "keep").tags == ("coder",)

    def test_a_failure_after_the_move_restores_both_sides(self, tmp_path):
        store = tmp_path / "store"
        make_skill(store, "keep", tags="coder", body="# old\n")
        source = make_skill(tmp_path / "src", "keep", body="# new\n")
        with (
            patch("agent_backbone.skills.write_tags", side_effect=OSError("disk full")),
            pytest.raises(OSError),
        ):
            add_skill(store, source, replace=True, tags=("all",))
        assert (source / "SKILL.md").read_text().endswith("# new\n")
        assert (store / "keep" / "SKILL.md").read_text().endswith("# old\n")
        assert not list(store.glob(".replaced-*"))


def _git_repo(path: Path) -> Path:
    (path / ".git" / "info").mkdir(parents=True)
    return path


class TestMaterialize:
    def test_links_selected_and_records_them(self, tmp_path):
        store = tmp_path / "store"
        make_skill(store, "a", tags="all")
        make_skill(store, "b", tags="coder")
        repo = _git_repo(tmp_path / "repo")
        manifest = manifest_path(tmp_path / "data", "leo")
        selected = select_skills(read_store(store), ("coder",), "leo")
        result = materialize(store, repo, (".claude/skills",), selected, manifest)
        assert result.ok
        assert result.linked == [".claude/skills/a", ".claude/skills/b"]
        link = repo / ".claude" / "skills" / "a"
        assert link.is_symlink() and os.readlink(link) == str(store / "a")
        assert (link / "SKILL.md").is_file()
        exclude = (repo / ".git" / "info" / "exclude").read_text()
        assert exclude.splitlines() == [
            EXCLUDE_BEGIN,
            "/.claude/skills/a",
            "/.claude/skills/b",
            EXCLUDE_END,
        ]
        assert manifest.is_file()

    def test_removes_only_its_own_stale_links_and_keeps_repo_skills(self, tmp_path):
        store = tmp_path / "store"
        make_skill(store, "a", tags="all")
        make_skill(store, "b", tags="all")
        repo = _git_repo(tmp_path / "repo")
        (repo / ".git" / "info" / "exclude").write_text("*.log\n")
        own = make_skill(repo / ".claude" / "skills", "own")
        foreign_link = repo / ".claude" / "skills" / "foreign"
        foreign_link.symlink_to(tmp_path / "elsewhere")
        manifest = manifest_path(tmp_path / "data", "leo")
        materialize(store, repo, (".claude/skills",), read_store(store), manifest)
        # b is untagged from now on
        write_tags(store / "b", ())
        result = materialize(
            store, repo, (".claude/skills",), select_skills(read_store(store), (), "leo"), manifest
        )
        assert result.removed == [".claude/skills/b"]
        assert not (repo / ".claude" / "skills" / "b").exists()
        assert own.is_dir() and foreign_link.is_symlink()
        exclude = (repo / ".git" / "info" / "exclude").read_text()
        assert exclude == f"*.log\n\n{EXCLUDE_BEGIN}\n/.claude/skills/a\n{EXCLUDE_END}\n"

    def test_a_dangling_link_the_repository_made_is_not_ours_to_delete(self, tmp_path):
        store = tmp_path / "store"
        make_skill(store, "a", tags="all")
        repo = _git_repo(tmp_path / "repo")
        manifest = manifest_path(tmp_path / "data", "leo")
        materialize(store, repo, (".claude/skills",), read_store(store), manifest)
        link = repo / ".claude" / "skills" / "a"
        link.unlink()
        link.symlink_to(tmp_path / "gone")  # the repository put its own dangling link there
        result = materialize(store, repo, (".claude/skills",), [], manifest)
        assert result.removed == [] and link.is_symlink()

    def test_exclude_block_is_the_union_over_agents_sharing_the_git_dir(self, tmp_path):
        store = tmp_path / "store"
        make_skill(store, "a", tags="agent:leo")
        make_skill(store, "b", tags="agent:ike")
        repo = _git_repo(tmp_path / "repo")
        manifests = tmp_path / "data" / "skills" / "materialized"
        entries = read_store(store)
        materialize(
            store,
            repo,
            (".claude/skills",),
            select_skills(entries, (), "leo"),
            manifests / "leo.json",
        )
        materialize(
            store,
            repo,
            (".agents/skills",),
            select_skills(entries, (), "ike"),
            manifests / "ike.json",
        )
        exclude = (repo / ".git" / "info" / "exclude").read_text()
        assert "/.claude/skills/a" in exclude and "/.agents/skills/b" in exclude
        # leo leaves: only leo's line goes
        materialize(store, repo, (".claude/skills",), [], manifests / "leo.json")
        exclude = (repo / ".git" / "info" / "exclude").read_text()
        assert "/.claude/skills/a" not in exclude and "/.agents/skills/b" in exclude

    def test_repository_owned_name_wins_as_a_conflict(self, tmp_path):
        store = tmp_path / "store"
        make_skill(store, "a", tags="all")
        repo = _git_repo(tmp_path / "repo")
        make_skill(repo / ".agents" / "skills", "a", body="# repo version\n")
        result = materialize(
            store, repo, (".agents/skills",), read_store(store), manifest_path(tmp_path, "x")
        )
        assert result.conflicts == [".agents/skills/a is the repository's own skill"]
        assert not result.ok and result.linked == []
        own = repo / ".agents" / "skills" / "a" / "SKILL.md"
        assert own.read_text().endswith("# repo version\n")

    def test_a_store_skill_without_skill_md_is_broken(self, tmp_path):
        store = tmp_path / "store"
        make_skill(store, "a", tags="all")
        selected = read_store(store)
        (store / "a" / "SKILL.md").unlink()
        repo = tmp_path / "repo"
        repo.mkdir()
        manifest = manifest_path(tmp_path, "x")
        result = materialize(store, repo, (".claude/skills",), selected, manifest)
        assert result.broken and "has no SKILL.md" in result.broken[0]

    def test_no_git_directory_means_no_exclude_and_still_links(self, tmp_path):
        store = tmp_path / "store"
        make_skill(store, "a", tags="all")
        repo = tmp_path / "repo"
        repo.mkdir()
        manifest = manifest_path(tmp_path, "x")
        result = materialize(store, repo, (".claude/skills",), read_store(store), manifest)
        assert result.ok and (repo / ".claude" / "skills" / "a").is_symlink()
        assert not (repo / ".git").exists()

    def test_worktree_git_file_resolves_to_the_common_dir(self, tmp_path):
        store = tmp_path / "store"
        make_skill(store, "a", tags="all")
        main = tmp_path / "main"
        (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
        (main / ".git" / "info").mkdir()
        (main / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
        materialize(store, wt, (".claude/skills",), read_store(store), manifest_path(tmp_path, "x"))
        assert "/.claude/skills/a" in (main / ".git" / "info" / "exclude").read_text()


async def test_commit_store_initialises_history_on_first_use(tmp_path):
    calls: list[tuple[str, ...]] = []

    async def fake_git(repo_dir, *args, timeout=30.0):
        calls.append(args)
        if args == ("init", "-q"):
            (Path(repo_dir) / ".git").mkdir()
        if args[:2] == ("diff", "--cached"):
            return 1, "", ""  # something is staged
        return 0, "", ""

    with patch("agent_backbone.git.run_git", fake_git):
        assert await skills.commit_store(tmp_path / "missing", "m") is False
        assert await skills.commit_store(tmp_path, "add x by leo") is True
        assert await skills.commit_store(tmp_path, "again") is True
    assert calls[0] == ("init", "-q")
    assert calls[1] == ("add", "-A") and calls[3] == ("commit", "-q", "-m", "add x by leo")
    assert ("init", "-q") not in calls[4:]
