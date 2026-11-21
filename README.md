# Seattle Theatre Pick of the Day

A zero-paid-service-cost agent for discovering Puget Sound theatre productions from third-party calendars, tracking each production once, and preparing a daily Facebook Page pick.

## Discovery and enrichment architecture

- **Discovery inventory:** Theatre Puget Sound Community Events Calendar, NWTheatre Calendar (including its iCalendar export), and Seattle Theatre Group's calendar.
- **Dates:** Only explicitly listed performance dates from those calendar sources qualify. A season page or advertised run range is not treated as proof of a performance on a particular day.
- **Selection:** The agent applies the editorial exclusions, one-production-ever rule, producer-rotation rule, and closing-date preference when a closing date is explicitly available in the calendar data.
- **Enrichment:** Only after a pick is selected does the agent open the dated calendar listing and follow its show/producer links to retrieve show information and resolve the best post destination. Official theatre websites are not part of the discovery inventory and cannot add or change performance dates.
- **Link priority:** Producer's specific show/event page, then ticketing page, then the original dated calendar listing.

## Current discovery sources

1. Theatre Puget Sound community calendar
2. NWTheatre regional calendar and iCalendar export
3. Seattle Theatre Group calendar (presenter listings receive lower priority)

Official producer websites are used only to enrich a selected pick. If an official page cannot be found or reached, the agent falls back to the dated calendar listing.

## Cost model

- GitHub Actions: free for public repositories under current policy.
- Meta Graph API: no per-post API fee; requires a Meta developer app, permissions, and Page access token.
- No paid AI or scraping service is used.

Public repository means source code is visible; credentials must remain in GitHub Actions Secrets.

## Setup and testing

1. Add repository secrets `FB_PAGE_ID` and `FB_PAGE_ACCESS_TOKEN`.
2. Configure a Meta app and Page token with `pages_manage_posts`, `pages_read_engagement`, and `pages_show_list`; the authorizing person must have the Page task that permits content creation.
3. Run Actions manually with `dry_run=true` and inspect the proposed selection and data files.
4. Seed `data/featured.json` with prior Page picks before enabling automatic publishing, to prevent repeats from older posts.
5. Keep scheduled runs in dry-run mode until calendar extraction, deduplication, enrichment links, and post previews have been validated.

Never commit or paste access tokens into chat. Tokens can expire or be invalidated by permission, app, security, or Page-access changes.

## Editorial scope and limitations

- Include plays, musicals, long-form improv, and sketch comedy.
- Exclude stand-up comedy, ballet, opera, classes, workshops, auditions, concerts/music tours, and non-production events.
- Only a specifically listed performance date qualifies; the agent does not assume every day in a show's run is a performance.
- A production is intended to be featured once, regardless of how many dates or calendars list it.
- Producer rotation discourages featuring the same company repeatedly within a rolling seven-day window.
- Third-party calendar extraction is still subject to source markup changes and occasional outages. Audit dry runs before enabling unattended publishing.
