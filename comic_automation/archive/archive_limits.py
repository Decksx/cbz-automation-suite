"""Archive-wide resource limits and member-name safety for CBZ reads.

One validator, applied to a ZIP's central directory (``infolist()``) right
after the archive is opened and before any member is read. It is the
archive-level counterpart of the per-member guards that already exist:
``MAX_COMIC_INFO_BYTES`` in ``inspection.py``,
``MAX_PAGE_UNCOMPRESSED_BYTES`` and the pinned ``Image.MAX_IMAGE_PIXELS``
in ``perceptual_hashing.py`` (#9, #18). Those check a single member; nothing
checked the archive as a whole, and nothing looked at member names at all.

Four checks, each with its own reason code. The reason code is the string
the job layer records as ``jobs.failure_category``, so it is part of the
operational vocabulary and must not be renamed casually:

* ``archive_too_many_entries`` -- more than ``MAX_ARCHIVE_ENTRIES``
  central-directory records (directories included, since every record is
  iterated and costs work even when it holds no data).
* ``archive_too_large`` -- the declared uncompressed sizes sum to more
  than ``MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES``.
* ``archive_unsafe_member_path`` -- a member name that would escape an
  extraction root: absolute (``/x``, ``\\x``, ``//server/share``), carrying
  a drive letter (``C:x``, ``C:/x``), or containing a ``..`` component under
  either separator.
* ``archive_encrypted`` -- a member with the traditional-encryption or
  strong-encryption general-purpose flag set. Opt-out via
  ``reject_encrypted=False`` for inspection, which reports encryption as a
  status rather than failing on it.

Declared metadata only: not a memory or decompression bound
-----------------------------------------------------------

The count and size caps are *admission checks on the central directory*.
The size cap reads ``ZipInfo.file_size``, the size the archive claims, and
the archive's author also writes the CRC-32. They refuse an archive whose
own metadata is out of bounds; they do not bound the work or the memory
spent decompressing an archive whose metadata lies.

Measured on CPython 3.11.3 (the project interpreter): a 1,000,000-byte
deflated member declared as 1,000 bytes, with its CRC-32 set to that of the
first 1,000 bytes, passes the validator at a 1,000-byte ceiling and
``ZipFile.read`` returns exactly 1,000 bytes *successfully*.
``ZipExtFile._read1`` hands the decompressor a large ``max_length`` and
slices to the declared length afterwards, so the full 1,000,000 bytes are
decompressed and allocated to produce them (tracemalloc peak above
900,000 bytes). ``test_the_size_cap_does_not_bound_decompressor_work``
pins that limitation. What a caller *retains* from ``read(info)`` is at
most the declared size; the transient decompressor allocation is not.

A real decompression/allocation budget (bounded ``decompressobj`` reads,
or a per-member ``compress_size`` ratio check) is an open roadmap Step 7
item. It is deliberately not implemented here.

What the limits are not
-----------------------

They are safety ceilings, not policy. Neither value was calibrated against
the live library -- that measurement is the operator's, against the
production database, and has not been made. They were chosen so that no
plausible comic archive comes near them: a 2,000-page omnibus is 25x under
the entry cap, and the total cap is about 82 times the 200 MiB per-page
ceiling.
Lowering either needs a measured distribution first, recorded with its
numerator, denominator and population.

The validator does not cover a decompression/allocation budget (above),
duplicate member names, nested archives, a processing timeout, a
temporary-disk budget, or NTFS alternate-data-stream names
(``page.png:stream``); those remain open roadmap Step 7 items. See
``docs/archive_io_resource_audit.md``, "Implemented 2026-10-22".
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable


# Ceiling on central-directory records per archive. See the module
# docstring for why this value is a ceiling and not a calibrated policy.
MAX_ARCHIVE_ENTRIES = 50_000

# Ceiling on the sum of declared uncompressed member sizes. 16 GiB is far
# beyond any single comic archive; it exists to reject a central directory
# that declares a decompression bomb before anything is read, not to
# constrain normal operation.
MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES = 16 * 1024 * 1024 * 1024

# General-purpose bit flags (APPNOTE 4.4.4). Bit 0 marks any encrypted
# member (traditional PKWARE and WinZip AES both set it); bit 6 marks
# PKWARE strong encryption. The spec says bit 6 implies bit 0, but a
# malformed archive can set bit 6 alone, and zipfile then raises
# NotImplementedError on open -- so both are checked.
ENCRYPTED_FLAG_MASK = 0x0001 | 0x0040

REASON_TOO_MANY_ENTRIES = "archive_too_many_entries"
REASON_TOO_LARGE = "archive_too_large"
REASON_UNSAFE_MEMBER_PATH = "archive_unsafe_member_path"
REASON_ENCRYPTED = "archive_encrypted"

_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")


class UnsafeArchiveError(ValueError):
    """An archive's central directory fails a resource or safety limit.

    ``reason`` is one of the ``REASON_*`` codes above and doubles as the job
    failure category. ``member`` names the offending entry where there is
    one (path and encryption checks), and is ``None`` for the archive-wide
    count and size checks.

    Deliberately a ``ValueError``, not an ``OSError`` or ``RuntimeError``:
    callers treat ``OSError`` as transient and retry it (the sanitizer's
    file-locked loop) and ``RuntimeError`` as ``archive_unreadable``.
    Neither is true of a hostile central directory, which will be just as
    hostile on the next attempt.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        member: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.member = member


def is_encrypted_member(entry: zipfile.ZipInfo) -> bool:
    """True when ``entry`` carries either encryption flag bit."""
    return bool(entry.flag_bits & ENCRYPTED_FLAG_MASK)


def unsafe_member_path_reason(name: str) -> str | None:
    """Why ``name`` would escape an extraction root, or ``None`` if it would not.

    Both separators are treated as separators. On Windows ``zipfile`` already
    rewrites ``\\`` to ``/`` when it reads a central directory, but on any
    other host it does not, and a name is judged by where it would land on
    the Windows machines this project runs on.

    Only an exact ``..`` component is traversal. ``...``, ``.. `` and
    ``..cover.png`` are ordinary names: measured on this Windows host on
    2026-10-22, ``os.path.abspath`` (``GetFullPathNameW``) resolves
    ``base/sub/../x`` to ``base/x`` but leaves ``...``, ``.. ``, ``. .``,
    ``....`` and `` ..`` in place as literal components.
    """
    normalized = name.replace("\\", "/")

    if normalized.startswith("/"):
        return "absolute path"

    if _DRIVE_LETTER_RE.match(normalized):
        return "drive-qualified path"

    if ".." in normalized.split("/"):
        return "parent-directory traversal"

    return None


def validate_archive_members(
    entries: Iterable[zipfile.ZipInfo],
    *,
    reject_encrypted: bool = True,
    max_entries: int | None = None,
    max_total_uncompressed_bytes: int | None = None,
) -> None:
    """Raise ``UnsafeArchiveError`` if the central directory breaks a limit.

    Called with ``archive.infolist()`` immediately after opening, before any
    member is read, so a rejected archive costs one central-directory parse
    and nothing else. Checks run in a fixed order -- count, total size, then
    per-member names and encryption -- so the reported reason for an archive
    that breaks several limits is deterministic.

    The limits are read at call time rather than bound as defaults, so a
    test that monkeypatches the module constant exercises the same code
    path production does. ``max_entries`` / ``max_total_uncompressed_bytes``
    override them for a caller with a stricter budget; no caller does today.
    """
    entry_list = list(entries)
    entry_limit = (
        MAX_ARCHIVE_ENTRIES if max_entries is None else max_entries
    )
    size_limit = (
        MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES
        if max_total_uncompressed_bytes is None
        else max_total_uncompressed_bytes
    )

    if len(entry_list) > entry_limit:
        raise UnsafeArchiveError(
            f"Archive has {len(entry_list)} entries, over the "
            f"{entry_limit}-entry limit.",
            reason=REASON_TOO_MANY_ENTRIES,
        )

    total = sum(int(entry.file_size) for entry in entry_list)

    if total > size_limit:
        raise UnsafeArchiveError(
            f"Archive declares {total} uncompressed bytes, over the "
            f"{size_limit}-byte limit.",
            reason=REASON_TOO_LARGE,
        )

    for entry in entry_list:
        path_problem = unsafe_member_path_reason(entry.filename)

        if path_problem is not None:
            raise UnsafeArchiveError(
                f"Unsafe member name {entry.filename!r}: {path_problem}.",
                reason=REASON_UNSAFE_MEMBER_PATH,
                member=entry.filename,
            )

        if reject_encrypted and is_encrypted_member(entry):
            raise UnsafeArchiveError(
                f"Member {entry.filename!r} is encrypted; encrypted "
                "archives are not read.",
                reason=REASON_ENCRYPTED,
                member=entry.filename,
            )
