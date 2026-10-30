# woodgoods-collectors — project handoff

Weekly and historical crawl of youth-sports tournament sources into the Woodgoods Ops app
(Lovable Cloud). Woodgoods sells custom heat-pressed hats and sunglasses from a booth at
tournaments. The data exists to answer one question per event: **is this weekend worth sending
a kit and an operator to, and at which venue/location on the site?**

Repo: github.com/jhgelwicks-maker/woodgoods-collectors (make it PUBLIC: private repos get
2,000 Actions minutes/month, and the backfill needs far more). Owner: Justin Gelwicks,
non-developer. Explain in plain language; never assume git or terminal familiarity.

**Read `../BUSINESS.md` (i.e. `~/WoodGoodsAI/BUSINESS.md`) at the start of every session.** It holds the
business context and goals. It lives outside this repo on purpose because the repo is public: never
copy it, or the money, contacts or plans in it, into the repo.

## Architecture

- **Collectors (this repo, Python 3.12, GitHub Actions)** scrape sources and POST rows to the app.
- **App (Lovable, separate — do not try to edit it here)** owns the database, UI, ingest endpoint,
  nightly jobs (precedent matching, recurrence placeholders, gating, benchmarks). Changes to the
  app are requested by messaging the Lovable project in plain English. In Lovable the project is named
  "Hat Hustle HQ" (the app itself says Woodgoods Ops).
- There is no direct database access from here. Everything goes through the ingest endpoint.

## Ingest contract (the thing that has never been verified end to end — do this FIRST)

`POST $INGEST_URL` (currently `https://opswg.lovable.app/api/public/ingest`), header
`x-ingest-key: $INGEST_KEY`, JSON body, one of:

- `{"source": "<slug>", "rows": [ ...event_candidates rows... ]}` — upsert on `dedupe_key`
- `{"source": "<slug>", "run": {started_at, finished_at, rows_seen, rows_upserted, errors, note}}` — source_runs
- `{"source": "<slug>", "history": [ ...event_history rows... ]}` — upsert on `dedupe_key`
- `{"contacts": [ {dedupe_key, name, role, email, phone, source_url, confidence, enriched_via} ]}` — from Clay

Rules the endpoint is supposed to follow: dedupe within batch; never overwrite a non-null value with
null; unknown keys ignored, not rejected; `first_seen_at` kept, `last_seen_at` set to now.

**Known failure (2026-10-06):** the backfill posted `history` batches and got
`400 {"error":"rows or run required"}` on every one → the `history` branch is missing or not
deployed on the published site. 8.5 hours of crawling saved nothing. Before any backfill rerun:
send one `history` row, one `rows` row, one `contacts` row by hand and confirm 200 + a visible row
in the app. If `history` is rejected, message Lovable: "ingest must accept {history:[...]} as
specified; it currently returns 'rows or run required'". Also ask it to republish.

## Row shape (event_candidates / event_history)

Produced by `collectors/common.py::normalize()`. Every collector returns `(rows, errors)` with
dicts carrying at least `source`, `source_event_id`, `name`, `sport`, `start_date`, `end_date`,
`city_states` (list of "City, ST"). normalize() adds: `dedupe_key = source:source_event_id`,
`days`, `is_league` (>3 days), `city`, `state`, `venue_count`, `split_venue`, `organizer_canon`,
`name_core`, `event_fingerprint` (sport|STATE|name_core), `calendar_week`, `age_groups`,
`grad_year_min/max`, `raw.gender`, `expected_attendance` (tournaments: teams × roster × 2.33;
festivals: parsed from text), and for festivals `size_score`, `destination_score`, `fit_score`,
signal `tags`. See docstring at top of common.py for the full key list.

Tourney Machine rows additionally carry: `venues[]` ({venue_name, address, lat, long, teams,
games, fields[], field_count, schedule[]}), `fields_used`, `teams_per_field`,
`teams_per_location_avg`, `teams_per_location_sched_avg`, `schedule_profile`,
`schedule_quality` (0–100), `day1_hours`, `last_day_hours`, `last_day_pm_field_share`,
`documents[]` ({url, filename, type, kind: field_map|rules|logo|image|document}),
`has_field_map`, `clubs[]` ({club, teams}), `club_count`, `registration_snapshot`,
`weather_climatology` + `sun_score` + `uv_index` (upcoming), `weather_actual` (history).

## Sources

| module | what | notes |
|---|---|---|
| tourneymachine | public JSON search `api.tourneymachine.com/v2/Tournaments/Search?query=&per_page=100&page=` (year + sport keywords enumerate the whole index, ~2,500 upcoming) + event page `Public/Results/Tournament.aspx?IDTournament=` (team dropdown, `loadLocations()` complexes, addresses, documents) + `Division.aspx` (schedule rows: `data-gameid`, `data-facilityid`, `data-teamid`, `schedule_row date_YYYYMMDD`, times) | needs browser-ish headers; 429s at high parallelism; detail fetch only inside 120 days + ¼ of the rest per week; `collect_history()` archives events finished in last 10 days |
| backfill_tourneymachine | one-off: sequential short links `tourneymachine.com/R{n}` → event page redirect. R68000 ≈ Jan 2020, R15000 ≈ 2016, R188500 ≈ Oct 2026 | run via `backfill.yml` (workflow_dispatch, sharded matrix, newest first). Posts `history` every 150 rows. ~300 events / 170 min observed at 4,000-id shards with 0.5s sleep + serial division fetches — too slow; see fixes below |
| mayouthsoccer | state association HTML page; tournaments are table columns, parsed directly (no Claude, no key). Each "Tournament Website" link is fetched once; failures/error pages record `raw.website_info = "NO INFO AVAILABLE"` | site has a broken cert chain (verify=False for that host); dates may carry ordinals ("11th") |
| fairsandfestivals | 50 state pages, plain HTML; detail pages for dates/address/attendance; fees/contacts are paid-member fields (FF_COOKIE secret unlocks) | throttles after bursts; 2 workers, retry a blank state once |
| eventeny | national feed `eventeny.com/events/?page=N` (~1,000 events; the `?state=` filter only applies to page 1, so don't loop states); JSON-LD on each event page gives start/end/location/organizer; vendor page gives fees/deadline/rain date | 3 workers |
| organizers.find | who runs an event series + their website. Stages, cheapest first: stored listings (app's saved listings incl. past ones) → Tourney Machine live search → event PDFs (hints only) → Claude web research (paid). Listing stages prove the match with `event_match` (never name alone); web/PDF/model answers must pass `verify()` (we fetch the page and it must name the event AND its town/venue). Status: found_verified / needs_review / not_found | accuracy: `tools/eval_find_organizer.py` vs `tests/fixtures/find_organizer_truth.json` (38 real events); `organizers-eval.yml` runs it in Actions and writes summaries to branch `eval-results`. Replaced organizer_discovery.py (Oct 2026) |
| organizers.crawl | Crawl Organizer, lacrosse first, run locally (not in weekly.yml yet). A organizer list (`registry.py`: the 14 known + Tourney Machine organizers from a local export + `lacrosse_websites.json`, names/websites only) → B find event/contact/vendor pages → C extract (`extract.py`: schema.org, date-anchored blocks, event pages; Claude only with `--ai`) → D match (`match.py`: duplicate of an app listing? confirms/moves a placeholder? via `event_match`, one-to-one). Event rows `org-<slug>` carry `venues[]` so the app maps facilities. Organizer records (email/phone/vendor page/vendor contact) go to `~/WoodGoodsAI/crawl/<date>/`, never the repo, until the app has an organizers payload. Vendor contact only for organizers reaching 20+ teams by precedent (never by current registrations) | `python -m collectors.organizers.crawl --list / --org apex / --known`; accuracy vs the 14 scrapers: `tools/eval_crawl.py`. `--send` posts only new events for organizers without a scraper |
| woodgoods_lacrosse | wraps 18 organizer-site scrapers in `adapters/` + `core/` (NXT, MLT, APEX, Trilogy, Victory, PrimeTime, Adrenaline, Aloha, Alliance, Buku, Hogan's, ML8, NLF, PLL; `more_sites.py`: OT Lacrosse, Exposure Events (lacrosse listings, JSON via the page's own POST + token; skips organizers we scrape), Legends (event list base64-encoded in the page), Carpetbagger (cblax.org cards pair the event page, which has venues split by boys/girls, with a LeagueApps tournament whose member-portal JSON gives dates + divisions)). Sources `wg-<org>`. `adapters/details.py` fills age divisions (grad years from years/grades/U-ages, school-year aware) and contacts (organizer's own domain only; roles vendor/director/organizer) from each event page; contacts go to the app's `{"contacts":[...]}` after the rows (run.py) | 414 rows/zero errors on 2026-10-27; ages on 392, contacts on 375, a vendor inbox on 76. Rarely have team counts |

Secrets (GitHub → Settings → Secrets → Actions): `INGEST_URL`, `INGEST_KEY`, `EXPORT_KEY` (badges job), `ANTHROPIC_API_KEY`
(may be absent), `ANTHROPIC_WORKSPACE_ID` (optional), `FF_COOKIE` (optional). `run.py` skips
collectors whose required key is missing.

## Workflows

- `organizers-eval.yml`: manual or on push to `eval/find-organizer`; measures Find Organizer accuracy and cost.

- `weekly.yml`: Mon 06:00 UTC, matrix over the five sources, fail-fast off. Then the `badges` job (after every source,
  even if one failed): pulls event_candidates/event_history/venues with `EXPORT_KEY`, runs `tools/lacrosse_match_report.py`
  on that week's organizer-site rows (artifact from the woodgoods_lacrosse step) and sends the badge records. A scrape
  missing 3+ events and >30% of an organizer's app events retires nothing as gone; records not applied are retried once.
- `backfill.yml`: manual. Inputs start/end/shard_size. Plan job builds the matrix newest-first.
- `found-sites-crawl.yml`: manual only, meant to run once. Crawls every organizer in `collectors/organizers/lacrosse_websites.json`
  (82 sites found by `tools/web_confirm.py`'s Brave searches, with the event pages found there as start pages), default ONE job
  (~1 hour; one job also catches an event two organizers list). Steps: crawl (no send) -> `lacrosse_match_report.py --matched-only`
  dry run -> `crawl --send-file --match-csv --send` (crawl.select_for_send: events matching an app row go; a NEW event only if
  good_new_event(): tournament name, not a program/league/page label, <=3 days, not another organizer's event on a club's page,
  not from a "Team …"/"… Club" site, not another listing of a matched event within 30 days; held rows logged) -> re-pull
  event_candidates -> report `--matched-only --send` on `sent.jsonl` (badges; placeholders hidden behind organizer rows; no
  grey/not-on-site/retire records). Junk guards in the crawl: news/recruiting/camp pages, taglines, share links, venue-as-name,
  office-address towns (postal line on every page), conflicting dates on one site, copies and division lines merged.
  Full local dry run 2026-10-30: 164 crawled -> 77 pass (56 matched to app rows, 21 new). No artifacts (rows hold contacts).

## Mac-side setup (outside the repo, private)

- `~/WoodGoodsAI/.ingest.env` (INGEST_URL, INGEST_KEY) and `~/WoodGoodsAI/.export.env` (EXPORT_KEY). Never commit.
  GitHub's INGEST_KEY secret and Lovable's must match the Mac's. EXPORT_KEY lives in Lovable, on the Mac and (for the
  weekly badges job) in GitHub's secrets.
- Backfill now runs on the Mac: `python -m collectors.backfill_local START END --parallel 4`. Rows are written to
  `~/WoodGoodsAI/backup/backfill/R<a>-<b>.jsonl` before posting; `.done` markers make reruns resume;
  `python -m collectors.replay_history <files>` resends. Skips unfinished events and junk (pre-2014 dates, test/mock names).
  2026-10-08: R68000–185500 done, 101,717 history rows, 0 errors.
- Lacrosse match report: `python tools/lacrosse_match_report.py` scrapes the 14 organizer sites and matches every event
  against the app's upcoming lacrosse rows (placeholders, Tourney Machine, App data(manual-lacrosse-sheet)) from the newest local export
  (newest of `backup/db/current` and `fresh`). CSV in `~/WoodGoodsAI/reports/` with CONFIRMED / DATE MOVED / DATE CONFLICT / REVIEW /
  NOT ON ORGANIZER SITE / NEW plus tags. `--scraped <file>.scraped.jsonl` reruns without scraping.
  Badge records (`<out>.checks.json`, sent with `--send` as `{"source":"organizer-check","confirmations":[...]}`) for a surely
  matched event also tell the app which placeholders the organizer's site rows replace (`replaces` / `replaced_by`), last
  year's Tourney Machine records to link (`history_ids`), and last year's numbers at each site's venue (`last_year_venue`,
  added up over Saturday/Sunday tournaments of one weekend, with `schedule_quality` from the same `schedule_score()` every
  Tourney Machine event gets, run on that venue's days only). Placeholders hidden in favour of an organizer row still
  take part in matching (else confirmed events would turn back into NEW). Each run also retires leftover organizer rows:
  OLD COPY (an earlier id of a row whose scraper later learned the venue; `replaced_by` the current row) and GONE FROM
  ORGANIZER SITE (`retire: true`), only for upcoming rows of organizers the scrape reached. Organizer rows themselves carry `venues[]` (one venue per site).
  Found 2026-10-23: the app rolls placeholders forward by ISO week number (+371 days from 2026, which has 53 weeks), so
  7,851 of 9,176 placeholders (all sports) sit a week after the real weekend; organizers repeat at +364 days.
- Database backup: `tools/backup_db.py` pulls every table from the app's read-only `GET /api/public/export`
  (header `x-export-key`, 1,000 rows per page) into `~/WoodGoodsAI/backup/db/current` (+ `previous`).
  Runs Mondays 09:00 via `~/Library/LaunchAgents/com.woodgoods.dbbackup.plist`, and after every local backfill.
  Log: `~/WoodGoodsAI/backup/logs/db-backup.log`.

## Decisions already made (don't relitigate without reason)

- **Precedent over registrations.** For Tourney Machine events, current team_count is noise until
  the week of; the app shows `expected_teams` from last year's final (median of last 3), falls back
  to organizer precedent, then `fields_used × median teams/field`, then registrations.
  Registrations win only if higher than history or inside 7 days.
- **Recurrence placeholders**: every completed history row spawns next year's candidate (same
  ISO week/weekday) so lacrosse events that aren't listed until the week of still appear months out.
- **Venue > event.** Revenue depends on foot-traffic layout, not team count. Rules learned from
  9 venues Justin has worked (flow score 0–100): one forced corridor between parking and fields
  with the village on it is everything; roads/ponds split a site into separate venues and only the
  village's pod counts; parking adjacency only matters if walkable (tree lines/fences make the
  path); narrow corridors put team tents and porta-potties next to the village (good), wide grass
  margins drain dwell time; food co-located with the village is the single biggest amenity factor
  (lunch rush); score per sport (ignore baseball diamonds for lacrosse). Reference set: Fore Kicks
  III Norfolk MA 100, CT Sportsplex 85, River City (south grid) 78, SUSA Central Islip 78, Liberty
  Upper Marlboro 72, Maryland SoccerPlex pod 70 / site 30, Farmington CT 62, Blue Sky 45, United
  Sports Downingtown 28, Capelli Tinton Falls 20. Venue value = flow × teams in reach.
- **Effective teams** = teams on fields within walking reach of the village (from schedule field
  assignments), not the listing total. A 300-team SoccerPlex event is ~60 effective teams.
- **Unique team = exact team name within its division (Justin, 2026-10-20).** "Tomahawks 2028" and
  "Tomahawks 2027", "2028 Black" and "2028 Purple", "2028 A" and "2028 AA" are different teams, and the
  same name in two divisions is two teams. Count teams by Tourney Machine team ID (`data-teamid` / the
  team dropdown's option value), which is already unique per division — never by name alone. Any code
  that reads division schedules must keep the division with every game; a schedule that loses it merges
  clubs that reuse names across age groups (Fore Kicks Scarecrow 2025: 21 names for 55 teams). Sanity
  check every schedule: no team on two fields at the same time, no team with more than ~5 games a day.
  `clubs[]` deliberately rolls teams up to the club (organization) for audience tagging; it is not a
  team count.
- **Schedule quality**: long days both days + a meaningful share of Saturday's fields still playing
  after 2:30pm on the last day. Love 2 Lax (Sat 8–6:40 on 14 fields, Sun 8–5:20 on 10, 8 late) = 93.
  Sunday ending 11am ≈ 33; one championship field until 6 ≈ 39.
- **Weather**: Open-Meteo (no key). Climatology is a 20-year ±7-day base rate, never "it always
  rains this weekend". Sun score blends expected high, sunshine hours, UV, discounted by rain
  probability; hats and sunglasses sell on hot sunny days; southern venues score higher.
- Festivals: nothing excluded except non-public events; everything else is a tag + fit penalty.
  Power/generator tags were removed on purpose (Justin doesn't care).
- Boys' lacrosse is the only sport with revenue history. Everything else is unproven.

## Immediate tasks, in order

1. Verify the ingest contract (all four payload kinds) against the published app. Fix via Lovable
   message if any is rejected. Nothing else matters until this passes.
2. Backfill fixes: stop the shard immediately on a rejected post (raise, don't log and continue);
   fetch division pages with a 4-thread pool capped at 16 divisions/event; drop the per-event
   weather call (the app fills `weather_actual` nightly); sleep 0.25s between short-link fetches;
   shard_size default 1500; max-parallel 10. Measure seconds/id on 20 ids before launching.
3. Run ONE shard (e.g. 187000–188500) end to end and confirm rows appear in the app's Source
   health panel and Candidates/History, then launch 68000–188500 with the defaults.
4. Alliance adapter returns 0 rows — inspect.
5. Then, as Justin asks: GotSport (needs a browser network capture of the XHR; server fetch is
   403), Perfect Game / USSSA (JS-rendered; find XHR or use a render service), venue layout
   analyzer (OSM geometry + satellite + site-map annotations; calibration set above).

## Working style

Commit and push yourself; never hand Justin a zip. Before any multi-hour run, run a 2-minute
smoke test against the real endpoint. Log one summary line per collector. When something fails,
say what failed, what was lost, and what to do next, in that order.
