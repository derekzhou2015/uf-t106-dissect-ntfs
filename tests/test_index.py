from __future__ import annotations

import io
import logging
import struct
from typing import TYPE_CHECKING, BinaryIO
from unittest.mock import Mock

from dissect.ntfs.c_ntfs import ATTRIBUTE_TYPE_CODE, c_ntfs
from dissect.ntfs.index import IndexEntry, Match, _cmp_filename, _cmp_ulong
from dissect.ntfs.ntfs import NTFS

if TYPE_CHECKING:
    import pytest


def mock_filename_entry(filename: str) -> IndexEntry:
    attribute = c_ntfs._FILE_NAME(
        FileNameLength=len(filename),
        FileName=filename,
    ).dumps()
    header = c_ntfs._INDEX_ENTRY(
        Length=len(c_ntfs._INDEX_ENTRY) + len(attribute),
        KeyLength=len(attribute),
    )
    data = header.dumps() + attribute

    mock_root = Mock(attribute_type=ATTRIBUTE_TYPE_CODE.FILE_NAME)
    mock_index = Mock(root=mock_root)
    return IndexEntry(mock_index, io.BytesIO(data), 0)


def mock_ulong_entry(value: int) -> IndexEntry:
    header = c_ntfs._INDEX_ENTRY(
        Length=len(c_ntfs._INDEX_ENTRY) + 4,
        KeyLength=4,
    )
    data = header.dumps() + struct.pack("<I", value)

    mock_root = Mock(attribute_type=ATTRIBUTE_TYPE_CODE.UNUSED)
    mock_index = Mock(root=mock_root)
    return IndexEntry(mock_index, io.BytesIO(data), 0)


def test_cmp_filename() -> None:
    entry = mock_filename_entry("bbbb")

    assert _cmp_filename(entry, "CCCC") == Match.Greater
    assert _cmp_filename(entry, "BBBB") == Match.Equal
    assert _cmp_filename(entry, "BBBA") == Match.Less
    assert _cmp_filename(entry, "BBBBA") == Match.Greater
    assert _cmp_filename(entry, "BBBBB") == Match.Greater

    entry = mock_filename_entry("C_20127.NLS")
    assert _cmp_filename(entry, "CONFIG") == Match.Less


def test_cmp_ulong() -> None:
    entry = mock_ulong_entry(100)

    assert _cmp_ulong(entry, 99) == Match.Less
    assert _cmp_ulong(entry, 100) == Match.Equal
    assert _cmp_ulong(entry, 101) == Match.Greater


def test_index_lookup(ntfs_bin: BinaryIO) -> None:
    fs = NTFS(ntfs_bin)

    root = fs.mft.get("Large Directory")
    assert len(root.listdir()) == 2048

    for i in range(2048):
        entry = root.get(f"Directory {i}")
        assert entry.filename == f"Directory {i}"


def _corrupt_nth_indx_block(data: bytearray, n: int) -> int:
    """Return the offset of the n-th ``INDX`` block in a volume image."""
    offset = -1
    for _ in range(n + 1):
        offset = data.find(b"INDX", offset + 1)
        assert offset != -1
    return offset


def test_index_broken_buffer_signature(ntfs_bin: BinaryIO, caplog: pytest.LogCaptureFixture) -> None:
    """Test that an index buffer with a broken signature is skipped without a trace of corruption.

    This is the pre-existing tolerated behavior: buffers without a valid ``INDX`` signature are
    routinely encountered (stale or uninitialized allocation space) and silently skipped.
    """
    data = bytearray(ntfs_bin.read())
    offset = _corrupt_nth_indx_block(data, 81)
    data[offset : offset + 4] = b"XXXX"

    fs = NTFS(io.BytesIO(bytes(data)))
    with caplog.at_level(logging.WARNING, logger="dissect.ntfs"):
        root = fs.mft.get("Large Directory")
        entries = root.listdir()

    # The 17 entries of the broken buffer are missing, the rest is still enumerated
    assert len(entries) == 2048 - 17
    assert not caplog.records


def test_index_broken_buffer_fixup(ntfs_bin: BinaryIO, caplog: pytest.LogCaptureFixture) -> None:
    """Test that an index buffer with a valid signature but broken fixup data is skipped with a trace.

    Simulates a torn write (e.g. from a power loss): the ``INDX`` signature is intact but the fixup
    data is corrupted. Enumeration must skip the damaged buffer like any other broken buffer instead
    of crashing with a generic ``ValueError``, and must leave a trace in the log.
    """
    data = bytearray(ntfs_bin.read())
    offset = _corrupt_nth_indx_block(data, 81)

    # Corrupt the update sequence array offset (bytes 4-5) to an odd value, breaking the fixup
    data[offset + 4] |= 0x01

    fs = NTFS(io.BytesIO(bytes(data)))
    with caplog.at_level(logging.WARNING, logger="dissect.ntfs.index"):
        root = fs.mft.get("Large Directory")
        entries = root.listdir()

    # The 17 entries of the damaged buffer are missing, the rest is still enumerated
    assert len(entries) == 2048 - 17

    # A trace of the skipped damaged buffer is left in the log
    assert "invalid fixup data" in caplog.text


def test_index_broken_entry_stream(ntfs_bin: BinaryIO, caplog: pytest.LogCaptureFixture) -> None:
    """Test that an index buffer with a corrupted entry stream is skipped with a trace.

    Simulates corruption inside an ``INDX`` block that leaves the signature and fixup data intact:
    the first index entry has an invalid length. Enumeration must not silently lose the remaining
    entries of the buffer, but skip the buffer like any other broken buffer and leave a trace.
    """
    data = bytearray(ntfs_bin.read())
    offset = _corrupt_nth_indx_block(data, 81)

    # Corrupt the Length field of the first index entry in the buffer to an invalid value
    (first_entry_offset,) = struct.unpack_from("<I", data, offset + 0x18)
    struct.pack_into("<H", data, offset + 0x18 + first_entry_offset + 8, 1)

    fs = NTFS(io.BytesIO(bytes(data)))
    with caplog.at_level(logging.WARNING, logger="dissect.ntfs.index"):
        root = fs.mft.get("Large Directory")
        entries = root.listdir()

    # The 17 entries of the damaged buffer are missing, the rest is still enumerated
    assert len(entries) == 2048 - 17

    # A trace of the skipped damaged buffer is left in the log
    assert "invalid entry length" in caplog.text
