-- ═══════════════════════════════════════════════════════
-- Kenzia: kolom last_update_at + KOREKSI urutan backfill
-- Jalankan seluruh isi file ini di Supabase SQL Editor
-- ═══════════════════════════════════════════════════════

alter table public.series add column if not exists last_update_at timestamptz;

-- Backfill urutan BENAR: id KECIL = di-scrape dari halaman TERBARU situs
-- → timestamp LEBIH BARU. (Backfill lama terbalik — ini mengkoreksinya.)
with mn as (select min(id) as n from public.series)
update public.series s
set last_update_at = now() - (s.id - (select n from mn)) * interval '1 minute'
where s.last_update_at is null;

-- Koreksi juga first_seen_at yang kemarin terisi terbalik
with mn as (select min(id) as n from public.series)
update public.series s
set first_seen_at = now() - (s.id - (select n from mn)) * interval '1 minute';

create index if not exists series_last_update_idx on public.series (last_update_at desc nulls last);

-- Verifikasi: 5 teratas harus = update terbaru situs sumber
select slug, title, last_update_at from public.series order by last_update_at desc nulls last limit 5;
