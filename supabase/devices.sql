-- Run AFTER beta.sql and offline.sql. Rerunnable; existing workspaces keep their owner.
begin;
create table if not exists public.health_beta_devices (
  device_id uuid primary key references auth.users(id) on delete cascade,
  workspace_id uuid not null references auth.users(id),
  epoch uuid not null default gen_random_uuid(), active boolean not null default true,
  joined boolean not null default false, pending_pair uuid,
  label text not null default 'Browser', created_at timestamptz not null default now()
);
create table if not exists public.health_beta_pairs (
  id uuid primary key default gen_random_uuid(), secret_hash text not null unique,
  issuer uuid not null references auth.users(id), workspace_id uuid not null references auth.users(id),
  joiner uuid references auth.users(id), code text not null,
  state text not null default 'issued', expires_at timestamptz not null default now()+interval '5 minutes',
  previous jsonb, activated_epoch uuid
);
create table if not exists public.health_beta_events (
  workspace_id uuid primary key references auth.users(id), revision bigint not null default 0
);
create table if not exists public.health_beta_signals (
  device_id uuid primary key references auth.users(id) on delete cascade, revision bigint not null default 1
);
alter table public.health_beta_devices enable row level security;
alter table public.health_beta_pairs enable row level security;
alter table public.health_beta_events enable row level security;
alter table public.health_beta_signals enable row level security;
revoke all on public.health_beta_devices, public.health_beta_pairs, public.health_beta_events, public.health_beta_signals from public,anon,authenticated;
grant select on public.health_beta_events,public.health_beta_signals to authenticated;

create or replace function public.health_beta_workspace()
returns uuid language sql stable security definer set search_path=public,pg_temp as $$
  -- Old deployments can still initialize a brand-new anonymous workspace
  -- during rollout. An existing revoked/pending membership NEVER falls back.
  select case when exists(select 1 from public.health_beta_devices where device_id=auth.uid())
    then (select workspace_id from public.health_beta_devices where device_id=auth.uid() and active and pending_pair is null)
    else auth.uid() end
$$;
revoke all on function public.health_beta_workspace() from public,anon;
grant execute on function public.health_beta_workspace() to authenticated;

create or replace function public.health_beta_device_context(device_label text default 'Browser')
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare d public.health_beta_devices;
begin
  if auth.uid() is null then raise sqlstate 'PT400' using message='Authentication required'; end if;
  insert into public.health_beta_devices(device_id,workspace_id,label)
    values(auth.uid(),auth.uid(),left(coalesce(nullif(device_label,''),'Browser'),100))
    on conflict(device_id) do update set label=excluded.label where health_beta_devices.label is distinct from excluded.label;
  insert into public.health_beta_signals(device_id) values(auth.uid()) on conflict do nothing;
  select * into d from public.health_beta_devices where device_id=auth.uid();
  return jsonb_build_object('workspace_id',d.workspace_id,'device_epoch',d.epoch,'active',d.active,
    'joined',d.joined,'pending_pair',d.pending_pair);
end $$;

create or replace function public.health_beta_signal(target uuid)
returns void language sql security definer set search_path=public,pg_temp as $$
  insert into public.health_beta_signals(device_id) values(target)
  on conflict(device_id) do update set revision=health_beta_signals.revision+1
$$;
revoke all on function public.health_beta_signal(uuid) from public,anon,authenticated;

create or replace function public.health_beta_device_list()
returns jsonb language sql security definer set search_path=public,pg_temp as $$
  select coalesce(jsonb_agg(jsonb_build_object('id',device_id,'label',label,'current',device_id=auth.uid())
    order by created_at,device_id),'[]') from public.health_beta_devices
  where workspace_id=public.health_beta_workspace() and active and pending_pair is null
$$;

create or replace function public.health_beta_device_revoke(target uuid)
returns boolean language plpgsql security definer set search_path=public,pg_temp as $$
declare ws uuid:=public.health_beta_workspace();
begin
  if ws is null or target=auth.uid() then raise sqlstate 'PT400' using message='Cannot disconnect this device'; end if;
  -- Serialize against pairing/activation and workspace writes.
  perform 1 from public.health_beta_data where user_id=ws for update;
  if ws is distinct from public.health_beta_workspace() then raise sqlstate 'PT400' using message='Device disconnected'; end if;
  update public.health_beta_devices set active=false,epoch=gen_random_uuid()
    where device_id=target and workspace_id=ws and active;
  if not found then raise sqlstate 'PT400' using message='Device not found'; end if;
  update public.health_beta_pairs set state='rejected' where issuer=target and state in ('issued','requested','approved');
  perform public.health_beta_signal(target);
  perform public.health_beta_signal(auth.uid());
  return true;
end $$;

-- Read-only staging access exists ONLY after issuer approval, for the bound session.
create or replace function public.health_beta_pair_photo(folder text)
returns boolean language sql stable security definer set search_path=public,pg_temp as $$
  select exists(select 1 from public.health_beta_pairs p join public.health_beta_devices d on d.device_id=p.issuer
    where p.joiner=auth.uid() and p.workspace_id::text=folder and p.state in ('approved','activated')
      and p.expires_at>now() and d.active and d.pending_pair is null and d.workspace_id=p.workspace_id)
$$;

create or replace function public.health_beta_pair(action text, ticket uuid default null, secret text default null)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare p public.health_beta_pairs; d public.health_beta_devices; ws uuid; result jsonb; prior jsonb;
begin
  if auth.uid() is null then raise sqlstate 'PT400' using message='Authentication required'; end if;
  ws:=public.health_beta_workspace();
  if action='create' then
    if ws is null or length(secret)<>64 or secret !~ '^[a-f0-9]+$' then raise sqlstate 'PT400' using message='Pairing unavailable'; end if;
    perform 1 from public.health_beta_data where user_id=ws for update;
    if ws is distinct from public.health_beta_workspace() then raise sqlstate 'PT400' using message='Device disconnected'; end if;
    update public.health_beta_pairs set state='rejected' where issuer=auth.uid() and state in ('issued','requested','approved');
    insert into public.health_beta_pairs(secret_hash,issuer,workspace_id,code)
      values(md5(secret),auth.uid(),ws,lpad((floor(random()*1000000)::integer)::text,6,'0')) returning * into p;
  else
    if action='request' then
      select * into p from public.health_beta_pairs where secret_hash=md5(secret);
    else
      select * into p from public.health_beta_pairs where id=ticket;
    end if;
    if p.id is null then raise sqlstate 'PT400' using message='Pairing code is invalid or no longer available'; end if;
    -- Use the same lock order as revocation/writes. Recheck the ticket after
    -- obtaining the workspace lock so concurrent approval/cancel cannot race.
    perform 1 from public.health_beta_data where user_id=p.workspace_id for update;
    select * into p from public.health_beta_pairs where id=p.id for update;
    if action='request' then
      if p.state<>'issued' or p.expires_at<=now() or p.issuer=auth.uid() then raise sqlstate 'PT400' using message='Pairing code expired or already used'; end if;
      if not exists(select 1 from public.health_beta_devices where device_id=p.issuer and active and pending_pair is null and workspace_id=p.workspace_id) then raise sqlstate 'PT400' using message='Pairing device disconnected'; end if;
      update public.health_beta_pairs set joiner=auth.uid(),state='requested' where id=p.id returning * into p;
      perform public.health_beta_signal(p.issuer);
    else
      if auth.uid() not in (p.issuer,coalesce(p.joiner,p.issuer)) then raise sqlstate 'PT400' using message='Pairing access denied'; end if;
      if action in ('approve','stage','activate') and (p.expires_at<=now() or not exists(
        select 1 from public.health_beta_devices where device_id=p.issuer and active and pending_pair is null and workspace_id=p.workspace_id)) then raise sqlstate 'PT400' using message='Pairing code expired or device disconnected'; end if;
      if action='approve' then
        if auth.uid()<>p.issuer or ws<>p.workspace_id or p.state<>'requested' then raise sqlstate 'PT400' using message='Pairing approval denied'; end if;
        update public.health_beta_pairs set state='approved' where id=p.id returning * into p;
        perform public.health_beta_signal(p.joiner);
      elsif action='stage' then
        if auth.uid()<>p.joiner or p.state<>'approved' then raise sqlstate 'PT400' using message='Pairing approval required'; end if;
        select jsonb_build_object('cursor',w.revision,'changes',coalesce((select jsonb_agg(jsonb_build_object(
          'key',r.key,'value',r.body,'version',r.revision)) from public.health_beta_sync_records r where r.user_id=p.workspace_id),'[]'),
          'images',coalesce(w.document->'images','[]')) into result from public.health_beta_data w where w.user_id=p.workspace_id;
        return result;
      elsif action='activate' then
        if auth.uid()<>p.joiner then raise sqlstate 'PT400' using message='Pairing access denied'; end if;
        if p.state='approved' then
          select to_jsonb(x) into prior from public.health_beta_devices x where device_id=auth.uid() for update;
          if prior is null then raise sqlstate 'PT400' using message='Open the app before joining'; end if;
          update public.health_beta_devices set workspace_id=p.workspace_id,epoch=gen_random_uuid(),active=true,joined=true,pending_pair=p.id
            where device_id=auth.uid() returning * into d;
          update public.health_beta_pairs set previous=prior,activated_epoch=d.epoch,state='activated' where id=p.id returning * into p;
        elsif p.state<>'activated' then raise sqlstate 'PT400' using message='Pairing code already used'; end if;
      elsif action='finish' then
        if auth.uid()<>p.joiner or p.state not in ('activated','completed') then raise sqlstate 'PT400' using message='Pairing access denied'; end if;
        update public.health_beta_devices set pending_pair=null where device_id=auth.uid() and pending_pair=p.id and epoch=p.activated_epoch;
        update public.health_beta_pairs set state='completed',previous=null where id=p.id returning * into p;
        perform public.health_beta_signal(p.issuer); perform public.health_beta_signal(p.joiner);
      elsif action in ('cancel','reject') then
        if action='reject' and auth.uid()<>p.issuer then raise sqlstate 'PT400' using message='Pairing access denied'; end if;
        if p.state='activated' and auth.uid()=p.joiner then
          update public.health_beta_devices set workspace_id=(p.previous->>'workspace_id')::uuid,
            epoch=(p.previous->>'epoch')::uuid,active=(p.previous->>'active')::boolean,joined=(p.previous->>'joined')::boolean,pending_pair=null
            where device_id=auth.uid() and pending_pair=p.id and epoch=p.activated_epoch;
        elsif p.state in ('activated','completed') then raise sqlstate 'PT400' using message='Pairing already completed'; end if;
        update public.health_beta_pairs set state='rejected',previous=null where id=p.id returning * into p;
        perform public.health_beta_signal(p.issuer); if p.joiner is not null then perform public.health_beta_signal(p.joiner); end if;
      elsif action<>'status' then raise sqlstate 'PT400' using message='Invalid pairing action'; end if;
    end if;
  end if;
  select * into d from public.health_beta_devices where device_id=p.joiner;
  return jsonb_build_object('ticket',p.id,'state',case when p.expires_at<=now() and p.state in ('issued','requested','approved') then 'expired' else p.state end,
    'code',p.code,'expires_at',p.expires_at,'label',d.label,'workspace_id',p.workspace_id,'device_epoch',p.activated_epoch);
end $$;

-- Device-aware policies replace owner-only access, including direct API/Storage access.
drop policy if exists tester_data on public.health_beta_data;
create policy tester_data on public.health_beta_data to authenticated using(user_id=(select public.health_beta_workspace())) with check(user_id=(select public.health_beta_workspace()));
drop policy if exists own_sync_records on public.health_beta_sync_records;
create policy own_sync_records on public.health_beta_sync_records for select to authenticated using(user_id=(select public.health_beta_workspace()));
drop policy if exists tester_images_read on storage.objects;
create policy tester_images_read on storage.objects for select to authenticated using(bucket_id='health-diet-images' and
  ((storage.foldername(name))[1]=(select public.health_beta_workspace())::text or public.health_beta_pair_photo((storage.foldername(name))[1])));
drop policy if exists tester_images_insert on storage.objects;
create policy tester_images_insert on storage.objects for insert to authenticated with check(bucket_id='health-diet-images' and (storage.foldername(name))[1]=(select public.health_beta_workspace())::text);
drop policy if exists tester_images_update on storage.objects;
create policy tester_images_update on storage.objects for update to authenticated using(bucket_id='health-diet-images' and (storage.foldername(name))[1]=(select public.health_beta_workspace())::text)
  with check(bucket_id='health-diet-images' and (storage.foldername(name))[1]=(select public.health_beta_workspace())::text);
drop policy if exists workspace_events on public.health_beta_events;
create policy workspace_events on public.health_beta_events for select to authenticated using(workspace_id=(select public.health_beta_workspace()));
drop policy if exists device_signals on public.health_beta_signals;
create policy device_signals on public.health_beta_signals for select to authenticated using(device_id=(select auth.uid()));

-- Preserve the existing, tested delta/conflict/idempotency implementation.
do $$ declare f record; source text; begin
  for f in select oid from pg_proc where pronamespace='public'::regnamespace and proname in
    ('health_beta_save','health_beta_delete_week','health_beta_sync_pull','health_beta_sync_push') loop
    source:=pg_get_functiondef(f.oid);
    execute replace(source,'auth.uid()','public.health_beta_workspace()');
  end loop;
end $$;
create or replace function public.health_beta_device_push(operation jsonb, expected_workspace uuid, expected_epoch uuid)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
begin
  perform 1 from public.health_beta_data where user_id=expected_workspace for update;
  if expected_workspace is distinct from public.health_beta_workspace() or not exists(select 1 from public.health_beta_devices
    where device_id=auth.uid() and epoch=expected_epoch and active and pending_pair is null) then
    raise sqlstate 'PT400' using message='Device disconnected or workspace changed';
  end if;
  return public.health_beta_sync_push(operation);
end $$;
revoke all on function public.health_beta_device_push(jsonb,uuid,uuid) from public,anon;
grant execute on function public.health_beta_device_push(jsonb,uuid,uuid) to authenticated;
create or replace function public.health_beta_notify()
returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
begin
  insert into public.health_beta_events values(new.user_id,new.revision)
    on conflict(workspace_id) do update set revision=excluded.revision;
  return new;
end $$;
revoke all on function public.health_beta_notify() from public,anon,authenticated;
drop trigger if exists health_beta_notify_changes on public.health_beta_data;
create trigger health_beta_notify_changes after insert or update on public.health_beta_data for each row execute function public.health_beta_notify();
insert into public.health_beta_devices(device_id,workspace_id) select user_id,user_id from public.health_beta_data on conflict do nothing;
insert into public.health_beta_events select user_id,revision from public.health_beta_data on conflict do nothing;
do $$ begin
  if exists(select 1 from pg_publication where pubname='supabase_realtime') then
    if not exists(select 1 from pg_publication_tables where pubname='supabase_realtime' and tablename='health_beta_events' and schemaname='public') then alter publication supabase_realtime add table public.health_beta_events; end if;
    if not exists(select 1 from pg_publication_tables where pubname='supabase_realtime' and tablename='health_beta_signals' and schemaname='public') then alter publication supabase_realtime add table public.health_beta_signals; end if;
  end if;
end $$;
revoke all on function public.health_beta_device_context(text),public.health_beta_device_list(),public.health_beta_device_revoke(uuid),public.health_beta_pair(text,uuid,text),public.health_beta_pair_photo(text) from public,anon;
grant execute on function public.health_beta_device_context(text),public.health_beta_device_list(),public.health_beta_device_revoke(uuid),public.health_beta_pair(text,uuid,text),public.health_beta_pair_photo(text) to authenticated;
notify pgrst,'reload schema';
commit;
