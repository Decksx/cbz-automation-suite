"""Contract section 4 classification for the #214 oneshots planner, with no I/O."""

import pytest

from comic_automation.library.oneshot_classify import (
    NUMBERED_SINGLE, ONESHOT, REVIEW, SERIES, FolderFacts, classify_folder, number_evidence,
)


def classify(folder, files, taken=None, **facts):
    return classify_folder(FolderFacts(folder, tuple(files), **facts), set() if taken is None else taken)


def test_an_unnumbered_single_book_is_a_oneshot_keeping_its_name():
    result = classify("Some Title", ["Some Title.cbz"])
    assert (result.kind, result.target, result.renamed, result.reasons) == (
        ONESHOT, "_oneshots/Some Title.cbz", False, ())


@pytest.mark.parametrize("name", [
    "Title Vol 1", "Title Vol.01", "Title Volume 1", "Title v1", "Title v01",
    "Title Ch.1", "Title Chapter 01", "Title ch 1", "Title Chap 3",
    "Title #1", "Title # 01", "Title Issue 1",
    "Title Part 1", "Title Pt.1", "Title Episode 1", "Title Ep 01", "Title Act 1",
    "タイトル 第1話", "タイトル 第1巻", "タイトル 1巻", "Title Vol １",
])
def test_r1_any_explicit_number_including_one_stays_a_series(name):
    """R1 (David, 7023971642): a numbered book never goes to _oneshots, even number 1."""
    result = classify(name, [f"{name}.cbz"])
    assert result.kind == NUMBERED_SINGLE
    assert result.target is None
    assert result.number_evidence is not None


def test_r1_number_in_the_folder_name_alone_is_enough():
    result = classify("Title Vol 2", ["Title.cbz"])
    assert result.kind == NUMBERED_SINGLE and result.number_evidence.source == "folder"


@pytest.mark.parametrize("name", [
    "Departure", "Each Other", "Volcano", "Rich Kids", "Step Up", "Apt Living", "Fact Check",
    "Vortex", "Epic Tales", "Chopped",
])
def test_words_containing_a_token_are_not_numbers(name):
    assert classify(name, [f"{name}.cbz"]).kind == ONESHOT


@pytest.mark.parametrize("name", ["Script 3", "Kept 2", "Teach 4", "Deep 5", "Contract 6"])
def test_a_word_ending_in_a_token_is_not_that_token(name):
    """'Script 3' ends in 'pt 3' and 'Teach 4' in 'ch 4'; the word boundary keeps them
    from counting as Part/Chapter, so only the bare trailing number (review) remains."""
    result = classify(name, [f"{name}.cbz"])
    assert (result.kind, result.reasons) == (REVIEW, ("ambiguous_number",))


def test_full_width_letters_and_digits_still_count_as_numbered():
    result = classify("Ｔｉｔｌｅ Ｖｏｌ １", ["Ｔｉｔｌｅ Ｖｏｌ １.cbz"])
    assert result.kind == NUMBERED_SINGLE


@pytest.mark.parametrize("name", ["1984", "Mob Psycho 100", "Title 02", "Title - 3", "Title_7"])
def test_a_bare_trailing_number_is_ambiguous_and_never_moves(name):
    result = classify(name, [f"{name}.cbz"])
    assert (result.kind, result.reasons) == (REVIEW, ("ambiguous_number",))


def test_a_year_in_parentheses_is_not_a_number():
    assert classify("Title (2019)", ["Title (2019).cbz"]).kind == ONESHOT


def test_a_comicinfo_number_alone_is_review_not_r1():
    """Taggers often set Number=1 on true oneshots, so it alone can't decide."""
    result = classify("Title", ["Title.cbz"], comicinfo={"Number": "1"})
    assert (result.kind, result.reasons) == (REVIEW, ("ambiguous_number",))


def test_comicinfo_agreeing_with_the_name_stays_numbered():
    result = classify("Title Vol 2", ["Title Vol 2.cbz"], comicinfo={"Volume": "2"})
    assert result.kind == NUMBERED_SINGLE


def test_comicinfo_disagreeing_with_the_name_is_a_metadata_conflict():
    result = classify("Title Vol 2", ["Title Vol 2.cbz"], comicinfo={"Number": "5"})
    assert (result.kind, result.reasons) == (REVIEW, ("metadata_conflict",))


def test_a_non_numeric_comicinfo_number_is_ambiguous():
    result = classify("Title", ["Title.cbz"], comicinfo={"Number": "Special"})
    assert (result.kind, result.reasons) == (REVIEW, ("ambiguous_number",))


def test_empty_comicinfo_fields_do_not_count():
    assert classify("Title", ["Title.cbz"], comicinfo={"Number": " ", "Volume": ""}).kind == ONESHOT


@pytest.mark.parametrize("files,subdirs", [(["A.cbz", "B.cbz"], ()), (["A.cbz"], ("Extras",))])
def test_more_than_one_book_or_any_subfolder_is_a_series(files, subdirs):
    assert classify("Title", files, subdirectories=subdirs).kind == SERIES


@pytest.mark.parametrize("files", [["Title.cbz", "series.json"], ["Title.cbz", "cover.jpg"]])
def test_any_sidecar_keeps_the_folder_in_review(files):
    """F5: v1 moves no sidecar, series.json included."""
    result = classify("Title", files)
    assert (result.kind, result.reasons) == (REVIEW, ("extra_files",))


def test_a_folder_with_only_unsupported_files_is_review():
    assert classify("Title", ["Title.cb7"]).reasons == ("extra_files",)


def test_an_empty_folder_is_review():
    assert classify("Title", []).reasons == ("empty",)


@pytest.mark.parametrize("folder,facts,reason", [
    ("_oneshots", {}, "reserved_name"), ("_OneShots", {}, "reserved_name"),
    ("NUL", {}, "unsafe_path"), ("Title.", {}, "unsafe_path"),
    ("Title", {"reparse_point": True}, "reparse_point"),
    ("Title", {"unreadable": True}, "unreadable"),
    ("Title", {"comicinfo_unreadable": True}, "unreadable"),
])
def test_unsafe_or_unreadable_folders_never_move(folder, facts, reason):
    result = classify(folder, ["Title.cbz"], **facts)
    assert result.kind == REVIEW and reason in result.reasons


def test_an_unsafe_book_name_is_review():
    assert "unsafe_path" in classify("Title", ["AUX.cbz"]).reasons


@pytest.mark.parametrize("book,expected", [(".cbz", "_oneshots/Title.cbz"), (".cbr", "_oneshots/Title.cbr"),
                                           ("--.pdf", "_oneshots/Title.pdf")])
def test_a_non_descriptive_name_takes_the_series_title_and_keeps_its_suffix(book, expected):
    result = classify("Title", [book])
    assert (result.kind, result.target, result.renamed) == (ONESHOT, expected, True)


def test_a_name_collision_falls_back_to_the_title_then_to_review():
    taken: set[str] = set()
    first = classify("Alpha", ["Book.cbz"], taken)
    second = classify("Beta", ["BOOK.cbz"], taken)
    assert first.target == "_oneshots/Book.cbz"
    assert (second.target, second.renamed) == ("_oneshots/Beta.cbz", True)
    taken.add("gamma.cbz")
    third = classify("Gamma", ["Book.cbz"], taken)
    assert (third.kind, third.reasons) == (REVIEW, ("target_collision",))


def test_an_existing_oneshots_file_counts_as_taken():
    """A file already in _oneshots blocks both the book's name and the same title fallback."""
    result = classify("Title", ["Title.cbz"], {"title.cbz"})
    assert (result.kind, result.reasons, result.target) == (REVIEW, ("target_collision",), None)


def test_numbered_with_a_sidecar_is_review_not_numbered_single():
    result = classify("Title Vol 1", ["Title Vol 1.cbz", "series.json"])
    assert (result.kind, result.reasons) == (REVIEW, ("extra_files",))


def test_explicit_token_beats_a_bare_trailing_number():
    hit, unclear = number_evidence("Title Vol 3 2019", "file")
    assert hit is not None and hit.kind == "volume" and unclear is None
