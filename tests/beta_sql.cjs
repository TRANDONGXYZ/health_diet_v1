// Optional real PostgreSQL (PGlite) regression check; never connects to Supabase.
// npm install --prefix .tools/sql-check --no-save --package-lock=false @electric-sql/pglite
const {PGlite}=require('../.tools/sql-check/node_modules/@electric-sql/pglite');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const migration=fs.readFileSync(path.join(__dirname,'../supabase/beta.sql'),'utf8');
const a='11111111-1111-4111-8111-111111111111',b='22222222-2222-4222-8222-222222222222';
const setup=`
create role anon nologin;
create role authenticated nologin;
create schema auth;
create table auth.users(id uuid primary key);
insert into auth.users values ('${a}'),('${b}');
create function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid
$$;
create schema storage;
create table storage.buckets(id text primary key, name text, public boolean, file_size_limit bigint, allowed_mime_types text[]);
create table storage.objects(bucket_id text, name text, primary key(bucket_id,name));
alter table storage.objects enable row level security;
create function storage.foldername(name text) returns text[] language sql immutable as $$ select string_to_array(name,'/') $$;
grant usage on schema public,auth,storage to authenticated,anon;
grant select,insert,update on storage.objects to authenticated;
`;
async function run(){
  const db=new PGlite();
  try{
    await db.exec(setup);
    // Reproduce the original reported error using PostgreSQL's PL/pgSQL parser.
    const broken=migration.replace('+ (case when i<=2 then cost else 1 end) > limits[i]', '+ case when i<=2 then cost else 1 end > limits[i]');
    assert.notEqual(broken,migration);
    await assert.rejects(db.exec(broken),error=>error.code==='42601');
    await db.exec(migration);
    await db.exec(migration); // Existing installations can safely rerun the script.
    await db.exec(`set role authenticated; select set_config('request.jwt.claim.sub','${a}',false);`);
    const value=async(sql,params=[])=>Object.values((await db.query(sql,params)).rows[0])[0];
    assert.equal(await value('select public.health_beta_save(0,$1::jsonb) as revision',[JSON.stringify({test:'A'})]),1);
    assert.equal(await value('select public.health_beta_save(1,$1::jsonb) as revision',[JSON.stringify({test:'A updated'})]),2);
    await assert.rejects(db.query("select public.health_beta_save(1,'{}'::jsonb)"),error=>error.code==='PT409');
    await db.query("insert into storage.objects values ('health-diet-images',$1)",[a+'/photo.png']);
    await db.exec(`select set_config('request.jwt.claim.sub','${b}',false);`);
    assert.deepEqual((await db.query('select * from public.health_beta_data')).rows,[]);
    assert.deepEqual((await db.query('select * from storage.objects')).rows,[]);
    await assert.rejects(db.query("insert into storage.objects values ('health-diet-images',$1)",[a+'/forged.png']),error=>error.code==='42501');
    await assert.rejects(db.query('select * from public.health_beta_usage'),error=>error.code==='42501');
    await db.exec(`select set_config('request.jwt.claim.sub','${a}',false);`);
    for(let i=0;i<4;i++)assert.equal(await value('select public.health_beta_take_ai(false)'),true);
    assert.equal(await value('select public.health_beta_take_ai(false)'),false,'Four requests per tester per minute');
    await db.exec("reset role; truncate public.health_beta_usage; set role authenticated;");
    for(let i=0;i<4;i++)assert.equal(await value('select public.health_beta_take_ai(true)'),true);
    await db.exec("reset role; delete from public.health_beta_usage where scope like 'minute:%'; set role authenticated;");
    assert.equal(await value('select public.health_beta_take_ai(false)'),false,'Audio consumes five of twenty daily units');
    await db.exec(`select set_config('request.jwt.claim.sub','${b}',false);`);
    assert.equal(await value('select public.health_beta_take_ai(false)'),true,'Other tester retains their own daily allowance');
    await db.exec("reset role; update public.health_beta_usage set used=200 where scope='day:global'; set role authenticated;");
    assert.equal(await value('select public.health_beta_take_ai(false)'),false,'Global daily cap applies to every tester');
    const week='2026-11-16',otherWeek='2026-11-23';
    const document={tables:{plans:[{id:week,body:{week_start:week,days:[]}},{id:otherWeek,body:{week_start:otherWeek,days:[]}}],recipes:[{id:'recipe',body:{name:'Keep me'}}],state:[{id:'week_expansion',body:{[week]:false}}],foods:[],cache:[]},images:['keep.png'],config:{keep:'settings'}};
    await value('select public.health_beta_save(0,$1::jsonb)',[JSON.stringify(document)]); // Tester B.
    await db.exec(`select set_config('request.jwt.claim.sub','${a}',false);`);
    await value('select public.health_beta_save(2,$1::jsonb)',[JSON.stringify(document)]);
    const deleted=await value('select public.health_beta_delete_week($1)',[week]);
    assert.deepEqual(deleted,{ok:true,week_start:week});
    const expected=structuredClone(document);expected.tables.plans=expected.tables.plans.filter(plan=>plan.id!==week);
    assert.deepEqual(await value('select document from public.health_beta_data'),expected);
    await assert.rejects(db.query('select public.health_beta_save(3,$1::jsonb)',[JSON.stringify(document)]),error=>error.code==='PT409','A stale full save cannot resurrect the deleted week');
    await assert.rejects(db.query('select public.health_beta_delete_week($1)',[week]),error=>error.code==='PT404');
    await assert.rejects(db.query("select public.health_beta_delete_week('2026-11-17')"),error=>error.code==='22023');
    await db.exec(`select set_config('request.jwt.claim.sub','${b}',false);`);
    assert.deepEqual(await value('select document from public.health_beta_data'),document,'Tester B is unchanged');
    console.log('PASS: reproduced original 42601; fixed full migration compiles and reruns in PostgreSQL; revision checks, owner policies and minute/daily/global AI limits execute correctly. Supabase schemas are test fixtures.');
  }finally{await db.close();}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
