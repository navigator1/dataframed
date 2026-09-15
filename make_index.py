#!/usr/bin/env python3
"""
Bygger index.html som listar alla feed-filer i mappen med kopierbara adresser.

Körs automatiskt av GitHub Actions efter att feedarna uppdaterats, men går
lika bra att köra för hand:

  python make_index.py --base-url https://navigator1.github.io/poddar
"""

import argparse
import glob
import html
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

CSS = """
:root { color-scheme: light dark; }
body { font-family: system-ui, -apple-system, sans-serif; line-height: 1.5;
       max-width: 46rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
h1 { font-size: 1.5rem; margin-bottom: .25rem; }
p.sub { color: #666; margin-top: 0; font-size: .9rem; }
section { margin-top: 2rem; }
h2 { font-size: 1.1rem; margin-bottom: .5rem; }
ul { list-style: none; padding: 0; margin: 0; }
li { border: 1px solid #8884; border-radius: .5rem; padding: .7rem .9rem;
     margin-bottom: .5rem; }
.row { display: flex; justify-content: space-between; align-items: baseline;
       gap: 1rem; flex-wrap: wrap; }
.meta { color: #666; font-size: .85rem; }
code { background: #8881; padding: .15rem .4rem; border-radius: .3rem;
       font-size: .8rem; word-break: break-all; }
button { font: inherit; font-size: .8rem; padding: .25rem .7rem; cursor: pointer;
         border: 1px solid #8886; border-radius: .4rem; background: transparent; }
"""

JS = """
document.addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-url]');
  if (!b) return;
  try {
    await navigator.clipboard.writeText(b.dataset.url);
    const t = b.textContent; b.textContent = 'Kopierad';
    setTimeout(() => b.textContent = t, 1200);
  } catch { location.href = b.dataset.url; }
});
"""


def feed_info(path):
    try:
        channel = ET.parse(path).getroot().find("channel")
    except Exception:
        return None
    items = channel.findall("item")
    dates = []
    for i in items:
        txt = i.findtext("pubDate")
        if not txt:
            continue
        try:
            d = parsedate_to_datetime(txt)
            dates.append(d if d.tzinfo else d.replace(tzinfo=timezone.utc))
        except Exception:
            pass
    return {
        "file": os.path.basename(path),
        "title": channel.findtext("title") or os.path.basename(path),
        "count": len(items),
        "first": min(dates).date() if dates else None,
        "last": max(dates).date() if dates else None,
    }


def group_key(filename):
    """rb-001.xml och rb-002.xml hör ihop; sed.xml står för sig själv."""
    stem = os.path.splitext(filename)[0]
    return stem.rsplit("-", 1)[0] if stem.rsplit("-", 1)[-1].isdigit() else stem


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="", help="t.ex. https://namn.github.io/repo")
    p.add_argument("--dir", default=".")
    p.add_argument("-o", "--output", default="index.html")
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    feeds = []
    for path in sorted(glob.glob(os.path.join(args.dir, "*.xml"))):
        info = feed_info(path)
        if info and info["count"]:
            feeds.append(info)
    if not feeds:
        print("Hittade inga feed-filer.")
        return

    groups = {}
    for f in feeds:
        groups.setdefault(group_key(f["file"]), []).append(f)

    out = ["<!doctype html><html lang=sv><meta charset=utf-8>",
           "<meta name=viewport content='width=device-width,initial-scale=1'>",
           "<title>Podcast-arkiv</title>", f"<style>{CSS}</style>",
           "<h1>Podcast-arkiv</h1>",
           "<p class=sub>Kompletta feeds, äldsta avsnittet först. "
           "Klistra in adressen i din poddapp.</p>"]

    for key in sorted(groups):
        parts = sorted(groups[key], key=lambda f: f["file"])
        total = sum(f["count"] for f in parts)
        name = html.escape(parts[0]["title"].split(" [")[0])
        out.append(f"<section><h2>{name}</h2>")
        out.append(f"<p class=meta>{total} avsnitt i {len(parts)} "
                   f"{'del' if len(parts) == 1 else 'delar'}</p><ul>")
        for f in parts:
            url = f"{base}/{f['file']}" if base else f["file"]
            span = (f"{f['first']} – {f['last']}" if f["first"] else "okänt spann")
            out.append(
                "<li><div class=row>"
                f"<strong>{html.escape(f['file'])}</strong>"
                f"<button data-url=\"{html.escape(url)}\">Kopiera</button></div>"
                f"<div class=meta>{f['count']} avsnitt · {span}</div>"
                f"<code>{html.escape(url)}</code></li>")
        out.append("</ul></section>")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out.append(f"<p class=sub>Uppdaterad {stamp}</p>")
    out.append(f"<script>{JS}</script></html>")

    with open(os.path.join(args.dir, args.output), "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"Skrev {args.output}: {len(groups)} poddar, "
          f"{sum(f['count'] for f in feeds)} avsnitt")


if __name__ == "__main__":
    main()
