#!/usr/bin/env python3
"""
Komplett DataFramed-feed: alla avsnitt från 2018 till idag.

Feeden hos Captivate innehåller bara de senaste ~300 avsnitten (#78+).
Avsnitt #1-77 finns bara i Wayback Machine som snapshots av Buzzsprout-
och Sounder-feedarna.

Strategi:
  1. Hämta den levande Captivate-feeden (sparar metadata i cache).
  2. Hämta gamla feed-snapshots från Wayback Machine för de avsnitt
     som saknas i Captivate (äldst = Buzzsprout-snapshots från 2018-2022,
     sedan Sounder-snapshots 2022-2023).
  3. Slå ihop, deduplicera på GUID, sortera äldst-först, dela i chunk-filer.

Kör det igen när som helst för att uppdatera - nya avsnitt i Captivate
hämtas, gamla rörss inte alls.

  python df_feed.py build --chunk 100 --output df.xml
  python df_feed.py info          # visa vad cachen innehåller
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime

CAPTIVATE = "https://feeds.captivate.fm/dataframed/"
BUZZSPROUT_CDX = (
    "https://web.archive.org/cdx/search/cdx"
    "?url=feeds.buzzsprout.com/147669.rss"
    "&output=json&fl=timestamp,statuscode"
    "&filter=statuscode:200&limit=500"
    "&from=20180101&to=20220401"
)
SOUNDER_CDX = (
    "https://web.archive.org/cdx/search/cdx"
    "?url=feeds.pod.co/dataframed/rss.xml"
    "&output=json&fl=timestamp,statuscode"
    "&filter=statuscode:200&limit=200"
    "&from=20220301&to=20230401"
)
# Alternativa Sounder-URL:er som Wayback kan ha indexerat
SOUNDER_ALTS = [
    "soundcloud.com/feeds/users/dataframed/sounds.rss",
    "dataframed.sounder.fm/rss",
    "feeds.sounder.fm/13474/rss.xml",
]

CACHE = "df_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; personal-archive-feed/1.0)"}


def get(url, timeout=60, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 403, 401):
                raise
            last = e
        except Exception as e:
            last = e
        wait = 2 ** attempt
        print(f"    försök {attempt + 1} gav {last}, väntar {wait}s",
              file=sys.stderr)
        time.sleep(wait)
    raise last


def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("items", {})
            d.setdefault("wayback_done", False)
            return d
    return {"items": {}, "wayback_done": False}


def save_cache(c):
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


def register_namespaces(raw):
    head = raw[:raw.find(">", raw.find("<rss")) + 1] if "<rss" in raw else raw[:4000]
    for prefix, uri in re.findall(r'xmlns:([A-Za-z0-9_-]+)\s*=\s*"([^"]+)"', head):
        ET.register_namespace(prefix, uri)


def item_date(item):
    txt = item.findtext("pubDate")
    if not txt:
        return None
    try:
        d = parsedate_to_datetime(txt)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def item_guid(item):
    g = item.find("guid")
    return g.text.strip() if g is not None and g.text else (
        item.findtext("link") or "")


# ---------- Wayback ----------

def cdx_timestamps(cdx_url):
    """Hämtar listan av Wayback-snapshots för en feed-URL."""
    try:
        rows = json.loads(get(cdx_url))
    except Exception as e:
        print(f"  CDX-fel: {e}", file=sys.stderr)
        return []
    # Första raden är headers
    return [r[0] for r in rows[1:] if r[1] == "200"]


def wayback_url(orig_url, timestamp):
    return f"https://web.archive.org/web/{timestamp}id_/{orig_url}"


def parse_feed_items(raw):
    """Plockar ut alla <item> ur en feed-XML. Returnerar lista av ET-element."""
    try:
        register_namespaces(raw)
        root = ET.fromstring(raw)
        return root.findall(".//item")
    except ET.ParseError:
        return []


def fetch_wayback_items(cdx_url, orig_url, label, delay=1.0):
    """
    Hämtar Wayback-snapshots och plockar ur unika <item>-element.
    Kör snapshots i omvänd ordning (nyast-→äldst) och stannar när
    inga nya GUID:ar hittas tre gånger i rad.
    """
    timestamps = cdx_timestamps(cdx_url)
    if not timestamps:
        return {}
    print(f"  {label}: {len(timestamps)} snapshots att gå igenom",
          file=sys.stderr)
    items = {}  # guid -> xml-sträng
    no_new = 0
    for i, ts in enumerate(reversed(timestamps)):
        url = wayback_url(orig_url, ts)
        try:
            raw = get(url, timeout=45)
        except Exception as e:
            print(f"    {ts}: {e}", file=sys.stderr)
            time.sleep(delay)
            continue
        found = 0
        for it in parse_feed_items(raw):
            guid = item_guid(it)
            if guid and guid not in items:
                items[guid] = ET.tostring(it, encoding="unicode")
                found += 1
        if found == 0:
            no_new += 1
            if no_new >= 3:
                print(f"  {label}: inga nya på 3 snapshots, avbryter",
                      file=sys.stderr)
                break
        else:
            no_new = 0
        if (i + 1) % 10 == 0:
            print(f"  {label}: {i+1}/{len(timestamps)} snapshots, "
                  f"{len(items)} unika avsnitt", file=sys.stderr)
        time.sleep(delay)
    return items


# ---------- build ----------

def cmd_build(args):
    cache = load_cache()
    stored = cache["items"]  # guid -> xml-sträng

    # 1. Levande Captivate-feed
    print("Hämtar Captivate-feeden...", file=sys.stderr)
    raw = get(CAPTIVATE)
    register_namespaces(raw)
    tree = ET.ElementTree(ET.fromstring(raw))
    channel = tree.getroot().find("channel")
    live = channel.findall("item")
    fresh = 0
    for it in live:
        g = item_guid(it)
        if g and g not in stored:
            stored[g] = ET.tostring(it, encoding="unicode")
            fresh += 1
    print(f"  {len(live)} avsnitt i feeden, {fresh} nya sparade.",
          file=sys.stderr)
    save_cache(cache)

    # 2. Wayback Machine (bara om vi inte redan har gjort det)
    if not cache["wayback_done"]:
        print("Hämtar gamla avsnitt från Wayback Machine...", file=sys.stderr)
        before = len(stored)

        # Buzzsprout (2018-2022)
        bz = fetch_wayback_items(
            BUZZSPROUT_CDX,
            "feeds.buzzsprout.com/147669.rss",
            "Buzzsprout",
            delay=args.delay,
        )
        for g, xml in bz.items():
            if g not in stored:
                stored[g] = xml

        # Sounder (2022-2023) — prova flera möjliga URL:er
        sounder_found = False
        for sounder_url in ["feeds.pod.co/dataframed/rss.xml"] + SOUNDER_ALTS:
            cdx = (
                "https://web.archive.org/cdx/search/cdx"
                f"?url={sounder_url}&output=json&fl=timestamp,statuscode"
                "&filter=statuscode:200&limit=200"
                "&from=20220301&to=20230401"
            )
            sd = fetch_wayback_items(cdx, sounder_url, f"Sounder ({sounder_url})",
                                     delay=args.delay)
            if sd:
                for g, xml in sd.items():
                    if g not in stored:
                        stored[g] = xml
                sounder_found = True
                break
        if not sounder_found:
            print("  Sounder: inga snapshots hittades", file=sys.stderr)

        added = len(stored) - before
        print(f"Wayback: +{added} avsnitt (totalt {len(stored)}).",
              file=sys.stderr)
        cache["wayback_done"] = True
        save_cache(cache)
    else:
        print(f"Wayback redan klar ({len(stored)} avsnitt i cachen).",
              file=sys.stderr)

    # 3. Bygg feed
    far = datetime.max.replace(tzinfo=timezone.utc)
    items = []
    for xml_text in stored.values():
        try:
            it = ET.fromstring(xml_text)
            items.append(it)
        except ET.ParseError:
            continue

    items.sort(key=lambda i: item_date(i) or far)
    if args.newest_first:
        items.reverse()

    dates = [item_date(i) for i in items if item_date(i)]
    print(f"{len(items)} avsnitt, "
          f"{min(dates).date() if dates else '?'} till "
          f"{max(dates).date() if dates else '?'}",
          file=sys.stderr)

    original_title = channel.findtext("title") or "DataFramed"
    base, ext = os.path.splitext(args.output)
    ext = ext or ".xml"

    def write(group, path, label):
        for old in channel.findall("item"):
            channel.remove(old)
        for it in group:
            channel.append(it)
        channel.find("title").text = original_title + (f" {label}" if label else "")
        tree.write(path, encoding="utf-8", xml_declaration=True)

    if args.chunk:
        parts = [items[i:i + args.chunk]
                 for i in range(0, len(items), args.chunk)]
        for n, group in enumerate(parts, 1):
            name = f"{base}-{n:03d}{ext}"
            write(group, name, f"[{n}/{len(parts)}]")
            ds = [item_date(i) for i in group if item_date(i)]
            span = f"{min(ds).date()} till {max(ds).date()}" if ds else "?"
            print(f"  {name}: {len(group)} avsnitt, {span}", file=sys.stderr)
    else:
        write(items, base + ext, "")
        print(f"Klart: {base + ext}", file=sys.stderr)


def cmd_info(args):
    cache = load_cache()
    items = []
    for xml_text in cache["items"].values():
        try:
            it = ET.fromstring(xml_text)
            d = item_date(it)
            n = it.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}episode") or ""
            items.append((d, n, it.findtext("title") or ""))
        except ET.ParseError:
            continue
    items.sort(key=lambda x: x[0] or datetime.min.replace(tzinfo=timezone.utc))
    print(f"Cache: {len(items)} avsnitt, wayback_done={cache['wayback_done']}")
    if items:
        print(f"Äldst: {items[0][0].date() if items[0][0] else '?'}  "
              f"ep={items[0][1]}  {items[0][2][:60]}")
        print(f"Nyast: {items[-1][0].date() if items[-1][0] else '?'}  "
              f"ep={items[-1][1]}  {items[-1][2][:60]}")
        undated = sum(1 for d, _, _ in items if d is None)
        if undated:
            print(f"Varning: {undated} avsnitt saknar datum")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("-o", "--output", default="df.xml")
    b.add_argument("--chunk", type=int, default=100)
    b.add_argument("--delay", type=float, default=1.0,
                   help="Sekunder mellan Wayback-anrop (var snäll mot servern)")
    b.add_argument("--newest-first", action="store_true")
    b.add_argument("--reset-wayback", action="store_true",
                   help="Hämta Wayback-data på nytt trots att den redan gjorts")
    b.set_defaults(func=cmd_build)

    i = sub.add_parser("info")
    i.set_defaults(func=cmd_info)

    args = p.parse_args()
    if hasattr(args, "reset_wayback") and args.reset_wayback:
        cache = load_cache()
        cache["wayback_done"] = False
        save_cache(cache)
    args.func(args)


if __name__ == "__main__":
    main()
