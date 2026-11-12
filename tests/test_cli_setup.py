"""`locus setup --managed | --local` (handover book §5, grill fix 6): one command that sets the
embedder, checks a managed key without spending anything, writes the .env the engine reads
(owner-only, other lines kept) and never switches an existing index's embedding model silently
(constraint 4). Offline: account-status is a fake urlopen, the index uses HashEmbedder's name."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import urllib.error
from pathlib import Path

import pytest

from locus import cli, config, configure
from locus.config import get_settings

KEY = "locus_live_TESTkey0123456789abcdefghijklmnop"
OTHER_KEY = "locus_live_FAKEotherKey9876543210zyxwvutsrq"

PLAN = {
    "tier": "personal",
    "status": "trialing",
    "trial_end": "no_end_yet",
    "daily_quota": 3000,
    "used_today": 0,
    "api_key_count": 1,
    "canonical_url": "https://locusfiles.com",
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """A clean environment per test: no LOCUS_* from the shell, no source-checkout .env, the
    working folder in tmp_path, and os.environ restored afterwards (get_settings' load_dotenv
    writes into it directly, which monkeypatch alone would not undo)."""
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("LOCUS_"):
            del os.environ[key]
    os.environ["LOCUS_DATA_DIR"] = str(tmp_path / "data")
    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", None)
    work = tmp_path / "proj"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(configure, "_interactive", lambda: True)
    get_settings.cache_clear()
    yield work
    get_settings.cache_clear()
    os.environ.clear()
    os.environ.update(saved)


class _Account:
    """Fake urllib.request.urlopen for the account-status endpoint; records every request."""

    def __init__(self, payload=None, *, status: int | None = None, unreachable: bool = False):
        self.payload = PLAN if payload is None else payload
        self.status = status
        self.unreachable = unreachable
        self.requests: list = []

    def __call__(self, req, timeout=15):  # noqa: ARG002 - urlopen's signature
        self.requests.append(req)
        if self.unreachable:
            raise urllib.error.URLError("connection refused")
        if self.status is not None:
            raise urllib.error.HTTPError(req.full_url, self.status, "refused", {}, None)
        body = json.dumps(self.payload).encode()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return body

        return _Resp()


def _fake_account(monkeypatch, **kwargs) -> _Account:
    account = _Account(**kwargs)
    monkeypatch.setattr("urllib.request.urlopen", account)
    return account


def _no_network(monkeypatch) -> None:
    def _refuse(req, timeout=15):  # noqa: ARG001
        raise AssertionError(f"no network call expected, got {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", _refuse)


def _key_prompt(monkeypatch, key: str = KEY) -> list[str]:
    asked: list[str] = []

    def _ask(prompt: str) -> str:
        asked.append(prompt)
        return key

    monkeypatch.setattr(configure, "_ask_key", _ask)
    return asked


def _never_prompt(monkeypatch) -> None:
    def _ask(prompt: str) -> str:  # noqa: ARG001
        raise AssertionError("the key must not be asked for")

    monkeypatch.setattr(configure, "_ask_key", _ask)


def _run(*argv: str) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(["setup", *argv])
    return int(exc.value.code or 0)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _make_index(data_dir: Path, collection: str = "locus", embedder: str = "hash-256") -> None:
    pytest.importorskip("chromadb")
    from locus.ingest.store import ChromaStore

    store = ChromaStore(data_dir, collection, embedder)
    store.upsert(["a:0"], [[1.0] + [0.0] * 7], ["text"], [{"path": "/v/a.md", "chunk_index": 0}])


# --- managed -------------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_managed_creates_an_owner_only_env_with_the_embedder_and_key(_isolated, monkeypatch, capsys):
    account = _fake_account(monkeypatch)
    asked = _key_prompt(monkeypatch)

    assert _run("--managed") == 0

    env = _isolated / ".env"
    assert env.is_file() and _mode(env) == 0o600
    text = env.read_text()
    assert "LOCUS_EMBEDDER=managed\n" in text
    assert f"LOCUS_API_KEY={KEY}\n" in text
    assert len(asked) == 1
    out = capsys.readouterr()
    assert KEY not in out.out and KEY not in out.err, "the key is never printed"
    assert "Plan: personal (trialing)" in out.out
    assert "locus index" in out.out and "claude mcp add locus" in out.out and '"mcpServers"' in out.out
    assert "<your key>" in out.out
    # The check is the account-status read, with the key as a bearer token: never an embedding call.
    assert len(account.requests) == 1
    req = account.requests[0]
    assert req.full_url == "https://locusfiles.com/api/support/account-status"
    assert req.get_method() == "GET" and req.data is None
    assert req.get_header("Authorization") == f"Bearer {KEY}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_managed_updates_an_existing_env_in_place_and_tightens_its_mode(_isolated, monkeypatch):
    env = _isolated / ".env"
    env.write_text(
        "# my settings\n"
        "LOCUS_EMBEDDER=local\n"
        "\n"
        "export LOCUS_API_KEY=locus_live_oldoldoldoldold\n"
        "LOCUS_CHUNK_TOKENS=700\n"
        "# LOCUS_API_KEY=commented-out-stays\n"
        "NOTION_CLIENT_ID=abc"  # no final newline
    )
    os.chmod(env, 0o644)
    _fake_account(monkeypatch)
    _key_prompt(monkeypatch)

    assert _run("--managed", "--new-key") == 0

    assert _mode(env) == 0o600
    assert env.read_text() == (
        "# my settings\n"
        "LOCUS_EMBEDDER=managed\n"
        "\n"
        f"export LOCUS_API_KEY={KEY}\n"
        "LOCUS_CHUNK_TOKENS=700\n"
        "# LOCUS_API_KEY=commented-out-stays\n"
        "NOTION_CLIENT_ID=abc"
    )


def test_update_env_file_appends_missing_keys_and_keeps_crlf(tmp_path):
    folder = tmp_path / "crlf"
    folder.mkdir()
    env = folder / ".env"
    env.write_bytes(b"A=1\r\nLOCUS_EMBEDDER=local\r\n")
    configure.update_env_file(env, {"LOCUS_EMBEDDER": "managed", "LOCUS_COLLECTION": "locus-managed"})
    assert env.read_bytes() == (
        b"A=1\r\nLOCUS_EMBEDDER=managed\r\n# written by `locus setup`\r\nLOCUS_COLLECTION=locus-managed\r\n"
    )
    assert [p.name for p in folder.iterdir()] == [".env"], "no temporary file left behind"


def test_managed_uses_an_already_set_key_without_asking(_isolated, monkeypatch, capsys):
    os.environ["LOCUS_API_KEY"] = KEY
    account = _fake_account(monkeypatch)
    _never_prompt(monkeypatch)

    assert _run("--managed") == 0

    assert f"LOCUS_API_KEY={KEY}" in (_isolated / ".env").read_text()
    assert account.requests[0].get_header("Authorization") == f"Bearer {KEY}"
    out = capsys.readouterr()
    assert "already set" in out.out
    assert KEY not in out.out + out.err


def test_managed_reads_the_key_already_saved_in_the_env_file(_isolated, monkeypatch):
    (_isolated / ".env").write_text(f"LOCUS_EMBEDDER=local\nLOCUS_API_KEY={KEY}\n")
    _fake_account(monkeypatch)
    _never_prompt(monkeypatch)

    assert _run("--managed") == 0
    assert (_isolated / ".env").read_text() == f"LOCUS_EMBEDDER=managed\nLOCUS_API_KEY={KEY}\n"


def test_new_key_asks_even_when_one_is_set(_isolated, monkeypatch, capsys):
    os.environ["LOCUS_API_KEY"] = OTHER_KEY
    account = _fake_account(monkeypatch)
    asked = _key_prompt(monkeypatch, KEY)

    assert _run("--managed", "--new-key") == 0

    assert asked and account.requests[0].get_header("Authorization") == f"Bearer {KEY}"
    assert f"LOCUS_API_KEY={KEY}" in (_isolated / ".env").read_text()
    err = capsys.readouterr().err
    # The shell's own LOCUS_API_KEY wins over the file: say so, without showing either key.
    assert "a different value is set in this shell's environment" in err
    assert KEY not in err and OTHER_KEY not in err


def test_a_rejected_key_saves_nothing_and_is_never_printed(_isolated, monkeypatch, capsys):
    _fake_account(monkeypatch, status=401)
    _key_prompt(monkeypatch)

    assert _run("--managed") == 1

    assert not (_isolated / ".env").exists()
    out = capsys.readouterr()
    assert "did not accept this key" in out.err
    assert KEY not in out.out + out.err


def test_an_unreachable_server_saves_nothing_and_names_skip_check(_isolated, monkeypatch, capsys):
    _fake_account(monkeypatch, unreachable=True)
    _key_prompt(monkeypatch)

    assert _run("--managed") == 1

    assert not (_isolated / ".env").exists()
    err = capsys.readouterr().err
    assert "Could not check the key" in err and "--skip-check" in err


def test_skip_check_saves_without_any_network_call(_isolated, monkeypatch, capsys):
    _no_network(monkeypatch)
    _key_prompt(monkeypatch)

    assert _run("--managed", "--skip-check") == 0
    assert f"LOCUS_API_KEY={KEY}" in (_isolated / ".env").read_text()
    assert "not checked" in capsys.readouterr().out


@pytest.mark.parametrize(
    "payload",
    [
        {**PLAN, "status": "past_due"},
        {**PLAN, "status": "canceled"},
        {**PLAN, "status": "trialing", "trial_end": "ended"},
    ],
)
def test_an_inactive_plan_saves_the_valid_key_with_a_warning(_isolated, monkeypatch, capsys, payload):
    _fake_account(monkeypatch, payload=payload)
    _key_prompt(monkeypatch)

    assert _run("--managed") == 0
    assert "LOCUS_EMBEDDER=managed" in (_isolated / ".env").read_text()
    assert "not active right now" in capsys.readouterr().err


def test_a_valid_key_without_a_plan_record_is_saved_with_a_warning(_isolated, monkeypatch, capsys):
    _fake_account(monkeypatch, status=404)  # account-status: the key is known, no subscription row
    _key_prompt(monkeypatch)

    assert _run("--managed") == 0
    assert f"LOCUS_API_KEY={KEY}" in (_isolated / ".env").read_text()
    out = capsys.readouterr()
    assert "found no plan for this account" in out.err and KEY not in out.out + out.err


def test_a_server_error_saves_nothing(_isolated, monkeypatch, capsys):
    _fake_account(monkeypatch, status=500)
    _key_prompt(monkeypatch)

    assert _run("--managed") == 1
    assert not (_isolated / ".env").exists()
    assert "Could not check the key with Locus right now (HTTP 500)" in capsys.readouterr().err


def test_no_key_and_no_terminal_saves_nothing(_isolated, monkeypatch, capsys):
    monkeypatch.setattr(configure, "_interactive", lambda: False)
    _never_prompt(monkeypatch)
    _no_network(monkeypatch)

    assert _run("--managed") == 2
    assert not (_isolated / ".env").exists()
    assert "no terminal to ask on" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["", "   ", "locus_live_has space", 'locus_live_"quoted"', "short"])
def test_a_value_that_is_not_a_key_saves_nothing(_isolated, monkeypatch, bad):
    _no_network(monkeypatch)
    _key_prompt(monkeypatch, bad)

    assert _run("--managed") == 2
    assert not (_isolated / ".env").exists()


def test_an_invalid_api_base_stops_before_asking_for_the_key(_isolated, monkeypatch, capsys):
    os.environ["LOCUS_API_BASE"] = "http://example.com"  # plain http off loopback is refused
    _never_prompt(monkeypatch)
    _no_network(monkeypatch)

    assert _run("--managed") == 2
    assert not (_isolated / ".env").exists()
    assert "LOCUS_API_BASE" in capsys.readouterr().err


def test_the_env_file_found_in_a_parent_folder_is_the_one_updated(_isolated, monkeypatch):
    parent_env = _isolated / ".env"
    parent_env.write_text("LOCUS_EMBEDDER=local\n")
    sub = _isolated / "deeper" / "still"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    _fake_account(monkeypatch)
    _key_prompt(monkeypatch)

    assert _run("--managed") == 0
    assert not (sub / ".env").exists()
    assert "LOCUS_EMBEDDER=managed" in parent_env.read_text()


def test_a_shell_embedder_that_overrides_the_file_is_called_out(_isolated, monkeypatch, capsys):
    os.environ["LOCUS_EMBEDDER"] = "local"
    _fake_account(monkeypatch)
    _key_prompt(monkeypatch)

    assert _run("--managed") == 0
    assert "LOCUS_EMBEDDER=local is set in this shell's environment" in capsys.readouterr().err


# --- constraint 4: never switch an existing index's embedding model silently -------------------------


def test_an_index_built_with_another_embedder_stops_setup_first(_isolated, tmp_path, monkeypatch, capsys):
    _make_index(tmp_path / "data")
    (_isolated / ".env").write_text("LOCUS_EMBEDDER=hash\n")
    _never_prompt(monkeypatch)
    _no_network(monkeypatch)

    assert _run("--managed") == 2

    assert (_isolated / ".env").read_text() == "LOCUS_EMBEDDER=hash\n", "nothing changed"
    err = capsys.readouterr().err
    assert "'hash-256'" in err and "re-index into a new collection" in err
    assert "locus setup --managed --collection locus-managed" in err
    assert "LOCUS_EMBEDDER=hash" in err  # how to get back to the old index


def test_a_new_collection_lets_the_switch_go_ahead_and_is_saved(_isolated, tmp_path, monkeypatch, capsys):
    _make_index(tmp_path / "data")
    _fake_account(monkeypatch)
    _key_prompt(monkeypatch)

    assert _run("--managed", "--collection", "locus-managed") == 0

    text = (_isolated / ".env").read_text()
    assert "LOCUS_EMBEDDER=managed" in text and "LOCUS_COLLECTION=locus-managed" in text
    assert '"LOCUS_COLLECTION": "locus-managed"' in capsys.readouterr().out
    from locus.ingest.store import recorded_collection

    assert recorded_collection(tmp_path / "data", "locus") == ("hash-256", 1), "the old index is untouched"
    assert recorded_collection(tmp_path / "data", "locus-managed") is None, "setup creates no collection"


def test_the_app_settings_carry_the_collection_and_data_dir_already_set(_isolated, tmp_path, monkeypatch, capsys):
    """A re-run (say --new-key after an earlier --collection switch) prints app settings that open
    the same index: the app reads no .env, so the collection and data folder must be in them."""
    (_isolated / ".env").write_text(f"LOCUS_EMBEDDER=managed\nLOCUS_API_KEY={KEY}\nLOCUS_COLLECTION=locus-managed\n")
    _fake_account(monkeypatch)
    _key_prompt(monkeypatch, OTHER_KEY)

    assert _run("--managed", "--new-key") == 0

    out = capsys.readouterr().out
    client = json.loads(next(line for line in out.splitlines() if '"mcpServers"' in line))
    env = client["mcpServers"]["locus"]["env"]
    assert env["LOCUS_COLLECTION"] == "locus-managed"
    assert env["LOCUS_DATA_DIR"] == str(tmp_path / "data")  # set by the shell in this test (_isolated)
    assert env["LOCUS_API_KEY"] == "<your key>" and OTHER_KEY not in out and KEY not in out
    assert "-e LOCUS_COLLECTION=locus-managed" in out


def test_default_settings_add_nothing_to_the_app_settings(_isolated, monkeypatch, capsys):
    del os.environ["LOCUS_DATA_DIR"]
    monkeypatch.setenv("HOME", str(_isolated))  # the default ~/.locus, kept inside the test folder
    get_settings.cache_clear()
    _no_network(monkeypatch)

    assert _run("--local") == 0

    out = capsys.readouterr().out
    client = json.loads(next(line for line in out.splitlines() if '"mcpServers"' in line))
    assert client["mcpServers"]["locus"]["env"] == {"LOCUS_EMBEDDER": "local"}


def test_an_index_already_built_with_the_managed_embedder_is_kept(_isolated, tmp_path, monkeypatch):
    _make_index(tmp_path / "data", embedder="managed:voyage-4")
    _fake_account(monkeypatch)
    _key_prompt(monkeypatch)

    assert _run("--managed") == 0
    assert "LOCUS_COLLECTION" not in (_isolated / ".env").read_text()


def test_a_bad_collection_name_saves_nothing(_isolated, monkeypatch):
    _never_prompt(monkeypatch)
    _no_network(monkeypatch)
    assert _run("--managed", "--collection", "no spaces allowed") == 2
    assert not (_isolated / ".env").exists()


# --- local -------------------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_local_writes_the_embedder_keeps_the_key_and_makes_no_network_call(_isolated, monkeypatch, capsys):
    (_isolated / ".env").write_text(f"LOCUS_EMBEDDER=managed\nLOCUS_API_KEY={KEY}\n")
    _no_network(monkeypatch)
    _never_prompt(monkeypatch)

    assert _run("--local") == 0

    env = _isolated / ".env"
    # The key stays: `locus serve --transport relay` and locus_account_status still use it.
    assert env.read_text() == f"LOCUS_EMBEDDER=local\nLOCUS_API_KEY={KEY}\n"
    assert _mode(env) == 0o600
    out = capsys.readouterr()
    assert "on-device embeddings" in out.out
    assert "LOCUS_API_KEY" not in out.out and KEY not in out.out + out.err


def test_local_without_the_local_extra_still_saves_and_says_how_to_install_it(_isolated, monkeypatch, capsys):
    _no_network(monkeypatch)
    monkeypatch.setattr(configure.importlib.util, "find_spec", lambda name: None)

    assert _run("--local") == 0
    assert "LOCUS_EMBEDDER=local" in (_isolated / ".env").read_text()
    assert '"locusfiles[local]"' in capsys.readouterr().err


def test_local_refuses_to_switch_a_managed_index(_isolated, tmp_path, monkeypatch, capsys):
    _make_index(tmp_path / "data", embedder="managed:voyage-4")
    _no_network(monkeypatch)

    assert _run("--local") == 2
    assert not (_isolated / ".env").exists()
    assert "locus setup --local --collection locus-local" in capsys.readouterr().err


def test_setup_requires_a_mode():
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["setup"])
    assert exc.value.code == 2


def test_run_setup_reports_the_file_it_wrote(_isolated, monkeypatch):
    _no_network(monkeypatch)
    out, err = io.StringIO(), io.StringIO()
    result = configure.run_setup("local", out=out, err=err)
    assert result.code == 0 and result.env_file == _isolated / ".env"
    assert str(_isolated / ".env") in out.getvalue()
    assert f"Locus reads it when you run `locus` in {_isolated} or a folder inside it." in out.getvalue()


def test_a_source_checkout_without_a_local_env_writes_its_own_env(_isolated, tmp_path, monkeypatch):
    """get_settings falls back to a source checkout's .env (editable installs), so setup writes there
    when no .env is found from the working folder, and says it is read from any folder."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", checkout)
    _no_network(monkeypatch)
    out = io.StringIO()
    result = configure.run_setup("local", out=out, err=io.StringIO())
    assert result.env_file == checkout / ".env" and (checkout / ".env").is_file()
    assert not (_isolated / ".env").exists()
    assert "This source checkout reads it from any folder that has no .env of its own." in out.getvalue()
