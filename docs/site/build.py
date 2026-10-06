#!/usr/bin/env python3
"""Turn the pages in docs/site into a website GitHub Pages can serve.

The pages are written as fragments: a <title>, a stylesheet and a body, with no
doctype, no charset and no viewport. That is the format the claude.ai page host
expects, because it wraps every page in its own document. Served as they are by
GitHub Pages they would render in quirks mode and badly on a phone.

So the sources stay as they are — they publish in two places — and this script
wraps each one in a complete document, adds a thin bar linking the pages
together, writes a landing page, and puts the result in an output directory.
The GitHub Action in .github/workflows/pages.yml runs it on every push.

    python3 docs/site/build.py _site          # build
    python3 -m http.server -d _site 8000      # look at it on http://localhost:8000
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "https://github.com/nuraci/HearAble"

# file in docs/site -> (published name, language, label in the bar)
PAGES = {
    "doc_en.html": ("documentation.html", "en", "Documentation"),
    "internals.html": ("internals.html", "en", "Technical reference"),
    "article_already_inside_the_box.html": ("article.html", "en", "Build report"),
    "doc_it.html": ("documentazione.html", "it", "Italiano"),
}

BAR_STYLE = """<style>
.site-bar{background:#141d2b;color:#e7edf3;font:500 13px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;
  padding-block:8px;padding-inline:16px;display:flex;flex-wrap:wrap;gap:4px 16px;align-items:center}
.site-bar a{color:#e7edf3;text-decoration:none;opacity:.8}
.site-bar a:hover,.site-bar a[aria-current]{opacity:1;text-decoration:underline;text-underline-offset:3px}
.site-bar .home{font-weight:700;letter-spacing:.12em;text-transform:uppercase;opacity:1;margin-right:8px}
.site-bar .gh{margin-left:auto}
</style>"""


def bar(current: str | None) -> str:
    links = ['<a class="home" href="index.html">HearAble</a>']
    for _, (name, _, label) in PAGES.items():
        mark = ' aria-current="page"' if name == current else ""
        links.append(f'<a href="{name}"{mark}>{label}</a>')
    links.append(f'<a class="gh" href="{REPO}">GitHub</a>')
    return '<nav class="site-bar" aria-label="Site">' + "".join(links) + "</nav>"


def wrap(fragment: str, lang: str, current: str | None) -> str:
    title = re.search(r"<title>(.*?)</title>", fragment, re.S)
    head_end = fragment.find('<header class="masthead">')
    if head_end < 0:
        head_end = fragment.find("<main>")
    head, body = fragment[:head_end], fragment[head_end:]
    head = re.sub(r"<title>.*?</title>", "", head, count=1, flags=re.S)
    return (
        "<!doctype html>\n"
        f'<html lang="{lang}">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        f"<title>{title.group(1) if title else 'HearAble'}</title>\n"
        f"{head.strip()}\n{BAR_STYLE}\n</head>\n<body>\n"
        f"{bar(current)}\n{body.strip()}\n</body>\n</html>\n"
    )


def landing() -> str:
    """A front door in the same visual language as the pages it points to."""
    style = re.search(r"<style>.*?</style>", (HERE / "doc_en.html").read_text(), re.S).group(0)
    fonts = re.findall(r'<link[^>]+>', (HERE / "doc_en.html").read_text())
    cards = [
        ("documentation.html", "Documentation",
         "What HearAble is for, where the audio comes from, and both machines in full."),
        ("internals.html", "Technical reference",
         "The shipped system component by component, with every file involved."),
        ("article.html", "Build report",
         "How the pieces fit, what the real numbers are, and what was learned."),
        ("documentazione.html", "Documentazione in italiano",
         "La documentazione completa, in italiano."),
    ]
    items = "\n".join(
        f'<a class="card" href="{href}"><h2>{t}</h2><p>{d}</p></a>' for href, t, d in cards)
    body = f"""<main>
<div class="hero">
  <p class="eyebrow">Italian live subtitling · DVB-T2 · on-device</p>
  <h1>Subtitles for the television you already own</h1>
  <p class="standfirst">HearAble puts <strong>Italian subtitles on live broadcast television</strong>, about a second behind the voice, on a consumer receiver and a fanless mini PC. Nothing leaves the house, nothing is subscribed to, and the remote control still works.</p>
  <p class="byline">By <b>Nunzio Raciti</b>, with <b>Claude</b> (Opus) as development assistant</p>
</div>
<section class="cards">
{items}
</section>
<p class="repo">Source code, deployment and handover notes: <a href="{REPO}">{REPO.replace('https://', '')}</a></p>
</main>"""
    extra = """<style>
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;padding-block:40px 0}
.card{display:block;background:var(--surface);border:1px solid var(--rule);border-radius:8px;padding:18px 20px;color:var(--ink);text-decoration:none}
.card:hover{border-color:var(--accent-fill)}
.card h2{font-size:1.15rem;margin:0 0 8px}
.card p{margin:0;color:var(--ink-2);font-size:.95rem}
.repo{margin-top:40px;font-family:var(--sans);font-size:14px;color:var(--ink-3)}
</style>"""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        "<title>HearAble</title>\n" + "\n".join(fonts) + f"\n{style}\n{extra}\n{BAR_STYLE}\n"
        f"</head>\n<body>\n{bar(None)}\n{body}\n</body>\n</html>\n"
    )


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "_site")
    out.mkdir(parents=True, exist_ok=True)
    for source, (name, lang, _) in PAGES.items():
        (out / name).write_text(wrap((HERE / source).read_text(), lang, name), encoding="utf-8")
        print(f"  {source:42s} -> {name}")
    (out / "index.html").write_text(landing(), encoding="utf-8")
    print(f"  {'(landing page)':42s} -> index.html")
    # Plain HTML: tell GitHub Pages not to run Jekyll over it.
    (out / ".nojekyll").write_text("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
