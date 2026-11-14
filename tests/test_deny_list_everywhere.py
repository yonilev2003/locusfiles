"""S-01: the deny-list applies on every way of indexing, not only `--roots`.

Plain `locus index <folder>`, `locus watch <folder>`, `--prune` and the consent screen all use the same matcher
(policy.Matcher over `<data dir>/policy.toml`), and `--prune` removes index entries of files the rules now refuse.
HashEmbedder, tmp folders, a fake home: offline, no paid API.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import cli  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402
from locus.policy import Matcher, Policy, matcher_for_folder  # noqa: E402
from locus.preflight import build_preflight  # noqa: E402

QUIET = lambda _msg: None  # noqa: E731

FILES = {
    "plan.md": "the launch plan about zebras",
    "passwords.txt": "bank login: hunter2",  # secret_file rule: whole word "passwords"
    "api-token.md": "sk-test-0000",  # secret_file rule: whole word "token"
    "private/diary.md": "a private diary entry about zebras",  # denied by the policy file's [deny] paths
}


def _settings(tmp_path: Path, embedder: str = "hash") -> Settings:
    return Settings(
        data_dir=tmp_path / "data", collection="denylist", embedder=embedder, voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip


def _tree(root: Path) -> Path:
    for rel, text in FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


def _write_policy(settings: Settings, private: Path) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "policy.toml").write_text(f'[deny]\npaths = ["{private}"]\n')


def _names(store: ChromaStore) -> set[str]:
    return {Path(p).name for p in store.list_paths()}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """settings, embedder, store, notes folder, and a fake home that `cli` sees through Path.home()."""
    settings = _settings(tmp_path)
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    notes = _tree(tmp_path / "notes")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_components", lambda _settings: (embedder, store))
    return settings, embedder, store, notes, home


def _matcher(notes: Path, home: Path) -> Matcher:
    return matcher_for_folder(notes, Policy(deny_paths=(str(notes / "private"),)), home)


def test_index_folder_with_a_matcher_refuses_secrets_and_says_how_many_without_names(world):
    settings, embedder, store, notes, home = world
    lines: list[str] = []
    stats = index_folder(notes, embedder, store, settings, log=lines.append, matcher=_matcher(notes, home))
    assert _names(store) == {"plan.md"}
    assert stats.denied == 3 and "denied=3" in stats.summary()
    line = next(line for line in lines if line.startswith("refused by the deny-list"))
    assert "secret_file 2, user_deny 1" in line
    assert not any(name in line for name in ("passwords.txt", "api-token", "private", "diary"))  # counts only


def test_without_a_matcher_nothing_changes(world):
    settings, embedder, store, notes, _home = world
    stats = index_folder(notes, embedder, store, settings, log=QUIET)
    assert _names(store) == {"plan.md", "passwords.txt", "api-token.md", "diary.md"}
    assert stats.denied == 0 and "denied" not in stats.summary()


def test_prune_removes_entries_the_rules_now_refuse_and_keeps_the_rest(world):
    settings, embedder, store, notes, home = world
    index_folder(notes, embedder, store, settings, log=QUIET)  # an index made before the deny-list applied here
    assert len(_names(store)) == 4
    stats = index_folder(notes, embedder, store, settings, prune=True, log=QUIET, matcher=_matcher(notes, home))
    assert _names(store) == {"plan.md"}
    assert stats.files_removed == 3


def test_without_prune_old_entries_stay_and_the_run_names_the_flag(world):
    settings, embedder, store, notes, home = world
    index_folder(notes, embedder, store, settings, log=QUIET)
    lines: list[str] = []
    index_folder(notes, embedder, store, settings, log=lines.append, matcher=_matcher(notes, home))
    assert len(_names(store)) == 4  # removal is what --prune is for
    assert any("--prune" in line and "refused by the deny-list" in line for line in lines)


def test_a_denied_root_is_refused_and_prune_empties_its_entries(world, tmp_path):
    settings, embedder, store, _notes, home = world
    ssh = _tree(tmp_path / "home" / ".ssh" / "stuff")  # a folder the built-in rules always deny
    index_folder(ssh, embedder, store, settings, log=QUIET)
    assert store.list_paths()
    stats = index_folder(
        ssh, embedder, store, settings, prune=True, log=QUIET, matcher=matcher_for_folder(ssh, Policy(), home)
    )
    assert store.list_paths() == [] and stats.files_indexed == 0 and stats.denied == 1


# --- the command line ---------------------------------------------------------------------------------------------


def test_plain_locus_index_applies_the_deny_list_and_the_policy_file(world):
    settings, _embedder, store, notes, _home = world
    _write_policy(settings, notes / "private")
    args = cli.build_parser().parse_args(["index", str(notes)])
    assert cli.cmd_index(args) == 0
    assert _names(store) == {"plan.md"}


def test_locus_index_prune_removes_what_an_older_index_let_in(world):
    settings, embedder, store, notes, _home = world
    index_folder(notes, embedder, store, settings, log=QUIET)
    _write_policy(settings, notes / "private")
    args = cli.build_parser().parse_args(["index", str(notes), "--prune"])
    assert cli.cmd_index(args) == 0
    assert _names(store) == {"plan.md"}


def test_a_broken_policy_file_stops_a_plain_index_instead_of_failing_open(world, capsys):
    settings, _embedder, store, notes, _home = world
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "policy.toml").write_text('[deny]\npath = ["/typo/in/the/key"]\n')
    args = cli.build_parser().parse_args(["index", str(notes)])
    assert cli.cmd_index(args) == cli.EXIT_NEEDS_YES
    assert store.list_paths() == []
    assert "policy file problem" in capsys.readouterr().err


def test_locus_estimate_counts_the_same_files_index_would(world, capsys):
    settings, _embedder, _store, notes, _home = world
    _write_policy(settings, notes / "private")
    args = cli.build_parser().parse_args(["estimate", str(notes), "--sample-seconds", "1"])
    assert cli.cmd_estimate(args) == 0
    out = capsys.readouterr().out
    assert "Files that would be indexed: 1 " in out and "refused by the deny-list" in out


def test_consent_screen_does_not_offer_to_send_refused_files(world):
    settings, _embedder, _store, notes, home = world
    hosted = _settings(notes.parent, embedder="voyage")
    plain = build_preflight(notes, hosted, sample_seconds=1)
    guarded = build_preflight(notes, hosted, sample_seconds=1, matcher=_matcher(notes, home))
    assert len(plain.to_send) == 4 and [e.path.name for e in guarded.to_send] == ["plan.md"]
    assert "refused by the deny-list" in guarded.render()


def test_locus_watch_passes_the_same_matcher_to_its_first_pass_and_to_the_watcher(world, monkeypatch):
    settings, embedder, store, notes, _home = world
    index_folder(notes, embedder, store, settings, log=QUIET)  # entries from before the rules
    _write_policy(settings, notes / "private")
    seen: dict = {}

    def fake_watch(root, _embedder, _store, _settings, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr("locus.ingest.watcher.watch_folder", fake_watch)
    args = cli.build_parser().parse_args(["watch", str(notes)])
    assert cli.cmd_watch(args) == 0
    assert _names(store) == {"plan.md"}  # the first pass prunes with the matcher
    matcher = seen["matcher"]
    assert matcher.decide(notes / "passwords.txt").allowed is False
    assert matcher.decide(notes / "private" / "diary.md").allowed is False
    assert matcher.decide(notes / "plan.md").allowed is True


# --- the running watcher ------------------------------------------------------------------------------------------


def test_the_running_watcher_never_indexes_a_refused_file(world):
    from locus.ingest.watcher import watch_folder

    settings, embedder, store, notes, home = world
    root = notes.resolve()
    stop = threading.Event()

    def run() -> None:
        import contextlib

        with contextlib.suppress(Exception):
            watch_folder(
                root, embedder, store, settings, debounce_seconds=0.1, poll_seconds=0.1, stop=stop,
                matcher=_matcher(root, home),
            )  # fmt: skip

    threading.Thread(target=run, daemon=True).start()
    try:
        time.sleep(0.4)  # let the observer start
        (root / "passwords.txt").write_text("changed: hunter3")
        (root / "private" / "diary.md").write_text("changed diary about zebras")
        (root / "new-note.md").write_text("a new note about zebras, written while watching")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and "new-note.md" not in _names(store):
            time.sleep(0.05)
        time.sleep(0.5)  # the refused files' events were handled in the same window
    finally:
        stop.set()
    assert "new-note.md" in _names(store)
    assert not _names(store) & {"passwords.txt", "diary.md"}
