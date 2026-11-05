-- Wai Intelligence initial Supabase schema
-- Apply in the Supabase SQL Editor before starting the application.

create extension if not exists pgcrypto;
create extension if not exists vector with schema extensions;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create table if not exists public.profiles (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  password_hash text,
  full_name text,
  role text not null default 'staff' check (role in ('admin', 'staff')),
  is_active boolean not null default true,
  language text not null default 'en-CA' check (language in ('en-CA', 'fr-CA')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Remove the previous Supabase Auth dependency and add local demo-password storage.
alter table public.profiles drop constraint if exists profiles_id_fkey;
alter table public.profiles alter column id set default gen_random_uuid();
alter table public.profiles add column if not exists password_hash text;

drop trigger if exists on_auth_user_created_wai on auth.users;
drop function if exists public.handle_new_auth_user();

create table if not exists public.data_imports (
  id uuid primary key default gen_random_uuid(),
  dataset_key text not null,
  source_file text not null,
  file_hash text not null,
  records_seen integer not null default 0,
  records_loaded integer not null default 0,
  records_rejected integer not null default 0,
  status text not null check (status in ('completed', 'completed_with_errors', 'failed')),
  error_summary jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  unique (dataset_key, file_hash)
);

create table if not exists public.gift_transactions (
  id uuid primary key default gen_random_uuid(),
  constituent_id text not null,
  gift_date date not null,
  amount numeric(14,2) not null check (amount >= 0),
  source_file text not null,
  record_hash text not null unique,
  created_at timestamptz not null default now()
);

create table if not exists public.crm_interactions (
  id uuid primary key default gen_random_uuid(),
  constituent_id text not null,
  crm_interaction_date date not null,
  crm_interaction_type text not null,
  source_file text not null,
  record_hash text not null unique,
  created_at timestamptz not null default now()
);

create table if not exists public.video_email_events (
  id uuid primary key default gen_random_uuid(),
  constituent_id text not null,
  contact_id text not null,
  sent_date date not null,
  opened_date date,
  started_video_date date,
  bounced boolean,
  unsubscribed boolean,
  clicks integer check (clicks is null or clicks >= 0),
  video_views integer check (video_views is null or video_views >= 0),
  started_video boolean,
  watched_video_25_percent boolean,
  watched_video_50_percent boolean,
  watched_video_75_percent boolean,
  finished_video boolean,
  video_shares_to_facebook integer check (video_shares_to_facebook is null or video_shares_to_facebook >= 0),
  source_file text not null,
  record_hash text not null unique,
  created_at timestamptz not null default now()
);

create table if not exists public.organizations (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  organization_type text,
  country text,
  website text,
  parent_organization_id uuid references public.organizations(id),
  source_status text not null default 'unverified',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.people (
  id uuid primary key default gen_random_uuid(),
  full_name text not null,
  organization_id uuid references public.organizations(id),
  role_title text,
  public_profile_url text,
  source_status text not null default 'unverified',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.relationship_edges (
  id uuid primary key default gen_random_uuid(),
  from_organization_id uuid references public.organizations(id),
  to_organization_id uuid references public.organizations(id),
  from_person_id uuid references public.people(id),
  to_person_id uuid references public.people(id),
  relationship_type text not null,
  evidence_item_id uuid,
  confidence_label text not null default 'unverified',
  created_at timestamptz not null default now()
);

create table if not exists public.source_registry (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  source_type text not null,
  url text not null,
  jurisdiction text,
  international_funding_relevance boolean not null default false,
  monitoring_frequency text not null default 'weekly' check (monitoring_frequency in ('daily', 'weekly', 'monthly', 'quarterly', 'manual')),
  is_active boolean not null default true,
  last_checked_at timestamptz,
  next_check_at timestamptz,
  reliability_notes text,
  created_by uuid references public.profiles(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.source_checks (
  id uuid primary key default gen_random_uuid(),
  source_id uuid not null references public.source_registry(id) on delete cascade,
  checked_at timestamptz not null default now(),
  status text not null,
  http_status integer,
  content_hash text,
  change_detected boolean not null default false,
  error_message text,
  created_at timestamptz not null default now()
);

create table if not exists public.source_documents (
  id uuid primary key default gen_random_uuid(),
  source_id uuid references public.source_registry(id),
  title text not null,
  source_url text,
  published_at timestamptz,
  retrieved_at timestamptz not null default now(),
  content_hash text,
  storage_path text,
  document_type text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.evidence_items (
  id uuid primary key default gen_random_uuid(),
  source_document_id uuid references public.source_documents(id),
  source_url text,
  source_title text,
  excerpt text,
  page_reference text,
  published_at timestamptz,
  checked_at timestamptz not null default now(),
  reliability_label text not null default 'unassessed',
  evidence_type text,
  extracted_by_model text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.document_chunks (
  id uuid primary key default gen_random_uuid(),
  source_document_id uuid not null references public.source_documents(id) on delete cascade,
  chunk_index integer not null,
  content text not null,
  page_reference text,
  embedding extensions.vector(768),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (source_document_id, chunk_index)
);

create table if not exists public.funding_opportunities (
  id uuid primary key default gen_random_uuid(),
  funder_organization_id uuid references public.organizations(id),
  source_id uuid references public.source_registry(id),
  title text not null,
  description text,
  opportunity_url text,
  published_at timestamptz,
  deadline date,
  funding_min numeric(14,2),
  funding_max numeric(14,2),
  currency text default 'CAD',
  eligibility_status text not null default 'uncertain' check (eligibility_status in ('eligible', 'ineligible', 'uncertain', 'not_assessed')),
  fit_score numeric(5,2),
  status text not null default 'new',
  key_requirements jsonb not null default '[]'::jsonb,
  uncertainties jsonb not null default '[]'::jsonb,
  last_verified_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.opportunity_assessments (
  id uuid primary key default gen_random_uuid(),
  opportunity_id uuid not null references public.funding_opportunities(id) on delete cascade,
  assessor_id uuid references public.profiles(id),
  criteria_version text,
  total_score numeric(5,2),
  criterion_scores jsonb not null default '{}'::jsonb,
  recommendation text,
  reasons_to_decline jsonb not null default '[]'::jsonb,
  uncertainty_notes jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.donor_preferences (
  id uuid primary key default gen_random_uuid(),
  constituent_id text not null unique,
  preferred_language text,
  preferred_channel text,
  communication_restrictions jsonb not null default '[]'::jsonb,
  consent_notes text,
  last_verified_at timestamptz,
  updated_by uuid references public.profiles(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.donor_notes (
  id uuid primary key default gen_random_uuid(),
  constituent_id text not null,
  note_text text not null,
  note_date date,
  source_type text,
  source_reference text,
  sensitivity_label text not null default 'internal',
  created_by uuid references public.profiles(id),
  created_at timestamptz not null default now()
);

create table if not exists public.stewardship_tasks (
  id uuid primary key default gen_random_uuid(),
  constituent_id text not null,
  title text not null,
  description text,
  due_date date,
  status text not null default 'open' check (status in ('open', 'in_progress', 'completed', 'cancelled')),
  suggested_by_agent boolean not null default false,
  evidence_item_ids uuid[] not null default '{}',
  assigned_to uuid references public.profiles(id),
  approved_by uuid references public.profiles(id),
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.reports (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  audience text not null,
  purpose text,
  reporting_period_start date,
  reporting_period_end date,
  language text not null default 'en-CA',
  status text not null default 'draft',
  narrative jsonb not null default '{}'::jsonb,
  created_by uuid references public.profiles(id),
  approved_by uuid references public.profiles(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.report_metrics (
  id uuid primary key default gen_random_uuid(),
  report_id uuid references public.reports(id) on delete cascade,
  metric_key text not null,
  metric_label text not null,
  metric_value numeric,
  prior_value numeric,
  unit text,
  calculation_definition text,
  source_references jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.forecast_runs (
  id uuid primary key default gen_random_uuid(),
  metric_key text not null,
  forecast_start date not null,
  forecast_end date not null,
  model_name text not null,
  assumptions jsonb not null default '{}'::jsonb,
  results jsonb not null default '[]'::jsonb,
  accuracy_metrics jsonb not null default '{}'::jsonb,
  created_by uuid references public.profiles(id),
  created_at timestamptz not null default now()
);

create table if not exists public.partner_assessments (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id),
  framework_version text not null,
  status text not null default 'draft',
  recommendation text not null default 'pending_review',
  research_coverage jsonb not null default '{}'::jsonb,
  open_questions jsonb not null default '[]'::jsonb,
  assessor_id uuid references public.profiles(id),
  decision_maker_id uuid references public.profiles(id),
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.assessment_findings (
  id uuid primary key default gen_random_uuid(),
  assessment_id uuid not null references public.partner_assessments(id) on delete cascade,
  criterion_key text not null,
  finding_type text not null,
  finding_text text not null,
  evidence_item_ids uuid[] not null default '{}',
  uncertainty_label text not null default 'unresolved',
  organization_response text,
  reviewer_note text,
  created_at timestamptz not null default now()
);

create table if not exists public.proposals (
  id uuid primary key default gen_random_uuid(),
  opportunity_id uuid references public.funding_opportunities(id),
  title text not null,
  donor_organization_id uuid references public.organizations(id),
  status text not null default 'draft',
  lead_id uuid references public.profiles(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.proposal_versions (
  id uuid primary key default gen_random_uuid(),
  proposal_id uuid not null references public.proposals(id) on delete cascade,
  version_label text not null,
  storage_path text,
  content_hash text,
  requirements_check jsonb not null default '{}'::jsonb,
  critique jsonb not null default '{}'::jsonb,
  created_by uuid references public.profiles(id),
  created_at timestamptz not null default now()
);

create table if not exists public.grants (
  id uuid primary key default gen_random_uuid(),
  proposal_id uuid references public.proposals(id),
  donor_organization_id uuid references public.organizations(id),
  title text not null,
  agreement_version text,
  start_date date,
  end_date date,
  currency text default 'CAD',
  awarded_amount numeric(14,2),
  status text not null default 'active',
  project_manager_id uuid references public.profiles(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.grant_amendments (
  id uuid primary key default gen_random_uuid(),
  grant_id uuid not null references public.grants(id) on delete cascade,
  amendment_number text not null,
  effective_date date,
  storage_path text,
  content_hash text,
  extracted_changes jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.obligations (
  id uuid primary key default gen_random_uuid(),
  grant_id uuid not null references public.grants(id) on delete cascade,
  obligation_type text not null,
  obligation_text text not null,
  classification text not null check (classification in ('binding', 'guidance', 'internal_assumption')),
  source_document text not null,
  source_version text,
  clause_reference text,
  due_date date,
  reporting_period text,
  status text not null default 'open',
  evidence_item_ids uuid[] not null default '{}',
  assigned_to uuid references public.profiles(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.reviewer_feedback (
  id uuid primary key default gen_random_uuid(),
  module_key text not null,
  entity_type text not null,
  entity_id uuid,
  feedback_type text not null,
  feedback_text text,
  corrected_output jsonb,
  submitted_by uuid references public.profiles(id),
  created_at timestamptz not null default now()
);

create table if not exists public.approval_records (
  id uuid primary key default gen_random_uuid(),
  subject text not null,
  decision_type text not null,
  entity_type text not null,
  entity_id uuid,
  status text not null default 'pending' check (status in ('pending', 'approved', 'rejected', 'needs_changes', 'escalated')),
  assigned_to uuid references public.profiles(id),
  reviewed_by uuid references public.profiles(id),
  decision_note text,
  created_at timestamptz not null default now(),
  reviewed_at timestamptz
);

create table if not exists public.audit_events (
  id uuid primary key default gen_random_uuid(),
  actor_id uuid references public.profiles(id),
  event_type text not null,
  entity_type text,
  entity_id uuid,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_gift_transactions_gift_date on public.gift_transactions(gift_date);
create index if not exists idx_gift_transactions_constituent on public.gift_transactions(constituent_id);
create index if not exists idx_crm_interactions_constituent_date on public.crm_interactions(constituent_id, crm_interaction_date desc);
create index if not exists idx_video_email_constituent_sent on public.video_email_events(constituent_id, sent_date desc);
create index if not exists idx_funding_opportunities_deadline on public.funding_opportunities(deadline);
create index if not exists idx_source_registry_active_next_check on public.source_registry(is_active, next_check_at);
create index if not exists idx_evidence_checked_at on public.evidence_items(checked_at desc);
create index if not exists idx_obligations_due_date_status on public.obligations(due_date, status);
create index if not exists idx_approval_records_status on public.approval_records(status, created_at desc);
create index if not exists idx_audit_events_created_at on public.audit_events(created_at desc);

-- Keep modified timestamps consistent.
do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'profiles', 'organizations', 'people', 'source_registry', 'funding_opportunities',
    'donor_preferences', 'stewardship_tasks', 'reports', 'partner_assessments',
    'proposals', 'grants', 'obligations'
  ] loop
    execute format('drop trigger if exists set_updated_at on public.%I', table_name);
    execute format('create trigger set_updated_at before update on public.%I for each row execute function public.set_updated_at()', table_name);
  end loop;
end;
$$;

-- Enable RLS on every application table.
do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'profiles', 'data_imports', 'gift_transactions', 'crm_interactions', 'video_email_events',
    'organizations', 'people', 'relationship_edges', 'source_registry', 'source_checks',
    'source_documents', 'evidence_items', 'document_chunks', 'funding_opportunities',
    'opportunity_assessments', 'donor_preferences', 'donor_notes', 'stewardship_tasks',
    'reports', 'report_metrics', 'forecast_runs', 'partner_assessments', 'assessment_findings',
    'proposals', 'proposal_versions', 'grants', 'grant_amendments', 'obligations',
    'reviewer_feedback', 'approval_records', 'audit_events'
  ] loop
    execute format('alter table public.%I enable row level security', table_name);
  end loop;
end;
$$;

-- This application authenticates demo users in FastAPI. Browser clients do not
-- access Supabase directly; all database access is performed by the server-side
-- service-role client after FastAPI validates the signed application session.
do $$
declare
  policy_row record;
begin
  for policy_row in
    select schemaname, tablename, policyname
    from pg_policies
    where schemaname = 'public'
  loop
    execute format(
      'drop policy if exists %I on %I.%I',
      policy_row.policyname,
      policy_row.schemaname,
      policy_row.tablename
    );
  end loop;
end;
$$;

drop function if exists public.is_active_user();
drop function if exists public.is_admin();

revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
grant usage on schema public to service_role;
grant all privileges on all tables in schema public to service_role;
grant all privileges on all sequences in schema public to service_role;
alter default privileges in schema public grant all on tables to service_role;
alter default privileges in schema public grant all on sequences to service_role;
