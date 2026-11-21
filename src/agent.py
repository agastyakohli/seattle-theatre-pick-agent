import argparse
import hashlib
import html
import json
import re
import sys
from datetime import date, datetime, timedelta
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


def ical_unescape(value):
    return (value or "").replace(r"\n", "\n").replace(r"\N", "\n").replace(r"\,", ",").replace(r"\;", ";").replace(r"\\", "\\")


def parse_ical_value(value, params=None):
    params = params or {}
    value = value.strip()
    try:
        if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
            return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=TZ)
        if re.fullmatch(r"\d{8}T\d{6}Z", value):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
        if re.fullmatch(r"\d{8}T\d{6}", value):
            parsed = datetime.strptime(value, "%Y%m%dT%H%M%S")
            tzid = params.get("TZID")
            try:
                return parsed.replace(tzinfo=ZoneInfo(tzid)).astimezone(TZ) if tzid else parsed.replace(tzinfo=TZ)
            except Exception:
                return parsed.replace(tzinfo=TZ)
        parsed = dateparser.parse(value)
        if parsed and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ) if parsed else None
    except Exception:
        return None


def parse_ai1ec_ical(text, source):
    """Convert NWTheatre's All-in-One Event Calendar export into dated listings."""
    unfolded = re.sub(r"\r?\n[ \t]", "", text or "")
    events = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, flags=re.S | re.I)
    today = datetime.now(TZ).date()
    horizon = today + timedelta(days=400)
    results = []

    for block in events:
        props = {}
        for line in block.splitlines():
            if not line or ":" not in line:
                continue
            left, value = line.split(":", 1)
            bits = left.split(";")
            key = bits[0].upper()
            params = {}
            for bit in bits[1:]:
                if "=" in bit:
                    k, v = bit.split("=", 1)
                    params[k.upper()] = v.strip('"')
            props.setdefault(key, []).append((value.strip(), params))

        def first(key):
            values = props.get(key, [])
            return values[0] if values else ("", {})

        title = clean(ical_unescape(first("SUMMARY")[0]))
        start_raw, start_params = first("DTSTART")
        start_dt = parse_ical_value(start_raw, start_params)
        if not title or not start_dt or first("STATUS")[0].upper() == "CANCELLED":
            continue

        dates = {start_dt.date()}
        rule_raw = first("RRULE")[0]
        if rule_raw:
            try:
                from dateutil.rrule import rrulestr
                rule = rrulestr(rule_raw, dtstart=start_dt)
                dates.update(dt.date() for dt in rule.between(
                    datetime.combine(today, datetime.min.time(), tzinfo=TZ),
                    datetime.combine(horizon, datetime.max.time(), tzinfo=TZ), inc=True))
            except Exception:
                pass
        for raw, params in props.get("RDATE", []):
            for value in raw.split(","):
                dt = parse_ical_value(value, params)
                if dt:
                    dates.add(dt.date())
        for raw, params in props.get("EXDATE", []):
            for value in raw.split(","):
                dt = parse_ical_value(value, params)
                if dt:
                    dates.discard(dt.date())

        future_dates = sorted(d.isoformat() for d in dates if today <= d <= horizon)
        if not future_dates:
            continue

        end_raw, end_params = first("DTEND")
        end_dt = parse_ical_value(end_raw, end_params) if end_raw else None
        url = ical_unescape(first("URL")[0]) or source["url"]
        location = clean(ical_unescape(first("LOCATION")[0]))
        description = clean(ical_unescape(first("DESCRIPTION")[0]))[:600]
        categories = clean(ical_unescape(first("CATEGORIES")[0]))
        results.append({
            "title": title, "start_date": future_dates[0],
            "end_date": future_dates[-1], "performance_dates": future_dates,
            "venue": location, "producer": "", "description": description,
            "categories": categories, "url": url, "event_url": url,
            "source": source["name"]
        })
    return results


def identity(title, producer_or_venue):
    # Prefer producer identity so the same production from multiple calendars
    # merges even when one source names the company and another names its venue.
    raw = clean(title).casefold() + "|" + clean(producer_or_venue).casefold()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def title_tokens(value):
    ignored = {"the", "a", "an", "of", "and", "at", "in", "for", "by"}
    return {word for word in re.findall(r"[a-z0-9]+", clean(value).casefold()) if len(word) > 1 and word not in ignored}


def is_ticketing_host(host):
    host = (host or "").casefold().removeprefix("www.")
    return any(domain in host for domain in (
        "eventbrite.", "ticketmaster.", "showclix.", "brownpapertickets.",
        "ticketweb.", "tixr.", "universe.com", "etix.com", "seetickets."
    ))


def event_links(event_url, fallback=None, item=None):
    """Enrich the selected calendar pick; prefer producer page, ticketing page, then calendar listing."""
    fallback = fallback or event_url
    producer_url = ""
    ticket_url = ""
    try:
        response = requests.get(event_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        page_description = ""
        event_host = urlparse(event_url).netloc.casefold()
        event_is_ticketing = is_ticketing_host(event_host)
        if item is not None and not event_is_ticketing:
            # The selected occurrence page is more authoritative than snippets
            # inherited from aggregator cards or older Eventbrite occurrences.
            meta = soup.select_one('meta[property="og:description"], meta[name="description"]')
            if meta:
                page_description = clean(meta.get("content"))
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
                        organizer_url = urljoin(event_url, org["url"])
                        organizer_host = urlparse(organizer_url).netloc.casefold()
                        # Ticketing-platform organizer profiles are not producer websites.
                        if not is_ticketing_host(organizer_host):
                            producer_url = organizer_url
                if item is not None and not event_is_ticketing and obj.get("description") and any("Event" in t for t in (obj.get("@type", []) if isinstance(obj.get("@type"), list) else [obj.get("@type", "")])):
                    page_description = clean(obj.get("description"))
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
        # TPS labels the producer link "Website:" separately from its anchor.
        # Find that labeled anchor even when the label and link are siblings.
        for label_node in soup.find_all(["h2", "h3", "h4", "strong", "b", "dt", "p", "div"]):
            label_text = clean(label_node.get_text(" ", strip=True)).casefold().strip()
            if label_text in ("website:", "website"):
                candidate = label_node.find("a", href=True) or label_node.find_next("a", href=True)
                if candidate:
                    href = urljoin(event_url, candidate.get("href", ""))
                    host = urlparse(href).netloc.casefold()
                    if href.startswith(("http://", "https://")) and host and host != page_host:
                        producer_url = href
                        break
        # Restrict ticket detection to event content so global site/footer links
        # (such as TPS Gregory Awards tickets) cannot be mistaken for this show's tickets.
        main_content = soup.select_one("main, article, .tribe-events-single, .event-content, .event-details")
        scan_root = main_content or soup
        for anchor in scan_root.select("a[href]"):
            href = urljoin(event_url, anchor.get("href", ""))
            label = clean(anchor.get_text(" ", strip=True)).casefold()
            host = urlparse(href).netloc.casefold()
            if not href.startswith(("http://", "https://")) or not host:
                continue
            context = clean(anchor.parent.get_text(" ", strip=True)).casefold() if anchor.parent else ""
            if (any(term in label for term in ("official website", "producer website", "company website", "show website", "visit website", "website"))
                    or context.startswith("website:") or context.startswith("website ")) and host != page_host:
                producer_url = href
            if not ticket_url and (any(term in host for term in ("eventbrite.", "ticketmaster.", "showclix.", "brownpapertickets.")) or any(term in label for term in ("buy tickets", "get tickets", "tickets"))):
                ticket_url = href
        if item is not None and page_description:
            item["description"] = page_description[:600]
        # If this is a producer's own event URL (as configured), retain that
        # specific event link even when the site redirects to a ticketing host.
        original_host = urlparse(event_url).netloc.casefold()
        item_producer = clean(item.get("producer") if item else "").casefold()
        for configured_source in CFG.get("sources", []):
            configured_producer = clean(configured_source.get("producer", "")).casefold()
            configured_host = urlparse(configured_source.get("url", "")).netloc.casefold()
            if (item_producer and configured_producer == item_producer
                    and configured_host == original_host):
                producer_url = event_url
                break
    except Exception as exc:
        print("LINK LOOKUP WARNING:", str(exc))
    # A dated event page on a non-calendar, non-ticketing domain is itself
    # the producer's specific show page (e.g. Unexpected Productions).
    calendar_hosts = {
        urlparse(source.get("url", "")).netloc.casefold().removeprefix("www.")
        for source in CFG.get("sources", [])
    }
    event_host = urlparse(event_url).netloc.casefold().removeprefix("www.")
    if event_host and event_host not in calendar_hosts and not is_ticketing_host(event_host):
        producer_url = event_url
    # If the calendar only exposes the producer homepage, look for a
    # production-specific page on that site (including its experiences/shows index).
    if producer_url and producer_url != event_url and item is not None and item.get("title"):
        try:
            title_words = {
                w for w in re.findall(r"[a-z0-9]+", clean(item["title"]).casefold())
                if len(w) > 2 and w not in {"all", "the", "show", "play", "production", "experience"}
            }
            home = requests.get(producer_url, headers=HEADERS, timeout=10)
            home.raise_for_status()
            home_soup = BeautifulSoup(home.text, "html.parser")
            candidates = []
            for a in home_soup.select("a[href]"):
                label = clean(a.get_text(" ", strip=True)).casefold()
                href = urljoin(home.url, a.get("href", ""))
                if href.startswith(("http://", "https://")) and urlparse(href).netloc.casefold() == urlparse(home.url).netloc.casefold():
                    overlap = len(title_words & set(re.findall(r"[a-z0-9]+", label)))
                    if overlap:
                        candidates.append((overlap, href))
            candidates.sort(reverse=True)
            min_overlap = min(2, len(title_words)) if title_words else 999
            if candidates and candidates[0][0] >= min_overlap:
                producer_url = candidates[0][1]
            else:
                # Follow one likely portfolio/experiences index and match the show title there.
                index_links = []
                for a in home_soup.select("a[href]"):
                    label = clean(a.get_text(" ", strip=True)).casefold()
                    href = urljoin(home.url, a.get("href", ""))
                    if any(word in label for word in ("experience", "project", "production", "show", "work")):
                        if href.startswith(("http://", "https://")) and urlparse(href).netloc.casefold() == urlparse(home.url).netloc.casefold():
                            index_links.append(href)
                for index_url in list(dict.fromkeys(index_links))[:2]:
                    index = requests.get(index_url, headers=HEADERS, timeout=10)
                    index.raise_for_status()
                    index_soup = BeautifulSoup(index.text, "html.parser")
                    matches = []
                    for a in index_soup.select("a[href]"):
                        label = clean(a.get_text(" ", strip=True)).casefold()
                        href = urljoin(index.url, a.get("href", ""))
                        overlap = len(title_words & set(re.findall(r"[a-z0-9]+", label)))
                        if overlap and href.startswith(("http://", "https://")) and urlparse(href).netloc.casefold() == urlparse(index.url).netloc.casefold():
                            matches.append((overlap, href))
                    matches.sort(reverse=True)
                    if matches and matches[0][0] >= min_overlap:
                        producer_url = matches[0][1]
                        break
            # Do not publish a generic company homepage or portfolio index as
            # though it were the production page. The dated calendar listing
            # remains the safe fallback when no show-specific page is found.
            if producer_url and producer_url != event_url:
                final_path = urlparse(producer_url).path.rstrip("/")
                if not final_path or final_path.casefold() in ("/experiences", "/shows", "/productions", "/work"):
                    producer_url = ""
        except Exception as exc:
            print("PRODUCER PAGE LOOKUP WARNING:", str(exc))
    # Read show-specific descriptive metadata from the official producer page,
    # but only after this dated calendar occurrence has already been selected.
    if producer_url and producer_url != event_url and item is not None:
        try:
            official_response = requests.get(producer_url, headers=HEADERS, timeout=10)
            official_response.raise_for_status()
            official_soup = BeautifulSoup(official_response.text, "html.parser")
            official_description = ""
            meta = official_soup.select_one(
                'meta[property="og:description"], meta[name="description"]')
            if meta:
                official_description = clean(meta.get("content"))
            if not official_description:
                for script in official_soup.select('script[type="application/ld+json"]'):
                    try:
                        payload = json.loads(script.string or script.get_text())
                    except Exception:
                        continue
                    stack = payload if isinstance(payload, list) else [payload]
                    while stack and not official_description:
                        obj = stack.pop()
                        if isinstance(obj, list):
                            stack.extend(obj)
                        elif isinstance(obj, dict):
                            stack.extend(obj.get("@graph", []) if isinstance(obj.get("@graph"), list) else [])
                            if obj.get("description"):
                                official_description = clean(obj["description"])
            if official_description:
                item["description"] = official_description[:600]
        except Exception as exc:
            print("OFFICIAL SHOW ENRICHMENT WARNING:", str(exc))
    return producer_url or ticket_url or event_url or fallback, producer_url, ticket_url

def extract(source):
    response = requests.get(source["url"], headers=HEADERS, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    # Capture lightweight response diagnostics so empty extraction can be
    # distinguished from blocked, redirected, or unexpectedly small pages.
    diagnostic = {
        "status": response.status_code,
        "final_url": response.url,
        "bytes": len(response.content),
        "page_title": clean(soup.title.get_text(" ", strip=True))[:140] if soup.title else "(no title)",
        "jsonld_scripts": len(soup.select('script[type="application/ld+json"]')),
        "event_nodes": len(soup.select('[itemtype*="Event"], article, .event, .tribe-events-calendar-list__event')),
        "time_nodes": len(soup.select('[itemprop="startDate"], time[datetime], time')),
        "links": len(soup.select("a[href]")),
        "scripts": [
            {"type": (node.get("type") or "text/javascript")[:60],
             "src": (urljoin(response.url, node.get("src")) if node.get("src") else "(inline)"),
             "bytes": len(node.get_text() or "")}
            for node in soup.select("script")
        ][:20],
        "iframes": [urljoin(response.url, node.get("src")) for node in soup.select("iframe[src]")][:10],
        "calendar_markers": {
            marker: response.text.casefold().count(marker)
            for marker in ("tribe-events", "fullcalendar", "fc-event", "event-list", "calendar", "startdate", "data-date", "event-date", "wp-json", "ai1ec")
        },
        "date_attributes": [
            {"tag": node.name, "attrs": {key: str(value)[:180] for key, value in node.attrs.items()
                                          if key in ("data-date", "data-start", "data-end", "data-event-id", "class", "href")},
             "text": clean(node.get_text(" ", strip=True))[:180]}
            for node in soup.select("[data-date], [data-start], [data-event-id]")
        ][:20],
        "jsonld_summaries": [
            {"types": obj.get("@type"), "name": clean(obj.get("name", ""))[:140],
             "startDate": obj.get("startDate"), "endDate": obj.get("endDate"),
             "url": obj.get("url")}
            for script in soup.select('script[type="application/ld+json"]')
            for obj in ([json.loads(script.string or script.get_text())] if (script.string or script.get_text()).strip() else [])
            if isinstance(obj, dict)
        ][:10],
        "calendar_inline_scripts": [
            re.sub(r"\\s+", " ", node.get_text() or "")[:900]
            for node in soup.select("script:not([src])")
            if re.search(r"ai1ec|calendar|ajax|event|wp-json", node.get_text() or "", re.I)
        ][:8],
    }
    results = []
    if source["name"] == "NWTheatre Calendar":
        feed_url = "https://www.nwtheatre.org/"
        feed_response = requests.get(
            feed_url,
            params={"plugin": "all-in-one-event-calendar",
                    "controller": "ai1ec_exporter_controller",
                    "action": "export_events", "no_html": "true"},
            headers={**HEADERS, "Accept": "text/calendar"},
            timeout=30)
        feed_response.raise_for_status()
        feed_text = feed_response.text or ""
        ai1ec_results = parse_ai1ec_ical(feed_text, source)
        diagnostic["ai1ec_export"] = {
            "status": feed_response.status_code,
            "content_type": feed_response.headers.get("Content-Type", ""),
            "bytes": len(feed_response.content),
            "vevents": feed_text.count("BEGIN:VEVENT"),
            "parsed_events": len(ai1ec_results),
            "dated_performances": sum(len(x.get("performance_dates", [])) for x in ai1ec_results)
        }
        results.extend(ai1ec_results)
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
                        event_item = {"title":clean(obj["name"]), "start_date":start, "end_date":end,
                          "performance_dates":[start], "venue":clean(venue), "producer":clean(producer),
                          "description":clean(obj.get("description"))[:600],
                          "url":link, "event_url":link, "source":source["name"]}
                        if end and start and end > start:
                            event_item["run_end_date"] = end
                        results.append(event_item)
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
        # Use the link attached to the event title, not the first link anywhere
        # in a broad parent container (which can point to an unrelated event).
        anchor = heading.select_one("a[href]") or heading.find_parent("a", href=True)
        if not anchor:
            anchor = node.select_one('[itemprop="url"][href], a.event-title[href], a.tribe-events-calendar-list__event-title-link[href]')
        venue_node = node.select_one('[itemprop="location"], .venue, .event-venue')
        desc_node = node.select_one('[itemprop="description"], .event-description')
        producer_node = node.select_one("[itemprop=organizer], .organizer, .presented-by, .producer")
        results.append({"title":title, "start_date":start, "end_date":start,
          "performance_dates":[start], "venue":clean(venue_node) if venue_node else "",
          "producer":clean(producer_node) if producer_node else "",
          "description":clean(desc_node)[:600] if desc_node else "",
          "url":urljoin(source["url"], anchor["href"]) if anchor and anchor.get("href") else source["url"],
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
    return {"productions": results, "diagnostic": diagnostic}


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

    # Exclude speakeasy/casino-format events regardless of whether they
    # describe themselves as immersive. Immersive plays remain eligible.
    speakeasy_patterns = [
        r"\bspeakeas(?:y|ies)\b",
        r"\bcasino\b",
        r"\bgambling\b",
    ]
    # A casino/gambling reference in the title is a clear event-format signal;
    # in descriptions, require a format phrase to avoid excluding a play just
    # because its story happens to mention a casino.
    if re.search(speakeasy_patterns[0], combined, re.IGNORECASE):
        return True
    if any(re.search(pattern, title, re.IGNORECASE) for pattern in speakeasy_patterns[1:]):
        return True
    if re.search(r"\bcasino\s+(?:night|format|style|themed|experience|event|party|games?)\b|\bgambling\s+(?:night|format|event|experience|party)\b", description, re.IGNORECASE):
        return True

    # Apply broad title-only exclusions such as tours and concerts.
    return any(term.casefold() in title for term in CFG.get("exclude_title_terms", []))

def parse_nwtheatre_run_end_dates(html_text, productions):
    """Attach independent directory closing dates to already-discovered shows only.

    This directory is validation metadata, never a source of candidate shows
    or performance dates. A title must match exactly before one date range.
    """
    soup = BeautifulSoup(html_text or "", "html.parser")
    page_text = clean(soup.get_text(" ", strip=True))
    date_pattern = r"\((\d{1,2})/(\d{1,2})\s*[-–]\s*(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\)"

    for production in productions:
        title = clean(production.get("title", ""))
        if not title:
            continue
        pattern = re.compile(re.escape(title) + r"\s*" + date_pattern, re.IGNORECASE)
        matches = list(pattern.finditer(page_text))
        parsed_dates = []
        for match in matches:
            month, day, end_month, end_day, end_year = match.groups()
            year = int(end_year or "2026")
            try:
                date(year, int(month), int(day))
                finish_year = year + (1 if int(end_month) < int(month) else 0)
                finish = date(finish_year, int(end_month), int(end_day))
            except ValueError:
                continue
            parsed_dates.append(finish.isoformat())
        unique_dates = sorted(set(parsed_dates))
        if len(unique_dates) == 1:
            production["run_end_date"] = unique_dates[0]
            production["run_end_source"] = "NWTheatre 2026 Shows directory"
    return productions

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
    records = {}
    # Re-key legacy records using producer identity where available, coalescing
    # duplicates that were previously split by inconsistent venue labels.
    for prior in existing:
        if not prior.get("title"):
            continue
        pid = identity(prior["title"], prior.get("producer") or prior.get("venue", ""))
        prior["id"] = pid
        if pid not in records:
            records[pid] = prior
        else:
            kept = records[pid]
            kept["performance_dates"] = sorted(set(kept.get("performance_dates", [])) | set(prior.get("performance_dates", [])))
            occurrences = kept.setdefault("performance_occurrences", [])
            seen = {(x.get("date"), x.get("event_url")) for x in occurrences}
            for occurrence in prior.get("performance_occurrences", []):
                key = (occurrence.get("date"), occurrence.get("event_url"))
                if key not in seen:
                    occurrences.append(occurrence)
                    seen.add(key)
            kept["sources"] = list(dict.fromkeys((kept.get("sources") or []) + (prior.get("sources") or [])))
    for item in incoming:
        if not item.get("title") or not item.get("url"):
            continue
        pid = identity(item["title"], item.get("producer") or item.get("venue", ""))
        item["id"] = pid
        item_dates = set(item.get("performance_dates") or [])
        if item.get("start_date"):
            item_dates.add(item["start_date"])
        # Keep each explicitly listed performance date paired with the event URL
        # that supplied it. This prevents a merged production from retaining an
        # unrelated/older occurrence URL.
        incoming_occurrences = [
            {"date": day, "event_url": item.get("event_url") or item.get("url")}
            for day in sorted(item_dates)
            if item.get("event_url") or item.get("url")
        ]
        prior = records.get(pid)
        if prior:
            # Preserve exact listed dates; never infer that a production runs daily.
            known = set(prior.get("performance_dates") or [])
            if prior.get("start_date"):
                known.add(prior["start_date"])
            prior["performance_dates"] = sorted(known | item_dates)
            occurrences = prior.get("performance_occurrences") or []
            occurrence_keys = {(x.get("date"), x.get("event_url")) for x in occurrences}
            for occurrence in incoming_occurrences:
                key = (occurrence["date"], occurrence["event_url"])
                if key not in occurrence_keys:
                    occurrences.append(occurrence)
                    occurrence_keys.add(key)
            prior["performance_occurrences"] = occurrences
            if prior["performance_dates"]:
                prior["start_date"] = prior["performance_dates"][0]
                prior["end_date"] = prior["performance_dates"][-1]
            if item.get("run_end_date") and (not prior.get("run_end_date") or item["run_end_date"] > prior["run_end_date"]):
                prior["run_end_date"] = item["run_end_date"]
            for key in ("venue", "producer", "description", "event_url", "producer_url", "ticket_url", "source_priority", "producer_type"):
                if item.get(key) and (not prior.get(key) or key in ("producer_url", "ticket_url", "source_priority", "producer_type")):
                    prior[key] = item[key]
            # Replace an older calendar URL with the best destination discovered now.
            # Refresh stale event URLs from the current calendar extraction.
            # Preserve a previously validated producer/ticket destination when present.
            if item.get("event_url"):
                prior["event_url"] = item["event_url"]
                if not prior.get("producer_url") and not prior.get("ticket_url"):
                    prior["url"] = item["event_url"]
            if item.get("url") and (item.get("producer_url") or item.get("ticket_url")):
                prior["url"] = item["url"]
            prior.setdefault("sources", [])
            if item["source"] not in prior["sources"]:
                prior["sources"].append(item["source"])
        else:
            item["performance_dates"] = sorted(item_dates)
            item["performance_occurrences"] = incoming_occurrences
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
    # Fetch calendars sequentially to reduce bursts that may trigger source-side throttling.
    for source in enabled_sources:
        try:
            extracted = extract(source)
            listings = extracted["productions"]
            found.extend(listings)
            dated = sum(1 for item in listings if item.get("performance_dates") or item.get("start_date"))
            print("Fetched:", source["name"])
            diag = extracted.get("diagnostic", {})
            print("SOURCE RESPONSE:", source["name"],
                  "| HTTP:", diag.get("status"),
                  "| bytes:", diag.get("bytes"),
                  "| final URL:", diag.get("final_url"),
                  "| title:", diag.get("page_title"))
            print("SOURCE MARKUP:", source["name"],
                  "| JSON-LD:", diag.get("jsonld_scripts"),
                  "| event-like nodes:", diag.get("event_nodes"),
                  "| time nodes:", diag.get("time_nodes"),
                  "| links:", diag.get("links"))
            if source["name"] == "NWTheatre Calendar":
                print("NWTHEATRE SCRIPTS:", json.dumps(diag.get("scripts", []), ensure_ascii=False))
                print("NWTHEATRE IFRAMES:", json.dumps(diag.get("iframes", []), ensure_ascii=False))
                print("NWTHEATRE CALENDAR MARKERS:", json.dumps(diag.get("calendar_markers", {}), ensure_ascii=False))
                print("NWTHEATRE DATE ELEMENTS:", json.dumps(diag.get("date_attributes", []), ensure_ascii=False))
                print("NWTHEATRE JSONLD:", json.dumps(diag.get("jsonld_summaries", []), ensure_ascii=False))
                print("NWTHEATRE INLINE CALENDAR DATA:", json.dumps(diag.get("calendar_inline_scripts", []), ensure_ascii=False))
                print("NWTHEATRE AI1EC EXPORT:", json.dumps(diag.get("ai1ec_export", {}), ensure_ascii=False))
            print("CALENDAR INVENTORY:", source["name"], "-", len(listings), "dated listings")
            for item in listings[:3]:
                print("  LISTING:", clean(item.get("title", "(untitled)")), "| dates:", ", ".join(item.get("performance_dates") or ([item.get("start_date")] if item.get("start_date") else [])) or "(none)")
        except Exception as exc:
            print("SOURCE ERROR", source["name"], str(exc), file=sys.stderr)
    productions = merge(productions, found)

    # Independent run-end cross-check: metadata only. This source cannot add
    # productions or performance dates; it can only validate a closing date
    # for a title already present in the calendar-derived inventory.
    try:
        directory_url = "https://www.nwtheatre.org/shows2026/"
        directory_response = requests.get(directory_url, headers=HEADERS, timeout=15)
        directory_response.raise_for_status()
        before_count = len(productions)
        productions = parse_nwtheatre_run_end_dates(directory_response.text, productions)
        validated = [p for p in productions if p.get("run_end_source") == "NWTheatre 2026 Shows directory"]
        print("RUN-END CROSS-CHECK:", len(validated), "existing productions matched; inventory unchanged:", len(productions) == before_count)
    except Exception as exc:
        print("RUN-END CROSS-CHECK WARNING:", str(exc))

    already = {x.get("production_id") for x in featured}
    # Title-level suppression preserves historical featured entries across
    # the identity-key migration from venue-based to producer-based IDs.
    already_titles = {clean(x.get("title")).casefold() for x in featured if x.get("title")}
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
        if p.get("id") in already or clean(p.get("title")).casefold() in already_titles or p.get("featured"):
            continue
        # A pick must have a performance explicitly listed for today.
        performance_dates = set(p.get("performance_dates") or [])
        if not performance_dates and p.get("start_date"):
            performance_dates.add(p["start_date"])
        if today not in performance_dates:
            continue
        # Reject stale calendar occurrences only when an independent directory
        # explicitly confirms that the production closed before today.
        if p.get("run_end_source") == "NWTheatre 2026 Shows directory" and p.get("run_end_date") and p["run_end_date"] < today:
            print("SKIPPED CLOSED PRODUCTION:", p.get("title"), "- run ended", p["run_end_date"])
            continue
        if is_excluded_event(p):
            print("SKIPPED NON-PRODUCTION EVENT:", p.get("title", "(untitled)"))
            continue
        key = producer_key(p)
        if key and key in recent_producers:
            print("SKIPPED FOR PRODUCER VARIETY:", p.get("title", "(untitled)"), "-", p.get("producer") or p.get("venue"))
            continue
        candidates.append(p)
    # A verified earlier closing date wins; unknown closing dates sort after known dates.
    # Existing presenter/source priorities break ties between equally urgent shows.
    candidates.sort(key=lambda p:(p.get("run_end_date") or "9999-12-31",
      1 if producer_is_presenter(p) else 0, p.get("source_priority", 3),
      p["title"].casefold(), p.get("venue","").casefold()))
    if not candidates:
        print("No validated future production found. No post will be published.")
    else:
        pick = candidates[0]
        # Report and use the exact occurrence URL associated with today's listed date.
        todays_occurrences = [
            occurrence for occurrence in (pick.get("performance_occurrences") or [])
            if occurrence.get("date") == today and occurrence.get("event_url")
        ]
        # Prefer the producing company's own domain for the matching date;
        # use a ticketing/calendar URL only when no producer event page exists.
        preferred_hosts = {
            urlparse(source.get("url", "")).netloc.casefold().removeprefix("www.")
            for source in CFG.get("sources", [])
            if source.get("producer") and clean(source.get("producer")).casefold() == clean(pick.get("producer")).casefold()
        }
        calendar_hosts = {
            urlparse(source.get("url", "")).netloc.casefold().removeprefix("www.")
            for source in CFG.get("sources", [])
        }
        def occurrence_rank(occurrence):
            host = urlparse(occurrence.get("event_url", "")).netloc.casefold().removeprefix("www.")
            is_calendar = host in calendar_hosts
            is_ticket = is_ticketing_host(host)
            is_known_producer = host in preferred_hosts
            # Prefer a producer's dated event page over a ticketing URL or aggregator listing.
            return (0 if is_known_producer or (not is_calendar and not is_ticket) else 1,
                    1 if is_ticket else 0, occurrence.get("event_url", ""))
        event_url = (sorted(todays_occurrences, key=occurrence_rank)[0].get("event_url") if todays_occurrences
                     else pick.get("event_url") or pick.get("url"))
        print("CONFIRMED PERFORMANCE DATE:", today)
        print("MATCHING EVENT LISTING:", event_url)
        preferred, producer_url, ticket_url = event_links(event_url, pick.get("url"), pick)
        # Use only destinations verified from this event page on this run.
        # Never let stale saved producer/ticket URLs override fresh resolution.
        pick["producer_url"] = producer_url
        pick["ticket_url"] = ticket_url
        pick["url"] = preferred
        print("LINK RESOLUTION:", "producer=", producer_url or "(none)", "ticket=", ticket_url or "(none)", "event=", event_url)
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
