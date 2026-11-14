# Third-party licences: Locus engine (`locusfiles`)

Direct runtime dependencies of this package, generated from each installed package's metadata on 2026-11-12.
None of them is bundled in this package: pip, pipx or uv installs each one from its own upstream
release, which carries its own licence text and any NOTICE file. Test and lint tools are not listed
because they are never installed for users. The Locus engine itself is licensed under
the PolyForm Noncommercial License 1.0.0 (see LICENSE and NOTICE).

| Package | Needed for | Version range | Licence (as declared) | Checked version | Ships its own NOTICE | Upstream |
|---|---|---|---|---|---|---|
| mcp | core | `>=2.2,<2.3` | MIT | 2.2.0 | no | https://github.com/modelcontextprotocol/python-sdk |
| chromadb | core | `>=1.5,<1.6` | Apache-2.0 | 1.5.9 | no | https://github.com/chroma-core/chroma |
| pydantic | core | `>=2.5` | MIT | 2.14.3 | no | https://github.com/pydantic/pydantic |
| python-dotenv | core | `>=1.0` | BSD-3-Clause | 1.2.4 | no | https://github.com/theskumar/python-dotenv |
| watchdog | core | `>=4.0` | Apache-2.0 | 7.1.0 | no | https://github.com/gorakhargosh/watchdog/ |
| pypdfium2 | core | `>=4.30` | BSD-3-Clause, Apache-2.0, dependency licenses | 5.15.2 | no | https://github.com/pypdfium2-team/pypdfium2 |
| python-docx | core | `>=1.1` | MIT | 1.2.0 | no | https://github.com/python-openxml/python-docx |
| voyageai | extra `voyage` | `>=0.3` | MIT | 0.6.0 | no | https://pypi.org/project/voyageai/ |
| openai | extra `openai` | `>=1.30` | Apache-2.0 | 3.49.0 | no | https://github.com/openai/openai-python |
| fastembed | extra `local` | `>=0.9,<1` | Apache-2.0 | 0.9.1 | yes | https://github.com/qdrant/fastembed |
| sentence-transformers | extra `local-torch` | `>=3.0` | Apache-2.0 | 6.3.0 | yes | https://github.com/huggingface/sentence-transformers/ |
| lancedb | extra `lancedb` | `>=0.15` | Apache-2.0 | 0.41.0 | no | https://github.com/lancedb/lancedb |
| realtime | extra `relay` | `>=2.31,<2.32` | MIT | 2.31.0 | no | https://github.com/supabase/supabase-py |
| httpx | extra `relay` | `>=0.27` | BSD-3-Clause | 0.28.1 | no | https://github.com/encode/httpx |
| cryptography | extra `relay` | `>=42` | Apache-2.0 OR BSD-3-Clause | 50.0.2 | no | https://github.com/pyca/cryptography/ |

Notes:

- pypdfium2: Its wheel bundles a PDFium binary; the licences of PDFium and the libraries built into it (abseil, FreeType, ICU, libjpeg-turbo, libpng, libtiff, OpenJPEG, zlib and others) ship in that wheel's licenses/ directory.

## Models downloaded at run time (not bundled)

| Model | Licence | Source | When |
|---|---|---|---|
| intfloat/multilingual-e5-small | MIT | https://huggingface.co/intfloat/multilingual-e5-small | Downloaded from Hugging Face on first use when LOCUS_EMBEDDER=local (the `local` extra). |
