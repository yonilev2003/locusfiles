"""The `search` and `fetch` tools of the tunnel mode run on the same engine as every other surface, so the folder rules
and the kill switch must apply to them too. Offline: HashEmbedder, tmp folders, the tools called as functions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("chromadb")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from locus import killswitch, service  # noqa: E402
from locus import remote_tools as rt  # noqa: E402
from locus.config import get_settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402
from locus.service import Engine  # noqa: E402


class FakeServer:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def add_tool(self, fn, *, name, **_kw) -> None:
        self.tools[name] = fn


@pytest.fixture
def world(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    (notes / "work").mkdir(parents=True)
    (notes / "home").mkdir()
    (notes / "work" / "report.md").write_text("the quarterly report about zebras", encoding="utf-8")
    (notes / "home" / "plan.md").write_text("the weekend plan about zebras", encoding="utf-8")
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    monkeypatch.setenv("LOCUS_COLLECTION", "remote-restrictions")
    get_settings.cache_clear()
    settings = get_settings()
    store = ChromaStore(settings.data_dir, settings.collection, HashEmbedder().name)
    index_folder(notes, HashEmbedder(), store, settings, log=lambda _m: None)
    monkeypatch.setattr(service, "freshen_for_serve", lambda *a, **k: None)
    engine = Engine(components=lambda: (HashEmbedder(), store, settings))
    server = FakeServer()
    rt.register_chatgpt_tools(server, engine, notice="UNTRUSTED NOTICE", annotations={"readOnlyHint": True})

    def search(query="zebras"):
        return asyncio.run(server.tools["search"](query=query))

    def fetch(path, chunk=0):
        return asyncio.run(server.tools["fetch"](id=rt.make_id(path, chunk)))

    def policy(text: str) -> None:
        (settings.data_dir / "policy.toml").write_text(text, encoding="utf-8")

    return SimpleNamespace(notes=notes, settings=settings, search=search, fetch=fetch, policy=policy)


def _paths(output) -> set[str]:
    return {item.title for item in output.results}


def test_both_files_are_found_and_read_before_any_rule(world):
    assert _paths(world.search()) == {"report.md", "plan.md"}
    assert "quarterly report" in world.fetch(str(world.notes / "work" / "report.md")).text


def test_an_excluded_folder_is_hidden_from_search_and_refused_by_fetch(world):
    world.policy(f'[[folder]]\npath = "{(world.notes / "work").as_posix()}"\nprofile = "excluded"\n')
    assert _paths(world.search()) == {"plan.md"}
    with pytest.raises(ToolError) as refused:
        world.fetch(str(world.notes / "work" / "report.md"))
    assert "quarterly report" not in str(refused.value)  # the refusal carries no file text
    assert "weekend plan" in world.fetch(str(world.notes / "home" / "plan.md")).text  # the rest keeps working


def test_the_kill_switch_stops_search_and_fetch_and_resume_restores_them(world):
    killswitch.pause(world.settings.data_dir)
    with pytest.raises(ToolError) as stopped_search:
        world.search()
    with pytest.raises(ToolError) as stopped_fetch:
        world.fetch(str(world.notes / "home" / "plan.md"))
    for stopped in (stopped_search, stopped_fetch):
        assert "paused" in str(stopped.value).lower()
        assert "zebras" not in str(stopped.value)
    killswitch.resume(world.settings.data_dir)
    assert _paths(world.search()) == {"report.md", "plan.md"}


def test_every_result_still_carries_the_path_and_chunk_number(world):
    for item in world.search().results:
        path, chunk = rt.parse_id(item.id)
        assert path.endswith(item.title) and chunk >= 0
