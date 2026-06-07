import httpx
import json
import re
import asyncio
import datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom
import os
import time
import warnings

warnings.filterwarnings("ignore")

# ────────────────────────────────────────────────
# Configuration — Change these to your GitHub info
# ────────────────────────────────────────────────
GITHUB_USERNAME = "YOUR_USERNAME"
REPO_NAME       = "sports-streams"
FOLDER_NAME     = "streams"

DEFAULT_LOGO = "https://streams.center/favicon.ico"

EPG_FILENAME    = "epg.xml"
M3U_FILENAME    = "streams_center.m3u"
M3U8_FILENAME   = "streams_center.m3u8"
STREAMS_JSON    = "streams_center.json"
CATEGORIES_JSON = "streams_center_categories.json"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# API endpoints discovered from bundle.js
API_BASE        = "https://backend.streamcenter.live/api"
API_PARTIES     = f"{API_BASE}/Parties?pageNumber=1&pageSize=500"
API_CATEGORIES  = f"{API_BASE}/Categories"
API_SETTINGS    = f"{API_BASE}/ApplicationSettings"
DECRYPT_URL     = "https://streams.center/embed/decrypt.php"
EMBED_BASE      = "https://streams.center"

# ESPN API (discovered in bundle)
ESPN_API        = "https://site.api.espn.com/apis/site/v2/sports"

# Category map (discovered from API /Categories endpoint)
CATEGORY_MAP = {
    1:       {"name": "Football",          "icon": "⚽", "priority": 1},
    2:       {"name": "Basketball",        "icon": "🏀", "priority": 2},
    3:       {"name": "Baseball",          "icon": "⚾", "priority": 3},
    4:       {"name": "American Football", "icon": "🏈", "priority": 4},
    5:       {"name": "Hockey",            "icon": "🏒", "priority": 5},
    6:       {"name": "Motor Sport",       "icon": "🏎️", "priority": 6},
    7:       {"name": "Fight MMA",         "icon": "🥊", "priority": 7},
    8:       {"name": "Boxing",            "icon": "🥊", "priority": 8},
    9:       {"name": "Football",          "icon": "⚽", "priority": 1},
    10:      {"name": "American Football", "icon": "🏈", "priority": 4},
    13:      {"name": "Baseball",          "icon": "⚾", "priority": 3},
    14:      {"name": "NCAA Division",     "icon": "🎓", "priority": 9},
    15:      {"name": "NCAAB",             "icon": "🎓", "priority": 10},
    16:      {"name": "Hockey",            "icon": "🏒", "priority": 5},
    17:      {"name": "WWE",               "icon": "💪", "priority": 11},
    4:       {"name": "Basketball",        "icon": "🏀", "priority": 2},
    "other": {"name": "Other Sports",     "icon": "🏆", "priority": 99},
}

NFL_PATTERNS    = ["NFL", "NATIONAL FOOTBALL"]
SEMAPHORE_LIMIT = 5
HTTP_TIMEOUT    = 30.0


# ────────────────────────────────────────────────
# HTTP Client (NO http2 to avoid h2 dependency)
# ────────────────────────────────────────────────

def make_client() -> httpx.AsyncClient:
    """Create async HTTP client — no http2 required."""
    return httpx.AsyncClient(
        timeout          = httpx.Timeout(HTTP_TIMEOUT),
        verify           = False,
        follow_redirects = True,
        # http2 = False  ← default, no h2 package needed
    )


def base_headers(referer=None):
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    if referer:
        h["Referer"] = referer
    return h


def api_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Origin":          "https://streamcenter.live",
        "Referer":         "https://streamcenter.live/",
    }


# ────────────────────────────────────────────────
# Fetch Categories from API
# ────────────────────────────────────────────────

async def fetch_categories(client: httpx.AsyncClient) -> dict:
    """
    Fetch real categories from /api/Categories.
    Returns {id: name} mapping.
    """
    try:
        r = await client.get(API_CATEGORIES, headers=api_headers())
        if r.status_code == 200:
            cats = r.json()
            print(f"  ✅ Got {len(cats)} categories from API")
            mapping = {}
            for cat in cats:
                cid  = cat.get("id")
                name = cat.get("name") or cat.get("label") or f"Category {cid}"
                if cid:
                    mapping[cid] = name
                    print(f"     {cid}: {name}")
            return mapping
    except Exception as e:
        print(f"  ⚠️  Categories API failed: {e}")

    # Fallback to hardcoded map
    return {k: v["name"] for k, v in CATEGORY_MAP.items() if k != "other"}


# ────────────────────────────────────────────────
# Fetch Events from API
# ────────────────────────────────────────────────

async def fetch_events(client: httpx.AsyncClient) -> list:
    """
    Fetch all events. Tries multiple API endpoints discovered in bundle.
    """
    endpoints = [
        f"{API_BASE}/Parties?pageNumber=1&pageSize=500",
        f"{API_BASE}/Parties?pageNumber=1&pageSize=50",
    ]

    for url in endpoints:
        try:
            print(f"  Trying: {url}")
            r = await client.get(url, headers=api_headers())
            r.raise_for_status()
            data = r.json()

            if isinstance(data, list) and len(data) > 0:
                print(f"  ✅ Got {len(data)} events")
                return data
            elif isinstance(data, dict):
                # Some APIs wrap in {data: [...], total: N}
                for key in ["data", "items", "results", "parties", "games", "events"]:
                    if key in data and isinstance(data[key], list):
                        print(f"  ✅ Got {len(data[key])} events (key={key})")
                        return data[key]
        except Exception as e:
            print(f"  ❌ Failed {url}: {e}")
            continue

    return []


# ────────────────────────────────────────────────
# Parse Video URLs
# ────────────────────────────────────────────────

def parse_video_urls(vid_str: str) -> list:
    """
    Parse videoUrl field.
    Formats seen:
      - 'url1<lang1>;url2<lang2>'
      - 'url1;url2'
      - 'url1'
    Returns [(url, language), ...]
    """
    results = []
    if not vid_str:
        return results

    for chunk in vid_str.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue

        if "<" in chunk:
            parts = chunk.split("<", 1)
            url   = parts[0].strip()
            lang  = parts[1].rstrip(">").strip() if len(parts) > 1 else "English"
        else:
            url  = chunk
            lang = "English"

        if url.startswith("http"):
            results.append((url, lang))

    return results


# ────────────────────────────────────────────────
# Stream URL Resolver
# ────────────────────────────────────────────────

async def resolve_stream_url(
    client: httpx.AsyncClient,
    url: str,
    semaphore: asyncio.Semaphore,
) -> str:
    """
    Full resolution pipeline:
    1. Already m3u8 → return
    2. PHP embed:
       a. Fetch ch*.php → find hls2.php iframe
       b. Fetch hls2.php → find encrypted input
       c. POST to decrypt.php → get m3u8
       d. Fallback: try edgestreams.pro directly
    """
    async with semaphore:

        # ── 1. Already direct m3u8 ──
        if ".m3u8" in url.lower():
            return url

        # ── 2. Not a PHP page ──
        if ".php" not in url.lower():
            return url

        try:
            # ── Step A: Fetch ch*.php ──
            r1 = await client.get(
                url,
                headers=base_headers(referer=EMBED_BASE + "/"),
            )

            # Direct m3u8 on first page
            m = re.search(
                r"""(https?://[^\s"'<>\[\]]+\.m3u8[^\s"'<>\[\]]*)""",
                r1.text, re.I
            )
            if m:
                return m.group(1).replace("\\/", "/")

            # Find inner iframe
            ifr = re.search(
                r"""<iframe[^>]+src\s*=\s*["']?\s*((?://|https?://|/)[^"'>\s]+)""",
                r1.text, re.I
            )
            if not ifr:
                return url

            inner = ifr.group(1).strip()
            if inner.startswith("//"):
                inner = "https:" + inner
            elif inner.startswith("/"):
                inner = EMBED_BASE + inner

            # Get stream ID for fallback
            sid_m     = re.search(r"stream=([a-zA-Z0-9]+)", inner)
            stream_id = sid_m.group(1) if sid_m else None

            # ── Step B: Fetch hls2.php ──
            r2 = await client.get(
                inner,
                headers=base_headers(referer=url),
            )

            # Direct m3u8 in hls2.php
            m2 = re.search(
                r"""(https?://[^\s"'<>\[\]]+\.m3u8[^\s"'<>\[\]]*)""",
                r2.text, re.I
            )
            if m2:
                return m2.group(1).replace("\\/", "/")

            # ── Step C: Find encrypted input → POST decrypt.php ──
            # Pattern from bundle: input: "BASE64STRING"
            enc_m = re.search(
                r"""input\s*:\s*["']([A-Za-z0-9+/=]{40,})["']""",
                r2.text
            )
            if enc_m:
                encrypted = enc_m.group(1)
                try:
                    dec = await client.post(
                        DECRYPT_URL,
                        data    = {"input": encrypted},
                        headers = {
                            **base_headers(referer=inner),
                            "Content-Type":     "application/x-www-form-urlencoded",
                            "X-Requested-With": "XMLHttpRequest",
                            "Origin":           EMBED_BASE,
                        },
                    )
                    if dec.is_success:
                        decrypted = dec.text.strip()
                        if ".m3u8" in decrypted:
                            return decrypted
                        # Try JSON response
                        try:
                            jdata = dec.json()
                            for key in ["url", "src", "source", "stream", "file", "m3u8", "link"]:
                                if key in jdata and ".m3u8" in str(jdata[key]):
                                    return str(jdata[key])
                        except Exception:
                            pass
                except Exception as e:
                    print(f"    decrypt.php error: {e}")

            # ── Step D: Direct edgestreams.pro (works without token) ──
            if stream_id:
                candidates = [
                    f"https://edgestreams.pro/hls/{stream_id}.m3u8",
                    f"https://edgestreams.pro/live/{stream_id}.m3u8",
                    f"https://edgestreams.pro/hls/{stream_id}/index.m3u8",
                ]
                for candidate in candidates:
                    try:
                        test = await client.get(
                            candidate,
                            headers={"Referer": EMBED_BASE + "/"},
                        )
                        if test.status_code == 200 and (
                            "#EXTM3U" in test.text[:200]
                            or "#EXT-X-" in test.text[:200]
                        ):
                            return candidate
                    except Exception:
                        pass

            return url

        except Exception as e:
            print(f"    ⚠️  Resolve [{url[:55]}]: {e}")
            return url


# ────────────────────────────────────────────────
# Category Helper
# ────────────────────────────────────────────────

def get_category_name(cat_id, name_upper: str, cat_map: dict) -> str:
    """Get category name from id or name patterns."""
    # NFL override by name
    if any(p in name_upper for p in NFL_PATTERNS):
        return "American Football"

    # From live categories API
    if cat_id in cat_map:
        return cat_map[cat_id]

    # From hardcoded map
    if cat_id in CATEGORY_MAP and cat_id != "other":
        return CATEGORY_MAP[cat_id]["name"]

    return "Other Sports"


def get_best_logo(event: dict) -> str:
    """Best logo for an event."""
    for field in ["logoTeam1", "logoTeam2", "logo", "icon", "image", "thumbnail"]:
        val = event.get(field)
        if val and isinstance(val, str) and val.startswith("http"):
            return val
    return DEFAULT_LOGO


def format_start(start_str: str) -> str:
    if not start_str:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return start_str


def epg_ts(dt: datetime.datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S +0000")


# ────────────────────────────────────────────────
# Build Streams
# ────────────────────────────────────────────────

async def build_streams(
    client: httpx.AsyncClient,
    games: list,
    cat_map: dict,
) -> list:
    """Parse events + resolve all stream URLs concurrently."""

    raw = []
    for game in games:
        vid_str = (game.get("videoUrl") or "").strip()
        if not vid_str:
            continue

        gid      = str(game.get("id", ""))
        name     = (
            game.get("gameName")
            or game.get("name")
            or game.get("title")
            or "Unknown Event"
        )
        start    = (
            game.get("beginPartie")
            or game.get("startDate")
            or game.get("date")
            or ""
        )
        cat_id   = game.get("categoryId")
        logo     = get_best_logo(game)
        name_up  = name.upper()
        cat_name = get_category_name(cat_id, name_up, cat_map)

        for url, lang in parse_video_urls(vid_str):
            display = f"{name} ({lang})" if lang.lower() not in ("english", "") else name
            raw.append({
                "id":            gid,
                "name":          display,
                "event_name":    name,
                "language":      lang,
                "url":           url,
                "category_id":   cat_id,
                "category_name": cat_name,
                "start":         start,
                "start_fmt":     format_start(start),
                "logo":          logo,
                "logo_team1":    game.get("logoTeam1", ""),
                "logo_team2":    game.get("logoTeam2", ""),
            })

    print(f"\n  📋 Raw stream entries  : {len(raw)}")
    print(f"  🔄 Resolving ({SEMAPHORE_LIMIT} concurrent)...")

    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)

    async def process(s: dict) -> dict:
        resolved = await resolve_stream_url(client, s["url"], semaphore)
        return {**s, "url": resolved}

    results = await asyncio.gather(
        *(process(s) for s in raw),
        return_exceptions=True,
    )

    # Only keep streams with a valid m3u8 URL
    valid = [
        r for r in results
        if isinstance(r, dict) and ".m3u8" in r.get("url", "")
    ]

    # Sort by start time
    valid.sort(key=lambda x: x.get("start") or "9999")

    print(f"  ✅ Valid m3u8 streams  : {len(valid)}")
    return valid


# ────────────────────────────────────────────────
# EPG Generator
# ────────────────────────────────────────────────

def generate_epg(streams: list, filepath: str):
    root = ET.Element("tv", attrib={
        "generator-info-name": "streams.center extractor",
        "generator-info-url":  EMBED_BASE,
    })

    now       = datetime.datetime.now(datetime.timezone.utc)
    seen_ch   = set()

    for s in streams:
        ch_id = s["id"]

        if ch_id not in seen_ch:
            seen_ch.add(ch_id)
            ch_el = ET.SubElement(root, "channel", id=ch_id)
            ET.SubElement(ch_el, "display-name", lang="en").text = s["event_name"]
            ET.SubElement(ch_el, "icon", src=s["logo"])

        # Parse start time
        try:
            if s.get("start"):
                st_dt = datetime.datetime.fromisoformat(
                    s["start"].replace("Z", "+00:00")
                )
            else:
                st_dt = now
        except Exception:
            st_dt = now

        en_dt = st_dt + datetime.timedelta(hours=3)

        prog = ET.SubElement(
            root, "programme",
            start   = epg_ts(st_dt),
            stop    = epg_ts(en_dt),
            channel = ch_id,
        )
        ET.SubElement(prog, "title",    lang="en").text = s["name"]
        ET.SubElement(prog, "category", lang="en").text = s["category_name"]
        ET.SubElement(prog, "icon",     src=s["logo"])

    raw    = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ")
    pretty = "\n".join(l for l in pretty.split("\n") if l.strip())

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(pretty)

    print(f"  💾 EPG   → {os.path.basename(filepath)} ({len(seen_ch)} channels)")


# ────────────────────────────────────────────────
# M3U Generator
# ────────────────────────────────────────────────

def generate_m3u(streams: list, filepath: str, epg_url: str = ""):
    now   = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f'#EXTM3U x-tvg-url="{epg_url}"',
        f"# Source: {EMBED_BASE}  |  API: {API_BASE}",
        f"# Generated: {now}  |  Streams: {len(streams)}",
        f"# Developed By: Farhad Hossain",
        "",
    ]

    for s in streams:
        name   = s["name"]
        logo   = s.get("logo") or DEFAULT_LOGO
        group  = s.get("category_name", "Other Sports")
        sid    = s["id"]
        url    = s["url"]
        start  = s.get("start_fmt", "")

        lines.append(
            f'#EXTINF:-1 '
            f'tvg-id="{sid}" '
            f'tvg-name="{name}" '
            f'tvg-logo="{logo}" '
            f'group-title="{group}"'
            f'{(chr(32) + "start=" + chr(34) + start + chr(34)) if start else ""},'
            f'{name}'
        )
        lines.append(
            "#EXTVLCOPT:http-user-agent=Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        lines.append(f"#EXTVLCOPT:http-referrer={EMBED_BASE}/")
        lines.append(url)
        lines.append("")

    content = "\n".join(lines)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"  💾 M3U   → {os.path.basename(filepath)} ({len(streams)} entries)")


# ────────────────────────────────────────────────
# JSON Generators
# ────────────────────────────────────────────────

def generate_json_flat(streams: list, filepath: str):
    out = {
        "source":        EMBED_BASE,
        "api":           API_BASE,
        "generated_at":  datetime.datetime.now(
                             datetime.timezone.utc
                         ).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "total_streams": len(streams),
        "streams":       streams,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  💾 JSON  → {os.path.basename(filepath)} ({len(streams)} streams)")


def generate_json_categories(streams: list, filepath: str):
    from collections import defaultdict

    grouped = defaultdict(list)
    for s in streams:
        grouped[s.get("category_name", "Other Sports")].append({
            "id":       s["id"],
            "name":     s["name"],
            "url":      s["url"],
            "start":    s.get("start_fmt", ""),
            "logo":     s.get("logo", DEFAULT_LOGO),
            "language": s.get("language", "English"),
        })

    # Sort categories by priority
    priority = {}
    for v in CATEGORY_MAP.values():
        if isinstance(v, dict):
            priority[v["name"]] = v.get("priority", 99)

    sorted_cats = sorted(
        grouped.items(),
        key=lambda x: priority.get(x[0], 99),
    )

    out = {
        "source":       EMBED_BASE,
        "generated_at": datetime.datetime.now(
                            datetime.timezone.utc
                        ).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "categories":   dict(sorted_cats),
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"  💾 CAT   → {os.path.basename(filepath)}")
    for cat, items in sorted_cats:
        icon = next(
            (v["icon"] for v in CATEGORY_MAP.values()
             if isinstance(v, dict) and v.get("name") == cat),
            "🏆"
        )
        print(f"         {icon} {cat:22s}: {len(items)}")


# ────────────────────────────────────────────────
# Summary Table
# ────────────────────────────────────────────────

def print_summary(streams: list):
    print(f"\n{'─'*70}")
    print(f"  {'Event':40s} {'Category':18s} {'Time':12s}")
    print(f"{'─'*70}")
    seen = set()
    for s in streams:
        key = s["id"]
        if key not in seen:
            seen.add(key)
            name  = s["event_name"][:38]
            cat   = s.get("category_name", "")[:16]
            start = s.get("start_fmt", "")[:11]
            print(f"  {name:40s} {cat:18s} {start}")
    print(f"{'─'*70}")
    print(f"  Unique events : {len(seen)}")
    print(f"  Total streams : {len(streams)}")


# ────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────

async def main():
    print("=" * 65)
    print("  streams.center / streamcenter.live Extractor")
    print("=" * 65)

    t0 = time.time()

    epg_url = (
        f"https://raw.githubusercontent.com/{GITHUB_USERNAME}/"
        f"{REPO_NAME}/main/{FOLDER_NAME}/{EPG_FILENAME}"
    )

    # ── No http2 → no h2 package needed ──
    async with make_client() as client:

        # ── Fetch categories ──
        print("\n📂 Fetching categories...")
        cat_map = await fetch_categories(client)

        # ── Fetch events ──
        print("\n📡 Fetching events...")
        games = await fetch_events(client)

        if not games:
            print("  ❌ No events found. Exiting.")
            return

        # ── Build & resolve streams ──
        print(f"\n🔄 Processing {len(games)} events...")
        streams = await build_streams(client, games, cat_map)

        if not streams:
            print("  ❌ No valid streams resolved.")
            return

    # ── Summary ──
    print_summary(streams)

    # ── Save files ──
    print(f"\n{'─'*65}")
    print(f"  Saving files → {BASE_DIR}")
    print(f"{'─'*65}")

    generate_epg(streams, os.path.join(BASE_DIR, EPG_FILENAME))
    generate_m3u(streams, os.path.join(BASE_DIR, M3U_FILENAME),  epg_url)
    generate_m3u(streams, os.path.join(BASE_DIR, M3U8_FILENAME), epg_url)
    generate_json_flat(streams, os.path.join(BASE_DIR, STREAMS_JSON))
    generate_json_categories(streams, os.path.join(BASE_DIR, CATEGORIES_JSON))

    elapsed = time.time() - t0
    print(f"\n{'='*65}")
    print(f"  ✅ Done in {elapsed:.1f}s")
    print(f"  Total streams : {len(streams)}")
    print(f"{'='*65}")


if __name__ == "__main__":
    asyncio.run(main())
