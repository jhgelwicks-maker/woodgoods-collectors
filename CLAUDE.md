# woodgoods-collectors — project handoff

Weekly and historical crawl of youth-sports tournament sources into the Woodgoods Ops app
(Lovable Cloud). Woodgoods sells custom heat-pressed hats and sunglasses from a booth at
tournaments. The data exists to answer one question per event: **is this weekend worth sending
a kit and an operator to, and at which venue/location on the site?**

Repo: github.com/jhgelwicks-maker/woodgoods-collectors (make it PUBLIC: private repos get
2,000 Actions minutes/month, and the backfill needs far more). Owner: Justin Gelwicks,
non-developer. Explain in plain language; never assume git or terminal familiarity.

## Architecture

- **Collectors (this repo, Python 3.12, GitHub Actions)** scrape sources and POST rows to the app.
- **App (Lovable, separate — do not try to edit it here)** owns the database, UI, ingest endpoint,
  nightly jobs (precedent matching, recurrence placeholders, gating, benchmarks). Changes to the
  app are requested by messaging the Lovable project "Woodgoods Ops" in plain English.
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
| mayouthsoccer | state association HTML page → Claude extracts JSON | needs ANTHROPIC_API_KEY (+ ANTHROPIC_WORKSPACE_ID header if the key is org-scoped); site has a broken cert chain (verify=False for that host) |
| fairsandfestivals | 50 state pages, plain HTML; detail pages for dates/address/attendance; fees/contacts are paid-member fields (FF_COOKIE secret unlocks) | throttles after bursts; 2 workers, retry a blank state once |
| eventeny | national feed `eventeny.com/events/?page=N` (~1,000 events; the `?state=` filter only applies to page 1, so don't loop states); JSON-LD on each event page gives start/end/location/organizer; vendor page gives fees/deadline/rain date | 3 workers |
| woodgoods_lacrosse | wraps 14 organizer-site scrapers in `adapters/` + `core/` (NXT, MLT, APEX, Trilogy, Victory, PrimeTime, Adrenaline, Aloha, Alliance, Buku, Hogan's, ML8, NLF, PLL). Sources `wg-<org>` | 205 rows/zero errors on 2026-10-06; Alliance returned 0 — check. Rarely have team counts |

Secrets (GitHub → Settings → Secrets → Actions): `INGEST_URL`, `INGEST_KEY`, `ANTHROPIC_API_KEY`
(may be absent), `ANTHROPIC_WORKSPACE_ID` (optional), `FF_COOKIE` (optional). `run.py` skips
collectors whose required key is missing.

## Workflows

- `weekly.yml`: Mon 06:00 UTC, matrix over the five sources, fail-fast off, 170-min timeout each.
- `backfill.yml`: manual. Inputs start/end/shard_size. Plan job builds the matrix newest-first.

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
