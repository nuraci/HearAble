# The website

The reader-facing pages, kept here as source, and published as the project's
website at **https://nuraci.github.io/HearAble/** by the GitHub Action in
`.github/workflows/pages.yml`.

The pages are fragments rather than complete HTML documents, because they also
publish on claude.ai, which wraps each page in its own document. `build.py`
wraps them for GitHub Pages instead — doctype, charset, viewport, a bar linking
the pages together — and writes a landing page. The sources are never edited by
the build.

```bash
python3 docs/site/build.py _site           # build
python3 -m http.server -d _site 8000       # preview on http://localhost:8000
```

| Source | On the website | Also on claude.ai |
|---|---|---|
| `doc_en.html` | `documentation.html` | https://claude.ai/artifact/JaPMgmrootuRfFqxzSkPe4 |
| `doc_it.html` | `documentazione.html` | https://claude.ai/artifact/MHaf6vWcULqndMzb7bqfuj |
| `article_already_inside_the_box.html` | `article.html` | https://claude.ai/artifact/YEshLrBb78V4sdsV7NjqGW |
| `internals.html` | `internals.html` | https://claude.ai/artifact/V7MwjpRWDJEN7NA6tPdn5d |

Every page carries the same stylesheet: tokens on `:root`, a dark variant, and
the project's own subtitle band (white on black at 85 %) used as a motif. Change
one and the others should follow.

All of them carry the same attribution: by Nunzio Raciti, with Claude (Opus) as
development assistant, every phase gated by tests and measurements.
