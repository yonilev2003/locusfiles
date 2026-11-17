"""R1: Downloads is a closed folder until the user opens it on purpose.

A parent root (`~`, `--roots`, a `roots` list in policy.toml) never covers it; `locus index ~/Downloads --yes` (or
answering the question) opens it by appending one marked `[[folder]]` block to policy.toml. An index made before
this existed is carried over by a one-time migration. Everything runs over a fake home in tmp_path, with
HashEmbedder: offline, no paid API.
"""

from __future__ import annotations

import os
import random
import stat
import unicodedata
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import cli, closed_folders  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings, get_settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder, iter_files  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402
from locus.policy import (  # noqa: E402
    Matcher,
    Policy,
    PolicyError,
    _by_first,
    _contains,
    _contains_any,
    _fold,
    load_policy,
    matcher_for_folder,
    parse_policy,
)

QUIET = lambda _msg: None  # noqa: E731

FILES = {
    "Downloads/lecture1.md": "lecture one covers eigenvalues and zebras",
    "Downloads/notes.md": "reading notes about graph theory",
    "Downloads/bank-statement.md": "statement for the month of october",
    "Documents/plan.md": "the launch plan",
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake home with the FILES above, the data folder beside it, the CLI wired to both (HashEmbedder, offline)."""
    home = (tmp_path / "home").resolve()
    for rel, text in FILES.items():
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    monkeypatch.setenv("LOCUS_COLLECTION", "closed")
    get_settings.cache_clear()
    return home, get_settings()


def run(capsys, *argv):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(list(argv))
    captured = capsys.readouterr()
    return stop.value.code, captured.out, captured.err


def policy_text(settings: Settings) -> str:
    f = settings.data_dir / "policy.toml"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def store_of(settings: Settings) -> ChromaStore:
    return ChromaStore(settings.data_dir, settings.collection, HashEmbedder().name)


def names(settings: Settings) -> set[str]:
    return {Path(p).name for p in store_of(settings).list_paths()}


# --- blocked ------------------------------------------------------------------------------------------------------


def test_downloads_is_closed_by_default_and_documents_is_not(world):
    home, _ = world
    m = matcher_for_folder(home, Policy(), home)
    for name in ("lecture1.md", "notes.md", "bank-statement.md"):
        verdict = m.decide(home / "Downloads" / name)
        assert (verdict.allowed, verdict.reason) == (False, "closed_folder")
    assert m.decide(home / "Documents" / "plan.md").allowed
    assert m.decide(home / "Downloads", is_dir=True).reason == "closed_folder"  # the walker prunes the folder itself


def test_a_walk_over_home_indexes_no_downloads_file(world):
    home, settings = world
    embedder, store = HashEmbedder(), store_of(settings)
    stats = index_folder(home, embedder, store, settings, log=QUIET, matcher=matcher_for_folder(home, Policy(), home))
    assert names(settings) == {"plan.md"}
    assert stats.denied == 1  # the folder, counted once, not its files


def test_the_run_summary_says_how_many_files_are_closed_and_how_to_open_them(world, capsys):
    home, _ = world
    code, out, _ = run(capsys, "index", str(home), "--yes")
    assert code == 0
    assert "3 file(s) in ~/Downloads are closed: run `locus index ~/Downloads` to open (it asks first)." in out
    assert "lecture1" not in out and "bank-statement" not in out  # counts only, no file names


def test_the_dry_run_names_the_opening_command_and_a_count_but_no_file(world, capsys):
    home, settings = world
    code, out, _ = run(capsys, "index", str(home), "--dry-run")
    assert code == 0
    assert "3 file(s) in ~/Downloads are closed: run `locus index ~/Downloads` to open" in out
    assert "lecture1" not in out and "bank-statement" not in out
    assert not (settings.data_dir / "policy.toml").exists()  # a preview opens nothing


def test_the_roots_dry_run_says_the_same(world, capsys):
    home, _ = world
    code, out, _ = run(capsys, "index", "--roots", str(home), "--dry-run")
    assert code == 0 and "denied_closed_folder" in out
    assert "3 file(s) in ~/Downloads are closed" in out


@pytest.mark.parametrize("text", ['[[folder]]\npath = "~"\nprofile = "untrusted"\n', 'roots = ["~"]\n'])
def test_a_parent_entry_or_a_roots_list_does_not_open_it(world, text):
    home, _ = world
    m = matcher_for_folder(home, parse_policy(text), home)
    assert m.decide(home / "Downloads" / "notes.md").reason == "closed_folder"
    assert m.decide(home / "Documents" / "plan.md").allowed


def test_roots_listed_in_the_policy_file_index_without_downloads(world, capsys):
    home, settings = world
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "policy.toml").write_text(f'roots = ["{home}"]\n', encoding="utf-8")
    code, _, _ = run(capsys, "index", "--yes", "--all")
    assert code == 0 and names(settings) == {"plan.md"}


def test_an_excluded_entry_does_not_open_it_and_keeps_its_own_reason(world):
    home, _ = world
    m = matcher_for_folder(home, parse_policy('[[folder]]\npath = "~/Downloads"\nprofile = "excluded"\n'), home)
    assert m.decide(home / "Downloads" / "notes.md").reason == "profile_excluded"


def test_case_and_unicode_spellings_of_the_folder_name_are_closed_too(world):
    home, _ = world
    m = matcher_for_folder(home, Policy(), home)
    assert m.decide(home / "DOWNLOADS" / "a.md").reason == "closed_folder"
    assert m.decide(home / "downloads" / "sub" / "a.md").reason == "closed_folder"
    decomposed = Path(unicodedata.normalize("NFD", str(home / "Downloads" / "résumé.md")))
    assert m.decide(decomposed).reason == "closed_folder"


# --- allowed: the explicit opt-in ---------------------------------------------------------------------------------


def test_index_downloads_with_yes_opens_it_and_indexes_all_three_files(world, capsys):
    home, settings = world
    code, out, err = run(capsys, "index", str(home / "Downloads"), "--yes")
    assert code == 0
    assert names(settings) == {"lecture1.md", "notes.md", "bank-statement.md"}
    text = policy_text(settings)
    assert '[[folder]]\npath = "~/Downloads"\nprofile = "untrusted"' in text
    assert closed_folders.BLOCK_BEGIN in text and closed_folders.BLOCK_END in text
    assert "is a closed folder" in err and "Confirmed with --yes." in err
    assert "3 file(s)" in err  # the question said how many files it would index


def test_once_opened_a_walk_over_home_includes_it(world, capsys):
    home, settings = world
    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0
    assert run(capsys, "index", str(home), "--prune", "--yes")[0] == 0
    assert names(settings) == {"lecture1.md", "notes.md", "bank-statement.md", "plan.md"}


def test_answering_yes_at_the_prompt_opens_it(world, capsys, monkeypatch):
    home, settings = world

    class Terminal:
        def isatty(self) -> bool:
            return True

        def readline(self) -> str:
            return "y\n"

    monkeypatch.setattr("sys.stdin", Terminal())
    code, _, err = run(capsys, "index", str(home / "Downloads"))
    assert code == 0 and "Open ~/Downloads? [y/N]" in err
    assert names(settings) == {"lecture1.md", "notes.md", "bank-statement.md"}


def test_answering_no_opens_nothing(world, capsys, monkeypatch):
    home, settings = world

    class Terminal:
        def isatty(self) -> bool:
            return True

        def readline(self) -> str:
            return "n\n"

    monkeypatch.setattr("sys.stdin", Terminal())
    code, _, err = run(capsys, "index", str(home / "Downloads"))
    assert code == cli.EXIT_DECLINED and "Nothing was opened or indexed." in err
    assert not (settings.data_dir / "chroma").exists() and policy_text(settings) == ""


def test_a_sub_folder_entry_opens_only_that_sub_folder(world, capsys):
    home, settings = world
    (home / "Downloads" / "uni").mkdir()
    (home / "Downloads" / "uni" / "week1.md").write_text("week one", encoding="utf-8")
    assert run(capsys, "index", str(home / "Downloads" / "uni"), "--yes")[0] == 0
    assert 'path = "~/Downloads/uni"' in policy_text(settings)
    m = matcher_for_folder(home, load_policy(settings.data_dir / "policy.toml"), home)
    assert m.decide(home / "Downloads" / "uni" / "week1.md").allowed
    assert m.decide(home / "Downloads" / "notes.md").reason == "closed_folder"
    assert m.decide(home / "Downloads" / "uni", is_dir=True).allowed
    assert m.decide(home / "Downloads", is_dir=True).allowed  # walked through on the way; its own files stay closed
    assert m.decide(home / "Downloads" / "other", is_dir=True).reason == "closed_folder"
    assert run(capsys, "index", str(home), "--yes")[0] == 0
    assert names(settings) == {"week1.md", "plan.md"}


def test_naming_downloads_still_asks_when_only_a_sub_folder_is_open(world, capsys):
    home, settings = world
    (home / "Downloads" / "uni").mkdir()
    (home / "Downloads" / "uni" / "week1.md").write_text("week one", encoding="utf-8")
    assert run(capsys, "index", str(home / "Downloads" / "uni"), "--yes")[0] == 0
    code, _, err = run(capsys, "index", str(home / "Downloads"))
    assert code == cli.EXIT_NEEDS_YES and "is a closed folder" in err


def test_without_yes_and_without_a_terminal_nothing_is_opened_and_the_message_names_the_command(world, capsys):
    home, settings = world
    code, out, err = run(capsys, "index", str(home / "Downloads"))
    assert code == cli.EXIT_NEEDS_YES
    assert f"`locus index {home / 'Downloads'} --yes`" in err
    assert not (settings.data_dir / "chroma").exists() and policy_text(settings) == ""


def test_watch_asks_the_same_question(world, capsys):
    home, settings = world
    code, _, err = run(capsys, "watch", str(home / "Downloads"))
    assert code == cli.EXIT_NEEDS_YES and f"locus watch {home / 'Downloads'} --yes" in err


def test_roots_named_on_the_command_line_are_asked_about_too(world, capsys):
    home, settings = world
    code, _, err = run(capsys, "index", "--roots", str(home / "Downloads"), "--yes", "--all")
    assert code == 0 and names(settings) == {"lecture1.md", "notes.md", "bank-statement.md"}
    assert 'path = "~/Downloads"' in policy_text(settings)


def test_a_symlink_to_downloads_is_still_downloads(world, capsys, tmp_path):
    home, settings = world
    link = home / "Documents" / "to-downloads"
    link.symlink_to(home / "Downloads", target_is_directory=True)
    code, _, err = run(capsys, "index", str(link))  # naming the link names the folder it leads to
    assert code == cli.EXIT_NEEDS_YES and "is a closed folder" in err
    assert run(capsys, "index", str(home / "Documents"), "--yes")[0] == 0  # a walk does not follow it
    assert names(settings) == {"plan.md"}


def test_a_downloads_folder_that_is_itself_a_link_is_closed_and_opens_through_its_name(world, capsys, tmp_path):
    home, settings = world
    moved = tmp_path / "elsewhere" / "Downloads"
    moved.parent.mkdir()
    (home / "Downloads").rename(moved)
    (home / "Downloads").symlink_to(moved, target_is_directory=True)
    m = matcher_for_folder(moved, Policy(), home)
    assert m.decide(moved / "notes.md").reason == "closed_folder"  # where the link leads is closed too
    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0
    assert names(settings) == {"lecture1.md", "notes.md", "bank-statement.md"}
    reopened = matcher_for_folder(moved, load_policy(settings.data_dir / "policy.toml"), home)
    assert reopened.decide(moved / "notes.md").allowed


def test_a_broken_policy_file_still_stops_a_plain_index(world, capsys):
    home, settings = world
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "policy.toml").write_text('[[folder]]\npath = "~/Downloads"\nprofil = "untrusted"\n')
    code, _, err = run(capsys, "index", str(home / "Documents"), "--yes")
    assert code == cli.EXIT_NEEDS_YES and "policy file problem" in err
    assert not (settings.data_dir / "chroma").exists()


# --- the policy file: one marked block, atomic, backed up ---------------------------------------------------------


def test_the_block_is_appended_once_keeps_the_old_text_and_a_backup(world, capsys):
    home, settings = world
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    original = '# my rules\n[deny]\npaths = ["~/Documents/taxes"]\n'
    (settings.data_dir / "policy.toml").write_text(original, encoding="utf-8")
    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0
    text = policy_text(settings)
    assert text.startswith(original) and text.count(closed_folders.BLOCK_BEGIN) == 1
    assert (settings.data_dir / "policy.toml.bak").read_text(encoding="utf-8") == original
    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0  # already open: nothing appended
    assert policy_text(settings).count(closed_folders.BLOCK_BEGIN) == 1
    assert parse_policy(policy_text(settings)).folders[0].path == "~/Downloads"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_policy_file_and_its_backup_are_owner_only(world, capsys):
    home, settings = world
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "policy.toml").write_text("version = 1\n", encoding="utf-8")
    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0
    for name in ("policy.toml", "policy.toml.bak"):
        assert stat.S_IMODE((settings.data_dir / name).stat().st_mode) == 0o600
    assert [p.name for p in settings.data_dir.iterdir() if p.name.endswith(".tmp")] == []  # no temp file left behind


def test_a_policy_that_cannot_be_read_back_is_never_written(tmp_path):
    f = tmp_path / "policy.toml"
    f.write_text("version = 1\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        closed_folders.write_policy_text(f, "this is [not toml")
    assert f.read_text(encoding="utf-8") == "version = 1\n"


def test_the_matcher_speed_up_gives_the_same_answers_as_the_plain_loop():
    rng = random.Random(7)
    seqs = tuple(
        tuple(_fold(x) for x in s)
        for s in ((".ssh",), (".config", "gcloud"), ("library", "keychains"), ("a", "b", "c"), ("snap", "firefox"))
    )
    index = _by_first(seqs)
    pool = [".ssh", "Config", ".config", "gcloud", "Library", "keychains", "a", "b", "c", "snap", "firefox", "x"]
    for _ in range(3000):
        parts = tuple(_fold(rng.choice(pool)) for _ in range(rng.randint(1, 8)))
        assert _contains_any(parts, index) == any(_contains(parts, s) for s in seqs)


# --- the one-time migration ---------------------------------------------------------------------------------------


def _index_the_old_way(home: Path, settings: Settings) -> None:
    """What an index built before Downloads was closed looks like: the whole home walked with no closed folder."""
    index_folder(home, HashEmbedder(), store_of(settings), settings, log=QUIET)


def test_the_migration_opens_a_downloads_the_index_already_holds_and_says_so_once(world, capsys):
    home, settings = world
    _index_the_old_way(home, settings)
    assert names(settings) == {"lecture1.md", "notes.md", "bank-statement.md", "plan.md"}
    code, out, err = run(capsys, "stats")
    assert code == 0
    expected = closed_folders.MIGRATION_LINE.format(folder="~/Downloads", policy=settings.data_dir / "policy.toml")
    assert err.count(expected) == 1
    assert expected == (
        f"locus: ~/Downloads is a closed folder from this version on. Your index already holds files from it, so Locus "
        f"opened it for you (profile untrusted) and recorded that in {settings.data_dir / 'policy.toml'}. Delete the "
        "marked block there to close it."
    )
    text = policy_text(settings)
    assert "opened by migration" in text and "rows already indexed" in text and text.count("[[folder]]") == 1
    code, _, err = run(capsys, "stats")  # a second start finds nothing to do and says nothing
    assert "closed folder" not in err and policy_text(settings) == text


def test_after_the_migration_a_prune_keeps_the_downloads_rows(world, capsys):
    home, settings = world
    _index_the_old_way(home, settings)
    run(capsys, "stats")  # any command that reads the index runs the migration first
    assert run(capsys, "index", str(home), "--prune", "--yes")[0] == 0
    assert names(settings) == {"lecture1.md", "notes.md", "bank-statement.md", "plan.md"}


def test_control_without_the_migration_a_prune_removes_the_downloads_rows(world):
    """Proves the migration is what protects the founder's index: the same sequence without it loses the rows."""
    home, settings = world
    _index_the_old_way(home, settings)
    stats = index_folder(
        home, HashEmbedder(), store_of(settings), settings, prune=True, log=QUIET,
        matcher=matcher_for_folder(home, load_policy(settings.data_dir / "policy.toml"), home),
    )  # fmt: skip
    assert names(settings) == {"plan.md"} and stats.files_removed == 3


def test_migration_does_nothing_for_an_index_without_downloads_rows(world, capsys):
    home, settings = world
    index_folder(home / "Documents", HashEmbedder(), store_of(settings), settings, log=QUIET)
    code, _, err = run(capsys, "stats")
    assert "closed folder" not in err and policy_text(settings) == ""  # Downloads stays closed for this index
    assert closed_folders.needs_opening(home / "Downloads", Policy(), home)


def test_a_folder_opened_and_then_closed_again_is_not_reopened_by_a_later_start(world, capsys):
    home, settings = world
    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0  # creates the collection under these rules
    (settings.data_dir / "policy.toml").write_text("version = 1\n", encoding="utf-8")  # the user deletes the block
    run(capsys, "stats")
    assert "[[folder]]" not in policy_text(settings)


def test_migration_leaves_a_broken_policy_alone_and_retries_next_time(world, capsys):
    home, settings = world
    _index_the_old_way(home, settings)
    (settings.data_dir / "policy.toml").write_text('[[folder]]\npath = "~"\nprofil = "x"\n', encoding="utf-8")
    code, out, err = run(capsys, "stats")
    assert "the closed-folder check could not run" in err
    assert not (settings.data_dir / "closed-folders.json").exists()
    (settings.data_dir / "policy.toml").write_text("version = 1\n", encoding="utf-8")
    code, _, err = run(capsys, "stats")
    assert "[[folder]]" in policy_text(settings) and "is a closed folder from this version on" in err


def test_the_start_up_check_writes_nothing_to_stdout(world, capsys):
    home, settings = world
    _index_the_old_way(home, settings)
    code, out, err = run(capsys, "stats")
    assert "closed folder" in err and "closed folder" not in out


# --- the same eligibility as before, once open --------------------------------------------------------------------


def test_opening_changes_which_files_are_eligible_by_nothing(world):
    """Every extension in DEFAULT_EXTENSIONS that the plain walker lists is listed by the opened matcher too."""
    home, settings = world
    for ext in DEFAULT_EXTENSIONS:
        (home / "Downloads" / f"file{ext}").write_text("x", encoding="utf-8")
    plain = {p.name for p in iter_files(home / "Downloads", settings)}
    opened = parse_policy('[[folder]]\npath = "~/Downloads"\nprofile = "untrusted"\n')
    through = {
        p.name
        for p in iter_files(home / "Downloads", settings, None, matcher_for_folder(home / "Downloads", opened, home))
    }
    assert through == plain and len(plain) >= len(DEFAULT_EXTENSIONS)
    assert Matcher(opened, home).decide(home / "Downloads" / "file.pdf").profile == "untrusted"
