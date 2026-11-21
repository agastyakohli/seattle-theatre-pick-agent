import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config/sources.json").read_text())
DATA = ROOT / "data"
TZ = ZoneInfo(CFG.get("timezone", "America/Los_Angeles"))
HEADERS = {"User-Agent": "SeattleTheatrePickBot/1.0 (community theatre calendar)"}

def clean(value):
    return re.sub(r"\\s+", " ", BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)).strip()

def parse_date(value):
    if not value:
        return None
    try:
        parsed = dateparser.parse(str(value), fuzzy=True)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ).date().isoformat()
    except Exception:
        return None

def identity(title, venue):
    raw = clean(title).casefold() + "|" + clean(venue).casefold()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def extract(source):
    response = requests.get(source["url"], headers=HEADERS, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    # JSON-LD is generally more reliable than guessing from arbitrary page markup.
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except Exception:
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                stack.extend(obj.get("@graph", []) if isinstance(obj.get("@graph"), list) else [])
                types = obj.get("@type", [])
                if isinstance(types, str):
                    types = [types]
                if any("Event" in t for t in types):
                    location = obj.get("location") or {}
                    venue = location.get("name", "") if isinstance(location, dict) else clean(location)
                    start = parse_date(obj.get("startDate"))
                    end = parse_date(obj.get("endDate")) or start
                    link = urljoin(source["url"], obj.get("url") or source["url"])
                    if obj.get("name") and start:
                        results.append({"title":clean(obj["name"]), "start_date":start, "end_date":end,
                          "venue":clean(venue), "description":clean(obj.get("description"))[:600],
                          "url":link, "source":source["name"]})
    # Generic event cards: deliberately low-confidence fallback.
    for node in soup.select('[itemtype*="Event"], article, .event, .tribe-events-calendar-list__event'):
        heading = node.select_one('[itemprop="name"], h1, h2, h3, h4, .event-title')
        time_node = node.select_one('[itemprop="startDate"], time[datetime], time')
        if not heading or not time_node:
            continue
        title = clean(heading)
        raw_date = time_node.get("content") or time_node.get("datetime") or clean(time_node)
        start = parse_date(raw_date)
        if not title or not start:
            continue
        anchor = node.select_one("a[href]")
        venue_node = node.select_one('[itemprop="location"], .venue, .event-venue')
        desc_node = node.select_one('[itemprop="description"], .event-description')
        results.append({"title":title, "start_date":start, "end_date":start,
          "venue":clean(venue_node) if venue_node else "",
          "description":clean(desc_node)[:600] if desc_node else "",
          "url":urljoin(source["url"], anchor["href"]) if anchor else source["url"],
          "source":source["name"]})
    # Feed entries are discovery leads; published dates are not performance dates,
    # so they are not eligible until an event date is independently extracted.
    if source.get("type") in ("rss", "atom"):
        feed = feedparser.parse(source["url"])
        for entry in feed.entries:
            summary = clean(entry.get("summary", ""))
            results.append({"title":clean(entry.get("title")), "start_date":None, "end_date":None,
              "venue":"", "description":summary[:600], "url":entry.get("link", source["url"]),
              "source":source["name"]})
    return results

def merge(existing, incoming):
    records = {p["id"]:p for p in existing if p.get("id")}
    for item in incoming:
        if not item.get("title") or not item.get("url"):
            continue
        pid = identity(item["title"], item.get("venue", ""))
        item["id"] = pid
        prior = records.get(pid)
        if prior:
            for key in ("start_date", "end_date", "venue", "description"):
                if item.get(key) and not prior.get(key):
                    prior[key] = item[key]
            prior.setdefault("sources", [])
            if item["source"] not in prior["sources"]:
                prior["sources"].append(item["source"])
        else:
            item["sources"] = [item.pop("source")]
            item["featured"] = False
            records[pid] = item
    return list(records.values())

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    productions_path = DATA / "productions.json"
    featured_path = DATA / "featured.json"
    productions = json.loads(productions_path.read_text()) if productions_path.exists() else []
    featured = json.loads(featured_path.read_text()) if featured_path.exists() else []
    found = []
    for source in CFG["sources"]:
        if not source.get("enabled"):
            continue
        try:
            found.extend(extract(source))
            print("Fetched:", source["name"])
        except Exception as exc:
            print("SOURCE ERROR", source["name"], str(exc), file=sys.stderr)
    productions = merge(productions, found)
    already = {x.get("production_id") for x in featured}
    today = datetime.now(TZ).date()
    candidates = []
    for p in productions:
        if p.get("id") in already or p.get("featured") or not p.get("start_date"):
            continue
        try:
            if date.fromisoformat(p.get("end_date") or p["start_date"]) < today:
                continue
        except ValueError:
            continue
        if any(term in p["title"].casefold() for term in CFG.get("exclude_title_terms", [])):
            continue
        candidates.append(p)
    candidates.sort(key=lambda p:(p["start_date"], p["title"].casefold()))
    if not candidates:
        print("No validated future production found. No post will be published.")
    else:
        pick = candidates[0]
        message = ("🎭 Seattle Theatre Pick of the Day: " + pick["title"] + "\n\n" +
          (pick.get("description") or "Discover a Puget Sound theatre production.") + "\n\n" +
          "📍 " + (pick.get("venue") or "Puget Sound") + "\n🎟️ Details & tickets: " + pick["url"])
        print("PROPOSED PICK:", pick["title"])
        print(message)
        if args.publish and not args.dry_run:
            page_id = __import__("os").environ.get("FB_PAGE_ID")
            token = __import__("os").environ.get("FB_PAGE_ACCESS_TOKEN")
            if not page_id or not token:
                raise RuntimeError("Missing FB_PAGE_ID or FB_PAGE_ACCESS_TOKEN secrets")
            version = __import__("os").environ.get("META_GRAPH_VERSION", "v27.0")
            response = requests.post("https://graph.facebook.com/" + version + "/" + page_id + "/feed",
              data={"message":message, "link":pick["url"], "access_token":token}, timeout=30)
            response.raise_for_status()
            featured.append({"production_id":pick["id"], "title":pick["title"],
              "published_at":datetime.now(TZ).isoformat(), "url":pick["url"]})
            pick["featured"] = True
            featured_path.write_text(json.dumps(featured, indent=2) + "\n")
            print("Facebook post created:", response.json().get("id"))
    productions_path.write_text(json.dumps(productions, indent=2, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    main()
