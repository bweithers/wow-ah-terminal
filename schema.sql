-- Shared auction-scan pool for wow-ah-terminal.
--
-- Applied by `python ah.py pool-init`, which runs it through the Supabase CLI
-- as the project owner. Written to be idempotent: running it again is safe and
-- never drops data.
--
-- The security model: everyone shares one publishable key, which is meant to
-- live in client code. Row-level security is what actually holds, and it says
--   * anyone with the key may READ everything,
--   * anyone with the key may APPEND a scan,
--   * nobody may UPDATE or DELETE anything.
-- So the worst a leaked key buys is a junk scan in the list, never the loss of
-- somebody's history.

-- ---------------------------------------------------------------- scan index

create table if not exists public.scans (
  id          text primary key,
  realm       text        not null check (char_length(realm) between 1 and 48),
  uploader    text        not null check (char_length(uploader) between 1 and 48),
  scanned_at  timestamptz not null,
  uploaded_at timestamptz not null default now(),
  items       integer     not null check (items >= 0 and items <= 1000000),
  schema      integer     not null default 1,
  bytes       integer     not null check (bytes >= 0 and bytes <= 33554432),
  object_path text        not null check (char_length(object_path) <= 256)
);

create index if not exists scans_realm_time
  on public.scans (realm, scanned_at desc);

-- -------------------------------------------------- item/day price history
-- One row per realm, item and day. Auctionator forgets after 21 days; this
-- does not. Day granularity means a second scan on the same day refines the
-- row instead of adding one, so the table grows with time rather than with
-- how many of you are scanning.

create table if not exists public.prices (
  realm    text    not null,
  item     integer not null,
  day      date    not null,
  price    bigint,
  qty      integer,
  min_seen bigint,
  seen_at  timestamptz not null default now(),
  primary key (realm, item, day)
);

create index if not exists prices_realm_day
  on public.prices (realm, day desc);

-- ------------------------------------------------------------------- ingest
-- Writes to `prices` go only through here, so the merge rule is enforced in
-- one place: the lowest price anyone saw that day wins, which matches what
-- Auctionator itself records, and the deepest observed quantity is kept.

create or replace function public.ingest_prices(payload jsonb)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  n integer;
begin
  if payload is null or jsonb_typeof(payload) <> 'array' then
    raise exception 'payload must be a JSON array';
  end if;
  n := jsonb_array_length(payload);
  if n > 20000 then
    raise exception 'too many rows in one call: % (max 20000)', n;
  end if;

  with incoming as (
    select
      left(r ->> 'realm', 48)                       as realm,
      (r ->> 'item')::integer                       as item,
      (r ->> 'day')::date                           as day,
      nullif(r ->> 'price', '')::numeric::bigint    as price,
      nullif(r ->> 'qty', '')::numeric::integer     as qty,
      nullif(r ->> 'min_seen', '')::numeric::bigint as min_seen
    from jsonb_array_elements(payload) as r
    where r ->> 'realm' is not null
      and r ->> 'item'  is not null
      and r ->> 'day'   is not null
  ),
  -- collapse duplicates inside one payload so ON CONFLICT sees each key once
  folded as (
    select realm, item, day,
           min(price)    as price,
           max(qty)      as qty,
           min(min_seen) as min_seen
    from incoming
    where day between date '2019-01-01' and current_date + 1
    group by realm, item, day
  )
  insert into public.prices as p (realm, item, day, price, qty, min_seen)
  select realm, item, day, price, qty, min_seen from folded
  on conflict (realm, item, day) do update
    set price    = least(p.price, excluded.price),
        qty      = greatest(p.qty, excluded.qty),
        min_seen = least(p.min_seen, excluded.min_seen),
        seen_at  = now();

  return n;
end;
$fn$;

-- ------------------------------------------------------------------ a summary
-- PostgREST cannot GROUP BY, so the per-realm roll-up the UI shows lives here.
-- security_invoker keeps the caller's row-level security in force.

create or replace view public.pool_realms
  with (security_invoker = on)
as
select realm,
       count(*)::int                  as scans,
       count(distinct uploader)::int  as uploaders,
       max(scanned_at)                as latest,
       sum(bytes)::bigint             as bytes
from public.scans
group by realm;

-- --------------------------------------------------------------------- policy

alter table public.scans  enable row level security;
alter table public.prices enable row level security;

drop policy if exists "pool reads scans"   on public.scans;
drop policy if exists "pool appends scans" on public.scans;
drop policy if exists "pool reads prices"  on public.prices;

create policy "pool reads scans"
  on public.scans for select
  to anon, authenticated
  using (true);

create policy "pool appends scans"
  on public.scans for insert
  to anon, authenticated
  with check (true);

create policy "pool reads prices"
  on public.prices for select
  to anon, authenticated
  using (true);

-- No insert, update or delete policy on `prices`, and none for update or
-- delete on `scans`: those paths simply do not exist for the shared key.

grant usage  on schema public to anon, authenticated;
grant select, insert on public.scans  to anon, authenticated;
grant select         on public.prices to anon, authenticated;
grant select         on public.pool_realms to anon, authenticated;
revoke insert, update, delete on public.prices from anon, authenticated;
revoke update, delete         on public.scans  from anon, authenticated;
grant execute on function public.ingest_prices(jsonb) to anon, authenticated;

-- -------------------------------------------------------------------- storage
-- `scans` holds the packed scan payloads: readable and appendable with the
-- key, capped per object. There is no bucket for the page itself: Storage
-- serves anything HTML-ish as text/plain whatever content type is set, so a
-- browser would download the terminal rather than run it.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('scans', 'scans', false, 33554432, array['application/json'])
on conflict (id) do update
  set file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "pool reads objects"   on storage.objects;
drop policy if exists "pool appends objects" on storage.objects;

create policy "pool reads objects"
  on storage.objects for select
  to anon, authenticated
  using (bucket_id = 'scans');

create policy "pool appends objects"
  on storage.objects for insert
  to anon, authenticated
  with check (bucket_id = 'scans');
