"""LOCUS_EMBEDDER=hash (the offline test embedder) is refused at runtime unless LOCUS_ALLOW_TEST_EMBEDDER=1 is set.

Its vectors are hashed words: indexing works and search then returns results that look plausible and mean nothing, so
a real user must not end up with it by a typo or a copied test setting. The test suite sets the variable for itself
(tests/conftest.py); these tests take it away again."""

from __future__ import annotations

import asyncio
import os

import pytest

from locus import cli, config, mcp_server
from locus.config import get_settings
from locus.ingest.embedder import HashEmbedder, expected_embedder_name, get_embedder


@pytest.fixture(autouse=True)
def _a_real_users_environment(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("LOCUS_") and key != "LOCUS_LOCAL_BACKEND":
            monkeypatch.delenv(key)
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server, "_state", {})
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _hash_settings(monkeypatch):
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    get_settings.cache_clear()
    return get_settings()


def test_the_test_embedder_is_refused_with_a_message_that_says_what_to_use_instead(monkeypatch):
    settings = _hash_settings(monkeypatch)
    with pytest.raises(RuntimeError) as err:
        get_embedder(settings)
    text = str(err.value)
    assert "LOCUS_EMBEDDER=hash is the offline test embedder" in text and "LOCUS_EMBEDDER=local" in text
    assert "LOCUS_ALLOW_TEST_EMBEDDER" not in text  # a user is not told how to switch it back on


def test_the_expected_name_for_a_read_only_open_is_refused_too(monkeypatch):
    with pytest.raises(RuntimeError, match="offline test embedder"):
        expected_embedder_name(_hash_settings(monkeypatch))


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_the_test_variable_allows_it(monkeypatch, value):
    monkeypatch.setenv("LOCUS_ALLOW_TEST_EMBEDDER", value)
    assert isinstance(get_embedder(_hash_settings(monkeypatch)), HashEmbedder)


@pytest.mark.parametrize("value", ["", "0", "no", "off"])
def test_other_values_leave_it_refused(monkeypatch, value):
    monkeypatch.setenv("LOCUS_ALLOW_TEST_EMBEDDER", value)
    with pytest.raises(RuntimeError):
        get_embedder(_hash_settings(monkeypatch))


def test_the_real_embedders_are_not_affected(monkeypatch):
    assert get_settings().embedder == "local"
    assert expected_embedder_name(get_settings()).startswith("local:")
    monkeypatch.setenv("LOCUS_EMBEDDER", "voyage")
    get_settings.cache_clear()
    assert expected_embedder_name(get_settings()).startswith("voyage:")


def test_the_unknown_embedder_message_no_longer_advertises_the_test_embedder(monkeypatch):
    monkeypatch.setenv("LOCUS_EMBEDDER", "nonsense")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as err:
        get_embedder(get_settings())
    assert "hash" not in str(err.value) and "local | voyage | openai | managed" in str(err.value)


def test_locus_index_exits_2_and_indexes_nothing(monkeypatch, tmp_path, capsys):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("a note about zebras")
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as exc:
        cli.main(["index", str(notes)])
    assert exc.value.code == 2
    assert "error: LOCUS_EMBEDDER=hash is the offline test embedder" in capsys.readouterr().err
    assert not (tmp_path / "data" / "chroma").exists()


def test_locus_doctor_reports_it_as_a_failure(monkeypatch, capsys):
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor"])
    out = capsys.readouterr().out
    assert exc.value.code == 1 and "[FAIL] embedder + store round-trip" in out and "offline test embedder" in out


def test_the_mcp_server_answers_a_search_with_the_reason_instead_of_searching(monkeypatch):
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    get_settings.cache_clear()
    out = asyncio.run(mcp_server.locus_search("zebra"))
    assert out.startswith("Locus is not configured:") and "offline test embedder" in out
