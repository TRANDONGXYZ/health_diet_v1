-- Run AFTER beta.sql. Safe to rerun; preserves every existing tester workspace.
-- Record versions/tombstones also track writes from older deployed clients.
create table if not exists public.health_beta_sync_records (
  user_id uuid not null references auth.users(id) on delete cascade,
  key text not null, body jsonb, revision bigint not null,
  primary key(user_id,key)
);
create table if not exists public.health_beta_sync_ops (
  user_id uuid not null references auth.users(id) on delete cascade,
  id uuid not null, digest text not null, result jsonb not null,
  primary key(user_id,id)
);
alter table public.health_beta_sync_records enable row level security;
alter table public.health_beta_sync_ops enable row level security;
revoke all on public.health_beta_sync_records, public.health_beta_sync_ops from public,anon,authenticated;
grant select on public.health_beta_sync_records to authenticated;
drop policy if exists own_sync_records on public.health_beta_sync_records;
create policy own_sync_records on public.health_beta_sync_records for select to authenticated using (user_id=(select auth.uid()));

create or replace function public.health_beta_entities(doc jsonb)
returns table(key text, body jsonb) language sql immutable set search_path=public,pg_temp as $$
  select t.name||'/'||(r->>'id'),r->'body'
    from (values ('recipes'),('foods')) as t(name),jsonb_array_elements(coalesce(doc->'tables'->t.name,'[]')) r
  union all
  select 'plans/'||(r->>'id'),(r->'body')-'days' from jsonb_array_elements(coalesce(doc#>'{tables,plans}','[]')) r
  union all
  select 'meals/'||(r->>'id')||'/'||(d->>'date'),d
    from jsonb_array_elements(coalesce(doc#>'{tables,plans}','[]')) r,
         jsonb_array_elements(coalesce(r#>'{body,days}','[]')) d
  union all
  select 'settings',r->'body' from jsonb_array_elements(coalesce(doc#>'{tables,state}','[]')) r where r->>'id'='settings'
  union all
  select 'expansion/'||x.key,x.value from jsonb_array_elements(coalesce(doc#>'{tables,state}','[]')) r,
         lateral jsonb_each(case when r->>'id'='week_expansion' then r->'body' else '{}' end) x
  union all
  select 'photos/'||name,jsonb_build_object('name',name) from jsonb_array_elements_text(coalesce(doc->'images','[]')) name
$$;

create or replace function public.health_beta_track_sync()
returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
begin
  insert into public.health_beta_sync_records(user_id,key,body,revision)
    select new.user_id,coalesce(a.key,b.key),a.body,new.revision
    from public.health_beta_entities(new.document) a
    full join (select key,body from public.health_beta_sync_records where user_id=new.user_id) b using(key)
    where a.body is distinct from b.body or b.key is null
    on conflict(user_id,key) do update set body=excluded.body,revision=excluded.revision;
  return new;
end $$;
drop trigger if exists health_beta_sync_changes on public.health_beta_data;
create trigger health_beta_sync_changes after insert or update on public.health_beta_data
  for each row execute function public.health_beta_track_sync();
insert into public.health_beta_sync_records(user_id,key,body,revision)
  select d.user_id,e.key,e.body,d.revision from public.health_beta_data d,lateral public.health_beta_entities(d.document) e
  on conflict(user_id,key) do nothing;

create or replace function public.health_beta_sync_pull(after_revision bigint default 0)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare workspace public.health_beta_data; result jsonb;
begin
  if auth.uid() is null then raise exception 'Authentication required'; end if;
  if after_revision<0 then raise exception 'Invalid cursor'; end if;
  select * into workspace from public.health_beta_data where user_id=auth.uid() for share;
  select jsonb_build_object('cursor',coalesce(workspace.revision,0),'changes',coalesce(jsonb_agg(
    jsonb_build_object('key',key,'value',body,'version',revision) order by revision,key),'[]')) into result
    from public.health_beta_sync_records where user_id=auth.uid() and revision>after_revision;
  if after_revision=0 then result=result||jsonb_build_object('images',coalesce(workspace.document->'images','[]')); end if;
  return result;
end $$;

create or replace function public.health_beta_sync_push(operation jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare
  who uuid:=auth.uid(); receipt public.health_beta_sync_ops; workspace public.health_beta_data;
  op_id uuid:=(operation->>'id')::uuid; digest text:=md5(operation::text);
  change jsonb; item_key text; val jsonb; actual public.health_beta_sync_records;
  entities jsonb; doc jsonb; conflicts jsonb:='[]'; result jsonb; kind text:=coalesce(operation->>'kind','change');
  rid text; parent text; rec jsonb; entry record; photo_bytes bigint; staged boolean:=false;
begin
  if who is null then raise exception 'Authentication required'; end if;
  select * into workspace from public.health_beta_data where user_id=who for update;
  if not found then raise exception 'Load your workspace before syncing'; end if;
  select * into receipt from public.health_beta_sync_ops where user_id=who and id=op_id;
  if found then
    if receipt.digest<>digest then raise exception 'Operation ID reused with a different payload'; end if;
    return receipt.result;
  end if;
  if octet_length(operation::text)>12582912 then raise exception 'Operation too large'; end if;
  if kind not in ('change','restore','photo') then raise exception 'Unknown operation'; end if;
  if kind='change' then
    if jsonb_typeof(operation->'changes')<>'array' or jsonb_array_length(operation->'changes') not between 1 and 32 then raise exception 'Invalid changes'; end if;
    if (select count(distinct v->>'key') from jsonb_array_elements(operation->'changes') v)<>jsonb_array_length(operation->'changes') then raise exception 'Duplicate change key'; end if;
    for change in select * from jsonb_array_elements(operation->'changes') loop
      item_key:=change->>'key'; val:=change->'value';
      if item_key !~ '^(recipes/.+|foods/.+|plans/[0-9-]{10}|meals/[0-9-]{10}/[0-9-]{10}|expansion/[0-9-]{10}|settings)$'
         or length(item_key)>4104 or not(change ? 'value') or (change->>'base')::bigint<0 then raise exception 'Invalid change'; end if;
      select * into actual from public.health_beta_sync_records where user_id=who and key=item_key;
      if coalesce(actual.revision,0)<>(change->>'base')::bigint
         or (item_key like 'recipes/%' and val<>'null'::jsonb and actual.body is null and actual.revision>0) then
        conflicts:=conflicts||jsonb_build_array(jsonb_build_object('key',item_key,'value',actual.body,'version',coalesce(actual.revision,0)));
      end if;
    end loop;
  elsif kind='restore' and (operation->>'base')::bigint<>workspace.revision then
    return jsonb_build_object('conflict',true,'changes','[]'::jsonb,'message','The workspace changed elsewhere. Review before replacing it.');
  end if;
  if jsonb_array_length(conflicts)>0 then return jsonb_build_object('conflict',true,'changes',conflicts,'message','This record changed elsewhere. Your local edit is retained.'); end if;
  doc:=workspace.document;
  if kind='photo' then
    staged:=coalesce((operation->>'staged')::boolean,false);
    if operation->>'name' !~ '^[a-f0-9]{64}\.png$' then raise exception 'Invalid image name'; end if;
    if (operation->>'size')::bigint not between 1 and 4000000 then raise exception 'Invalid image size'; end if;
    select coalesce(sum(coalesce((o.metadata->>'size')::bigint,(doc->'image_sizes'->>n.name)::bigint,0)),0)+(operation->>'size')::bigint into photo_bytes
      from jsonb_array_elements_text(coalesce(doc->'images','[]')) n(name)
      left join storage.objects o on o.bucket_id='health-diet-images' and o.name=who::text||'/'||n.name
      where n.name<>operation->>'name';
    if not staged and photo_bytes>33554432 then raise sqlstate 'PT413' using message='Photo storage limit reached'; end if;
    doc:=jsonb_set(doc,'{images}',(select coalesce(jsonb_agg(n order by n),'[]') from
      (select distinct jsonb_array_elements_text(coalesce(doc->'images','[]')||jsonb_build_array(operation->>'name')) n) names));
    doc:=jsonb_set(doc,'{image_sizes}',coalesce(doc->'image_sizes','{}')||jsonb_build_object(operation->>'name',(operation->>'size')::bigint));
  elsif kind='restore' then
    doc:=operation->'document';
    if jsonb_typeof(doc->'tables')<>'object' then raise exception 'Invalid restore'; end if;
  else
    select coalesce(jsonb_object_agg(key,body) filter(where body is not null),'{}') into entities
      from public.health_beta_sync_records where user_id=who;
    for change in select * from jsonb_array_elements(operation->'changes') loop
      item_key:=change->>'key'; val:=change->'value';
      if val='null'::jsonb then entities:=entities-item_key; else entities:=jsonb_set(entities,array[item_key],val); end if;
    end loop;
    -- Apply cascades after all explicit changes, keeping every scheduled date.
    for change in select * from jsonb_array_elements(operation->'changes') loop
      item_key:=change->>'key'; val:=change->'value';
      if item_key like 'recipes/%' and val='null'::jsonb then
        rid:=substring(item_key from 9);
        for entry in select key,value from jsonb_each(entities) where key like 'meals/%' and value->>'recipe_id'=rid loop
          entities:=jsonb_set(entities,array[entry.key],jsonb_build_object('date',entry.value->>'date','recipe_id',rid,'deleted',true));
        end loop;
      elsif item_key like 'plans/%' and val='null'::jsonb then
        parent:='meals/'||substring(item_key from 7)||'/';
        for entry in select key from jsonb_each(entities) where starts_with(key,parent) loop entities:=entities-entry.key; end loop;
      elsif item_key like 'meals/%' and val<>'null'::jsonb then
        parent:='plans/'||split_part(item_key,'/',2);
        rid:=val->>'recipe_id';
        if not(entities ? parent) then return jsonb_build_object('conflict',true,'changes','[]'::jsonb,'message','This week was deleted. Select another week.'); end if;
        if rid is not null and not coalesce((val->>'deleted')::boolean,false) then
          rec:=entities->('recipes/'||rid);
          if rec is null then return jsonb_build_object('conflict',true,'changes','[]'::jsonb,'message','This dish was deleted. Select another dish.'); end if;
          entities:=jsonb_set(entities,array[item_key],(val-'calories')||jsonb_build_object('recipe',
            (rec-'source_text'-'legacy_groups')||case when val ? 'calories' then jsonb_build_object('calories',val->'calories') else '{}'::jsonb end));
          if coalesce((entities->parent->>'vegan_only')::boolean,false) and coalesce((rec->>'vegan')::boolean,false) is not true then
            entities:=jsonb_set(entities,array[parent,'vegan_only'],'false'::jsonb);
          end if;
        end if;
      end if;
    end loop;
    doc:=jsonb_set(doc,'{tables,recipes}',(select coalesce(jsonb_agg(jsonb_build_object('id',substring(key from 9),'body',value) order by key),'[]') from jsonb_each(entities) where key like 'recipes/%'));
    doc:=jsonb_set(doc,'{tables,foods}',(select coalesce(jsonb_agg(jsonb_build_object('id',substring(key from 7),'body',value) order by key),'[]') from jsonb_each(entities) where key like 'foods/%'));
    doc:=jsonb_set(doc,'{tables,plans}',(select coalesce(jsonb_agg(jsonb_build_object('id',substring(p.key from 7),'body',p.value||jsonb_build_object('days',
      (select coalesce(jsonb_agg(m.value order by m.key),'[]') from jsonb_each(entities) m where starts_with(m.key,'meals/'||substring(p.key from 7)||'/')))) order by p.key),'[]') from jsonb_each(entities) p where p.key like 'plans/%'));
    doc:=jsonb_set(doc,'{tables,state}',(select coalesce(jsonb_agg(r),'[]') from jsonb_array_elements(coalesce(doc#>'{tables,state}','[]')) r where r->>'id' not in ('settings','week_expansion'))
      ||jsonb_build_array(jsonb_build_object('id','week_expansion','body',(select coalesce(jsonb_object_agg(substring(key from 11),value),'{}') from jsonb_each(entities) where key like 'expansion/%')))
      ||case when entities ? 'settings' then jsonb_build_array(jsonb_build_object('id','settings','body',entities->'settings')) else '[]'::jsonb end);
  end if;
  if not staged then
    update public.health_beta_data set document=doc,revision=revision+1 where user_id=who returning revision into workspace.revision;
  end if;
  select jsonb_build_object('id',op_id,'cursor',workspace.revision,'changes',coalesce(jsonb_agg(
    jsonb_build_object('key',key,'value',body,'version',revision) order by key),'[]')) into result
    from public.health_beta_sync_records where user_id=who and revision>coalesce((operation->>'since')::bigint,workspace.revision-1);
  insert into public.health_beta_sync_ops values(who,op_id,digest,result);
  return result;
end $$;
revoke all on function public.health_beta_sync_push(jsonb),public.health_beta_sync_pull(bigint),public.health_beta_track_sync() from public,anon;
grant execute on function public.health_beta_sync_push(jsonb),public.health_beta_sync_pull(bigint) to authenticated;
