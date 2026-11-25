---
id: models-and-languages
title: Embedding models and languages: what is the default, does Hebrew work
url: /free
keywords: model, models, embedding model, multilingual, multilingual-e5-small, bge-m3, languages, language, hebrew, english, arabic, russian, other languages, 100 languages, local model, LOCUS_LOCAL_MODEL, onnx, onnx runtime, download, 134 MB, hugging face, switch model, new collection, cross-language
updated: 2026-11-22
---
### Does it work with Hebrew and other languages?
The default on-device model, `intfloat/multilingual-e5-small`, covers about 100 languages. See Install for
the optional larger model and what was and was not tested.

**Default.** `intfloat/multilingual-e5-small` runs on your computer through ONNX Runtime. The first run downloads a
134 MB model from Hugging Face; the whole install is about 530 MB. No account and no API key.

**Hebrew.** Retrieval in Hebrew is weaker than in English. The measured gap, with its conditions and limits, is in the
README ("Retrieval quality, measured"); this help article does not restate those numbers. Searching in one language over
files in another is weaker than searching in the same language. For Hebrew or mixed material, ask the assistant to
search with both Hebrew and English terms.

**Larger or other models.** `LOCUS_LOCAL_MODEL` picks another local model. `BAAI/bge-m3` is an optional larger
download (about 2.3 GB) that needs the `local-torch` extra. Mixed-language files were not tested with it.

**Your own provider.** `voyage` or `openai` with your own key sends chunk text to that provider; see the privacy
article. `local` always remains available.

**Switching models.** One model per collection: after you change the model, index into a new collection
(`LOCUS_COLLECTION`). Do not mix models in one collection.
