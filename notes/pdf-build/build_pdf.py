#!/usr/bin/env python3
"""
Generic Cisco IOS-style guide PDF builder.

Usage:
    python3 build_pdf.py [guide_key]

    guide_key: initial (default) | optics | l2

Each guide is defined in GUIDES dict below.
"""

import subprocess
import sys
import re
import shutil
import unicodedata
import base64
from pathlib import Path
from datetime import date

SCRIPT_DIR = Path(__file__).resolve().parent
NOTES_DIR  = SCRIPT_DIR.parent
CSS_FILE   = SCRIPT_DIR / "cisco-style.css"

_logo_dark  = NOTES_DIR / "flaxlogo_SD_Blue dark_BG.png"
_logo_light = NOTES_DIR / "flaxlogo_SD_Blue.png"
LOGO_DARK_URI  = ("data:image/png;base64," + base64.b64encode(_logo_dark.read_bytes()).decode()  if _logo_dark.exists()  else "")
LOGO_LIGHT_URI = ("data:image/png;base64," + base64.b64encode(_logo_light.read_bytes()).decode() if _logo_light.exists() else "")

TODAY = date.today().strftime("%B %d, %Y")

RELEASE  = "wedge100s-2026.04.20"
PLATFORM = "x86_64-accton_wedge100s_32x-r0"
HWSKU    = "Accton-WEDGE100S-32X"
ASIC     = "Broadcom BCM56960 Tomahawk &nbsp;&middot;&nbsp; 32 &times; QSFP28 100G"
COMPANY  = "Flax Advisors, LLC"

# ─────────────────────────────────────────────────────────────
# Guide definitions
# ─────────────────────────────────────────────────────────────
GUIDES = {
    "initial": {
        "src":      NOTES_DIR / "SONiC-wedge100s-Initial-Setup-Guide.md",
        "out":      NOTES_DIR / "SONiC-wedge100s-Initial-Setup-Guide.pdf",
        "title":    "Initial Setup Guide",
        "subtitle": "Accton Wedge 100S-32X &nbsp;&middot;&nbsp; Broadcom Tomahawk BCM56960",
        "abstract": (
            "This guide covers initial deployment of the Accton Wedge 100S-32X running SONiC — "
            "from power-on through first login, Zero Touch Provisioning, management interface "
            "configuration, and interface commands. Written for operators familiar with "
            "Cisco IOS or Arista EOS who are new to SONiC."
        ),
        "header":   "SONiC Wedge 100S-32X Initial Setup Guide",
    },
    "optics": {
        "src":      NOTES_DIR / "SONiC-wedge100s-Optics-Setup-Guide.md",
        "out":      NOTES_DIR / "SONiC-wedge100s-Optics-Setup-Guide.pdf",
        "title":    "Optics Setup Guide",
        "subtitle": "Accton Wedge 100S-32X &nbsp;&middot;&nbsp; QSFP28 100G Transceivers",
        "abstract": (
            "Reference guide for optical transceiver configuration, DOM monitoring, "
            "FEC selection, and link bring-up on the Accton Wedge 100S-32X running SONiC. "
            "Covers SR4, LR4, CWDM4, and PSM4 module types."
        ),
        "header":   "SONiC Wedge 100S-32X Optics Setup Guide",
    },
    "l2": {
        "src":      NOTES_DIR / "SONiC-wedge100s-L2-Setup-Guide.md",
        "out":      NOTES_DIR / "SONiC-wedge100s-L2-Setup-Guide.pdf",
        "title":    "Layer-2 Switch Setup Guide",
        "subtitle": "Accton Wedge 100S-32X &nbsp;&middot;&nbsp; L2 / ToR Deployment",
        "abstract": (
            "How to configure the Accton Wedge 100S-32X as a pure Layer-2 switch under SONiC. "
            "Covers VLAN configuration, spanning tree, management VRF, and restoring L3 config."
        ),
        "header":   "SONiC Wedge 100S-32X L2 Setup Guide",
    },
    "dev": {
        "src":      NOTES_DIR / "SONiC-wedge100s-Developers-Guide.md",
        "out":      NOTES_DIR / "SONiC-wedge100s-Developers-Guide.pdf",
        "title":    "Developer's Guide",
        "subtitle": "Accton Wedge 100S-32X &nbsp;&middot;&nbsp; Build System &amp; Platform Internals",
        "abstract": (
            "Reference guide for developers porting or extending SONiC on the Accton Wedge 100S-32X. "
            "Covers the three-layer build pipeline (Makefile / slave.mk / WeasyPrint), git submodule "
            "workflow, quilt patch management, platform module architecture, I2C daemon design, "
            "and the OpenBMC communication layer."
        ),
        "header":   "SONiC Wedge 100S-32X Developer's Guide",
    },
    "bmc": {
        "src":      NOTES_DIR / "SONiC-wedge100s-openBMC-comms-Guide.md",
        "out":      NOTES_DIR / "SONiC-wedge100s-openBMC-comms-Guide.pdf",
        "title":    "OpenBMC Communications Guide",
        "subtitle": "Accton Wedge 100S-32X &nbsp;&middot;&nbsp; Host &harr; BMC Interface",
        "abstract": (
            "Documents the two host&harr;BMC communication paths on the Accton Wedge 100S-32X: "
            "the USB CDC ACM serial console (/dev/ttyACM0) used for bootstrap key provisioning, "
            "and the runtime SSH-over-USB-CDC-Ethernet path (root@fe80::ff:fe00:1%usb0). "
            "Covers the /run/wedge100s/ file interface, daemon architecture, and thermal/fan/PSU "
            "sensor access patterns."
        ),
        "header":   "SONiC Wedge 100S-32X OpenBMC Communications Guide",
    },
}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def slugify(text: str) -> str:
    """Turn a heading title into a URL-safe anchor id."""
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def make_cover(guide: dict) -> str:
    logo_html = (
        f'<img src="{LOGO_DARK_URI}" alt="Flax Advisors, LLC"'
        f' style="height:54pt;display:block;margin-bottom:18pt">'
        if LOGO_DARK_URI else ""
    )
    return f"""
<div class="cover-page">
  <div class="cover-banner">
    {logo_html}
    <div class="cover-cisco-wordmark">&#9632; SONiC</div>
    <div class="cover-cisco-tagline">Open Network Operating System &nbsp;&middot;&nbsp; Wedge 100S-32X</div>
    <div class="cover-title">{guide['title']}</div>
    <div class="cover-subtitle">{guide['subtitle']}</div>
  </div>
  <div class="cover-body">
    <div class="cover-meta">
      <p><span class="label">Release</span>{RELEASE}</p>
      <p><span class="label">Platform</span>{PLATFORM}</p>
      <p><span class="label">HwSKU</span>{HWSKU}</p>
      <p><span class="label">ASIC</span>{ASIC}</p>
      <p><span class="label">Revised</span>{TODAY}</p>
    </div>
    <p class="cover-abstract">{guide['abstract']}</p>
  </div>
  <div class="cover-footer">
    <span class="cover-footer-left">{COMPANY} &nbsp;&middot;&nbsp; For internal use</span>
    <span class="cover-footer-right">Verified on hardware &nbsp;&middot;&nbsp; {TODAY}</span>
  </div>
</div>
"""


# ─────────────────────────────────────────────────────────────
# TOC — with hyperlinks to heading anchors
# ─────────────────────────────────────────────────────────────
def build_toc(md_text: str) -> str:
    entries = []
    in_fence = False
    slug_counts: dict = {}

    for line in md_text.splitlines():
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r'^(#{1,3})\s+(.*)', line)
        if not m:
            continue
        level = len(m.group(1))
        title = m.group(2).strip()
        # deduplicate slugs (pandoc does the same)
        base = slugify(title)
        count = slug_counts.get(base, 0)
        slug_counts[base] = count + 1
        slug = base if count == 0 else f"{base}-{count}"

        if level == 1:
            css = "toc-entry-h1"
        elif level == 2:
            css = "toc-entry-h2"
        else:
            css = "toc-entry-h3"
        entries.append((css, title, slug))

    rows = []
    for css, title, slug in entries:
        rows.append(
            f'<div class="{css}">'
            f'<a class="toc-link" href="#{slug}">{title}</a>'
            f'<span class="toc-dots"></span>'
            f'</div>'
        )

    return (
        '<div class="toc-section">\n'
        '<h1 id="contents" style="page-break-before:avoid">Contents</h1>\n'
        + "\n".join(rows)
        + "\n</div>\n"
    )


# ─────────────────────────────────────────────────────────────
# Markdown pre-processing
# ─────────────────────────────────────────────────────────────
def preprocess_markdown(text: str) -> str:
    """Tag Warning/Caution blockquotes."""
    lines = text.splitlines(keepends=True)
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r'^> \*\*Warning', line):
            out.append('<div class="admonition warning">\n\n')
            out.append(line)
            i += 1
            while i < len(lines) and lines[i].startswith('>'):
                out.append(lines[i])
                i += 1
            out.append('\n</div>\n')
            continue
        if re.match(r'^> \*\*Caution', line):
            out.append('<div class="admonition caution">\n\n')
            out.append(line)
            i += 1
            while i < len(lines) and lines[i].startswith('>'):
                out.append(lines[i])
                i += 1
            out.append('\n</div>\n')
            continue
        out.append(line)
        i += 1
    return "".join(out)


# ─────────────────────────────────────────────────────────────
# HTML post-processing
# ─────────────────────────────────────────────────────────────
def postprocess_html(html: str) -> str:
    """Style code comments; decorate chapter h1/h2 headings with labels and anchor ids."""
    # Embed the light logo so WeasyPrint can find it (tmp HTML is not in notes/)
    if LOGO_LIGHT_URI:
        html = html.replace('src="flaxlogo_SD_Blue.png"', f'src="{LOGO_LIGHT_URI}"')

    # ── colour code comments ──────────────────────────────────
    def colour_code_lines(m):
        block = m.group(0)
        block = re.sub(
            r'(^|(?<=\n))(! .+)',
            r'\1<span style="color:#6b7280;font-style:italic">\2</span>',
            block,
        )
        block = re.sub(
            r'(^|(?<=\n))(# .+)',
            r'\1<span style="color:#6b7280;font-style:italic">\2</span>',
            block,
        )
        return block

    html = re.sub(
        r'<pre><code[^>]*>.*?</code></pre>',
        colour_code_lines,
        html,
        flags=re.DOTALL,
    )

    # ── add id attrs to ALL headings (h1–h4) ─────────────────
    # pandoc already adds id= attrs via its --id-prefix/headings extension;
    # if they are present, leave them; otherwise derive from text.
    slug_counts: dict = {}

    def heading_with_id(m):
        full     = m.group(0)
        tag      = m.group(1)          # e.g. h1, h2, h3
        attrs    = m.group(2) or ""    # existing attrs
        inner    = m.group(3)          # heading text (may contain HTML)
        close    = m.group(4)          # </h1> etc.

        # if pandoc already stamped an id, leave it
        if 'id=' in attrs:
            return full

        plain = re.sub(r'<[^>]+>', '', inner)   # strip inner HTML for slug
        base  = slugify(plain)
        count = slug_counts.get(base, 0)
        slug_counts[base] = count + 1
        slug  = base if count == 0 else f"{base}-{count}"

        return f'<{tag} id="{slug}"{attrs}>{inner}{close}'

    html = re.sub(
        r'<(h[1-4])([^>]*)>(.*?)(</h[1-4]>)',
        heading_with_id,
        html,
        flags=re.DOTALL,
    )

    # ── Chapter h1 label spans ────────────────────────────────
    def chapter_label(m):
        full  = m.group(0)
        tag   = m.group(1)
        inner = m.group(2)
        close = m.group(3)

        chap_m = re.match(r'(Chapter\s+\d+)\s*[—–-]\s*(.*)', inner, re.DOTALL)
        if chap_m:
            label = chap_m.group(1)
            rest  = chap_m.group(2)
            # preserve existing id= attr
            id_m = re.search(r'id="([^"]+)"', tag)
            id_attr = f' id="{id_m.group(1)}"' if id_m else ''
            return (
                f'<h1{id_attr}>'
                f'<span class="chapter-label">{label}</span>'
                f'{rest}'
                f'{close}'
            )
        app_m = re.match(r'(Appendix\s+\w+)\s*[—–-]\s*(.*)', inner, re.DOTALL)
        if app_m:
            label = app_m.group(1)
            rest  = app_m.group(2)
            id_m  = re.search(r'id="([^"]+)"', tag)
            id_attr = f' id="{id_m.group(1)}"' if id_m else ''
            return (
                f'<h1 class="appendix"{id_attr}>'
                f'<span class="chapter-label">{label}</span>'
                f'{rest}'
                f'{close}'
            )
        return full

    html = re.sub(
        r'(<h1[^>]*>)(.*?)(</h1>)',
        chapter_label,
        html,
        flags=re.DOTALL,
    )

    # ── Warning/Caution divs → styled blockquotes ────────────
    html = html.replace(
        '<div class="admonition warning">',
        '<blockquote style="background:#fef2f2;border-left:4px solid #dc2626;color:#7f1d1d">',
    )
    html = html.replace(
        '<div class="admonition caution">',
        '<blockquote style="background:#fffbeb;border-left:4px solid #d97706;color:#78350f">',
    )
    # close the admonition divs (only those — not other divs)
    # pandoc does not normally emit </div> so we can safely replace all
    html = re.sub(r'\n</div>\n', '\n</blockquote>\n', html)

    return html


# ─────────────────────────────────────────────────────────────
# Full document HTML wrapper
# ─────────────────────────────────────────────────────────────
def wrap_html(css: str, cover: str, toc: str, body: str, header_text: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{header_text}</title>
<style>
{css}

/* Override @top-left with per-guide title */
@page {{ @top-left {{ content: "{header_text}"; }} }}

/* TOC link styling */
a.toc-link {{
    color: inherit;
    text-decoration: none;
}}
a.toc-link:hover {{
    text-decoration: underline;
}}
</style>
</head>
<body>
{cover}
{toc}
<div id="content">
{body}
</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
def build(guide_key: str):
    guide = GUIDES[guide_key]
    src   = guide["src"]
    out   = guide["out"]
    tmp   = SCRIPT_DIR / f"_tmp_{guide_key}.html"

    if not src.exists():
        print(f"ERROR: source not found: {src}")
        sys.exit(1)

    for tool in ("pandoc",):
        if not shutil.which(tool):
            print(f"ERROR: '{tool}' not found. Run: sudo apt-get install -y pandoc")
            sys.exit(1)
    try:
        import weasyprint  # noqa: F401
    except ImportError:
        print("ERROR: weasyprint not installed. Run: pip3 install weasyprint")
        sys.exit(1)

    print(f"[{guide_key}] Reading   {src.name}")
    md_text = src.read_text(encoding="utf-8")

    print(f"[{guide_key}] Pre-processing …")
    md_text = preprocess_markdown(md_text)

    print(f"[{guide_key}] pandoc markdown → HTML …")
    result = subprocess.run(
        ["pandoc", "--from=markdown+smart", "--to=html5",
         "--no-highlight", "--wrap=none", "--id-prefix="],
        input=md_text.encode("utf-8"),
        capture_output=True,
    )
    if result.returncode != 0:
        print("pandoc error:", result.stderr.decode())
        sys.exit(1)
    body_html = result.stdout.decode("utf-8")

    print(f"[{guide_key}] Post-processing …")
    body_html = postprocess_html(body_html)

    css_text  = CSS_FILE.read_text(encoding="utf-8")
    toc_html  = build_toc(src.read_text(encoding="utf-8"))
    cover_html = make_cover(guide)

    full_html = wrap_html(css_text, cover_html, toc_html, body_html, guide["header"])
    tmp.write_text(full_html, encoding="utf-8")
    print(f"[{guide_key}] HTML      → {tmp.name}")

    print(f"[{guide_key}] WeasyPrint → {out.name} …")
    try:
        from weasyprint import HTML
        from weasyprint.text.fonts import FontConfiguration
        font_config = FontConfiguration()
        HTML(filename=str(tmp)).write_pdf(str(out), font_config=font_config)
    except Exception as e:
        print(f"WeasyPrint error: {e}")
        sys.exit(1)

    size_kb = out.stat().st_size // 1024
    print(f"[{guide_key}] ✓  {out.name}  ({size_kb} KB)")


def main():
    keys = sys.argv[1:] if len(sys.argv) > 1 else list(GUIDES.keys())
    # validate
    for k in keys:
        if k not in GUIDES:
            print(f"Unknown guide '{k}'. Valid keys: {', '.join(GUIDES)}")
            sys.exit(1)
    for k in keys:
        build(k)


if __name__ == "__main__":
    main()
