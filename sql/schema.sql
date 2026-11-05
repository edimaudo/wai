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
  full_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Remove legacy account and password fields; profile IDs remain as optional workflow attribution references.
alter table public.profiles drop constraint if exists profiles_id_fkey;
alter table public.profiles drop column if exists email;
alter table public.profiles drop column if exists password_hash;
alter table public.profiles drop column if exists role;
alter table public.profiles drop column if exists is_active;
alter table public.profiles drop column if exists language;
alter table public.profiles alter column id set default gen_random_uuid();


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

-- The open-access demonstration application uses a server-side Supabase client.
-- No application accounts or role-based access checks are used.
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

-- Functional workflow extensions. These tables are used by the server-side
-- Supabase client; no database credentials are sent to the browser.
create table if not exists public.constituent_profiles (
  constituent_id text primary key,
  managed_prospect boolean,
  lifetime_giving numeric(16,2),
  lifetime_gift_count integer,
  first_gift_date date,
  last_gift_date date,
  lifetime_crm_interactions integer,
  first_crm_interaction_date date,
  last_crm_interaction_date date,
  capacity_estimate text,
  source_file text not null,
  record_hash text not null unique,
  created_at timestamptz not null default now()
);

create table if not exists public.organization_context (
  id smallint primary key default 1 check (id = 1),
  mission text,
  vision text,
  long_term_objectives text,
  growth_plan text,
  key_advantages text,
  community_impact text,
  strategic_priorities text,
  values_statement text,
  refined_context jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create table if not exists public.funding_criteria (
  id smallint primary key default 1 check (id = 1),
  focus_terms text[] not null default array['water','sanitation','hygiene','WASH','climate resilience'],
  geography_terms text[] not null default array['international','Africa','Asia','Pacific','ODA'],
  excluded_terms text[] not null default array['invitation only','individual applicant','fellowship'],
  weights jsonb not null default '{"mission":35,"geography":25,"eligibility":25,"timing":15}'::jsonb,
  version integer not null default 1,
  updated_at timestamptz not null default now()
);

alter table public.source_registry add column if not exists last_content_hash text;
alter table public.source_registry add column if not exists last_http_status integer;
alter table public.source_registry add column if not exists last_status text;
alter table public.source_registry add column if not exists last_page_title text;
alter table public.funding_opportunities add column if not exists funder_name text;
alter table public.funding_opportunities add column if not exists focus_areas text;
alter table public.funding_opportunities add column if not exists geography text;
alter table public.funding_opportunities add column if not exists eligibility_text text;
alter table public.funding_opportunities add column if not exists contact_name text;
alter table public.funding_opportunities add column if not exists contact_email text;
alter table public.funding_opportunities add column if not exists support_case text;
alter table public.funding_opportunities add column if not exists decision_reason text;
alter table public.proposal_versions add column if not exists extracted_text text;
alter table public.proposal_versions add column if not exists document_type text;
alter table public.obligations add column if not exists review_status text not null default 'pending_review';
alter table public.obligations add column if not exists source_document_id uuid references public.source_documents(id);

create table if not exists public.workflow_tasks (
  id uuid primary key default gen_random_uuid(),
  module_key text not null,
  entity_type text,
  entity_id text,
  title text not null,
  description text,
  owner_label text,
  priority text not null default 'medium' check (priority in ('low','medium','high','urgent')),
  due_date date,
  status text not null default 'open' check (status in ('open','in_progress','completed','cancelled')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.grant_documents (
  id uuid primary key default gen_random_uuid(),
  grant_id uuid references public.grants(id) on delete cascade,
  proposal_id uuid references public.proposals(id) on delete cascade,
  document_type text not null,
  filename text not null,
  storage_path text not null,
  content_hash text not null,
  version_label text not null,
  extracted_text text,
  extraction_status text not null default 'completed',
  review_status text not null default 'pending_review',
  critique jsonb not null default '{}'::jsonb,
  candidate_obligations jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.partner_framework_criteria (
  id uuid primary key default gen_random_uuid(),
  criterion_key text not null unique,
  title text not null,
  description text not null,
  framework_version text not null default 'demonstration-assumption-v1',
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Uploaded policy source used by partnership criteria. A document is not active
-- until a reviewer maps every active criterion to an exact clause and activates it.
create table if not exists public.partner_framework_documents (
  id uuid primary key default gen_random_uuid(),
  filename text not null,
  storage_path text not null,
  content_hash text not null unique,
  extracted_text text not null,
  extraction_status text not null default 'completed',
  framework_version text not null,
  status text not null default 'pending_review' check (status in ('pending_review','active','superseded')),
  activated_at timestamptz,
  created_at timestamptz not null default now()
);

alter table public.partner_framework_criteria
  add column if not exists policy_document_id uuid references public.partner_framework_documents(id);
alter table public.partner_framework_criteria
  add column if not exists policy_clause_reference text;
alter table public.partner_assessments
  add column if not exists policy_document_id uuid references public.partner_framework_documents(id);
create unique index if not exists idx_one_active_partner_framework
  on public.partner_framework_documents(status) where status = 'active';
create index if not exists idx_partner_framework_created
  on public.partner_framework_documents(created_at desc);
alter table public.partner_framework_documents enable row level security;
revoke all on public.partner_framework_documents from anon, authenticated;
grant all on public.partner_framework_documents to service_role;

create index if not exists idx_constituent_profiles_id on public.constituent_profiles(constituent_id);
create index if not exists idx_workflow_tasks_status_due on public.workflow_tasks(status, due_date);
create index if not exists idx_grant_documents_grant on public.grant_documents(grant_id, created_at desc);
create index if not exists idx_grant_documents_proposal on public.grant_documents(proposal_id, created_at desc);

alter table public.constituent_profiles enable row level security;
alter table public.organization_context enable row level security;
alter table public.funding_criteria enable row level security;
alter table public.workflow_tasks enable row level security;
alter table public.grant_documents enable row level security;
alter table public.partner_framework_criteria enable row level security;
revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
grant all on all tables in schema public to service_role;
grant usage on schema public to service_role;

create table if not exists public.opportunity_briefs (
  id uuid primary key default gen_random_uuid(),
  opportunity_id uuid not null references public.funding_opportunities(id) on delete cascade,
  body text not null,
  fit_score numeric(5,2),
  criterion_scores jsonb not null default '{}'::jsonb,
  source_url text,
  status text not null default 'draft_requires_review',
  created_at timestamptz not null default now()
);
create index if not exists idx_opportunity_briefs_opportunity on public.opportunity_briefs(opportunity_id, created_at desc);
alter table public.opportunity_briefs enable row level security;
revoke all on all tables in schema public from anon, authenticated;
grant all on all tables in schema public to service_role;

-- Make shared task entity references polymorphic. Constituent IDs are source text IDs,
-- while opportunities, grants and assessments use UUIDs. Text preserves both exactly.
alter table public.workflow_tasks alter column entity_id type text using entity_id::text;

-- Audit events and generic task references span different source identifier types.
alter table public.audit_events alter column entity_id type text using entity_id::text;


-- Curated Canadian public funding and historical-award sources for first deployment.
-- These are source-monitoring entry points, not pre-approved funding opportunities.
insert into public.source_registry
  (name, source_type, url, jurisdiction, international_funding_relevance, monitoring_frequency, is_active, reliability_notes)
select v.name, v.source_type, v.url, 'Canada', true, v.frequency, true, v.notes
from (values
  ('Global Affairs Canada — current and upcoming international assistance calls', 'Government funding calls', 'https://www.international.gc.ca/world-monde/funding-financement/open_calls-appels_ouverts.aspx?lang=eng', 'weekly', 'Official source for current and upcoming international assistance opportunities. Verify the status, eligibility and dates on each individual call page.'),
  ('Global Affairs Canada — international assistance funding processes', 'Government funding strategy and process', 'https://www.international.gc.ca/world-monde/funding-financement/funding_processes-processus_financement.aspx?lang=eng', 'monthly', 'Official source describing funding mechanisms and strategic processes. Signals are not open calls unless a specific active opportunity is published.'),
  ('Grand Challenges Canada — current funding opportunities', 'Canadian foundation / innovation funder', 'https://www.grandchallenges.ca/apply-for-funding/', 'weekly', 'Official listing of open and past funding opportunities. Confirm the active call details and applicant eligibility in the linked funding opportunity document.'),
  ('Open Government of Canada — Global Affairs Canada grants and contributions', 'Government grants and awards data', 'https://search.open.canada.ca/grants/?owner_org=dfatd-maecd', 'monthly', 'Historical awards and recipient data for funder research and network mapping; not an application-call feed.')
) as v(name, source_type, url, frequency, notes)
where not exists (select 1 from public.source_registry s where s.url = v.url);

