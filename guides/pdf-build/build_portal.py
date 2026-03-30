#!/usr/bin/env python3
"""
Build a single-page HTML documentation portal for all SONiC Wedge 100S-32X guides.

Usage:
    python3 build_portal.py

Output: notes/index.html  (place alongside the .pdf files)
"""

import subprocess
import sys
import re
import shutil
import unicodedata
import base64
import tarfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
NOTES_DIR  = SCRIPT_DIR.parent
OUT_FILE   = NOTES_DIR / "index.html"

_logo_dark  = NOTES_DIR / "flaxlogo_SD_Blue dark_BG.png"
_logo_light = NOTES_DIR / "flaxlogo_SD_Blue.png"
LOGO_DARK_URI  = ("data:image/png;base64," + base64.b64encode(_logo_dark.read_bytes()).decode()  if _logo_dark.exists()  else "")
LOGO_LIGHT_URI = ("data:image/png;base64," + base64.b64encode(_logo_light.read_bytes()).decode() if _logo_light.exists() else "")

RELEASE  = "wedge100s-2026.04.20"
PLATFORM = "Accton Wedge 100S-32X"
COMPANY  = "Flax Advisors, LLC"
ASIC     = "Broadcom BCM56960 Tomahawk"

GUIDES = [
    dict(key="initial", title="Initial Setup Guide",       short="Initial Setup",
         src=NOTES_DIR / "SONiC-wedge100s-Initial-Setup-Guide.md",
         pdf="SONiC-wedge100s-Initial-Setup-Guide.pdf"),
    dict(key="optics",  title="Optics Setup Guide",        short="Optics",
         src=NOTES_DIR / "SONiC-wedge100s-Optics-Setup-Guide.md",
         pdf="SONiC-wedge100s-Optics-Setup-Guide.pdf"),
    dict(key="l2",      title="Layer-2 Switch Guide",      short="Layer-2",
         src=NOTES_DIR / "SONiC-wedge100s-L2-Setup-Guide.md",
         pdf="SONiC-wedge100s-L2-Setup-Guide.pdf"),
    dict(key="dev",     title="Developer's Guide",         short="Developer",
         src=NOTES_DIR / "SONiC-wedge100s-Developers-Guide.md",
         pdf="SONiC-wedge100s-Developers-Guide.pdf"),
    dict(key="bmc",     title="OpenBMC Comms Guide",       short="OpenBMC",
         src=NOTES_DIR / "SONiC-wedge100s-openBMC-comms-Guide.md",
         pdf="SONiC-wedge100s-openBMC-comms-Guide.pdf"),
]


# ── Slug (must match pandoc's heading-id algorithm + our prefix) ──────────────
def slugify(text: str) -> str:
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def guide_slug(key: str, text: str) -> str:
    return f"{key}-{slugify(text)}"


# ── Extract headings for sidebar nav ─────────────────────────────────────────
def extract_headings(md_text: str, key: str) -> list:
    """Return list of (level, title, anchor) for h1/h2 headings only."""
    entries = []
    in_fence = False
    slug_counts: dict = {}

    for line in md_text.splitlines():
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r'^(#{1,2})\s+(.*)', line)
        if not m:
            continue
        level = len(m.group(1))
        title = m.group(2).strip()
        base  = slugify(title)
        count = slug_counts.get(base, 0)
        slug_counts[base] = count + 1
        anchor = f"{key}-{base}" if count == 0 else f"{key}-{base}-{count}"
        entries.append((level, title, anchor))

    return entries


# ── Markdown pre-processing (no admonition wrapping needed for portal) ────────
def preprocess_markdown(text: str) -> str:
    return text


# ── HTML post-processing ──────────────────────────────────────────────────────
def postprocess_html(html: str, key: str) -> str:
    # Embed the light logo as data URI (content area is on white background)
    if LOGO_LIGHT_URI:
        html = html.replace('src="flaxlogo_SD_Blue.png"', f'src="{LOGO_LIGHT_URI}"')

    # colour code comments (! and # lines inside <pre>)
    def colour_code_lines(m):
        block = m.group(0)
        block = re.sub(
            r'(^|(?<=\n))(! .+)',
            r'\1<span class="code-comment">\2</span>', block)
        block = re.sub(
            r'(^|(?<=\n))(# .+)',
            r'\1<span class="code-comment">\2</span>', block)
        return block

    html = re.sub(r'<pre><code[^>]*>.*?</code></pre>',
                  colour_code_lines, html, flags=re.DOTALL)

    # fix heading ids — pandoc with --id-prefix=KEY- already stamps them;
    # but ensure any that slipped through get an id derived from the prefix
    slug_counts: dict = {}

    def heading_with_id(m):
        full  = m.group(0)
        tag   = m.group(1)
        attrs = m.group(2) or ""
        inner = m.group(3)
        close = m.group(4)
        if 'id=' in attrs:
            return full
        plain = re.sub(r'<[^>]+>', '', inner)
        base  = slugify(plain)
        count = slug_counts.get(base, 0)
        slug_counts[base] = count + 1
        slug = f"{key}-{base}" if count == 0 else f"{key}-{base}-{count}"
        return f'<{tag} id="{slug}"{attrs}>{inner}{close}'

    html = re.sub(r'<(h[1-4])([^>]*)>(.*?)(</h[1-4]>)',
                  heading_with_id, html, flags=re.DOTALL)

    # Detect Warning/Caution blockquotes by their first <strong> word and add class.
    # This avoids any div-wrapping that could bleed through unrelated HTML.
    def tag_admonition(m):
        inner = m.group(1)
        if re.search(r'<strong>[^<]*Warning', inner):
            return f'<blockquote class="admonition warning">{inner}</blockquote>'
        if re.search(r'<strong>[^<]*Caution', inner):
            return f'<blockquote class="admonition caution">{inner}</blockquote>'
        return m.group(0)

    html = re.sub(r'<blockquote>(.*?)</blockquote>', tag_admonition, html, flags=re.DOTALL)

    return html


# ── Build one guide's HTML body ───────────────────────────────────────────────
def render_guide(guide: dict) -> str:
    key     = guide["key"]
    src     = guide["src"]
    md_text = src.read_text(encoding="utf-8")
    md_text = preprocess_markdown(md_text)

    result = subprocess.run(
        ["pandoc", "--from=markdown+smart", "--to=html5",
         "--no-highlight", "--wrap=none", f"--id-prefix={key}-"],
        input=md_text.encode("utf-8"),
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"pandoc error ({key}):", result.stderr.decode())
        sys.exit(1)

    body = result.stdout.decode("utf-8")
    body = postprocess_html(body, key)
    return body


# ── Sidebar HTML ──────────────────────────────────────────────────────────────
def build_sidebar(all_headings: list) -> str:
    parts = ['<nav class="sidebar" id="sidebar">']
    parts.append('<div class="sidebar-inner">')

    for guide, headings in all_headings:
        key = guide["key"]
        # first h1 anchor is the guide section jump target
        first_anchor = headings[0][2] if headings else f"{key}-"
        parts.append(f'<div class="nav-group" data-guide="{key}">')
        parts.append(
            f'<a class="nav-guide-title" href="#{first_anchor}" '
            f'data-guide="{key}">{guide["title"]}</a>'
        )
        parts.append(f'<div class="nav-entries" id="nav-{key}">')
        for level, title, anchor in headings:
            cls = "nav-h1" if level == 1 else "nav-h2"
            parts.append(
                f'<a class="nav-link {cls}" href="#{anchor}" '
                f'data-guide="{key}">{title}</a>'
            )
        parts.append('</div></div>')  # nav-entries, nav-group

    parts.append('</div></nav>')
    return "\n".join(parts)


# ── Full page HTML ────────────────────────────────────────────────────────────
def build_page(sidebar_html: str, sections_html: str) -> str:
    pdf_map_js = "{" + ", ".join(
        f'"{g["key"]}": "{g["pdf"]}"' for g in GUIDES
    ) + "}"
    guide_names_js = "{" + ", ".join(
        f'"{g["key"]}": "{g["title"]}"' for g in GUIDES
    ) + "}"
    first_pdf = GUIDES[0]["pdf"]
    first_title = GUIDES[0]["title"]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SONiC {PLATFORM} Documentation — {COMPANY}</title>
<style>
/* ── Reset ───────────────────────────────────────────────── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

/* ── Fonts ───────────────────────────────────────────────── */
:root {{
  --font-sans: 'DejaVu Sans', 'Segoe UI', Arial, Helvetica, sans-serif;
  --font-mono: 'DejaVu Sans Mono', 'Cascadia Code', 'Consolas', 'Courier New', monospace;
  --bar-h: 48px;
  --sidebar-w: 268px;
  --blue-dark: #003087;
  --blue-mid:  #1e3a5f;
  --blue-acc:  #049fd9;
  --blue-bright: #1d4ed8;
  --sidebar-bg: #0f172a;
  --sidebar-text: #94a3b8;
  --sidebar-active: #f1f5f9;
  --sidebar-guide: #049fd9;
  --body-text: #1f2937;
  --code-bg: #f8fafc;
  --code-border: #cbd5e1;
}}

/* ── Top bar ─────────────────────────────────────────────── */
.top-bar {{
  position: fixed; top: 0; left: 0; right: 0; height: var(--bar-h);
  background: var(--blue-dark);
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 20px 0 16px;
  z-index: 1000;
  font-family: var(--font-sans);
  box-shadow: 0 2px 8px rgba(0,0,0,0.35);
}}

.top-bar-brand {{
  display: flex; align-items: center; gap: 12px;
  color: #ffffff;
}}

.top-bar-logo {{
  height: 28px;
  width: auto;
  display: block;
  flex-shrink: 0;
}}

.top-bar-company {{
  font-size: 16px; font-weight: 700; color: #ffffff;
  letter-spacing: 0.01em;
}}

.top-bar-sep {{ color: rgba(255,255,255,0.35); font-size: 15px; }}

.top-bar-product {{
  font-size: 15px; color: rgba(255,255,255,0.82);
}}

.top-bar-release {{
  font-size: 13px; color: var(--blue-acc);
  font-weight: 600; letter-spacing: 0.08em;
  background: rgba(4,159,217,0.15);
  border: 1px solid rgba(4,159,217,0.35);
  border-radius: 3px; padding: 2px 7px;
  margin-left: 4px;
}}

.pdf-btn {{
  display: flex; align-items: center; gap: 7px;
  background: rgba(255,255,255,0.10);
  border: 1px solid rgba(255,255,255,0.25);
  border-radius: 4px;
  color: #ffffff;
  font-family: var(--font-sans);
  font-size: 14px; font-weight: 600;
  padding: 6px 13px;
  text-decoration: none;
  transition: background 0.15s, border-color 0.15s;
  white-space: nowrap;
}}

.pdf-btn:hover {{
  background: rgba(255,255,255,0.20);
  border-color: rgba(255,255,255,0.45);
}}

.pdf-btn-icon {{ font-size: 16px; }}

.pdf-btn-label {{ opacity: 0.7; font-weight: 400; margin-left: 2px; }}

/* ── Layout ──────────────────────────────────────────────── */
.layout {{
  display: flex;
  margin-top: var(--bar-h);
  min-height: calc(100vh - var(--bar-h));
}}

/* ── Sidebar ─────────────────────────────────────────────── */
.sidebar {{
  position: fixed;
  top: var(--bar-h);
  left: 0;
  width: var(--sidebar-w);
  height: calc(100vh - var(--bar-h));
  overflow-y: auto;
  overflow-x: hidden;
  background: var(--sidebar-bg);
  scrollbar-width: thin;
  scrollbar-color: #1e3a5f transparent;
  z-index: 100;
}}

.sidebar::-webkit-scrollbar {{ width: 4px; }}
.sidebar::-webkit-scrollbar-thumb {{ background: #1e3a5f; border-radius: 2px; }}

.sidebar-inner {{
  padding: 12px 0 40px 0;
}}

.nav-group {{
  margin-bottom: 4px;
}}

.nav-guide-title {{
  display: block;
  padding: 9px 16px 7px 16px;
  font-family: var(--font-sans);
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--sidebar-guide);
  text-decoration: none;
  cursor: pointer;
  transition: color 0.15s;
  border-left: 3px solid transparent;
}}

.nav-guide-title:hover {{ color: #7dd3fc; }}
.nav-guide-title.active {{ border-left-color: var(--blue-acc); color: #7dd3fc; }}

.nav-entries {{
  overflow: hidden;
  max-height: 0;
  transition: max-height 0.25s ease;
}}

.nav-group.open .nav-entries {{
  max-height: 2000px;
}}

.nav-link {{
  display: block;
  font-family: var(--font-sans);
  text-decoration: none;
  color: var(--sidebar-text);
  transition: color 0.12s, background 0.12s;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  border-left: 3px solid transparent;
}}

.nav-link:hover {{
  color: var(--sidebar-active);
  background: rgba(255,255,255,0.05);
}}

.nav-link.active {{
  color: var(--sidebar-active);
  border-left-color: var(--blue-acc);
  background: rgba(4,159,217,0.08);
}}

.nav-h1 {{
  font-size: 14px; font-weight: 600;
  padding: 5px 14px 5px 16px;
}}

.nav-h2 {{
  font-size: 13px; font-weight: 400;
  padding: 4px 14px 4px 28px;
}}

/* ── Main content ────────────────────────────────────────── */
.content {{
  margin-left: var(--sidebar-w);
  min-width: 0;
  padding: 44px 64px 80px 60px;
  max-width: calc(920px + var(--sidebar-w));
  font-family: var(--font-sans);
  font-size: 12.5pt;
  line-height: 1.6;
  color: var(--body-text);
  background: #ffffff;
}}

/* guide section divider */
.guide-section {{
  border-top: 2px solid #e5e7eb;
  padding-top: 8px;
  margin-top: 0;
}}

.guide-section:first-child {{ border-top: none; }}

/* ── Typography ──────────────────────────────────────────── */
h1 {{
  font-size: 24pt; font-weight: 700;
  color: var(--blue-dark);
  border-bottom: 3px solid var(--blue-acc);
  padding-bottom: 6pt;
  margin-top: 36pt; margin-bottom: 14pt;
  scroll-margin-top: calc(var(--bar-h) + 20px);
}}

.guide-section > h1:first-child,
.guide-section > *:first-child h1 {{
  margin-top: 8pt;
}}

h2 {{
  font-size: 16pt; font-weight: 700;
  color: var(--blue-mid);
  border-bottom: 1px solid #bfdbfe;
  padding-bottom: 3pt;
  margin-top: 22pt; margin-bottom: 9pt;
  scroll-margin-top: calc(var(--bar-h) + 16px);
}}

h3 {{
  font-size: 13.5pt; font-weight: 700;
  color: #1e40af;
  margin-top: 16pt; margin-bottom: 6pt;
  scroll-margin-top: calc(var(--bar-h) + 16px);
}}

h4 {{
  font-size: 12.5pt; font-weight: 700;
  color: #374151;
  margin-top: 11pt; margin-bottom: 4pt;
  scroll-margin-top: calc(var(--bar-h) + 16px);
}}

p {{ margin: 0 0 8pt 0; }}

a {{ color: var(--blue-bright); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}

strong {{ font-weight: 700; color: #111827; }}

/* ── Code ────────────────────────────────────────────────── */
pre {{
  background: var(--code-bg);
  border: 1px solid var(--code-border);
  border-left: 4px solid var(--blue-acc);
  border-radius: 4px;
  padding: 10pt 13pt;
  margin: 8pt 0 13pt 0;
  font-family: var(--font-mono);
  font-size: 10.5pt;
  line-height: 1.5;
  overflow-x: auto;
  white-space: pre;
}}

code {{
  font-family: var(--font-mono);
  font-size: 10.5pt;
  background: #f1f5f9;
  border: 1px solid #e2e8f0;
  border-radius: 3px;
  padding: 1pt 4pt;
  color: #0f172a;
}}

pre code {{
  background: none; border: none; padding: 0;
  font-size: inherit; color: inherit;
}}

.code-comment {{
  color: #6b7280; font-style: italic;
}}

/* ── Tables ──────────────────────────────────────────────── */
table {{
  border-collapse: collapse;
  width: 100%;
  margin: 8pt 0 14pt 0;
  font-size: 11.5pt;
}}

thead tr {{ background: var(--blue-mid); color: #ffffff; }}
thead th {{
  padding: 6pt 9pt; text-align: left;
  font-weight: 600; font-size: 10.5pt;
  letter-spacing: 0.03em;
  border: 1px solid var(--blue-mid);
}}

tbody tr:nth-child(even) {{ background: #f0f7ff; }}
tbody tr:nth-child(odd)  {{ background: #ffffff; }}
tbody td {{
  padding: 5pt 9pt;
  border: 1px solid #d1d5db;
  vertical-align: top;
  line-height: 1.45;
}}

/* ── Blockquotes ─────────────────────────────────────────── */
blockquote {{
  margin: 10pt 0 12pt 0;
  padding: 8pt 12pt 8pt 14pt;
  border-radius: 3px;
  background: #eff6ff;
  border-left: 4px solid #3b82f6;
  color: var(--blue-mid);
}}

blockquote p:first-child::before {{
  content: "Note  ";
  font-weight: 700;
  color: var(--blue-bright);
  font-size: 11pt;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}

blockquote.admonition.warning {{
  background: #fef2f2;
  border-left-color: #dc2626;
  color: #7f1d1d;
}}

blockquote.admonition.caution {{
  background: #fffbeb;
  border-left-color: #d97706;
  color: #78350f;
}}

blockquote p {{ margin: 0 0 4pt 0; }}
blockquote p:last-child {{ margin-bottom: 0; }}

/* ── Lists ───────────────────────────────────────────────── */
ul, ol {{ margin: 4pt 0 8pt 0; padding-left: 20pt; }}
li {{ margin-bottom: 3pt; line-height: 1.5; }}
li > ul, li > ol {{ margin: 2pt 0; }}

hr {{ border: none; border-top: 1px solid #e5e7eb; margin: 16pt 0; }}
img {{ max-width: 100%; }}

/* ── Chapter label span (from build_pdf.py chapter_label) ── */
.chapter-label {{
  font-size: 11pt; font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--blue-acc);
  display: block; margin-bottom: 2pt;
}}
</style>
</head>
<body>

<!-- ── Top bar ──────────────────────────────────────────── -->
<header class="top-bar">
  <div class="top-bar-brand">
    {'<img class="top-bar-logo" src="' + LOGO_DARK_URI + '" alt="Flax Advisors, LLC">' if LOGO_DARK_URI else ''}
    <span class="top-bar-company">{COMPANY}</span>
    <span class="top-bar-sep">·</span>
    <span class="top-bar-product">SONiC {PLATFORM}</span>
    <span class="top-bar-release">{RELEASE}</span>
  </div>
  <a id="pdf-btn" class="pdf-btn" href="{first_pdf}" download>
    <span class="pdf-btn-icon">&#8595;</span>
    Download PDF
    <span class="pdf-btn-label" id="pdf-btn-label">{first_title}</span>
  </a>
</header>

<!-- ── Layout ───────────────────────────────────────────── -->
<div class="layout">

{sidebar_html}

<main class="content" id="main-content">
{sections_html}
</main>

</div><!-- .layout -->

<script>
(function() {{
  const pdfMap   = {pdf_map_js};
  const nameMap  = {guide_names_js};

  const pdfBtn   = document.getElementById('pdf-btn');
  const pdfLabel = document.getElementById('pdf-btn-label');
  let   activeGuide = '{GUIDES[0]["key"]}';

  // ── Sidebar open/close ──────────────────────────────────
  function openGuide(key) {{
    document.querySelectorAll('.nav-group').forEach(g => {{
      const isTarget = g.dataset.guide === key;
      g.classList.toggle('open', isTarget);
    }});
    document.querySelectorAll('.nav-guide-title').forEach(a => {{
      a.classList.toggle('active', a.dataset.guide === key);
    }});
  }}

  // ── PDF button update ───────────────────────────────────
  function activateGuide(key) {{
    if (key === activeGuide) return;
    activeGuide = key;
    pdfBtn.href = pdfMap[key];
    pdfBtn.setAttribute('download', pdfMap[key]);
    pdfLabel.textContent = nameMap[key];
    openGuide(key);
  }}

  // ── Sidebar nav-link click ──────────────────────────────
  document.querySelectorAll('.nav-link, .nav-guide-title').forEach(a => {{
    a.addEventListener('click', () => {{
      const key = a.dataset.guide;
      if (key) activateGuide(key);
    }});
  }});

  // ── Scroll-spy via IntersectionObserver ────────────────
  // Trigger when heading enters top 30% of viewport
  const observer = new IntersectionObserver(entries => {{
    entries.forEach(entry => {{
      if (!entry.isIntersecting) return;
      const id = entry.target.id;
      if (!id) return;

      // find guide from id prefix
      const key = Object.keys(pdfMap).find(k => id.startsWith(k + '-'));
      if (key) activateGuide(key);

      // active nav link
      document.querySelectorAll('.nav-link.active').forEach(el =>
        el.classList.remove('active'));
      const link = document.querySelector(`.nav-link[href="#${{id}}"]`);
      if (link) {{
        link.classList.add('active');
        // scroll sidebar to keep active link visible
        const sidebar = document.getElementById('sidebar');
        const linkTop = link.getBoundingClientRect().top;
        const sideTop = sidebar.getBoundingClientRect().top;
        const sideBot = sidebar.getBoundingClientRect().bottom;
        if (linkTop < sideTop + 40 || linkTop > sideBot - 40) {{
          link.scrollIntoView({{ block: 'nearest' }});
        }}
      }}
    }});
  }}, {{ rootMargin: '-8% 0px -72% 0px', threshold: 0 }});

  document.querySelectorAll('h1[id], h2[id], h3[id]').forEach(h =>
    observer.observe(h));

  // ── Open first guide on load ────────────────────────────
  openGuide('{GUIDES[0]["key"]}');

}})();
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    for tool in ("pandoc",):
        if not shutil.which(tool):
            print(f"ERROR: '{tool}' not found.")
            sys.exit(1)

    all_headings = []
    all_sections = []

    for guide in GUIDES:
        if not guide["src"].exists():
            print(f"SKIP: {guide['src'].name} not found")
            continue

        print(f"[{guide['key']}] rendering …")
        md_text  = guide["src"].read_text(encoding="utf-8")
        headings = extract_headings(md_text, guide["key"])
        body_html = render_guide(guide)

        all_headings.append((guide, headings))
        all_sections.append(
            f'<section class="guide-section" data-guide="{guide["key"]}" '
            f'data-pdf="{guide["pdf"]}">\n{body_html}\n</section>'
        )

    sidebar_html  = build_sidebar(all_headings)
    sections_html = "\n\n".join(all_sections)
    page_html     = build_page(sidebar_html, sections_html)

    OUT_FILE.write_text(page_html, encoding="utf-8")
    size_kb = OUT_FILE.stat().st_size // 1024
    print(f"\n✓  {OUT_FILE.name}  ({size_kb} KB)")

    # ── Bundle ────────────────────────────────────────────────
    BUNDLE_DIR  = "SONiC-wedge100s"
    bundle_path = NOTES_DIR / f"{BUNDLE_DIR}.tar.gz"

    bundle_files = (
        sorted(NOTES_DIR.glob("SONiC-wedge100s-*.md")) +
        sorted(NOTES_DIR.glob("SONiC-wedge100s-*.pdf")) +
        [NOTES_DIR / "index.html"] +
        [p for p in [
            NOTES_DIR / "flaxlogo_SD_Blue.png",
            NOTES_DIR / "flaxlogo_SD_Blue dark_BG.png",
        ] if p.exists()]
    )

    print(f"\nBundling → {bundle_path.name}")
    with tarfile.open(bundle_path, "w:gz") as tar:
        for f in bundle_files:
            if f.exists():
                tar.add(f, arcname=f"{BUNDLE_DIR}/{f.name}")
                print(f"   + {f.name}")

    bundle_kb = bundle_path.stat().st_size // 1024
    print(f"\n✓  {bundle_path.name}  ({bundle_kb} KB)")


if __name__ == "__main__":
    main()
