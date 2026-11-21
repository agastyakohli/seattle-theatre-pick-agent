import argparse
import hashlib
import html
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
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
    # Avoid parsing plain URLs as HTML; this also prevents noisy BeautifulSoup warnings.
    text = str(value or "")
    if re.match(r"^https?://\S+$", text.strip(), re.IGNORECASE):
        return text.strip()
    for _ in range(2):
        text = BeautifulSoup(html.unescape(text), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()
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

def event_links(event_url, fallback=None, item=None):
    """Enrich event details and prefer producer page, ticketing page, then calendar listing."""
    fallback = fallback or event_url
    producer_url = ""
    ticket_url = ""
    try:
        response = requests.get(event_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        if item is not None:
            if not item.get("description"):
                meta = soup.select_one('meta[property="og:description"], meta[name="description"]')
                if meta:
                    item["description"] = clean(meta.get("content"))[:600]
                if not item.get("description"):
                    desc = soup.select_one('[itemprop="description"], .event-description, .tribe-events-single-event-description')
                    if desc:
                        item["description"] = clean(desc)[:600]
            if not item.get("venue"):
                venue = soup.select_one('[itemprop="location"], .event-venue, .venue')
                if venue:
                    item["venue"] = clean(venue)
            if not item.get("producer"):
                organizer = soup.select_one('[itemprop="organizer"], .organizer, .presented-by, .producer, [class*="organizer"]')
                if organizer:
                    item["producer"] = clean(organizer)
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
                    continue
                if not isinstance(obj, dict):
                    continue
                stack.extend(obj.get("@graph", []) if isinstance(obj.get("@graph"), list) else [])
                org = obj.get("organizer") or obj.get("producer")
                if isinstance(org, list):
                    org = org[0] if org else {}
                if isinstance(org, dict):
                    if org.get("name") and item is not None and not item.get("producer"):
                        item["producer"] = clean(org.get("name"))
                    if org.get("url"):
                        producer_url = urljoin(event_url, org["url"])
                if item is not None and not item.get("description") and obj.get("description"):
                    item["description"] = clean(obj.get("description"))[:600]
                if item is not None and not item.get("venue"):
                    loc = obj.get("location")
                    if isinstance(loc, dict) and loc.get("name"):
                        item["venue"] = clean(loc.get("name"))
                offers = obj.get("offers") or []
                if isinstance(offers, dict):
                    offers = [offers]
                for offer in offers:
                    if isinstance(offer, dict) and offer.get("url") and not ticket_url:
                        ticket_url = urljoin(event_url, offer["url"])
        page_host = urlparse(event_url).netloc.casefold()
        for anchor in soup.select("a[href]"):
            href = urljoin(event_url, anchor.get("href", ""))
            label = clean(anchor.get_text(" ", strip=True)).casefold()
            host = urlparse(href).netloc.casefold()
            if not href.startswith(("http://", "https://")) or not host:
                continue
            # Many TPS cards label the producer link simply "Website".
            if any(term in label for term in ("official website", "producer website", "company website", "show website", "visit website", "website")) and host != page_host:
                producer_url = href
            if not ticket_url and (any(term in host for term in ("eventbrite.", "ticketmaster.", "showclix.", "brownpapertickets.")) or any(term in label for term in ("buy tickets", "get tickets", "tickets"))):
                ticket_url = href
    except Exception as exc:
        print("LINK LOOKUP WARNING:", str(exc))
    return producer_url or ticket_url or event_url or fallback, producer_url, ticket_url

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
                        organizer = obj.get("organizer") or obj.get("producer") or {}
                        if isinstance(organizer, list):
                            organizer = organizer[0] if organizer else {}
                        producer = organizer.get("name", "") if isinstance(organizer, dict) else clean(organizer)
                        results.append({"title":clean(obj["name"]), "start_date":start, "end_date":end,
                          "performance_dates":[start], "venue":clean(venue), "producer":clean(producer),
                          "description":clean(obj.get("description"))[:600],
                          "url":link, "event_url":link, "source":source["name"]})
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
        producer_node = node.select_one("[itemprop=organizer], .organizer, .presented-by, .producer")
        results.append({"title":title, "start_date":start, "end_date":start,
          "performance_dates":[start], "venue":clean(venue_node) if venue_node else "",
          "producer":clean(producer_node) if producer_node else "",
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
              "performance_dates":[], "venue":"", "producer":"", "description":summary[:600], "url":entry.get("link", source["url"]),
              "source":source["name"]})
    for item in results:
        event_url = item.get("event_url") or item.get("url") or source["url"]
        item["source_priority"] = source.get("priority", 3)
        if source.get("producer") and not item.get("producer"):
            item["producer"] = source["producer"]
        if source.get("producer_type"):
            item["producer_type"] = source["producer_type"]
        item["event_url"] = event_url
        item.setdefault("producer_url", "")
        item.setdefault("ticket_url", "")
        # Keep the discovered event link for now. Enrich only the selected pick.
        item["url"] = event_url
    return results


def is_excluded_event(item):
    """Reject listings outside the page's editorial scope."""
    title = clean(item.get("title", "")).casefold()
    description = clean(item.get("description", "")).casefold()
    combined = title + " " + description

    # Exclude instructional/participatory listings rather than public performances.
    instructional_patterns = [
        r"(?<![\w-])class(?:es)?(?![\w-])",
        r"\bworkshops?\b",
        r"\bmaster\s*class(?:es)?\b",
        r"\blessons?\b",
        r"\bcourses?\b",
        r"\btraining\b",
        r"\bcamps?\b",
        r"\bauditions?\b",
    ]
    if any(re.search(pattern, combined, re.IGNORECASE) for pattern in instructional_patterns):
        return True

    # Exclude ballet and opera, plus explicitly identified stand-up comedy.
    # Keep sketch comedy, improv, plays and musicals eligible.
    out_of_scope_patterns = [
        r"\bballet\b",
        r"\bopera\b",
        r"\bstand[ -]?up\b",
        r"\bstandup\b",
    ]
    if any(re.search(pattern, combined, re.IGNORECASE) for pattern in out_of_scope_patterns):
        return True

    # Apply broad title-only exclusions such as tours and concerts.
    return any(term.casefold() in title for term in CFG.get("exclude_title_terms", []))

def producer_is_presenter(item):
    if item.get("producer_type") == "presenter":
        return True
    text = clean(item.get("producer") or "").casefold()
    venue = clean(item.get("venue") or "").casefold()
    names = ("seattle theatre group", "broadway across america")
    venues = ("paramount theatre", "moore theatre", "neptune theatre")
    return any(name in text for name in names) or (not text and any(v in venue for v in venues))


def producer_key(item):
    # Prefer explicit producer/organizer; venue is a fallback where unavailable.
    value = clean(item.get("producer") or item.get("venue") or item.get("source") or "")
    return re.sub(r"\s+", " ", value).casefold()


def merge(existing, incoming):
    records = {p["id"]:p for p in existing if p.get("id")}
    for item in incoming:
        if not item.get("title") or not item.get("url"):
            continue
        pid = identity(item["title"], item.get("venue", ""))
        item["id"] = pid
        item_dates = set(item.get("performance_dates") or [])
        if item.get("start_date"):
            item_dates.add(item["start_date"])
        prior = records.get(pid)
        if prior:
            # Preserve exact listed dates; never infer that a production runs daily.
            known = set(prior.get("performance_dates") or [])
            if prior.get("start_date"):
                known.add(prior["start_date"])
            prior["performance_dates"] = sorted(known | item_dates)
            if prior["performance_dates"]:
                prior["start_date"] = prior["performance_dates"][0]
                prior["end_date"] = prior["performance_dates"][-1]
            for key in ("venue", "producer", "description", "event_url", "producer_url", "ticket_url", "source_priority", "producer_type"):
                if item.get(key) and (not prior.get(key) or key in ("producer_url", "ticket_url", "source_priority", "producer_type")):
                    prior[key] = item[key]
            # Replace an older calendar URL with the best destination discovered now.
            if item.get("url") and (item.get("producer_url") or item.get("ticket_url")):
                prior["url"] = item["url"]
            prior.setdefault("sources", [])
            if item["source"] not in prior["sources"]:
                prior["sources"].append(item["source"])
        else:
            item["performance_dates"] = sorted(item_dates)
            item["sources"] = [item.pop("source")]
            item["featured"] = False
            records[pid] = item
    return list(records.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--test-facebook", action="store_true")
    args = parser.parse_args()
    if args.test_facebook:
        import os
        page_id = os.environ.get("FB_PAGE_ID")
        token = os.environ.get("FB_PAGE_ACCESS_TOKEN")
        if not page_id or not token:
            raise RuntimeError("Missing FB_PAGE_ID or FB_PAGE_ACCESS_TOKEN secrets")
        version = os.environ.get("META_GRAPH_VERSION", "v27.0")
        response = requests.get("https://graph.facebook.com/" + version + "/" + page_id,
            params={"fields": "id,name", "access_token": token}, timeout=30)
        response.raise_for_status()
        page = response.json()
        if str(page.get("id")) != str(page_id):
            raise RuntimeError("Facebook returned a Page ID that does not match FB_PAGE_ID")
        print("FACEBOOK CONNECTION OK:", page.get("name"), "(", page.get("id"), ")")
        return
    productions_path = DATA / "productions.json"
    featured_path = DATA / "featured.json"
    productions = json.loads(productions_path.read_text()) if productions_path.exists() else []
    featured = json.loads(featured_path.read_text()) if featured_path.exists() else []
    found = []
    enabled_sources = [source for source in CFG["sources"] if source.get("enabled")]
    # Fetch calendars concurrently so one slow site does not hold up every other source.
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {pool.submit(extract, source): source for source in enabled_sources}
        for job in as_completed(jobs):
            source = jobs[job]
            try:
                found.extend(job.result())
                print("Fetched:", source["name"])
            except Exception as exc:
                print("SOURCE ERROR", source["name"], str(exc), file=sys.stderr)
    productions = merge(productions, found)
    already = {x.get("production_id") for x in featured}
    today = datetime.now(TZ).date().isoformat()
    candidates = []
    # One pick per producer within a rolling seven-day window.
    recent_cutoff = datetime.now(TZ).date().toordinal() - 7
    recent_producers = set()
    for entry in featured:
        published = entry.get("published_at")
        if not published:
            continue
        try:
            published_date = dateparser.parse(published).date()
        except Exception:
            continue
        if published_date.toordinal() >= recent_cutoff:
            key = producer_key(entry)
            if key:
                recent_producers.add(key)

    for p in productions:
        if p.get("id") in already or p.get("featured"):
            continue
        # A pick must have a performance explicitly listed for today.
        performance_dates = set(p.get("performance_dates") or [])
        if not performance_dates and p.get("start_date"):
            performance_dates.add(p["start_date"])
        if today not in performance_dates:
            continue
        if is_excluded_event(p):
            print("SKIPPED NON-PRODUCTION EVENT:", p.get("title", "(untitled)"))
            continue
        key = producer_key(p)
        if key and key in recent_producers:
            print("SKIPPED FOR PRODUCER VARIETY:", p.get("title", "(untitled)"), "-", p.get("producer") or p.get("venue"))
            continue
        candidates.append(p)
    candidates.sort(key=lambda p:(1 if producer_is_presenter(p) else 0, p.get("source_priority", 3), p["title"].casefold(), p.get("venue","").casefold()))
    if not candidates:
        print("No validated future production found. No post will be published.")
    else:
        pick = candidates[0]
        # Resolve producer/ticket links and fill missing details for this one event only.
        event_url = pick.get("event_url") or pick.get("url")
        preferred, producer_url, ticket_url = event_links(event_url, pick.get("url"), pick)
        pick["producer_url"] = pick.get("producer_url") or producer_url
        pick["ticket_url"] = pick.get("ticket_url") or ticket_url
        pick["url"] = preferred
        description = clean(pick.get("description")) or ("A live theatre production from " + clean(pick.get("producer") or "a local theatre company") + ".")
        message = ("🎭 Seattle Theatre Pick of the Day: " + clean(pick["title"]) + "\n\n" +
          description + "\n\n" +
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
              "producer":pick.get("producer") or "", "venue":pick.get("venue") or "",
              "published_at":datetime.now(TZ).isoformat(), "url":pick["url"]})
            pick["featured"] = True
            featured_path.write_text(json.dumps(featured, indent=2) + "\n")
            print("Facebook post created:", response.json().get("id"))
    productions_path.write_text(json.dumps(productions, indent=2, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    main()
