create extension if not exists pgcrypto;

create table if not exists public.n8n_sync_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null,
  finished_at timestamptz,
  status text not null check (status in ('running', 'success', 'partial_conflict', 'failed')),
  host_name text not null,
  instance text not null,
  mirror_root text not null,
  git_branch text,
  git_commit_before text,
  git_commit_after text,
  commit_created boolean not null default false,
  commit_sha text,
  push_succeeded boolean,
  task_name text,
  duration_ms integer,
  remote_changed_count integer not null default 0,
  staged_change_count integer not null default 0,
  conflict_count integer not null default 0,
  pruned_count integer not null default 0,
  error_message text,
  summary jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_n8n_sync_runs_started_at on public.n8n_sync_runs (started_at desc);
create index if not exists idx_n8n_sync_runs_status on public.n8n_sync_runs (status);

create table if not exists public.n8n_sync_run_conflicts (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.n8n_sync_runs(id) on delete cascade,
  instance text not null,
  workflow_id text not null,
  workflow_name text,
  local_path text,
  conflict_reason text not null,
  local_hash text,
  remote_hash text,
  baseline_local_hash text,
  baseline_remote_hash text,
  artifact_dir text,
  created_at timestamptz not null default now()
);

create index if not exists idx_n8n_sync_run_conflicts_run_id on public.n8n_sync_run_conflicts (run_id);
create index if not exists idx_n8n_sync_run_conflicts_workflow_id on public.n8n_sync_run_conflicts (workflow_id);
