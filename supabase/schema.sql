-- â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
-- Kenzia â€” Skema Supabase (idempotent, aman dijalankan berulang)
-- Jalankan di Supabase Dashboard â†’ SQL Editor
-- â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

-- Tabel series
create table if not exists public.series (
  id bigint generated always as identity primary key,
  slug text not null unique,
  title text,
  type text,
  status text,
  country text,
  released text,
  rating double precision,
  poster_url text,
  network text,
  director text,
  total_episodes text,
  synopsis text,
  cast_list jsonb default '[]'::jsonb,
  genres jsonb default '[]'::jsonb,
  source_url text,
  last_scraped_at timestamptz default now()
);

-- Tabel episodes
create table if not exists public.episodes (
  id bigint generated always as identity primary key,
  series_id bigint not null references public.series(id) on delete cascade,
  number int,
  title text,
  release_date text,
  source_url text not null unique,
  embeds jsonb default '[]'::jsonb,
  servers jsonb default '[]'::jsonb,   -- daftar server video: [{name, embed, stream, working, ads}]
  stale boolean default false,
  checked_at timestamptz
);

create index if not exists episodes_series_id_idx on public.episodes (series_id);
create index if not exists episodes_number_idx on public.episodes (series_id, number);

-- Tabel agregat genre & negara (diisi sync_supabase.py)
create table if not exists public.genres (
  name text primary key,
  count int default 0
);

create table if not exists public.countries (
  name text primary key,
  count int default 0
);

-- Tabel jadwal rilis mingguan (diisi sync_supabase.py via scraper.get_schedule)
create table if not exists public.schedule (
  day text primary key,
  items jsonb default '[]'::jsonb,
  updated_at timestamptz default now()
);

-- Kolom servers untuk data lama yang belum punya
alter table public.episodes add column if not exists servers jsonb default '[]'::jsonb;

-- Urutan "yang baru": waktu judul/episode pertama kali ditemukan scraper
alter table public.series add column if not exists first_seen_at timestamptz;
alter table public.episodes add column if not exists first_seen_at timestamptz;
alter table public.series add column if not exists last_update_at timestamptz;

create index if not exists series_first_seen_idx on public.series (first_seen_at desc nulls last);

-- â”€â”€ Row Level Security: buka baca untuk anon, tulis hanya service_role â”€â”€
alter table public.series enable row level security;
alter table public.episodes enable row level security;
alter table public.genres enable row level security;
alter table public.countries enable row level security;

drop policy if exists "public read schedule" on public.schedule;
create policy "public read schedule" on public.schedule for select using (true);
alter table public.schedule enable row level security;

drop policy if exists "public read series" on public.series;
create policy "public read series" on public.series for select using (true);

drop policy if exists "public read episodes" on public.episodes;
create policy "public read episodes" on public.episodes for select using (true);

drop policy if exists "public read genres" on public.genres;
create policy "public read genres" on public.genres for select using (true);

drop policy if exists "public read countries" on public.countries;
create policy "public read countries" on public.countries for select using (true);
