-- Persistent task status table: avoids task_id 404 after service restarts.
create table if not exists public.review_sync_tasks (
  task_id text primary key,
  asin text not null,
  mode text not null,
  translate_mode text not null,
  status text not null default 'queued',
  result jsonb,
  error jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_review_sync_tasks_created_at
on public.review_sync_tasks (created_at desc);

create index if not exists idx_review_sync_tasks_asin_created_at
on public.review_sync_tasks (asin, created_at desc);

-- Run history table: one record per execution.
create table if not exists public.review_sync_runs (
  id bigint generated always as identity primary key,
  asin text not null,
  mode text not null,
  translate_mode text not null,
  current_total integer not null,
  new_count integer not null,
  upserted_rows integer not null default 0,
  status text not null default 'success',
  error text not null default '',
  created_at timestamptz not null default now()
);

create index if not exists idx_review_sync_runs_asin_created_at
on public.review_sync_runs (asin, created_at desc);
