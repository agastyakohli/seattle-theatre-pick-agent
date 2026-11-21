# Seattle Theatre Pick of the Day

A zero-paid-service-cost starter for discovering Puget Sound theatre productions, tracking each production once, and publishing one eligible pick per day to a Facebook Page.

## Current implementation status

This is a **starter implementation**, not yet connected to your Facebook Page. It supports RSS/Atom and iCalendar source ingestion, HTML extraction fallback, title/date/venue deduplication, JSON production history, daily selection, Facebook Page publishing, and GitHub Actions execution. Generic HTML extraction needs validation and may need site-specific adapters.

## Cost model

- GitHub Actions: free for public repositories under current policy.
- Meta Graph API: no per-post API fee; requires a Meta developer app, permissions, and Page access token.
- No paid AI or scraping service is used.

Public repository means source code is visible; credentials must remain in GitHub Actions Secrets.

## Setup

1. Add repository secrets `FB_PAGE_ID` and `FB_PAGE_ACCESS_TOKEN`.
2. Configure a Meta app and Page token with `pages_manage_posts`, `pages_read_engagement`, and `pages_show_list`; the authorizing person must have the Page task that permits content creation.
3. Edit `config/sources.json` to add/disable sources.
4. Run Actions manually with `dry_run=true` and inspect the proposed selection and data files.
5. Seed `data/featured.json` with prior page picks before enabling automatic publishing, to prevent repeats from older posts.
6. Only enable scheduled publishing after validating source extraction and deduplication.

Never commit or paste access tokens into chat. Tokens can expire or be invalidated by permission, app, security, or Page-access changes.

## Initial discovery sources

- Theatre Puget Sound community calendar
- NWTheatre regional calendar
- Seattle Theatre Group calendar
- The 5th Avenue Theatre calendar

These are starting points, not comprehensive coverage. A source is not fully integrated until a test confirms title, dates, venue, and ticket URL extraction.

## Selection rules and limitations

The agent selects one unfeatured future production per day and skips days with no eligible candidate. A production is intended to be featured once regardless of multiple performances. Matching is currently conservative and title/venue based; ambiguous title collisions and aliases require further refinement. Generic HTML extraction is not production-ready. Audit initial runs before enabling unattended publishing.
