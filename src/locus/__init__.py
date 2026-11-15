"""Locus — local-first personal AI brain exposed over MCP."""

import os

__version__ = "0.2.0"

# S-08. ONNX Runtime 1.31 and later (the engine behind the default on-device embedder) includes Microsoft's
# usage-telemetry uploader in its macOS and Linux builds, on by default. It starts when `onnxruntime` is imported and
# reads this variable once, at that moment, so it has to be set before the first import of onnxruntime or fastembed
# anywhere in the process: calling `onnxruntime.disable_telemetry_events()` afterwards only mutes events, while the
# uploader, its device-ID file and its event queue already exist. Set, not defaulted, like the Chroma and Hugging
# Face switches (ingest/store.py, ingest/embedder.py): a user's environment does not turn it back on.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
