"""`locus --version` prints the installed version and exits 0, without a subcommand and without touching the index."""

from __future__ import annotations

import pytest

from locus import __version__, cli


def test_version_flag_prints_the_version_and_exits_zero(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(SystemExit) as stop:
        cli.main(["--version"])
    out = capsys.readouterr()
    assert stop.value.code == 0
    assert out.out.strip() == f"locus {__version__}" and out.err == ""
    assert not (tmp_path / "data").exists()  # nothing was created
