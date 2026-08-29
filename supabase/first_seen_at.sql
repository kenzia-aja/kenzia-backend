-- ══════════════════════════════════════════════════════════
-- Kenzia: kolom first_seen_at (urutan "yang baru dulu")
-- Jalankan seluruh isi file ini sekaligus di Supabase SQL Editor
-- ══════════════════════════════════════════════════════════

-- 1. Tambah kolom (aman dijalankan berulang)
alter table public.series add column if not exists first_seen_at timestamptz;
alter table public.episodes add column if not exists first_seen_at timestamptz;

-- 2. Backfill cerdas:
--    id kecil = di-scrape lebih dulu dari halaman TERBARU situs sumber
--    → diberi timestamp lebih tua agar urutan "terbaru" tetap benar
with mx as (select max(id) as m from public.series)
update public.series s
set first_seen_at = now() - ((select m from mx) - s.id) * interval '1 minute'
where s.first_seen_at is null;

-- 3. Backfill episode: pakai checked_at bila ada
update public.episodes e
set first_seen_at = coalesce(e.checked_at, now())
where e.first_seen_at is null;

-- 4. Index untuk sorting cepat
create index if not exists series_first_seen_idx on public.series (first_seen_at desc nulls last);

-- 5. Verifikasi: 5 judul terbaru (harus urutan masuk akal, bukan acak)
select slug, title, first_seen_at
from public.series
order by first_seen_at desc nulls last
limit 5;
