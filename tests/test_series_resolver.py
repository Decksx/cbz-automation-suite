"""Deterministic coverage for canonical series importing."""

from __future__ import annotations

import json
from pathlib import Path

from comic_automation.series_resolver import (
    ALIAS_SEED_NAME,
    REGISTRY_NAME,
    SeriesResolver,
    alias_key,
    build_registry,
    normalize_apostrophe_names,
    normalize_apostrophes,
    resolve_from_registry,
    safe_name,
    settle_file_conflict,
    write_registry,
)


def test_alias_key_ignores_case_accents_hyphens_and_underscores() -> None:
    assert alias_key("Martíal_Evolution: Start-by...") == alias_key(
        "martial evolution start by"
    )


def test_apostrophe_variants_normalize_to_ascii_for_new_names() -> None:
    variants = ("Debra's Story", "Debra’s Story", "Debraʼs Story", "Debra＇s Story")
    assert {normalize_apostrophes(value) for value in variants} == {"Debra's Story"}
    assert {safe_name(value) for value in variants} == {"Debra's Story"}


def test_apostrophe_repair_merges_directories_and_keeps_larger_file(
    tmp_path: Path,
) -> None:
    library = tmp_path / "Comix"
    straight = library / "Debra's Story"
    curly = library / "Debra’s Story"
    straight.mkdir(parents=True)
    curly.mkdir(parents=True)
    (straight / "Debra's Story.cbz").write_bytes(b"small")
    (curly / "Debra’s Story.cbz").write_bytes(b"larger archive")
    (straight / "Same.cbz").write_bytes(b"same")
    (curly / "Same.cbz").write_bytes(b"same")
    quarantine = tmp_path / "quarantine" / "Comix"

    preview = normalize_apostrophe_names(library, quarantine, apply=False)
    assert preview["affected_count"] == 2
    assert straight.is_dir() and curly.is_dir()

    result = normalize_apostrophe_names(library, quarantine, apply=True)

    assert result["changed"] is True
    assert straight.is_dir()
    assert not curly.exists()
    assert (straight / "Debra's Story.cbz").read_bytes() == b"larger archive"
    assert (straight / "Same.cbz").read_bytes() == b"same"
    quarantined = list((quarantine / "_apostrophe_normalization").rglob("*.cbz"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"small"
    assert any(action["action"] == "deleted_exact_duplicate" for action in result["actions"])


def test_live_resolver_can_repair_apostrophes_before_building_registry(
    tmp_path: Path,
) -> None:
    library = tmp_path / "Comix"
    (library / "Debra's Story").mkdir(parents=True)
    (library / "Debra’s Story").mkdir()

    resolver = SeriesResolver(library, repair_apostrophes=True)

    assert (library / "Debra's Story").is_dir()
    assert not (library / "Debra’s Story").exists()
    assert resolver.resolve("Debra’s Story")["relative_path"] == "Debra's Story"
    assert resolver.registry["ambiguous_aliases"] == {}


def test_registry_keeps_seed_aliases_after_canonical_apostrophe_changes(
    tmp_path: Path,
) -> None:
    library = tmp_path / "Comix"
    (library / "Debra's Story").mkdir(parents=True)
    (library / ALIAS_SEED_NAME).write_text(
        json.dumps({"Debra’s Story": ["Debra Story Alternate"]}),
        encoding="utf-8",
    )

    registry = build_registry(library)

    result = resolve_from_registry(registry, "Debra Story Alternate")
    assert result["matched"] is True
    assert result["relative_path"] == "Debra's Story"


def test_registry_learns_translated_and_romanized_metadata_aliases(
    tmp_path: Path,
) -> None:
    library = tmp_path / "Manga"
    canonical = library / "The Apothecary Diaries"
    canonical.mkdir(parents=True)
    (canonical / "series.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "name": "The Apothecary Diaries",
                    "alternate_titles": [
                        "Kusuriya no Hitorigoto",
                        "薬屋のひとりごと",
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    registry = build_registry(library)

    romaji = resolve_from_registry(registry, "Kusuriya-no-Hitorigoto")
    japanese = resolve_from_registry(registry, "薬屋のひとりごと")
    assert romaji["relative_path"] == "The Apothecary Diaries"
    assert japanese["relative_path"] == "The Apothecary Diaries"


def test_fuzzy_resolution_requires_95_percent_and_rejects_close_competitors() -> None:
    registry = {
        "aliases_by_normalized_key": {
            alias_key("Martial Evolution Start by Awakening Monsters"): "One",
            alias_key("Martial Evolution Start by Awakening Monsterz"): "Two",
        },
        "ambiguous_aliases": {},
    }

    result = resolve_from_registry(
        registry, "Martial Evolution Start by Awakening Monsterx", threshold=0.95
    )

    assert result["matched"] is False
    assert result["match_type"] == "ambiguous_fuzzy"


def test_live_resolver_builds_then_persists_new_aliases(tmp_path: Path) -> None:
    library = tmp_path / "Comix"
    (library / "Primary English Title").mkdir(parents=True)
    resolver = SeriesResolver(library)

    resolver.register(
        "Primary English Title",
        {"Romaji Puraimari Taitoru", "Primary_English-Title"},
    )

    assert (library / REGISTRY_NAME).is_file()
    assert (library / ALIAS_SEED_NAME).is_file()
    reloaded = SeriesResolver(library)
    result = reloaded.resolve("Romaji-Puraimari_Taitoru")
    assert result["matched"] is True
    assert result["relative_path"] == "Primary English Title"


def test_exact_duplicate_is_removed_without_quarantine(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming.cbz"
    existing = tmp_path / "library.cbz"
    quarantine = tmp_path / "quarantine" / "incoming.cbz"
    incoming.write_bytes(b"same bytes")
    existing.write_bytes(b"same bytes")

    outcome = settle_file_conflict(incoming, existing, quarantine)

    assert outcome["action"] == "deleted_exact_duplicate"
    assert not incoming.exists()
    assert existing.read_bytes() == b"same bytes"
    assert not quarantine.exists()


def test_larger_file_wins_and_smaller_file_is_quarantined(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming.cbz"
    existing = tmp_path / "library.cbz"
    quarantine = tmp_path / "quarantine" / "book.cbz"
    incoming.write_bytes(b"larger incoming archive")
    existing.write_bytes(b"small")

    outcome = settle_file_conflict(incoming, existing, quarantine)

    assert outcome["action"] == "kept_larger_incoming"
    assert existing.read_bytes() == b"larger incoming archive"
    assert quarantine.read_bytes() == b"small"


def test_registry_write_is_loadable_json(tmp_path: Path) -> None:
    library = tmp_path / "Manga"
    library.mkdir()
    registry = {
        "series": [],
        "aliases_by_normalized_key": {},
        "ambiguous_aliases": {},
    }

    path = write_registry(library, registry)

    assert json.loads(path.read_text(encoding="utf-8")) == registry


def test_atomic_registry_temp_name_avoids_samba_appledouble_veto(
    tmp_path: Path, monkeypatch
) -> None:
    """TOWER refuses ``._*`` names, so atomic writes must not use that prefix."""
    from comic_automation import series_resolver

    real_mkstemp = series_resolver.tempfile.mkstemp
    observed: list[str] = []

    def recording_mkstemp(*, prefix: str, suffix: str, dir: Path):
        observed.append(prefix)
        return real_mkstemp(prefix=prefix, suffix=suffix, dir=dir)

    monkeypatch.setattr(series_resolver.tempfile, "mkstemp", recording_mkstemp)
    library = tmp_path / "Manga"
    library.mkdir()

    series_resolver.write_registry(library, {"version": 1})

    assert observed == ["_series_registry.json."]
    assert not observed[0].startswith("._")
