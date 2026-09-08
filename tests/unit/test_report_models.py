"""Length, structure and link boundaries shared by every publishing tool."""

import pytest
from pydantic import ValidationError

from agent_backbone.models import ProgressReport, ReportLink, report_example


@pytest.mark.parametrize(
    "section,limit",
    [("goal", 240), ("progress", 480), ("blockers", 320), ("next", 320), ("note", 240)],
)
def test_exact_section_limit_and_one_character_over(section, limit):
    data = report_example()
    data[section] = {"text": "a" * limit, "links": []}
    if section == "blockers":
        data[section]["kind"] = "none"
    assert len(getattr(ProgressReport.model_validate(data), section).text) == limit
    data[section]["text"] += "b"
    with pytest.raises(ValidationError) as exc:
        ProgressReport.model_validate(data)
    assert exc.value.errors()[0]["loc"] == (section, "text")


@pytest.mark.parametrize("section", ["goal", "progress", "blockers", "next"])
def test_required_sections_and_explicit_link_lists(section):
    data = report_example()
    del data[section]
    with pytest.raises(ValidationError):
        ProgressReport.model_validate(data)
    data = report_example()
    del data[section]["links"]
    with pytest.raises(ValidationError):
        ProgressReport.model_validate(data)


@pytest.mark.parametrize("value", ["", "   ", "a\nb", "a\x1bb", "a\x00b", "a\u202eb", "\ud800"])
def test_plain_single_paragraph_without_terminal_or_bidi_controls(value):
    data = report_example()
    data["goal"]["text"] = value
    with pytest.raises(ValidationError):
        ProgressReport.model_validate(data)


def test_unicode_is_counted_as_characters_and_unknown_fields_are_rejected():
    data = report_example()
    data["goal"]["text"] = "ሰ" * 240
    assert ProgressReport.model_validate(data).goal.text == "ሰ" * 240
    data["extra_log"] = "not a report section"
    with pytest.raises(ValidationError):
        ProgressReport.model_validate(data)


def test_total_text_titles_and_links_are_bounded():
    data = report_example()
    for field, length in (("goal", 240), ("progress", 480), ("blockers", 300), ("next", 300)):
        data[field]["text"] = "a" * length
        data[field]["links"] = []
    data["note"] = {"text": "a" * 180, "links": []}
    assert ProgressReport.model_validate(data)
    data["note"]["links"] = [{"title": "One", "url": "https://example.com"}]
    with pytest.raises(ValidationError, match="maximum 1500"):
        ProgressReport.model_validate(data)
    data = report_example()
    link = {"title": "Issue", "url": "https://github.com/example/shop/issues/1"}
    for section in data.values():
        if isinstance(section, dict):
            section["links"] = [link, link]
    with pytest.raises(ValidationError, match="maximum 6"):
        ProgressReport.model_validate(data)
    data["goal"]["links"].append(link)
    with pytest.raises(ValidationError):
        ProgressReport.model_validate(data)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://user:secret@example.com",
        "https://example.com\nhi",
        "https:///missing",
        "/relative",
        "https://example.com:bad/",
        "https://example.com/" + "a" * 400,
    ],
)
def test_invalid_and_oversize_links_are_rejected(url):
    with pytest.raises(ValidationError):
        ReportLink(title="Read more", url=url)


def test_status_has_explicit_blocker_semantics():
    data = report_example()
    data["status"] = "blocked"
    with pytest.raises(ValidationError, match="explain a blocker"):
        ProgressReport.model_validate(data)
    data["blockers"]["kind"] = "owner"
    data["blockers"]["text"] = "Please choose which checkout design to use."
    assert ProgressReport.model_validate(data)
    data["status"] = "inactive"
    with pytest.raises(ValidationError, match="unresolved blocker"):
        ProgressReport.model_validate(data)
