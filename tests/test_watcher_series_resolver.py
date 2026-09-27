"""Watcher integration checks for canonical series routing and quarantine."""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

from comic_automation.series_resolver import SeriesResolver, alias_key
from scripts import cbz_watcher as watcher


def _resolver(tmp_path: Path) -> SeriesResolver:
    library = tmp_path / "Manga"
    (library / "Martial Evolution Start by Awakening the King of Monsters").mkdir(
        parents=True
    )
    return SeriesResolver(
        library,
        quarantine_root=tmp_path / "quarantine" / "Manga",
    )


def test_import_target_uses_existing_z_series_for_cosmetic_variants(
    tmp_path: Path, monkeypatch
) -> None:
    resolver = _resolver(tmp_path)
    monkeypatch.setattr(watcher, "_series_resolvers", {"manga": resolver})

    name, destination, result, selected = watcher._resolve_import_target(
        "manga",
        "martial_evolution-start by awakening the king of monsters",
        str(tmp_path / "staging"),
    )

    assert name == "Martial Evolution Start by Awakening the King of Monsters"
    assert Path(destination) == resolver.root
    assert result["match_type"] == "exact_alias"
    assert selected is resolver


def test_ambiguous_import_target_stays_in_legacy_staging(
    tmp_path: Path, monkeypatch
) -> None:
    resolver = _resolver(tmp_path)
    title = "Martial Evolution Alternate"
    (resolver.root / "Series A").mkdir()
    (resolver.root / "Series B").mkdir()
    key = alias_key(title)
    resolver.registry["ambiguous_aliases"] = {key: ["Series A", "Series B"]}
    resolver.registry["aliases_by_normalized_key"].pop(key, None)
    monkeypatch.setattr(watcher, "_series_resolvers", {"manga": resolver})
    staging = tmp_path / "staging" / "Manga"

    name, destination, result, _ = watcher._resolve_import_target(
        "manga", title, str(staging)
    )

    assert name == title
    assert Path(destination) == staging
    assert result["match_type"] == "ambiguous_exact"


def test_existing_series_in_other_library_overrides_source_category(
    tmp_path: Path, monkeypatch
) -> None:
    comix_root = tmp_path / "Comix"
    manga_root = tmp_path / "Manga"
    (comix_root / "Mixed Source Series").mkdir(parents=True)
    manga_root.mkdir()
    comix = SeriesResolver(comix_root)
    manga = SeriesResolver(manga_root)
    monkeypatch.setattr(
        watcher, "_series_resolvers", {"comix": comix, "manga": manga}
    )

    name, destination, result, selected = watcher._resolve_import_target(
        "manga", "Mixed_Source-Series", str(tmp_path / "staging" / "Manga")
    )

    assert name == "Mixed Source Series"
    assert Path(destination) == comix.root
    assert result["match_type"] == "exact_alias"
    assert result["source_category"] == "manga"
    assert result["selected_category"] == "comix"
    assert selected is comix


def test_same_exact_title_in_both_libraries_goes_to_review_staging(
    tmp_path: Path, monkeypatch
) -> None:
    comix_root = tmp_path / "Comix"
    manga_root = tmp_path / "Manga"
    (comix_root / "Shared Title").mkdir(parents=True)
    (manga_root / "Shared Title").mkdir(parents=True)
    comix = SeriesResolver(comix_root)
    manga = SeriesResolver(manga_root)
    monkeypatch.setattr(
        watcher, "_series_resolvers", {"comix": comix, "manga": manga}
    )
    staging = tmp_path / "staging" / "Manga"

    name, destination, result, selected = watcher._resolve_import_target(
        "manga", "Shared Title", str(staging)
    )

    assert name == "Shared Title"
    assert Path(destination) == staging
    assert result["match_type"] == "ambiguous_cross_library"
    assert {item["category"] for item in result["candidates"]} == {
        "comix",
        "manga",
    }
    assert selected is None


def test_manual_library_move_is_followed_before_registry_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    comix_root = tmp_path / "Comix"
    manga_root = tmp_path / "Manga"
    moved_name = "Corrected Library Series"
    (manga_root / moved_name).mkdir(parents=True)
    comix_root.mkdir()
    manga = SeriesResolver(manga_root)
    comix = SeriesResolver(comix_root)
    (manga_root / moved_name).rename(comix_root / moved_name)
    monkeypatch.setattr(
        watcher, "_series_resolvers", {"comix": comix, "manga": manga}
    )

    name, destination, result, selected = watcher._resolve_import_target(
        "manga", moved_name, str(tmp_path / "staging" / "Manga")
    )

    assert name == moved_name
    assert Path(destination) == comix.root
    assert result["match_type"] == "relocated_series"
    assert result["evidence_category"] == "manga"
    assert result["selected_category"] == "comix"
    assert selected is comix


def test_new_series_uses_source_category_as_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    comix_root = tmp_path / "Comix"
    manga_root = tmp_path / "Manga"
    comix_root.mkdir()
    manga_root.mkdir()
    comix = SeriesResolver(comix_root)
    manga = SeriesResolver(manga_root)
    monkeypatch.setattr(
        watcher, "_series_resolvers", {"comix": comix, "manga": manga}
    )

    name, destination, result, selected = watcher._resolve_import_target(
        "manga", "Brand New Series", str(tmp_path / "staging" / "Manga")
    )

    assert name == "Brand New Series"
    assert Path(destination) == manga.root
    assert result["match_type"] == "none"
    assert result["selected_category"] == "manga"
    assert selected is manga


def test_watcher_merge_quarantines_smaller_nonduplicate(tmp_path: Path) -> None:
    source = tmp_path / "incoming"
    destination = tmp_path / "Manga" / "Series"
    quarantine = tmp_path / "quarantine" / "Manga"
    source.mkdir()
    destination.mkdir(parents=True)
    (source / "chapter.cbz").write_bytes(b"small")
    (destination / "chapter.cbz").write_bytes(b"larger existing archive")

    watcher._merge_directories(
        source, destination, conflict_quarantine_root=quarantine
    )

    assert (destination / "chapter.cbz").read_bytes() == b"larger existing archive"
    assert (quarantine / "Series" / "chapter.cbz").read_bytes() == b"small"
    assert not (source / "chapter.cbz").exists()


def test_consistent_translated_comicinfo_series_is_available_to_resolver(
    tmp_path: Path,
) -> None:
    archives = []
    for number in (1, 2):
        path = tmp_path / f"book-{number}.cbz"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "ComicInfo.xml",
                "<ComicInfo><Series>The Apothecary Diaries</Series></ComicInfo>",
            )
        archives.append(path)

    assert watcher._consistent_comicinfo_series(archives) == "The Apothecary Diaries"


def test_live_registry_aliases_include_original_and_translated_titles() -> None:
    parsed = SimpleNamespace(
        series="Kusuriya no Hitorigoto",
        original_title="薬屋のひとりごと",
        translated_title="The Apothecary Diaries",
    )

    aliases = watcher._observed_series_aliases(
        "Kusuriya_no_Hitorigoto",
        "The Apothecary Diaries",
        "The Apothecary Diaries",
        [parsed],
    )

    assert aliases == {
        "Kusuriya_no_Hitorigoto",
        "Kusuriya no Hitorigoto",
        "薬屋のひとりごと",
        "The Apothecary Diaries",
    }


def test_watcher_cli_defaults_to_z_but_requires_explicit_enable() -> None:
    args = watcher.build_argument_parser().parse_args([])

    assert args.series_resolver is False
    assert args.resolver_comix_root == Path(r"Z:\comics\Comix")
    assert args.resolver_manga_root == Path(r"Z:\comics\Manga")
    assert args.resolver_quarantine_root == Path(
        r"Z:\comics\_resolver_quarantine"
    )
    assert args.resolver_threshold == 0.95
    assert args.resolver_normalize_apostrophes is False
