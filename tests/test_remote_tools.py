"""remote_tools.py: the id that carries path and chunk number, and the file address. The tools themselves, over HTTP and
against the demo vault, are in test_remote_http.py."""

from __future__ import annotations

import pytest

from locus import remote_tools as rt


@pytest.mark.parametrize("path", ["/a/b.md", "/a/with space/üñí.md", "/a/with#hash/c.md", "/a/b#c.md"])
@pytest.mark.parametrize("chunk", [0, 7, 120])
def test_an_id_round_trips_its_path_and_chunk_index(path, chunk):
    assert rt.parse_id(rt.make_id(path, chunk)) == (path, chunk)


@pytest.mark.parametrize("bad", ["", "no-hash", "/a/b.md#", "/a/b.md#x", "/a/b.md#-1", "#3", "/a/b.md#1.5", "/a#٣x"])
def test_anything_that_is_not_an_id_from_search_is_refused(bad):
    with pytest.raises(ValueError):
        rt.parse_id(bad)


def test_the_file_address_is_a_file_uri_and_never_raises():
    assert rt.file_uri("/a/b c.md") == "file:///a/b%20c.md"
    assert rt.file_uri("relative/x.md").startswith("file://")


def test_the_title_is_the_file_name():
    assert rt._title("/a/b/c.md") == "c.md"
    assert rt._title("/") == "/"
