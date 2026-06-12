#!/usr/bin/env python3
"""Render docs/USER_GUIDE.md to a self-contained PDF with the screenshots
embedded, for handing out.

  python tools/build_guide_pdf.py

Needs: python-markdown and WeasyPrint (pip install markdown weasyprint).
"""
import os
import re

import markdown
from weasyprint import HTML

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
SHOTS = os.path.join(DOCS, "screenshots")
SRC = os.path.join(DOCS, "USER_GUIDE.md")
PDF = os.path.join(DOCS, "USER_GUIDE.pdf")

CSS = """
@page { size: A4; margin: 16mm 15mm;
        @bottom-center { content: counter(page) " / " counter(pages);
                         font-size: 8pt; color: #90a4ae; } }
body { font-family: 'Liberation Sans', 'DejaVu Sans', sans-serif;
       font-size: 10.5pt; color: #1a1a1a; line-height: 1.45; }
h1 { font-size: 22pt; color: #102a43; border-bottom: 3px solid #2e7d32;
     padding-bottom: 6px; }
h2 { font-size: 15pt; color: #102a43; margin-top: 22px;
     border-bottom: 1px solid #cfd8dc; padding-bottom: 3px;
     page-break-after: avoid; }
h3 { font-size: 12.5pt; color: #243b53; margin-top: 16px;
     page-break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 10px 0;
        page-break-inside: avoid; }
th, td { border: 1px solid #b0bec5; padding: 5px 8px; text-align: left;
         vertical-align: top; font-size: 9.5pt; }
th { background: #eceff1; }
code { font-family: 'Liberation Mono', 'DejaVu Sans Mono', monospace;
       background: #f2f4f6; padding: 1px 4px; border-radius: 3px;
       font-size: 9pt; }
pre { background: #f2f4f6; border: 1px solid #d0d7de; border-radius: 5px;
      padding: 10px; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #90a4ae; background: #f6f8fa;
             margin: 10px 0; padding: 6px 12px; color: #37474f; }
figure { margin: 14px 0; text-align: center; page-break-inside: avoid; }
figure img { max-width: 100%; border: 1px solid #b0bec5; border-radius: 4px; }
figcaption { font-size: 9pt; color: #607d8b; margin-top: 4px;
             font-style: italic; }
"""

# WeasyPrint has no colour-emoji font, so swap the legend emoji for coloured
# bullets and drop the pause glyph.
GLYPHS = {
    "\U0001F7E2": '<span style="color:#2e7d32">●</span>',  # green
    "\U0001F7E0": '<span style="color:#e08a00">●</span>',  # amber
    "\U0001F534": '<span style="color:#c62828">●</span>',  # red
    "❚❚ ": "",                                          # pause bars
}


def replace_screenshots(text):
    """Turn '*(Screenshot: `file.png` - caption)*' markers into <figure>s."""
    pat = re.compile(r"\*\(Screenshot:\s*`([^`]+)`\s*[—-]\s*(.*?)\)\*",
                     re.DOTALL)

    def sub(m):
        fname = m.group(1).strip()
        caption = " ".join(m.group(2).split())
        src = os.path.join(SHOTS, fname)
        return (f'<figure><img src="{src}" />'
                f'<figcaption>{caption}</figcaption></figure>')

    return pat.sub(sub, text)


def main():
    with open(SRC, encoding="utf-8") as f:
        text = f.read()
    text = replace_screenshots(text)
    body = markdown.markdown(
        text, extensions=["tables", "fenced_code", "sane_lists"])
    for bad, good in GLYPHS.items():
        body = body.replace(bad, good)
    html = (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<style>{CSS}</style></head><body>{body}</body></html>")
    HTML(string=html, base_url=DOCS).write_pdf(PDF)
    print("wrote", PDF, f"({os.path.getsize(PDF)} bytes)")


if __name__ == "__main__":
    main()
