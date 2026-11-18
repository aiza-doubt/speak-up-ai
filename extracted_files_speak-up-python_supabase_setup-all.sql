-- Run this in Supabase SQL Editor. RLS is enabled on every user-owned table.
create extension if not exists pgcrypto;
create table if not exists public.profiles (
 id uuid primary key references auth.users(id) on delete cascade,
 display_name text,
 english_level text not null default 'A1–A2' check (english_level in ('A1–A2','B1–B2')),
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now()
);
create table if not exists public.speaking_sessions (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
 topic text not null, question text not null, transcript text not null default '',
 duration_seconds integer not null default 0, word_count integer not null default 0, wpm integer not null default 0,
 recording_path text, created_at timestamptz not null default now()
);
create table if not exists public.assessments (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
 session_id uuid not null references public.speaking_sessions(id) on delete cascade,
 result jsonb not null, created_at timestamptz not null default now()
);
create table if not exists public.vocabulary (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
 word text not null, translation text default '', example text default '', learned boolean not null default false,
 activated_at timestamptz, created_at timestamptz not null default now()
);
create table if not exists public.goals (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
 title text not null, target integer not null check(target>0), current integer not null default 0,
 unit text not null default 'sessions', created_at timestamptz not null default now()
);
create table if not exists public.daily_challenges (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
 challenge_date date not null, prompt text not null, completed boolean not null default false,
 completed_at timestamptz, unique(user_id,challenge_date)
);
create table if not exists public.conversation_messages (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
 role text not null check(role in ('user','assistant')), content text not null, created_at timestamptz not null default now()
);
create table if not exists public.learning_recommendations (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
 title text not null, description text not null, source_session_id uuid references public.speaking_sessions(id) on delete set null,
 completed boolean not null default false, created_at timestamptz not null default now()
);
do $$ declare t text; begin
 foreach t in array array['speaking_sessions','assessments','vocabulary','goals','daily_challenges','conversation_messages','learning_recommendations'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('drop policy if exists "Users manage own rows" on public.%I',t);
  execute format('create policy "Users manage own rows" on public.%I for all using (auth.uid() = user_id) with check (auth.uid() = user_id)',t);
 end loop;
end $$;
alter table public.profiles enable row level security;
drop policy if exists "Users manage own profile" on public.profiles;
create policy "Users manage own profile" on public.profiles for all using (auth.uid() = id) with check (auth.uid() = id);
-- Optional profile auto-creation trigger.
create or replace function public.handle_new_user() returns trigger language plpgsql security definer set search_path=public as $$
begin insert into public.profiles(id,display_name) values(new.id,coalesce(new.raw_user_meta_data->>'display_name','Learner')) on conflict do nothing; return new; end; $$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users for each row execute procedure public.handle_new_user();

-- 002_complete_integration.sql
-- Run AFTER 001_initial_schema.sql. Safe to run more than once.

-- 1. Make sure every existing account has a profile row.
insert into public.profiles (id, display_name)
select id, coalesce(raw_user_meta_data->>'display_name', 'Learner') from auth.users
on conflict (id) do nothing;

-- 2. Keep profiles.updated_at current.
create or replace function public.set_updated_at() returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end; $$;
drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at before update on public.profiles
  for each row execute procedure public.set_updated_at();

-- 3. One assessment per speaking session (re-analysing replaces the old result).
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'assessments_session_id_key') then
    delete from public.assessments a using public.assessments b
      where a.session_id = b.session_id
        and (a.created_at < b.created_at or (a.created_at = b.created_at and a.id < b.id));
    alter table public.assessments add constraint assessments_session_id_key unique (session_id);
  end if;
end $$;

-- 4. Stronger RLS: a row can only point at a session that belongs to the same user.
drop policy if exists "Users manage own rows" on public.assessments;
drop policy if exists "Users manage own assessments" on public.assessments;
create policy "Users manage own assessments" on public.assessments for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id and exists (
    select 1 from public.speaking_sessions s where s.id = session_id and s.user_id = auth.uid()));

drop policy if exists "Users manage own rows" on public.learning_recommendations;
drop policy if exists "Users manage own recommendations" on public.learning_recommendations;
create policy "Users manage own recommendations" on public.learning_recommendations for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id and (source_session_id is null or exists (
    select 1 from public.speaking_sessions s where s.id = source_session_id and s.user_id = auth.uid())));

-- 5. Size limits so nobody can store huge rows.
do $$ declare c record; begin
  for c in select * from (values
    ('speaking_sessions','speaking_sessions_transcript_len','char_length(transcript) <= 20000'),
    ('speaking_sessions','speaking_sessions_nonneg','duration_seconds >= 0 and word_count >= 0 and wpm >= 0'),
    ('vocabulary','vocabulary_word_len','char_length(word) between 1 and 200'),
    ('goals','goals_title_len','char_length(title) between 1 and 200'),
    ('goals','goals_current_nonneg','"current" >= 0'),
    ('conversation_messages','conversation_content_len','char_length(content) <= 4000'),
    ('profiles','profiles_name_len','display_name is null or char_length(display_name) <= 80')
  ) as t(tbl, cname, expr) loop
    if not exists (select 1 from pg_constraint where conname = c.cname) then
      execute format('alter table public.%I add constraint %I check (%s)', c.tbl, c.cname, c.expr);
    end if;
  end loop;
end $$;

-- 6. Indexes for the queries the app runs.
create index if not exists speaking_sessions_user_created_idx on public.speaking_sessions (user_id, created_at desc);
create index if not exists vocabulary_user_created_idx on public.vocabulary (user_id, created_at desc);
create index if not exists goals_user_created_idx on public.goals (user_id, created_at);
create index if not exists conversation_user_created_idx on public.conversation_messages (user_id, created_at);
create index if not exists recommendations_user_created_idx on public.learning_recommendations (user_id, created_at desc);
create index if not exists assessments_user_idx on public.assessments (user_id);
