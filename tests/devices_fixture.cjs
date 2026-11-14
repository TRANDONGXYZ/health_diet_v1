// Isolated provider fixture: real PostgreSQL policies/functions; simulated Auth,
// Storage HTTP and Realtime transport. No production credentials or tester data.
const {PGlite}=require('../.tools/sql-check/node_modules/@electric-sql/pglite');
const fs=require('node:fs'),http=require('node:http'),https=require('node:https'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto'),{spawn,spawnSync}=require('node:child_process'),{WebSocketServer}=require('ws');
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function database(){const db=new PGlite();await db.exec(`
 create role anon nologin;create role authenticated nologin;create schema auth;
 create table auth.users(id uuid primary key);
 create function auth.uid() returns uuid language sql stable as $$select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;
 create schema storage;create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint,allowed_mime_types text[]);
 create table storage.objects(bucket_id text,name text,metadata jsonb default '{}',primary key(bucket_id,name));
 alter table storage.objects enable row level security;
 create function storage.foldername(name text) returns text[] language sql immutable as $$select string_to_array(name,'/')$$;
 grant usage on schema public,auth,storage to authenticated,anon;
 grant select,insert,update on storage.objects to authenticated;
 `);for(const name of ['beta','offline','devices'])await db.exec(fs.readFileSync('supabase/'+name+'.sql','utf8'));return db;}
async function start(){
 const db=await database(),images=new Map(),stats=[],streams=new Set(),folder=fs.mkdtempSync(path.join(os.tmpdir(),'health-devices-'));let tail=Promise.resolve(),app;
 const serial=fn=>{const next=tail.then(fn,fn);tail=next.catch(()=>{});return next;};
 const as=async(uid,fn)=>{await db.exec(`set role authenticated;select set_config('request.jwt.claim.sub','${uid}',false);`);try{return await fn();}finally{await db.exec('reset role');}};
 const value=async(sql,args=[])=>Object.values((await db.query(sql,args)).rows[0])[0];
 async function notify(){for(const stream of streams){if(!stream.joined)continue;await as(stream.uid,async()=>{for(const table of ['health_beta_events','health_beta_signals']){
   const rows=(await db.query('select * from '+table)).rows;
   for(const row of rows){const column=row.workspace_id?'workspace_id':'device_id';if(!stream.filters.some(f=>f.table===table&&f.filter===column+'=eq.'+row[column]))continue;const key=table+':'+row[column],prior=stream.seen.get(key)||0;if(prior!==row.revision){stream.seen.set(key,row.revision);stream.ws.send(JSON.stringify({topic:stream.topic,event:'postgres_changes',payload:{data:{table,record:row,type:'UPDATE'}}}));}}
 }});}}
 const provider=http.createServer((req,res)=>{const chunks=[];req.on('data',c=>chunks.push(c));req.on('end',()=>serial(async()=>{const bytes=Buffer.concat(chunks),url=new URL(req.url,'http://fixture'),uid=(req.headers.authorization||'').replace('Bearer ','');stats.push({path:url.pathname,body:bytes.length?bytes.toString():null,uid});try{
   if(url.pathname==='/auth/v1/signup'){const id=crypto.randomUUID();await db.query('insert into auth.users values($1)',[id]);res.end(JSON.stringify({user:{id},access_token:id,refresh_token:id,expires_at:Math.floor(Date.now()/1000)+3600}));return;}
   const result=await as(uid,async()=>{
    if(url.pathname.startsWith('/storage/v1/object/')){const name=url.pathname.split('health-diet-images/')[1];if(req.method==='GET'){const access=await db.query("select name from storage.objects where bucket_id='health-diet-images' and name=$1",[name]);if(!access.rows.length||!images.has(name)){res.statusCode=403;return{};}res.setHeader('Content-Type','image/png');return images.get(name);}
     await db.query("insert into storage.objects values('health-diet-images',$1,$2::jsonb) on conflict(bucket_id,name) do update set metadata=excluded.metadata",[name,JSON.stringify({size:bytes.length})]);images.set(name,bytes);return {Key:name};}
    if(url.pathname==='/rest/v1/health_beta_data')return(await db.query('select revision,document from health_beta_data')).rows;
    if(url.pathname.startsWith('/rest/v1/rpc/')){const name=url.pathname.split('/').at(-1);if(!/^health_beta_[a-z_]+$/.test(name))throw new Error('Unknown function');const args=JSON.parse(bytes.toString()||'{}'),keys=Object.keys(args);if(!keys.every(k=>/^[a-z_]+$/.test(k)))throw new Error('Invalid argument');return value('select '+name+'('+keys.map((k,i)=>k+'=> $'+(i+1)).join(',')+')',Object.values(args));}
    throw new Error('Unknown fixture path');
   });res.end(Buffer.isBuffer(result)?result:JSON.stringify(result));await notify();
 }catch(e){res.statusCode=e.code?.startsWith('PT')?Number(e.code.slice(2)):e.code==='42501'?403:500;res.end(JSON.stringify({code:e.code,message:e.message}));}}));});
 const sockets=new WebSocketServer({server:provider});
 sockets.on('connection',ws=>{const stream={ws,uid:null,seen:new Map()};streams.add(stream);ws.on('close',()=>streams.delete(stream));ws.on('message',raw=>serial(async()=>{const msg=JSON.parse(raw);if(msg.event==='heartbeat'){ws.send(JSON.stringify({...msg,event:'phx_reply',payload:{status:'ok',response:{}}}));return;}
   if(msg.event!=='phx_join')return;stream.uid=msg.payload.access_token;stream.topic=msg.topic;stream.filters=msg.payload.config.postgres_changes;await as(stream.uid,async()=>{for(const table of ['health_beta_events','health_beta_signals'])for(const r of (await db.query('select * from '+table)).rows)stream.seen.set(table+':'+(r.workspace_id||r.device_id),r.revision);});
   stream.joined=true;ws.send(JSON.stringify({topic:msg.topic,event:'phx_reply',ref:msg.ref,payload:{status:'ok',response:{postgres_changes:msg.payload.config.postgres_changes.map((_,i)=>({id:i+1}))}}}));
 }));});
 await new Promise(r=>provider.listen(8796,'127.0.0.1',r));
 const cert=spawnSync('C:/Program Files/Git/usr/bin/openssl.exe',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',path.join(folder,'key.pem'),'-out',path.join(folder,'cert.pem'),'-days','1','-subj','/CN=localhost'],{windowsHide:true,encoding:'utf8'});if(cert.status!==0)throw new Error(cert.stderr);
 const proxy=https.createServer({key:fs.readFileSync(path.join(folder,'key.pem')),cert:fs.readFileSync(path.join(folder,'cert.pem'))},(req,res)=>{const upstream=http.request({hostname:'127.0.0.1',port:8797,path:req.url,method:req.method,headers:req.headers},reply=>{res.writeHead(reply.statusCode,reply.headers);reply.pipe(res);});upstream.on('error',()=>{res.statusCode=503;res.end('{}');});req.pipe(upstream);res.on('close',()=>upstream.destroy());});await new Promise(r=>proxy.listen(8795,'127.0.0.1',r));
 app=spawn(process.env.PYTHON||'python',['tests/devices_server.py','--port','8797'],{env:{...process.env,APP_MODE:'hosted',DEVICE_PAIRING_ENABLED:'1',PUBLIC_ORIGIN:'https://127.0.0.1:8795',SESSION_SECRET:'test-only-device-secret-'.repeat(3),SUPABASE_URL:'https://fixture.supabase.co',SUPABASE_PUBLISHABLE_KEY:'sb_publishable_fixture',HEALTH_TEST_PROVIDER:'http://127.0.0.1:8796',MEAL_APP_DATA:folder},windowsHide:true,stdio:'pipe'});
 let log='';app.stderr.on('data',b=>{log+=b;});for(let i=0;i<100;i++){try{if((await fetch('http://127.0.0.1:8797/healthz')).ok)break;}catch{}if(i===99)throw new Error(log);await wait(100);}
 return {db,serial,as,value,stats,streams,notify,origin:'https://127.0.0.1:8795',log:()=>log,
   async close(){app.kill();await new Promise(r=>app.once('exit',r));for(const s of streams)s.ws.terminate();await new Promise(r=>sockets.close(r));proxy.closeAllConnections();await new Promise(r=>proxy.close(r));provider.closeAllConnections();await new Promise(r=>provider.close(r));await tail;await db.close();if(!path.resolve(folder).startsWith(path.join(os.tmpdir(),'health-devices-')))throw new Error('Unsafe cleanup');fs.rmSync(folder,{recursive:true,force:true});}};
}
module.exports={database,start,wait};
