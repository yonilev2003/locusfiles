# ruff: noqa
# SEC-022 fixture for the Semgrep rule locus-chroma-embedded-only (.semgrep/locus.yml). Run:
#   semgrep --test --metrics=off --config .semgrep/locus.yml tests/semgrep/locus_chroma_fixture.py
# Each `ruleid:` line must be flagged and each `ok:` line must not, so deleting or weakening the
# guard fails a test instead of leaving a silent "0 findings". Not a pytest module (no test_ prefix);
# never imported or run. tests/test_chroma_embedded_only.py reads the same cases. Semgrep's default
# ignore list skips tests/, so `semgrep scan .` does not report these lines; if a .semgrepignore is
# ever added without tests/, the repo scan fails on them (loudly, not silently).
import subprocess

import chromadb
from chromadb import HttpClient
from chromadb.config import Settings

# ok: locus-chroma-embedded-only
store = chromadb.PersistentClient(path="/tmp/locus-chroma")

# ruleid: locus-chroma-embedded-only
remote = chromadb.HttpClient(host="localhost", port=8000)

# ruleid: locus-chroma-embedded-only
remote_async = chromadb.AsyncHttpClient(host="localhost")

# ruleid: locus-chroma-embedded-only
cloud = chromadb.CloudClient(api_key="x")

# ruleid: locus-chroma-embedded-only
imported = HttpClient(host="localhost")

# ruleid: locus-chroma-embedded-only
via_settings = chromadb.Client(Settings(chroma_api_impl="chromadb.api.fastapi.FastAPI"))

# ruleid: locus-chroma-embedded-only
import chromadb.api.fastapi

# ruleid: locus-chroma-embedded-only
import chromadb.server.fastapi

# ruleid: locus-chroma-embedded-only
asgi_target = "chromadb.app:app"

# ruleid: locus-chroma-embedded-only
from chromadb.cli import cli

# ruleid: locus-chroma-embedded-only
import chromadb_rust_bindings

# ruleid: locus-chroma-embedded-only
subprocess.run(["chroma", "run", "--path", "/tmp/locus-chroma"])

# ruleid: locus-chroma-embedded-only
subprocess.run("chroma run --path /tmp/locus-chroma", shell=True)
