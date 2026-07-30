from comic_automation.archive.handlers import (
    InspectArchiveHandler,
    InvalidInspectionJobError,
)
from comic_automation.archive.hashing import (
    ArchiveHash,
    ArchiveHashRepository,
    CalculateArchiveHashHandler,
    calculate_archive_hash,
)
from comic_automation.archive.inspection import (
    ArchiveInspection,
    ArchiveInspectionError,
    CorruptArchiveError,
    ComicInfoMetadata,
    UnsupportedArchiveFormatError,
    UnsafeComicInfoError,
    inspect_archive,
    inspect_cbz,
)
from comic_automation.archive.page_hashing import (
    ArchivePageHashes,
    ArchivePageHashRepository,
    HashArchivePagesHandler,
    PageContentHash,
    calculate_page_hashes,
)
from comic_automation.archive.perceptual_hashing import (
    ArchivePerceptualHashes,
    ArchivePerceptualHashRepository,
    HashArchivePagesPerceptualHandler,
    PagePerceptualHash,
    calculate_perceptual_hashes,
    difference_hash,
    perceptual_hash,
)
from comic_automation.archive.perceptual_reuse_analysis import (
    analyze_reuse_opportunity,
    connect_read_only,
)
from comic_automation.archive.quarantine import (
    QuarantineCandidate,
    QuarantineItemResult,
    QuarantineRepository,
    UnsupportedQuarantineCategoryError,
    execute_quarantine,
    propose_quarantine_filename,
)
from comic_automation.archive.repository import (
    ArchiveInspectionRepository,
    ArchiveLocationNotFoundError,
)

__all__ = [
    "ArchiveInspection",
    "ArchiveInspectionError",
    "ArchiveInspectionRepository",
    "CorruptArchiveError",
    "ArchiveLocationNotFoundError",
    "ArchivePageHashes",
    "ArchivePageHashRepository",
    "ArchivePerceptualHashes",
    "ArchivePerceptualHashRepository",
    "ArchiveHash",
    "ArchiveHashRepository",
    "CalculateArchiveHashHandler",
    "ComicInfoMetadata",
    "InspectArchiveHandler",
    "InvalidInspectionJobError",
    "HashArchivePagesHandler",
    "HashArchivePagesPerceptualHandler",
    "PageContentHash",
    "PagePerceptualHash",
    "QuarantineCandidate",
    "QuarantineItemResult",
    "QuarantineRepository",
    "UnsupportedQuarantineCategoryError",
    "UnsupportedArchiveFormatError",
    "UnsafeComicInfoError",
    "inspect_archive",
    "inspect_cbz",
    "calculate_archive_hash",
    "calculate_page_hashes",
    "calculate_perceptual_hashes",
    "analyze_reuse_opportunity",
    "connect_read_only",
    "difference_hash",
    "perceptual_hash",
    "execute_quarantine",
    "propose_quarantine_filename",
]
