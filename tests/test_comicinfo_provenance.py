"""Namespaced ComicInfo provenance, and the adult signal measured on it (#31).

`read_comic_info()` read a fixed list of plain element names, so the two
fields carrying the accepted adult evidence were invisible to the
classifier: the ComicInfo template writes `Categories` and `SourceMihon`
under a prefix bound to the XMLSchema namespace, and `root.find("Categories")`
returns None for a namespaced element.

Three things are proven here, in this order:

* the namespaced fields are extracted, and the unnamespaced ones are not
  disturbed -- checked through `plan_series`, the real consumer, so a
  regression cannot hide behind a helper that happens to return the right
  dict while the classification path changed;
* each of the three evidence classes classifies independently, against the
  actual `config/routing.v2.json` rather than a convenient local config;
* the two exclusions the measurement depends on hold -- `SourceMihon=komga`
  as circular provenance, and an accepted domain appearing anywhere in a URL
  other than its host.

The namespace fixtures are built from the watcher's own `COMICINFO_TEMPLATE`.
Hand-writing the XML would prove the parser handles the XML the test author
imagined; issue #31's fixture criterion exists because the template's actual
prefixes (`ty:` for Categories, `mh:` for SourceMihon) are not the `xsd:` its
root declares, and a hand-written approximation is exactly how that detail
would be missed.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts.cbz_library_reclassify import (
    COMICINFO_NAMESPACE,
    plan_series,
    read_comic_info,
)
from scripts.cbz_routing import build_context, load, parse, resolve
from scripts.cbz_watcher import COMICINFO_TEMPLATE

REPO_ROOT = Path(__file__).resolve().parents[1]
STAGED_CONFIG = REPO_ROOT / "config" / "routing.v2.json"
ADULT_RULE = "explicit adult content"


# ── fixtures built from the real template ────────────────────────


def _template_with(**values: str) -> str:
    """The watcher's ComicInfo template with *values* filled in.

    Substitutes into the template text rather than rebuilding the document,
    so the namespace declarations, prefixes and element order stay exactly
    as the watcher emits them. A test that regenerated the XML would be
    asserting against its own idea of the format.
    """
    xml = COMICINFO_TEMPLATE
    for name, value in values.items():
        # Both the empty `<X></X>` form and the pre-filled SourceMihon.
        for prefix in ("", "ty:", "mh:"):
            empty = f"<{prefix}{name}"
            if empty in xml:
                start = xml.index(empty)
                open_end = xml.index(">", start) + 1
                close = xml.index(f"</{prefix}{name}>", open_end)
                xml = xml[:open_end] + value + xml[close:]
                break
        else:                                   # pragma: no cover - test bug
            raise AssertionError(f"{name} is not in COMICINFO_TEMPLATE")
    return xml


def _cbz(path: Path, xml: str | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        if xml is not None:
            zf.writestr("ComicInfo.xml", xml)
        zf.writestr("001.jpg", b"page")
    return path


def test_the_template_still_namespaces_the_provenance_fields():
    """The premise every other test here rests on.

    If the watcher ever emits these unprefixed, the extraction below would
    silently stop finding them while still passing on its own fixtures --
    so the shape of the real template is asserted, not assumed.
    """
    assert "<ty:Categories" in COMICINFO_TEMPLATE
    assert "<mh:SourceMihon" in COMICINFO_TEMPLATE
    assert COMICINFO_TEMPLATE.count('"http://www.w3.org/2001/XMLSchema"') >= 3
    assert COMICINFO_NAMESPACE == "{http://www.w3.org/2001/XMLSchema}"


# ── extraction ───────────────────────────────────────────────────


def test_namespaced_fields_are_extracted_under_their_local_names(tmp_path):
    archive = _cbz(tmp_path / "a.cbz", _template_with(
        Categories="gooey, extra gooey", SourceMihon="hitomi"))
    info = read_comic_info(archive)
    assert info["Categories"] == "gooey, extra gooey"
    assert info["SourceMihon"] == "hitomi"
    # Keyed on the local name, so a config never has to spell the URI.
    assert not any(k.startswith("{") for k in info)


def test_an_xsd_prefixed_document_reads_identically(tmp_path):
    """Prefixes do not reach the parser; the namespace URI does.

    Not a substitute for the template fixture above -- it is the same
    assertion made against a document the watcher does not write, recording
    that archives from other tools are read on the URI and not on a prefix
    this repository happens to use.
    """
    xml = (
        '<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        "<Series>S</Series>"
        '<xsd:Categories>gooey</xsd:Categories>'
        '<xsd:SourceMihon>hitomi</xsd:SourceMihon>'
        "</ComicInfo>"
    )
    info = read_comic_info(_cbz(tmp_path / "a.cbz", xml))
    assert info["Categories"] == "gooey"
    assert info["SourceMihon"] == "hitomi"


def test_an_unnamespaced_categories_element_is_not_read(tmp_path):
    """Deliberate, and recorded here so it reads as a decision.

    The census measured the namespaced form only. Accepting a plain
    <Categories> would admit evidence outside the population the 97.87%
    coverage and 0.31% adjudicated false-positive rate were computed over.
    """
    xml = "<ComicInfo><Series>S</Series><Categories>gooey</Categories></ComicInfo>"
    assert "Categories" not in read_comic_info(_cbz(tmp_path / "a.cbz", xml))


def test_an_empty_namespaced_element_is_absent_not_empty(tmp_path):
    # The template ships both fields empty, so this is the common case for
    # an archive the watcher has touched but no scraper has filled in.
    info = read_comic_info(_cbz(tmp_path / "a.cbz", COMICINFO_TEMPLATE))
    assert "Categories" not in info
    assert info["SourceMihon"] == "Komga"       # the template's own default


def test_context_exposes_both_fields_to_a_routing_config(tmp_path):
    info = read_comic_info(_cbz(tmp_path / "a.cbz", _template_with(
        Categories="gooey", SourceMihon="hitomi")))
    context = build_context("src", "Some Series", info)
    # Field references are casefolded, so a config writes comicinfo.Categories.
    assert context["comicinfo.categories"] == "gooey"
    assert context["comicinfo.sourcemihon"] == "hitomi"


# ── the unnamespaced fields are undisturbed ──────────────────────


def _plan(tmp_path: Path, xml: str | None, cfg):
    """Plan one series from a single archive carrying *xml*.

    Goes through plan_series rather than read_comic_info so the assertion is
    about what the classifier decided, not about what a helper returned. A
    change that read the fields but stopped passing them into build_context
    would satisfy the helper and fail here.
    """
    series_dir = tmp_path / "src" / "Series"
    _cbz(series_dir / "ch01.cbz", xml)
    roots = {"comix": tmp_path / "comix", "manga": tmp_path / "manga",
             "graphic_novels": tmp_path / "gn"}
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    return plan_series(series_dir, cfg, "src", 5, roots)


def _staged_cfg(tmp_path: Path):
    """The real staged config, with destinations pointed at a tmpdir.

    The signal under test is the one in the repository, not a paraphrase of
    it -- that is the whole point of criterion 5. Only the destination paths
    are rewritten, so no test can touch the live library.
    """
    raw = json.loads(STAGED_CONFIG.read_text(encoding="utf-8"))
    raw["destinations"] = {
        "comix": str(tmp_path / "comix"),
        "manga": str(tmp_path / "manga"),
        "graphic_novels": str(tmp_path / "gn"),
    }
    return parse(raw)


@pytest.mark.parametrize("fields, expected_key", [
    ({"LanguageISO": "ja"}, "manga"),
    ({"Manga": "Yes"}, "manga"),
    ({"Publisher": "Shueisha"}, "manga"),
    ({"Genre": "seinen"}, "manga"),
    ({"Web": "https://mangadex.org/chapter/1"}, "manga"),
    ({"Series": "Nothing Here"}, "graphic_novels"),
])
def test_the_unnamespaced_fields_still_classify_as_before(
        tmp_path, fields, expected_key):
    """Regression for criterion 2, through the consumer.

    These are the pre-#31 signals, evaluated by the real staged config. If
    the namespaced extraction had disturbed the existing loop -- reordered
    it, overwritten a key, or made a missing element raise -- the origin
    rules would move and these would fail.
    """
    body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    row = _plan(tmp_path, f"<ComicInfo>{body}</ComicInfo>", _staged_cfg(tmp_path))
    assert row.dest_key == expected_key


def test_an_archive_without_comicinfo_still_plans(tmp_path):
    row = _plan(tmp_path, None, _staged_cfg(tmp_path))
    assert row.dest_key == "graphic_novels"
    assert row.with_comicinfo == 0


# ── the three evidence classes, independently ────────────────────
#
# Each accepted feature below is one the census actually accepted, and each
# test drives exactly one evidence class so a channel that stopped working
# cannot be covered for by another.


def test_an_accepted_domain_classifies_as_adult(tmp_path):
    row = _plan(tmp_path, _template_with(Web="https://hentainexus.com/view/1"),
                _staged_cfg(tmp_path))
    assert row.dest_key == "comix"
    assert row.reason_rule == ADULT_RULE


def test_an_accepted_source_mihon_value_classifies_as_adult(tmp_path):
    row = _plan(tmp_path, _template_with(SourceMihon="hitomi"),
                _staged_cfg(tmp_path))
    assert row.dest_key == "comix"
    assert row.reason_rule == ADULT_RULE


def test_an_accepted_category_token_classifies_as_adult(tmp_path):
    # Categories is comma-joined, so the accepted token has to be matched
    # within the list rather than as the whole field value.
    row = _plan(tmp_path, _template_with(Categories="Manga, gooey, Action"),
                _staged_cfg(tmp_path))
    assert row.dest_key == "comix"
    assert row.reason_rule == ADULT_RULE


def test_a_two_word_category_token_matches_whole(tmp_path):
    # "extra gooey" contains a space; splitting on anything but the comma
    # would break it into two tokens that match nothing.
    row = _plan(tmp_path, _template_with(Categories="extra gooey"),
                _staged_cfg(tmp_path))
    assert row.reason_rule == ADULT_RULE


# ── the two exclusions the measurement depends on ────────────────


def test_source_mihon_komga_is_not_adult_evidence(tmp_path):
    """Circular provenance: `komga` means "re-imported from the Komga library".

    Treating it as adult evidence would infer the classification from the
    current placement it is supposed to be deciding. The watcher's own
    template writes SourceMihon=Komga, so this is the value most archives
    that passed through this repository carry.
    """
    row = _plan(tmp_path, _template_with(SourceMihon="Komga"),
                _staged_cfg(tmp_path))
    assert row.dest_key != "comix"
    assert row.reason_rule != ADULT_RULE


def test_komga_is_absent_from_the_staged_provenance_list():
    # Guards the config itself, not just the evaluation: the exclusion is a
    # deliberate omission, and an omission is easy to undo by accident.
    cfg = load(STAGED_CONFIG)
    assert "komga" not in {v.casefold() for v in cfg.lists["adult_provenance"]}


@pytest.mark.parametrize("url", [
    "https://example.com/?ref=hentainexus.com",
    "https://example.com/hentainexus.com/1",
    "https://hentainexus.com.example.test/1",
])
def test_an_accepted_domain_outside_the_host_is_not_adult_evidence(
        tmp_path, url):
    """A substring match here would classify another site's series as adult.

    Asserted on the rule rather than the destination: these URLs may still
    reach some destination through an unrelated origin rule, and the claim
    under test is that the adult rule did not fire.
    """
    row = _plan(tmp_path, _template_with(Web=url), _staged_cfg(tmp_path))
    assert row.reason_rule != ADULT_RULE


# ── the staged config still activates nothing ────────────────────


def test_the_staged_signal_carries_all_125_accepted_features():
    """Criterion: the encoded signal is the measured artifact, not a subset.

    17,096/17,468 coverage and both false-positive rates were computed
    against all 125 accepted features. A later edit trimming the list to the
    67 that contribute new series would leave those recorded figures
    describing a signal the file no longer contains.
    """
    cfg = load(STAGED_CONFIG)
    assert len(cfg.lists["adult_domains"]) == 59
    assert len(cfg.lists["adult_provenance"]) == 60
    assert len(cfg.lists["adult_categories"]) == 6


def test_both_false_positive_rates_travel_with_the_signal():
    """The rates are not interchangeable, so neither may stand alone.

    0.46% is the pre-adjudication figure and 0.31% is the rate the signal
    was accepted at. Recording only one -- either one -- is how a number
    outlives the qualifier that makes it meaningful.
    """
    raw = json.loads(STAGED_CONFIG.read_text(encoding="utf-8"))
    note = raw["signals"]["_comment_adult_signal"]
    assert "0.46%" in note and "0.31%" in note
    assert "ERIKA" in note
    assert "97.87%" in note


def test_reading_the_provenance_fields_did_not_activate_v2():
    """#31 makes evidence visible. It does not switch anything on."""
    from scripts.cbz_watcher_router import VALID_MODES

    assert "active" not in VALID_MODES
    assert VALID_MODES == frozenset({"off", "shadow"})
