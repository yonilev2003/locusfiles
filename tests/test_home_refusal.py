"""The home folder named as one folder is refused by index, watch and quickstart unless --all is given. Offline."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import cli  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    (home / "notes").mkdir(parents=True)
    (home / "notes" / "a.md").write_text("a note about the roadmap\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    monkeypatch.setenv("LOCUS_ALLOW_TEST_EMBEDDER", "1")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from locus.config import get_settings

    get_settings.cache_clear()
    return home


@pytest.mark.parametrize("spelling", ["home", "tilde", "env", "parent", "symlink"])
@pytest.mark.parametrize("command", ["index", "watch", "quickstart"])
def test_the_home_folder_is_refused_however_it_is_spelled(home, capsys, tmp_path, spelling, command):
    link = tmp_path / "home-link"
    link.symlink_to(home)
    target = {
        "home": str(home),
        "tilde": "~",
        "env": os.environ["HOME"] + os.sep,
        "parent": str(home.parent),
        "symlink": str(link),
    }[spelling]
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main([command, target])
    err = capsys.readouterr().err
    assert stop.value.code == 2 and "your whole home folder" in err and "--all" in err
    assert not (tmp_path / "data" / "chroma").exists()  # nothing was opened or indexed


def test_a_folder_inside_the_home_is_not_refused(home, capsys):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(["index", str(home / "notes")])
    assert stop.value.code == 0


def test_all_opts_in_for_a_plain_index_of_the_home(home, capsys):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(["index", str(home), "--all"])
    assert stop.value.code == 0 and "your whole home folder" not in capsys.readouterr().err


def test_a_differently_cased_spelling_of_the_home_is_refused(home, capsys, tmp_path):
    swapped = str(home).swapcase()
    if not os.path.exists(swapped) or not os.path.samefile(swapped, home):
        pytest.skip("a case-sensitive file system: the swapped spelling is another path")
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(["index", swapped])
    assert stop.value.code == 2 and "your whole home folder" in capsys.readouterr().err


def test_a_dry_run_on_the_home_is_allowed(home, capsys):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(["index", str(home), "--dry-run"])
    assert stop.value.code == 0
