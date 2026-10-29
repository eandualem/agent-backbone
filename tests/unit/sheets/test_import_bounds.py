"""Archive checks run before the native importer and count streamed bytes."""

import io
import zipfile

import pytest

from agent_backbone.sheets import SheetError
from agent_backbone.sheets._interchange import _checked_zip


def archive(name, body):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, body)
    return out.getvalue()


@pytest.mark.parametrize(
    "name,content",
    [
        ("xl/vbaProject.bin", b"macro"),
        ("xl/externallinks/externalLink1.xml", b"<x/>"),
        ("xl/worksheets/sheet1.xml", b'<!DOCTYPE x [<!ENTITY x "payload">]><x/>'),
        (
            "xl/_rels/workbook.xml.rels",
            b'<Relationships><Relationship TargetMode="External" Target="file:///unknown"/></Relationships>',
        ),
    ],
)
def test_unsafe_xlsx_features_refused(name, content):
    with pytest.raises(SheetError) as failed:
        _checked_zip(archive(name, content))
    assert failed.value.detail["code"] == "unsupported_import"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_xml_entities_refused_regardless_of_encoding(encoding):
    content = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        '<!DOCTYPE x [<!ENTITY value "expanded">]><x>&value;</x>'
    ).encode(encoding)
    with pytest.raises(SheetError, match="XML entity declarations"):
        _checked_zip(archive("xl/worksheets/sheet1.xml", content))


def test_zip_declared_and_actual_streamed_sizes_are_both_bounded(monkeypatch):
    from agent_backbone.sheets import _interchange

    monkeypatch.setattr(_interchange, "UNPACKED_BYTES", 8)
    raw = archive("large.bin", b"a" * 32)
    with pytest.raises(SheetError, match="decompressed"):
        _checked_zip(raw)
    tiny = archive("tiny.bin", b"a")
    original = zipfile.ZipFile.open

    # Even a source advertising tiny entries must not supply an unbounded stream.
    def different_stream(self, entry, *args, **kwargs):
        return io.BytesIO(b"a" * 32)

    monkeypatch.setattr(zipfile.ZipFile, "open", different_stream)
    with pytest.raises(SheetError, match="actual decompressed"):
        _checked_zip(tiny)
    monkeypatch.setattr(zipfile.ZipFile, "open", original)
