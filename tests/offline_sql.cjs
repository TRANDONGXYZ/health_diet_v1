// Real PostgreSQL execution, including the upgrade of existing beta documents.
const {PGlite}=require('../.tools/sql-check/node_modules/@electric-sql/pglite');
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const a='11111111-1111-4111-8111-111111111111',b='22222222-2222-4222-8222-222222222222';
async function run(){const db=new PGlite();try{
  await db.exec(`create role anon nologin; create role authenticated nologin; create schema auth; create table auth.users(id uuid primary key); insert into auth.users values('${a}'),('${b}'); create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$; create schema storage; create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint,allowed_mime_types text[]); create table storage.objects(bucket_id text,name text,metadata jsonb default '{}'); create function storage.foldername(name text) returns text[] language sql immutable as $$ select string_to_array(name,'/') $$; grant usage on schema public,auth,storage to authenticated,anon;`);
  await db.exec(fs.readFileSync('supabase/beta.sql','utf8'));
  const query=async(sql,params=[])=>Object.values((await db.query(sql,params)).rows[0])[0];
  await db.exec(`set role authenticated; select set_config('request.jwt.claim.sub','${a}',false);`);
  const recipe={id:'r1',name:'Phở',ingredients:[],servings:2},week='2026-11-16',day='2026-11-16';
  const doc={tables:{recipes:[{id:'r1',body:recipe}],foods:[],plans:[{id:week,body:{week_start:week,method:'manual',days:[{date:day,recipe_id:null}]}}],state:[{id:'weekly_history_migrated',body:true}],cache:[]},config:null,images:[]};
  await query('select health_beta_save(0,$1::jsonb)',[JSON.stringify(doc)]);
  await db.exec('reset role');await db.exec(fs.readFileSync('supabase/offline.sql','utf8'));await db.exec(fs.readFileSync('supabase/offline.sql','utf8'));
  await db.exec(`set role authenticated; select set_config('request.jwt.claim.sub','${a}',false);`);
  const pull=()=>query('select health_beta_sync_pull(0)'),push=op=>query('select health_beta_sync_push($1::jsonb)',[JSON.stringify(op)]);
  let initial=await pull();assert.equal(initial.cursor,1);assert.equal(initial.changes.length,3,'Existing records migrate without loss');
  const key=`meals/${week}/${day}`,op={id:crypto.randomUUID(),since:1,changes:[{key,base:1,value:{date:day,recipe_id:'r1'}}]};
  const result=await push(op);assert.equal(result.changes.length,1);assert.equal(result.changes[0].value.recipe.name,'Phở');assert.equal(result.cursor,2);assert.deepEqual(await push(op),result,'Lost response retry has same durable receipt');assert.equal((await pull()).cursor,2);
  const conflict=await push({id:crypto.randomUUID(),changes:[{key,base:1,value:{date:day,recipe_id:null}}]});assert.equal(conflict.conflict,true);assert.equal((await pull()).cursor,2);
  const deletion=await push({id:crypto.randomUUID(),since:2,changes:[{key:'recipes/r1',base:1,value:null}]});assert.equal(deletion.changes.length,2);assert.deepEqual(deletion.changes.find(c=>c.key===key).value,{date:day,recipe_id:'r1',deleted:true});
  assert.equal((await push({id:crypto.randomUUID(),changes:[{key:'recipes/r1',base:3,value:recipe}]})).conflict,true,'Tombstones prevent resurrection even with a newer version');
  await assert.rejects(push({...op,changes:[{key,base:2,value:{date:day,recipe_id:null}}]}),/Operation ID reused/);
  await db.exec(`select set_config('request.jwt.claim.sub','${b}',false);`);assert.deepEqual((await pull()).changes,[]);assert.equal((await db.query('select * from health_beta_sync_records')).rows.length,0);
  await assert.rejects(db.query('select * from health_beta_sync_ops'),error=>error.code==='42501');
  await db.exec(`select set_config('request.jwt.claim.sub','${a}',false);`);
  const delWeek=await push({id:crypto.randomUUID(),since:3,changes:[{key:'plans/'+week,base:1,value:null}]});assert.equal(delWeek.changes.length,2);assert.equal((await pull()).changes.find(c=>c.key===key).value,null);
  const staleSlot=await push({id:crypto.randomUUID(),changes:[{key,base:4,value:{date:day,recipe_id:null}}]});assert.equal(staleSlot.conflict,true,'Editing a slot cannot resurrect its deleted week');
  // Old backend writes are observed by new incremental clients, too.
  const old=await query('select revision from health_beta_data');doc.tables.recipes=[{id:'legacy',body:{id:'legacy',name:'Old client'}}];await query('select health_beta_save($1,$2::jsonb)',[old,JSON.stringify(doc)]);assert.ok((await pull()).changes.some(c=>c.key==='recipes/legacy'));
  const foodId='salt / pepper',food=await push({id:crypto.randomUUID(),since:(await pull()).cursor,changes:[{key:'foods/'+foodId,base:0,value:{id:foodId,name:'Salt / pepper',kcal:12}}]});assert.equal(food.changes[0].value.name,'Salt / pepper','Older ingredient IDs may contain slashes');
  const name='a'.repeat(64)+'.png',imageOp={id:crypto.randomUUID(),kind:'photo',name,size:100,since:(await pull()).cursor};
  const image=await push(imageOp);assert.equal(image.changes.length,1);assert.equal(image.changes[0].key,'photos/'+name);assert.deepEqual(await push(imageOp),image);
  await db.exec('reset role');await db.query("insert into storage.objects values('health-diet-images',$1,$2::jsonb)",[a+'/'+name,JSON.stringify({size:33554432})]);await db.exec(`set role authenticated; select set_config('request.jwt.claim.sub','${a}',false);`);
  await assert.rejects(push({id:crypto.randomUUID(),kind:'photo',name:'b'.repeat(64)+'.png',size:100}),error=>error.code==='PT413','Existing private image sizes count toward the aggregate cap');
  console.log('PASS PostgreSQL: migration, granular slot response, atomic deletion cascades, durable idempotency, version conflicts, tombstones, owner isolation and legacy-write tracking.');
}finally{await db.close();}}
run().catch(error=>{console.error(error);process.exitCode=1;});
