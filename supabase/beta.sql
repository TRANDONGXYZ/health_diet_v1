-- Run once in the Supabase SQL Editor. No service-role key is needed by the app.
create table if not exists public.health_beta_data (
  user_id uuid primary key references auth.users(id) on delete cascade,
  revision bigint not null default 1,
  document jsonb not null check (jsonb_typeof(document) = 'object' and octet_length(document::text) <= 12582912)
);
alter table public.health_beta_data enable row level security;
revoke all on public.health_beta_data from anon;
grant select, insert, update on public.health_beta_data to authenticated;
drop policy if exists tester_data on public.health_beta_data;
create policy tester_data on public.health_beta_data to authenticated
  using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);

create or replace function public.health_beta_save(expected_revision bigint, new_document jsonb)
returns bigint language plpgsql security invoker set search_path = public, pg_temp as $$
declare result bigint;
begin
  if auth.uid() is null then raise exception 'Authentication required'; end if;
  if expected_revision = 0 then
    insert into public.health_beta_data(user_id, revision, document)
      values(auth.uid(), 1, new_document) on conflict do nothing returning revision into result;
  else
    update public.health_beta_data set document = new_document, revision = revision + 1
      where user_id = auth.uid() and revision = expected_revision returning revision into result;
  end if;
  if result is null then raise sqlstate 'PT409' using message = 'Data changed; reload before retrying'; end if;
  return result;
end $$;
revoke all on function public.health_beta_save(bigint,jsonb) from public, anon;
grant execute on function public.health_beta_save(bigint,jsonb) to authenticated;

insert into storage.buckets(id, name, public, file_size_limit, allowed_mime_types)
  values('health-diet-images', 'health-diet-images', false, 4000000, array['image/png'])
  on conflict(id) do update set public = false, file_size_limit = 4000000, allowed_mime_types = array['image/png'];
drop policy if exists tester_images_read on storage.objects;
drop policy if exists tester_images_insert on storage.objects;
drop policy if exists tester_images_update on storage.objects;
create policy tester_images_read on storage.objects for select to authenticated
  using (bucket_id = 'health-diet-images' and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy tester_images_insert on storage.objects for insert to authenticated
  with check (bucket_id = 'health-diet-images' and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy tester_images_update on storage.objects for update to authenticated
  using (bucket_id = 'health-diet-images' and (storage.foldername(name))[1] = (select auth.uid())::text)
  with check (bucket_id = 'health-diet-images' and (storage.foldername(name))[1] = (select auth.uid())::text);

-- Persistent caps shared by all Render processes/deploys. No client access to counters.
create table if not exists public.health_beta_usage (
  scope text not null, slot bigint not null, used integer not null,
  primary key(scope, slot)
);
alter table public.health_beta_usage enable row level security;
revoke all on public.health_beta_usage from public, anon, authenticated;
create or replace function public.health_beta_take_ai(audio_request boolean default false)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
declare
  who text := auth.uid()::text;
  cost integer := case when audio_request then 5 else 1 end;
  day_slot bigint := floor(extract(epoch from now()) / 86400);
  minute_slot bigint := floor(extract(epoch from now()) / 60);
  scopes text[]; slots bigint[]; limits integer[] := array[20,200,4,10]; i integer;
begin
  if who is null then return false; end if;
  perform pg_advisory_xact_lock(731826491);
  scopes := array['day:'||who,'day:global','minute:'||who,'minute:global'];
  slots := array[day_slot,day_slot,minute_slot,minute_slot];
  for i in 1..4 loop
    if coalesce((select used from public.health_beta_usage where scope=scopes[i] and slot=slots[i]),0)
       + (case when i<=2 then cost else 1 end) > limits[i] then
      return false;
    end if;
  end loop;
  for i in 1..4 loop
    insert into public.health_beta_usage values(scopes[i],slots[i],case when i<=2 then cost else 1 end)
      on conflict(scope,slot) do update set used = health_beta_usage.used + excluded.used;
  end loop;
  delete from public.health_beta_usage where (scope like 'day:%' and slot < day_slot - 1)
    or (scope like 'minute:%' and slot < minute_slot - 1);
  return true;
end $$;
revoke all on function public.health_beta_take_ai(boolean) from public, anon;
grant execute on function public.health_beta_take_ai(boolean) to authenticated;
