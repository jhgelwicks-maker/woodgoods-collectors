-- Run in the Supabase SQL editor of the Lovable project. Idempotent.
-- 1) Columns the collectors write (added only if missing).
alter table event_candidates
  add column if not exists source text,
  add column if not exists source_event_id text,
  add column if not exists dedupe_key text,
  add column if not exists name text,
  add column if not exists sport text,
  add column if not exists organizer_name text,
  add column if not exists start_date date,
  add column if not exists end_date date,
  add column if not exists days int,
  add column if not exists is_league boolean default false,
  add column if not exists venue_name text,
  add column if not exists city text,
  add column if not exists state text,
  add column if not exists city_states jsonb default '[]'::jsonb,
  add column if not exists venue_count int,
  add column if not exists split_venue boolean default false,
  add column if not exists team_count int,
  add column if not exists division_count int,
  add column if not exists divisions text,
  add column if not exists vendor_contact_email text,
  add column if not exists vendor_contact_name text,
  add column if not exists age_groups text,
  add column if not exists listing_url text,
  add column if not exists official_url text,
  add column if not exists raw jsonb,
  add column if not exists gate_passed boolean default false,
  add column if not exists clay_sent_at timestamptz,
  add column if not exists first_seen_at timestamptz default now(),
  add column if not exists last_seen_at timestamptz;
create unique index if not exists event_candidates_dedupe_key on event_candidates (dedupe_key);

-- 2) Run log per collector (source health: a source that drops to zero is broken).
create table if not exists source_runs (
  id bigserial primary key, source text, started_at timestamptz, finished_at timestamptz,
  rows_seen int, rows_upserted int, errors int, note text
);

-- 3) Editable filter rules. Store everything; gate on read. Edit these rows from the Lovable admin.
create table if not exists filter_rules (
  key text primary key, value jsonb, description text
);
insert into filter_rules (key, value, description) values
  ('min_teams',      '30',                                        'Minimum registered teams'),
  ('max_days',       '3',                                         'Longer than this is a league, not a tournament'),
  ('sports',         '["lacrosse","soccer","baseball","softball","volleyball","hockey","field hockey","football"]', 'Sports to keep'),
  ('states',         '["MA","NH","CT","RI","NY","NJ","PA","MD","VT","ME"]', 'States to keep (any venue state matches)'),
  ('future_only',    'true',                                      'Drop events already started')
on conflict (key) do nothing;

-- 4) gate_passed is computed by a function so changing a rule can re-gate everything.
create or replace function compute_gate(c event_candidates) returns boolean language sql stable as $$
  select
    coalesce(c.team_count, 0) >= (select (value)::int from filter_rules where key='min_teams')
    and coalesce(c.days, 1) <= (select (value)::int from filter_rules where key='max_days')
    and c.sport = any (select jsonb_array_elements_text(value) from filter_rules where key='sports')
    and exists (select 1 from jsonb_array_elements_text(c.city_states) cs
                where split_part(cs, ', ', 2) = any (select jsonb_array_elements_text(value) from filter_rules where key='states'))
    and (not (select (value)::boolean from filter_rules where key='future_only') or c.start_date >= current_date)
$$;

create or replace function trg_set_gate() returns trigger language plpgsql as $$
begin new.gate_passed := compute_gate(new); return new; end $$;
drop trigger if exists set_gate on event_candidates;
create trigger set_gate before insert or update on event_candidates
  for each row execute function trg_set_gate();

-- Call after editing filter_rules to re-gate every row.
create or replace function regate_all() returns int language plpgsql as $$
declare n int; begin update event_candidates set gate_passed = compute_gate(event_candidates); get diagnostics n = row_count; return n; end $$;

-- 5) Clay: rows that pass the gate and haven't been sent. Point a Supabase Database Webhook
--    (Database > Webhooks) at this: table event_candidates, events INSERT + UPDATE, and in the
--    edge function / Zapier filter on gate_passed = true AND clay_sent_at IS NULL, then POST the row
--    to your Clay webhook table URL and set clay_sent_at. Clay writes contacts back with its HTTP API
--    action to /rest/v1/candidate_contacts.
create or replace view clay_queue as
  select id, dedupe_key, name, sport, organizer_name, city, state, start_date, listing_url, official_url, team_count
  from event_candidates where gate_passed and clay_sent_at is null;
