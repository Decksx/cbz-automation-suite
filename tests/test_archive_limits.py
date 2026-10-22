"""Archive-wide resource limits and member-name safety (roadmap Step 7).

Covers `comic_automation/archive/archive_limits.py` and every boundary it is
wired into: inspection (and its job handler), exact page hashing, perceptual
hashing, and `cbz_library_maintenance.write_comicinfo`.

Every fixture is synthetic and built in `tmp_path`. Hostile central
directories are produced by building an ordinary zip and then patching the
declared size or the encryption flag bits in place, so a test never has to
write gigabytes to declare gigabytes.

Several tests replace `zipfile.ZipFile.open` with a tripwire. That is the
"refused before any member is read" claim made observable: `ZipFile.read`
and `testzip` both go through `open`, so if a guard ever moved below the
first member read, the tripwire fires instead of the expected refusal.
"""

from __future__ import annotations

import struct
import tracemalloc
import warnings
import zipfile
import zlib
from pathlib import Path

import pytest

from comic_automation.archive import archive_limits
from comic_automation.archive.archive_limits import (
    MAX_ARCHIVE_ENTRIES,
    MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES,
    REASON_ENCRYPTED,
    REASON_TOO_LARGE,
    REASON_TOO_MANY_ENTRIES,
    REASON_UNSAFE_MEMBER_PATH,
    UnsafeArchiveError,
    is_encrypted_member,
    unsafe_member_path_reason,
    validate_archive_members,
)
from comic_automation.archive.handlers import InspectArchiveHandler
from comic_automation.archive.inspection import (
    UnsafeArchiveContentError,
    inspect_archive,
)
from comic_automation.archive.page_hashing import calculate_page_hashes
from comic_automation.archive.perceptual_hashing import (
    calculate_perceptual_hashes,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import (
    JobQueue,
    JobStatus,
    JobWorker,
    PermanentJobError,
)
from scripts.cbz_library_maintenance import write_comicinfo
from tests import fault_injection as fi
from tests import golden_corpus as gc


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

NEW_XML = "<ComicInfo><Title>Rewritten</Title></ComicInfo>"

# Central-directory and local-header field offsets (APPNOTE 4.3.7, 4.3.12).
_CD_SIGNATURE = b"PK\x01\x02"
_CD_FLAGS = 8
_CD_CRC = 16
_CD_UNCOMPRESSED_SIZE = 24
_CD_NAME_LENGTH = 28
_CD_HEADER_OFFSET = 42
_CD_FIXED = 46
_LOCAL_FLAGS = 6


# --- fixture helpers ---------------------------------------------------------


def _build(path: Path, names: list[str]) -> Path:
    """A real, decodable archive: ComicInfo plus one PNG per name."""
    entries: list[tuple[str, bytes]] = [(gc.COMIC_INFO, gc.comic_info_xml())]
    entries.extend(
        (name, gc.page_image(index + 1)) for index, name in enumerate(names)
    )
    return gc.build_cbz(path, entries)


def _patch_central_directory(
    path: Path,
    member: str,
    *,
    declared_size: int | None = None,
    crc: int | None = None,
    flag_bits_or: int = 0,
) -> None:
    """Rewrite one member's central-directory record (and local flags).

    `declared_size` replaces the central-directory uncompressed size, which
    is the value `ZipInfo.file_size` reports. `crc` replaces the
    central-directory CRC-32, which is the value `ZipExtFile` checks the
    returned bytes against. `flag_bits_or` is OR-ed into the general-purpose
    flags in both the central directory and the local header, so the
    archive is consistently marked rather than half-marked.
    """
    data = bytearray(path.read_bytes())
    position = data.find(_CD_SIGNATURE)
    patched = False

    while position != -1:
        name_length = struct.unpack_from(
            "<H", data, position + _CD_NAME_LENGTH
        )[0]
        name = bytes(
            data[position + _CD_FIXED:position + _CD_FIXED + name_length]
        ).decode("utf-8")

        if name == member:
            if declared_size is not None:
                struct.pack_into(
                    "<I", data, position + _CD_UNCOMPRESSED_SIZE,
                    declared_size,
                )
            if crc is not None:
                struct.pack_into("<I", data, position + _CD_CRC, crc)
            if flag_bits_or:
                flags = struct.unpack_from("<H", data, position + _CD_FLAGS)[0]
                struct.pack_into(
                    "<H", data, position + _CD_FLAGS, flags | flag_bits_or
                )
                local = struct.unpack_from(
                    "<I", data, position + _CD_HEADER_OFFSET
                )[0]
                local_flags = struct.unpack_from(
                    "<H", data, local + _LOCAL_FLAGS
                )[0]
                struct.pack_into(
                    "<H", data, local + _LOCAL_FLAGS,
                    local_flags | flag_bits_or,
                )
            patched = True

        position = data.find(_CD_SIGNATURE, position + 4)

    assert patched, f"{member!r} not found in {path}"
    path.write_bytes(bytes(data))


def _declared_bomb(path: Path) -> Path:
    """Five real pages whose central directory claims ~20 GB in total.

    0xF0000000 per member stays below the 0xFFFFFFFF ZIP64 sentinel, so
    zipfile reads the patched value at face value. 5 x 4,026,531,840 =
    20,132,659,200 bytes, over the 17,179,869,184-byte (16 GiB) ceiling.
    """
    names = [f"{index:03d}.png" for index in range(1, 6)]
    _build(path, names)
    for name in names:
        _patch_central_directory(path, name, declared_size=0xF0000000)
    return path


def _encrypted(path: Path, flag: int = 0x0001) -> Path:
    """Two pages, the second carrying an encryption flag bit."""
    _build(path, ["001.png", "002.png"])
    _patch_central_directory(path, "002.png", flag_bits_or=flag)
    return path


@pytest.fixture
def arm_no_member_reads(monkeypatch: pytest.MonkeyPatch):
    """Returns a callable that makes any later member open fail the test.

    Armed explicitly, after the fixture archive is built, because writing
    an archive goes through `ZipFile.open` too.
    """

    def tripwire(self, name, *args, **kwargs):
        raise AssertionError(
            f"member {getattr(name, 'filename', name)!r} was opened; the "
            "limit must refuse the archive on its central directory alone"
        )

    def arm() -> None:
        monkeypatch.setattr(zipfile.ZipFile, "open", tripwire)

    return arm


def _infos(path: Path) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(path) as archive:
        return archive.infolist()


# --- the fixtures are what they claim ----------------------------------------


def test_the_declared_bomb_fixture_declares_over_the_ceiling(
    tmp_path: Path,
) -> None:
    """Guards the fixture, so a size test cannot pass on an inert archive."""
    infos = _infos(_declared_bomb(tmp_path / "bomb.cbz"))

    assert sum(i.file_size for i in infos) > MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES
    assert (tmp_path / "bomb.cbz").stat().st_size < 64 * 1024


def test_the_encrypted_fixture_really_is_unreadable_without_a_password(
    tmp_path: Path,
) -> None:
    """The flag patch must produce what zipfile treats as encryption.

    This is the failure the encrypted-member guard exists to pre-empt: a
    bare RuntimeError from `ZipFile.open`, which the hashing modules would
    otherwise file as `archive_unreadable`.
    """
    path = _encrypted(tmp_path / "enc.cbz")

    with zipfile.ZipFile(path) as archive:
        with pytest.raises(RuntimeError, match="encrypted"):
            archive.read("002.png")


def test_the_size_cap_does_not_bound_decompressor_work(
    tmp_path: Path,
) -> None:
    """Pins the limitation: the cap is an admission check on metadata only.

    A 1,000,000-byte deflated member is declared as 1,000 bytes, with its
    CRC-32 set to that of the first 1,000 bytes -- the attacker writes the
    CRC as well as the size. It passes the validator at a 1,000-byte
    ceiling, and `ZipFile.read` *succeeds*, returning exactly the declared
    1,000 bytes. On CPython 3.11.3 `ZipExtFile._read1` hands the
    decompressor a large `max_length` and only slices to the declared
    length afterwards, so the whole 1,000,000 bytes are decompressed and
    allocated to produce them; tracemalloc's peak shows it.

    If this ever starts failing, the interpreter has begun bounding
    decompression by the declared size, and the open "decompression /
    allocation budget" item in `docs/archive_io_resource_audit.md` should be
    re-examined rather than this test deleted.
    """
    payload = b"\0" * 1_000_000
    path = tmp_path / "liar.cbz"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.bin", payload)
    _patch_central_directory(
        path, "big.bin", declared_size=1_000, crc=zlib.crc32(payload[:1_000])
    )

    infos = _infos(path)
    validate_archive_members(infos, max_total_uncompressed_bytes=1_000)

    with zipfile.ZipFile(path) as archive:
        tracemalloc.start()
        try:
            returned = archive.read("big.bin")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert returned == payload[:1_000]
    # Far more than the declared 1,000 bytes was produced to return them.
    assert peak > 900_000


# --- the validator -----------------------------------------------------------


def test_ordinary_corpus_archives_pass_every_limit(tmp_path: Path) -> None:
    """The control: normal archives, including the 120-page one, pass."""
    for name in ("ordinary", "many_entries", "unicode_paths"):
        validate_archive_members(_infos(gc.build_case(name, tmp_path)))


def test_entry_count_at_the_limit_passes_and_one_over_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary on a lowered limit; the limit is read at call time."""
    monkeypatch.setattr(archive_limits, "MAX_ARCHIVE_ENTRIES", 4)
    at_limit = _infos(_build(tmp_path / "four.cbz", ["1.png", "2.png", "3.png"]))
    over = _infos(
        _build(tmp_path / "five.cbz", ["1.png", "2.png", "3.png", "4.png"])
    )

    validate_archive_members(at_limit)
    with pytest.raises(UnsafeArchiveError) as caught:
        validate_archive_members(over)

    assert caught.value.reason == REASON_TOO_MANY_ENTRIES
    assert caught.value.member is None


def test_the_real_entry_ceiling_refuses_an_archive_one_entry_over(
    tmp_path: Path,
) -> None:
    """The shipped constant, not a monkeypatched one, against a real zip.

    MAX_ARCHIVE_ENTRIES + 1 empty stored members: a few megabytes, built in
    about a second, and the only test that proves the production value is
    enforced rather than just the comparison.
    """
    path = tmp_path / "many.cbz"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for index in range(MAX_ARCHIVE_ENTRIES + 1):
            archive.writestr(
                zipfile.ZipInfo(f"{index:06d}.png", gc.FIXED_DATE_TIME), b""
            )

    with pytest.raises(UnsafeArchiveContentError) as caught:
        inspect_archive(path)

    assert caught.value.reason == REASON_TOO_MANY_ENTRIES


def test_declared_total_size_over_the_ceiling_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(UnsafeArchiveError) as caught:
        validate_archive_members(_infos(_declared_bomb(tmp_path / "b.cbz")))

    assert caught.value.reason == REASON_TOO_LARGE


def test_declared_total_size_at_the_limit_passes_and_one_byte_over_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    infos = _infos(_build(tmp_path / "a.cbz", ["1.png", "2.png"]))
    total = sum(i.file_size for i in infos)

    monkeypatch.setattr(
        archive_limits, "MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES", total
    )
    validate_archive_members(infos)

    monkeypatch.setattr(
        archive_limits, "MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES", total - 1
    )
    with pytest.raises(UnsafeArchiveError) as caught:
        validate_archive_members(infos)

    assert caught.value.reason == REASON_TOO_LARGE


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("../escape.png", "parent-directory traversal"),
        ("nested/../../escape.png", "parent-directory traversal"),
        ("nested/..", "parent-directory traversal"),
        ("nested\\..\\..\\escape.png", "parent-directory traversal"),
        ("/absolute.png", "absolute path"),
        ("\\absolute.png", "absolute path"),
        ("//server/share/x.png", "absolute path"),
        ("\\\\server\\share\\x.png", "absolute path"),
        ("C:x.png", "drive-qualified path"),
        ("C:/Windows/x.png", "drive-qualified path"),
        ("c:\\x.png", "drive-qualified path"),
    ],
)
def test_escaping_member_names_are_identified(name: str, why: str) -> None:
    """Both separators, because a name is judged by where it lands on Windows."""
    assert unsafe_member_path_reason(name) == why


@pytest.mark.parametrize(
    "name",
    [
        "001.png",
        "日本語/003.png",
        "...",
        "..cover.png",
        "a..b/x.png",
        "Vol. 1/x.png",
        ".hidden/x.png",
        "./x.png",
        "dir/.. /x.png",
        "1:2.png",
    ],
)
def test_lookalike_names_are_not_treated_as_traversal(name: str) -> None:
    """Only an exact `..` component traverses (measured; see module docs).

    A false positive here would refuse a legitimate archive, which is the
    "keep normal behaviour identical" half of the guard's contract.
    """
    assert unsafe_member_path_reason(name) is None


@pytest.mark.parametrize(
    "name", ["../escape.png", "/absolute.png", "C:/x.png"]
)
def test_an_archive_with_an_escaping_member_is_refused(
    tmp_path: Path, name: str
) -> None:
    path = _build(tmp_path / "unsafe.cbz", ["001.png", name])

    with pytest.raises(UnsafeArchiveError) as caught:
        validate_archive_members(_infos(path))

    assert caught.value.reason == REASON_UNSAFE_MEMBER_PATH
    assert caught.value.member == name


@pytest.mark.parametrize("flag", [0x0001, 0x0040])
def test_an_encrypted_member_is_refused_by_default(
    tmp_path: Path, flag: int
) -> None:
    """Bit 0 (any encryption) and bit 6 (strong encryption) both count."""
    infos = _infos(_encrypted(tmp_path / "enc.cbz", flag))

    assert is_encrypted_member(infos[-1])
    with pytest.raises(UnsafeArchiveError) as caught:
        validate_archive_members(infos)

    assert caught.value.reason == REASON_ENCRYPTED
    assert caught.value.member == "002.png"


def test_reject_encrypted_false_lets_an_encrypted_archive_through(
    tmp_path: Path,
) -> None:
    validate_archive_members(
        _infos(_encrypted(tmp_path / "enc.cbz")), reject_encrypted=False
    )


def test_unsafe_archive_error_is_neither_oserror_nor_runtimeerror() -> None:
    """Callers retry OSError and file RuntimeError as archive_unreadable.

    A hostile central directory is neither transient nor unreadable, so the
    type must keep it out of both branches.
    """
    assert not issubclass(UnsafeArchiveError, (OSError, RuntimeError))


# --- inspection and its handler ----------------------------------------------


def test_inspection_refuses_a_declared_bomb_before_reading_any_member(
    tmp_path: Path, arm_no_member_reads
) -> None:
    path = _declared_bomb(tmp_path / "bomb.cbz")
    arm_no_member_reads()

    with pytest.raises(UnsafeArchiveContentError) as caught:
        inspect_archive(path, verify_crc=True)

    assert caught.value.reason == REASON_TOO_LARGE


def test_inspection_refuses_an_unsafe_member_name(tmp_path: Path) -> None:
    path = _build(tmp_path / "unsafe.cbz", ["001.png", "../escape.png"])

    with pytest.raises(UnsafeArchiveContentError) as caught:
        inspect_archive(path)

    assert caught.value.reason == REASON_UNSAFE_MEMBER_PATH
    assert caught.value.member == "../escape.png"


def test_inspection_still_reports_encryption_as_a_status(
    tmp_path: Path,
) -> None:
    """Unchanged contract: encryption is reported, not raised."""
    result = inspect_archive(_encrypted(tmp_path / "enc.cbz"))

    assert result.status == "encrypted"
    assert result.encrypted is True


def test_inspection_with_crc_check_reports_an_encrypted_archive(
    tmp_path: Path,
) -> None:
    """Previously a bare RuntimeError from testzip() escaped inspection.

    The CRC pass opens every member and zipfile demands a password for the
    encrypted one. Now the pass is skipped for an encrypted archive and the
    result says the CRC was not verified.
    """
    result = inspect_archive(_encrypted(tmp_path / "enc.cbz"), verify_crc=True)

    assert result.status == "encrypted"
    assert result.crc_verified is False


def test_inspection_of_an_ordinary_archive_with_crc_check_is_unchanged(
    tmp_path: Path,
) -> None:
    """The control for the test above: CRC is still verified normally."""
    result = inspect_archive(gc.build_case("ordinary", tmp_path), verify_crc=True)

    assert result.status == "ok"
    assert result.crc_verified is True
    assert result.page_count == 3


def _run_inspection_job(
    tmp_path: Path, archive: Path, *, verify_crc: bool = False
):
    """One inspect_archive job through a real worker, returning the job."""
    with database_connection(tmp_path / "jobs.db") as connection:
        apply_migrations(connection, MIGRATION_DIRECTORY)
        stat = archive.stat()
        archive_id = int(
            connection.execute(
                "INSERT INTO archive_files (file_size) VALUES (?)",
                (stat.st_size,),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, file_size, modified_time_ns
            )
            VALUES (?, ?, ?, ?)
            """,
            (archive_id, str(archive.resolve()), stat.st_size,
             stat.st_mtime_ns),
        )
        queue = JobQueue(connection)
        queued = queue.enqueue(
            "inspect_archive", archive_id=archive_id, max_attempts=3
        )
        worker = JobWorker(
            queue,
            {
                "inspect_archive": InspectArchiveHandler(
                    connection, verify_crc=verify_crc
                )
            },
            worker_id="archive-limits-test-worker",
            poll_interval_seconds=0,
        )
        worker.run_once()
        return queue.get(queued.id)


def _encrypted_directory_record(path: Path, flag: int) -> Path:
    """An ordinary page plus a `folder/` directory record carrying `flag`.

    `testzip()` opens every central-directory record, directories included,
    so a flag on the directory record alone is enough to make it raise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(zipfile.ZipInfo("folder/", gc.FIXED_DATE_TIME), b"")
        archive.writestr(
            zipfile.ZipInfo("folder/001.png", gc.FIXED_DATE_TIME),
            gc.page_image(1),
        )
    _patch_central_directory(path, "folder/", flag_bits_or=flag)
    return path


@pytest.mark.parametrize("flag", [0x0001, 0x0040])
def test_an_encrypted_directory_record_is_reported_by_crc_inspection(
    tmp_path: Path, flag: int
) -> None:
    """Encryption is judged over every record, not only file records.

    Inspection used to scan only files, so a flagged directory record left
    `encrypted=False` and the CRC pass then raised a bare RuntimeError
    (bit 0) or NotImplementedError (bit 6) out of testzip().
    """
    path = _encrypted_directory_record(tmp_path / "dir.cbz", flag)

    with zipfile.ZipFile(path) as archive:
        assert archive.getinfo("folder/").is_dir()
        assert is_encrypted_member(archive.getinfo("folder/"))

    result = inspect_archive(path, verify_crc=True)

    assert result.status == "encrypted"
    assert result.encrypted is True
    assert result.crc_verified is False


@pytest.mark.parametrize("flag", [0x0001, 0x0040])
def test_an_encrypted_directory_record_does_not_become_unclassified(
    tmp_path: Path, flag: int
) -> None:
    """Through the real handler and worker: the job completes."""
    archive = _encrypted_directory_record(tmp_path / "lib" / "dir.cbz", flag)

    job = _run_inspection_job(tmp_path, archive, verify_crc=True)

    assert job.status == JobStatus.COMPLETED
    assert job.failure_category is None


def test_handler_records_the_reason_code_as_a_permanent_failure(
    tmp_path: Path,
) -> None:
    """Fails on the first attempt with the specific category.

    Without the dedicated mapping the error would still be permanent, but
    filed under the generic `archive_inspection_error` bucket.
    """
    archive = _build(tmp_path / "lib" / "unsafe.cbz", ["../escape.png"])

    job = _run_inspection_job(tmp_path, archive)

    assert job.status == JobStatus.FAILED
    assert job.attempts == 1
    assert job.failure_category == REASON_UNSAFE_MEMBER_PATH


def test_a_crc_checking_inspection_job_completes_on_an_encrypted_archive(
    tmp_path: Path,
) -> None:
    """End to end for the testzip() fix.

    Before it, the RuntimeError from testzip() reached the worker, which
    records any uncategorized exception as a retryable `unclassified_error`.
    """
    archive = _encrypted(tmp_path / "lib" / "enc.cbz")

    job = _run_inspection_job(tmp_path, archive, verify_crc=True)

    assert job.status == JobStatus.COMPLETED


# --- exact page hashing ------------------------------------------------------


def test_page_hashing_refuses_a_declared_bomb_before_reading(
    tmp_path: Path, arm_no_member_reads
) -> None:
    path = _declared_bomb(tmp_path / "bomb.cbz")
    arm_no_member_reads()

    with pytest.raises(PermanentJobError) as caught:
        calculate_page_hashes(path)

    assert caught.value.category == REASON_TOO_LARGE


def test_page_hashing_refuses_too_many_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm_no_member_reads
) -> None:
    monkeypatch.setattr(archive_limits, "MAX_ARCHIVE_ENTRIES", 2)
    path = _build(tmp_path / "a.cbz", ["1.png", "2.png"])
    arm_no_member_reads()

    with pytest.raises(PermanentJobError) as caught:
        calculate_page_hashes(path)

    assert caught.value.category == REASON_TOO_MANY_ENTRIES


def test_page_hashing_refuses_an_unsafe_member_name(tmp_path: Path) -> None:
    path = _build(tmp_path / "u.cbz", ["001.png", "nested/../../x.png"])

    with pytest.raises(PermanentJobError) as caught:
        calculate_page_hashes(path)

    assert caught.value.category == REASON_UNSAFE_MEMBER_PATH


def test_page_hashing_files_an_encrypted_member_as_encrypted(
    tmp_path: Path,
) -> None:
    """Previously `archive_unreadable`, via zipfile's RuntimeError."""
    with pytest.raises(PermanentJobError) as caught:
        calculate_page_hashes(_encrypted(tmp_path / "enc.cbz"))

    assert caught.value.category == REASON_ENCRYPTED


# --- perceptual hashing ------------------------------------------------------


def test_perceptual_hashing_refuses_a_declared_bomb_before_reading(
    tmp_path: Path, arm_no_member_reads
) -> None:
    path = _declared_bomb(tmp_path / "bomb.cbz")
    arm_no_member_reads()

    with pytest.raises(PermanentJobError) as caught:
        calculate_perceptual_hashes(path)

    assert caught.value.category == REASON_TOO_LARGE


def test_perceptual_hashing_refuses_too_many_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm_no_member_reads
) -> None:
    monkeypatch.setattr(archive_limits, "MAX_ARCHIVE_ENTRIES", 2)
    path = _build(tmp_path / "a.cbz", ["1.png", "2.png"])
    arm_no_member_reads()

    with pytest.raises(PermanentJobError) as caught:
        calculate_perceptual_hashes(path)

    assert caught.value.category == REASON_TOO_MANY_ENTRIES


def test_perceptual_hashing_refuses_an_unsafe_member_name(
    tmp_path: Path,
) -> None:
    path = _build(tmp_path / "u.cbz", ["001.png", "/absolute.png"])

    with pytest.raises(PermanentJobError) as caught:
        calculate_perceptual_hashes(path)

    assert caught.value.category == REASON_UNSAFE_MEMBER_PATH


def test_perceptual_hashing_files_an_encrypted_member_as_encrypted(
    tmp_path: Path,
) -> None:
    with pytest.raises(PermanentJobError) as caught:
        calculate_perceptual_hashes(_encrypted(tmp_path / "enc.cbz"))

    assert caught.value.category == REASON_ENCRYPTED


def test_hashing_an_ordinary_archive_is_unaffected(tmp_path: Path) -> None:
    """The control for the four refusals per module above."""
    path = gc.build_case("ordinary", tmp_path)

    assert calculate_page_hashes(path).page_count == 3
    assert calculate_perceptual_hashes(path).page_count == 3


# --- the library-maintenance rewrite path -----------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda p: _declared_bomb(p),
        lambda p: _encrypted(p),
        lambda p: _build(p, ["001.png", "../escape.png"]),
    ],
    ids=["declared_bomb", "encrypted", "unsafe_name"],
)
def test_write_comicinfo_refuses_and_leaves_the_original_intact(
    tmp_path: Path, build
) -> None:
    """Refused before materializing: original byte-identical, no temp file."""
    path = build(tmp_path / "target.cbz")
    before = fi.file_snapshot([path])

    assert write_comicinfo(path, gc.COMIC_INFO, NEW_XML, dry_run=False) is False

    assert fi.file_snapshot([path]) == before
    assert not list(tmp_path.glob("*.tmp.cbz"))
    assert not list(tmp_path.glob("*.bak.cbz"))


def test_write_comicinfo_refuses_too_many_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive_limits, "MAX_ARCHIVE_ENTRIES", 2)
    path = _build(tmp_path / "target.cbz", ["1.png", "2.png"])
    before = fi.file_snapshot([path])

    assert write_comicinfo(path, gc.COMIC_INFO, NEW_XML, dry_run=False) is False
    assert fi.file_snapshot([path]) == before


def test_write_comicinfo_reads_each_duplicate_name_record_by_its_own_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate names must not all resolve to the last record.

    Three `page.bin` records of 0, 0 and 1,000 bytes fit a 1,000-byte
    ceiling. Reading by name returns the *last* record for every one of
    them, so the old rewrite materialized 3,000 bytes and wrote an archive
    that fails the same validator. Reading by `ZipInfo` keeps each record's
    own bytes. Duplicate names themselves are still accepted (a policy for
    them is out of scope); this pins only that each record is copied as is.
    """
    monkeypatch.setattr(
        archive_limits, "MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES", 1_000
    )
    path = tmp_path / "dupes.cbz"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # "Duplicate name"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            for size in (0, 0, 1_000):
                archive.writestr(
                    zipfile.ZipInfo("page.bin", gc.FIXED_DATE_TIME),
                    b"x" * size,
                )

    def record_payloads(archive_path: Path) -> list[tuple[str, bytes]]:
        with zipfile.ZipFile(archive_path) as archive:
            return [
                (info.filename, archive.read(info))
                for info in archive.infolist()
            ]

    original = record_payloads(path)
    assert [len(data) for _, data in original] == [0, 0, 1_000]

    with warnings.catch_warnings():
        # The rewrite re-emits the duplicates, and zipfile warns again.
        warnings.simplefilter("ignore", UserWarning)
        assert write_comicinfo(path, None, NEW_XML, dry_run=False) is True

    rewritten = record_payloads(path)
    assert rewritten[:3] == original
    assert rewritten[3] == ("ComicInfo.xml", NEW_XML.encode("utf-8"))
    validate_archive_members(
        _infos(path),
        max_total_uncompressed_bytes=1_000 + len(NEW_XML.encode("utf-8")),
    )


def test_write_comicinfo_still_rewrites_an_ordinary_archive(
    tmp_path: Path,
) -> None:
    path = gc.build_case("ordinary", tmp_path)

    assert write_comicinfo(path, gc.COMIC_INFO, NEW_XML, dry_run=False) is True
    assert gc.member_payloads(path)[gc.COMIC_INFO] == NEW_XML.encode("utf-8")
