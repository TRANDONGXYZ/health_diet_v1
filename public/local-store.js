'use strict';
// IndexedDB is the commit boundary. A record change and its outbox entry are
// represented by the same durable operation, in one transaction. Views replay
// the ordered outbox over acknowledged records, never over a stale HTTP state.
const HealthLocal = (() => {
  const LOOPBACK=['127.0.0.1','localhost'].includes(location.hostname);
  const DB='health-diet-offline-v1', stores=['meta','records','outbox','photos'];
  let database, onChange=()=>{}, onStatus=()=>{}, syncing=false, timer, retry=1000, sessionAt=0, blockedIdentity=false,lastViewStamp='';
  let live=null,liveTimer,catchRequested=false,hintCursor=0,liveGeneration=0,pairTicket=null,readInFlight=false;
  const channel=typeof BroadcastChannel==='function'?new BroadcastChannel(DB):null, urls=new Map();
  const req=request=>new Promise((resolve,reject)=>{request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});
  const done=tx=>new Promise((resolve,reject)=>{tx.oncomplete=resolve;tx.onabort=tx.onerror=()=>reject(tx.error||new Error('Browser storage transaction failed.'));});
  const lock=(name,fn)=>navigator.locks?navigator.locks.request(DB+name,fn):fn();
  const clone=value=>structuredClone(value), id=()=>crypto.randomUUID();
  const crcTable=Uint32Array.from({length:256},(_,n)=>{for(let j=0;j<8;j++)n=n&1?0xedb88320^(n>>>1):n>>>1;return n>>>0;});
  function crc(bytes){let n=0xffffffff;for(const b of bytes)n=crcTable[(n^b)&255]^(n>>>8);return (n^0xffffffff)>>>0;}
  async function checkPNG(bytes){
    const fail=()=>{throw new Error('Invalid PNG image; existing data was kept.');};
    if(bytes.length<45||![137,80,78,71,13,10,26,10].every((n,i)=>bytes[i]===n))fail();
    const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength),compressed=[];let at=8,header=false,end=false,expected=0;
    while(at+12<=bytes.length){const size=view.getUint32(at),last=at+12+size;if(last>bytes.length)fail();const kind=String.fromCharCode(...bytes.slice(at+4,at+8));if(crc(bytes.subarray(at+4,last-4))!==view.getUint32(last-4))fail();
      if(!header){if(kind!=='IHDR'||size!==13||view.getUint32(at+8)!==500||view.getUint32(at+12)!==500||bytes[at+16]!==8||![2,6].includes(bytes[at+17])||bytes[at+18]||bytes[at+19]||bytes[at+20])fail();expected=500*(1+500*(bytes[at+17]===6?4:3));header=true;}
      if(kind==='IDAT')compressed.push(bytes.subarray(at+8,last-4));
      if(kind==='IEND'){if(size||last!==bytes.length)fail();end=true;break;}at=last;
    }
    if(!header||!end)fail();const reader=new Blob(compressed).stream().pipeThrough(new DecompressionStream('deflate')).getReader();let count=0;
    for(;;){const {value,done}=await reader.read();if(done)break;count+=value.length;if(count>expected){await reader.cancel();fail();}}
    if(count!==expected)fail();
  }
  function equal(a,b){if(a===b)return true;if(!a||!b||typeof a!=='object'||typeof b!=='object'||Array.isArray(a)!==Array.isArray(b))return false;const keys=Object.keys(a).filter(k=>!k.startsWith('__')).sort(),other=Object.keys(b).filter(k=>!k.startsWith('__')).sort();return keys.length===other.length&&keys.every((key,i)=>key===other[i]&&equal(a[key],b[key]));}
  function sameOriginal(key,a,b){
    if(!key.startsWith('meals/')||!a||!b)return equal(a,b);
    // Recipe projections can refresh while the picker is open without changing
    // the assignment itself. Still detect changes to dates, choices and servings.
    const assignment=value=>{const {recipe,calories,serving_plan,...saved}=value;return serving_plan?{...saved,serving_plan:{servings:serving_plan.servings}}:saved;};
    return equal(assignment(a),assignment(b));
  }
  async function db(){if(database)return database;database=await new Promise((resolve,reject)=>{const r=indexedDB.open(DB,1);r.onupgradeneeded=()=>stores.forEach(name=>r.result.createObjectStore(name,{keyPath:name==='records'?'key':name==='photos'?'name':'id'}));r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);r.onblocked=()=>reject(new Error('Close older app tabs to update browser storage.'));});return database;}
  async function read(){const tx=(await db()).transaction(stores,'readonly'),finished=done(tx);const [meta,records,ops,photos]=await Promise.all(stores.map(name=>req(tx.objectStore(name).getAll())));await finished;return {meta:Object.fromEntries(meta.map(x=>[x.id,x.value])),records,ops:ops.sort((a,b)=>a.order-b.order),photos};}
  function cascade(map,changes){
    const touched=new Set(changes.map(c=>c.key));
    for(const c of changes){if(c.value===null)delete map[c.key];else map[c.key]=clone(c.value);}
    for(const c of changes){
      if(c.key.startsWith('recipes/')&&c.value===null)for(const [key,day] of Object.entries(map))if(key.startsWith('meals/')&&day.recipe_id===c.key.slice(8)){map[key]={date:day.date,recipe_id:day.recipe_id,deleted:true};touched.add(key);}
      if(c.key.startsWith('plans/')&&c.value===null)for(const key of Object.keys(map))if(key.startsWith('meals/'+c.key.slice(6)+'/')){delete map[key];touched.add(key);}
      if(c.key.startsWith('meals/')&&c.value?.recipe_id){const parent='plans/'+c.key.split('/')[1],r=map['recipes/'+c.value.recipe_id];if(map[parent]?.vegan_only&&r&&r.vegan!==true)map[parent].vegan_only=false;}
    }
    return touched;
  }
  function entities(data,parents={}){let map=Object.fromEntries(data.records.filter(x=>x.value!==null).map(x=>[x.key,clone(x.value)]));for(const op of data.ops){let touched;if(op.kind==='restore'){const next=clone(op.entities);touched=new Set([...Object.keys(map),...Object.keys(next)]);map=next;}else if(op.kind==='photo'){const key='photos/'+op.name;map[key]={name:op.name};touched=new Set([key]);}else touched=cascade(map,op.changes);for(const key of touched)parents[key]=op.id;}return refreshLinkedMeals(map);}
  function refreshLinkedMeals(map){
    // Linked meals are projections of the latest recipe, not independent copies.
    // Only the recipe operation needs syncing; dates and saved serving counts stay put.
    // Use the same defaults as the editor when comparing older saved recipes.
    for(const [key,r] of Object.entries(map))if(key.startsWith('recipes/'))map[key]={...r,ingredients:r.ingredients||r.groups?.flatMap(g=>g.ingredients)||r.legacy_groups?.flatMap(g=>g.ingredients)||[],good_for_running:!!r.good_for_running};
    const foods=Object.entries(map).filter(([key])=>key.startsWith('foods/')).map(([,food])=>food),recipes=new Map(),portions=new Map();
    for(const [key,day] of Object.entries(map)){
      if(!key.startsWith('meals/')||day.deleted||!day.recipe_id)continue;
      const source=map['recipes/'+day.recipe_id];if(!source)continue;
      if(!recipes.has(day.recipe_id))recipes.set(day.recipe_id,snapshot({...source,ingredients:source.ingredients||source.groups?.flatMap(g=>g.ingredients)||source.legacy_groups?.flatMap(g=>g.ingredients)||[]},foods));
      const recipe=recipes.get(day.recipe_id),next={...day,recipe:clone(recipe)};
      if(day.serving_plan){const cacheKey=JSON.stringify([day.recipe_id,day.serving_plan.servings]);if(!portions.has(cacheKey))portions.set(cacheKey,makeServingPlan(recipe,foods,day.serving_plan.servings));next.serving_plan=clone(portions.get(cacheKey));}
      map[key]=next;
    }
    return map;
  }
  function calendar(){const now=new Date(),fmt=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`,add=(d,n)=>{const copy=new Date(d);copy.setDate(copy.getDate()+n);return copy;},monday=add(now,-(now.getDay()+6)%7);return {today:fmt(now),week_start:fmt(monday),week_end:fmt(add(monday,6)),advance_limit:fmt(add(monday,56)),dates:Array.from({length:7-(now.getDay()+6)%7},(_,i)=>fmt(add(now,i)))};}
  function view(data){const map=entities(data),plans=Object.entries(map).filter(([k])=>k.startsWith('plans/')).map(([key,value])=>({...value,days:Object.entries(map).filter(([k])=>k.startsWith('meals/'+key.slice(6)+'/')).map(([,v])=>v).sort((a,b)=>a.date.localeCompare(b.date))})).sort((a,b)=>b.week_start.localeCompare(a.week_start)),cal=calendar();return {...data.meta.profile,versions:Object.fromEntries(data.records.map(r=>[r.key,r.version])),settings_record:map.settings||null,settings:{...data.meta.defaults,...map.settings},recipes:Object.entries(map).filter(([k])=>k.startsWith('recipes/')).map(([k,v])=>({...v,ingredients:v.ingredients||v.groups?.flatMap(g=>g.ingredients)||[],good_for_running:!!v.good_for_running,__version:data.records.find(r=>r.key===k)?.version||0})),foods:Object.entries(map).filter(([k])=>k.startsWith('foods/')).map(([,v])=>v),plans,plan:plans.find(p=>p.week_start===cal.week_start)||null,calendar:cal,plan_job:data.meta.planJob||null,week_expansion:Object.fromEntries(Object.entries(map).filter(([k])=>k.startsWith('expansion/')).map(([k,v])=>[k.slice(10),v]))};}
  async function publish(){const data=await read();for(const p of data.photos)if(!urls.has(p.name))urls.set(p.name,URL.createObjectURL(p.blob));const stamp=[data.meta.profile?.uid,data.meta.profile?.workspace_id,data.meta.profile?.device_epoch,data.meta.cursor,data.ops.map(o=>o.id).join(','),data.photos.length,data.meta.planJob?.id,data.meta.lastPlanJob,calendar().today].join('|');if(data.meta.profile&&stamp!==lastViewStamp){lastViewStamp=stamp;onChange(view(data));}onStatus({online:navigator.onLine,syncing,pending:data.ops.length,error:data.meta.error||'',disconnected:!!data.meta.disconnected,conflict:data.ops.find(x=>x.conflict)});}
  async function write(fn){return lock(':write',async()=>{const data=await read(),tx=(await db()).transaction(stores,'readwrite'),finished=done(tx);try{const value=fn(data,tx);await finished;channel?.postMessage('changed');await publish();return value;}catch(error){try{tx.abort();}catch{}await finished.catch(()=>{});throw error;}});}
  async function commit(changes,extra={},metadata={}){await write((data,tx)=>{if(!data.meta.profile)throw new Error('Connect once before saving on this device.');const records=Object.fromEntries(data.records.map(r=>[r.key,r])),parents={},current=entities(data,parents);changes=changes.filter(c=>!equal(current[c.key]??null,c.value)||(c.original!==undefined&&!sameOriginal(c.key,current[c.key]??null,c.original)));if(!changes.length){for(const [key,value] of Object.entries(metadata))tx.objectStore('meta').put({id:key,value});return;}const op={id:id(),kind:'change',order:(data.meta.order||0)+1,changes:changes.map(c=>{const {original,...change}=c,provided=original!==undefined,same=provided&&sameOriginal(c.key,current[c.key]??null,original);return {...change,base:same?(records[c.key]?.version||0):(c.base??records[c.key]?.version??0),stale:provided&&!same,depends_on:parents[c.key]||null};}),...extra};if(op.changes.some(c=>c.stale))op.conflict={message:'This record changed while its editor was open. Your local edit is retained; review before syncing.'};tx.objectStore('outbox').put(op);tx.objectStore('meta').put({id:'order',value:op.order});for(const [key,value] of Object.entries(metadata))tx.objectStore('meta').put({id:key,value});});schedule();}
  async function disconnected(){closeLive();await write((data,tx)=>{tx.objectStore('meta').put({id:'disconnected',value:true});tx.objectStore('meta').put({id:'error',value:'This device has been disconnected. Local data and pending edits are kept. Rejoin with a fresh QR code.'});});}
  async function request(path,body,options={}){
    const pairing=path.startsWith('/api/devices/'),data=await read(),profile=data.meta.profile;
    if(blockedIdentity&&path!=='/api/sync/session'&&!pairing)throw new Error('Server identity changed. Export your local data before resetting this workspace.');
    if(data.meta.disconnected&&!pairing&&path!=='/api/sync/session')throw new Error('This device has been disconnected. Local edits are kept.');
    if(!navigator.onLine&&(!LOOPBACK||profile?.hosted))throw new Error('You are offline. This feature needs a connection; your local edits are saved.');
    const headers={'X-Workspace-ID':profile?.workspace_id||'','X-Device-Epoch':profile?.device_epoch||''};
    if(body!==undefined)Object.assign(headers,{'Content-Type':'application/json','X-App-Token':profile?.token||''});
    let response;try{response=await fetch(path,{cache:'no-store',signal:options.signal,headers,...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});}
    catch(error){if(options.signal?.aborted)throw new DOMException('Dialog closed','AbortError');throw new Error('The server connection is unavailable. Local edits are saved; online features need a reachable server.');}
    let value;try{value=await response.json();}catch{throw new Error('The server is unavailable. Local changes remain pending.');}
    if(!response.ok){if(value.code==='device_disconnected')await disconnected();const e=new Error(value.error||'The server is unavailable. Local changes remain pending.');e.status=response.status;e.code=value.code;throw e;}
    if(!pairing&&path!=='/api/sync/session'&&profile){const latest=(await read()).meta.profile;if(latest?.uid!==profile.uid||latest?.workspace_id!==profile.workspace_id||latest?.device_epoch!==profile.device_epoch)throw new Error('Workspace changed while the request was in flight. Its response was not applied.');}
    return value;
  }
  const identityStamp=p=>[p?.uid,p?.workspace_id,p?.device_epoch].join('|');
  async function session(){
    const before=(await read()).meta.profile,p=await request('/api/sync/session');let applied=false,revoked=false;
    await write((data,tx)=>{
      // A pairing transaction in another tab may have replaced the local
      // workspace while this request was in flight. Ignore its stale profile.
      if(identityStamp(data.meta.profile)!==identityStamp(before))return;
      if(p.active===false){revoked=true;tx.objectStore('meta').put({id:'disconnected',value:true});tx.objectStore('meta').put({id:'error',value:'This device has been disconnected. Local data and pending edits are kept. Rejoin with a fresh QR code.'});return;}
      const old=data.meta.profile;if(old&&(old.uid!==p.uid||old.workspace_id&&old.workspace_id!==p.workspace_id||old.device_epoch&&old.device_epoch!==p.device_epoch)){blockedIdentity=true;throw new Error('Your server workspace changed. Local data and pending edits were kept. Rejoin with a fresh QR code.');}
      tx.objectStore('meta').put({id:'profile',value:p});applied=true;
    });
    if(revoked){closeLive();throw new Error('This device has been disconnected.');}
    if(applied){blockedIdentity=false;sessionAt=Date.now();}
  }
  async function merge(result,ack=null){await write((data,tx)=>{const records=tx.objectStore('records'),queue=tx.objectStore('outbox'),old=Object.fromEntries(data.records.map(r=>[r.key,r]));for(const c of result.changes||[]){if(c.version>=(old[c.key]?.version||0))records.put(c);}
    if(ack){queue.delete(ack.id);for(const next of data.ops.filter(op=>op.order>ack.order)){
      if(next.wire)continue;
      for(const c of next.changes||[]){const update=(result.changes||[]).find(r=>r.key===c.key);if(!c.stale&&c.depends_on===ack.id){if(update)c.base=update.version;c.depends_on=null;}}
      if(next.kind==='restore'&&next.base===data.meta.cursor&&result.cursor<=data.meta.cursor+(ack.kind==='photo'&&ack.staged?0:1))next.base=result.cursor;
      queue.put(next);
    }}tx.objectStore('meta').put({id:'cursor',value:Math.max(data.meta.cursor||0,result.cursor||0)});
    tx.objectStore('meta').put({id:'error',value:''});});}
  async function errorMessage(message){await write((data,tx)=>tx.objectStore('meta').put({id:'error',value:message}));}
  function schedule(delay=0){clearTimeout(timer);timer=setTimeout(sync,delay);}
  async function catchup(cursor=0,afterSubscription=false){let data=await read();if(data.meta.pairingPending){await recoverPairing();data=await read();connectLive();}if(document.hidden||!navigator.onLine||data.meta.disconnected||data.meta.pairingPending)return;if(cursor&&(data.meta.cursor||0)>=cursor)return;hintCursor=Math.max(hintCursor,cursor);if(!cursor&&(!readInFlight||afterSubscription))catchRequested=true;schedule(25);}
  function closeLive(){liveGeneration++;clearTimeout(liveTimer);live?.close();live=null;}
  function watchPairing(ticket){pairTicket=ticket;closeLive();connectLive();}
  async function connectLive(){
    if(document.hidden||!navigator.onLine||live)return;const generation=++liveGeneration;
    try{
      let data=await read();if(!data.meta.profile?.hosted)return;
      if(!pairTicket&&data.meta.pairingPending){await recoverPairing();data=await read();if(data.meta.pairingPending){liveTimer=setTimeout(connectLive,5000);return;}}
      if(!pairTicket&&data.meta.disconnected)return;
      if(!pairTicket&&Date.now()-sessionAt>30000)await session();data=await read();
      if(generation!==liveGeneration||document.hidden||!data.meta.profile.pairing)return;
      const source=new EventSource('/api/sync/events'+(pairTicket?'?pair='+encodeURIComponent(pairTicket):''));live=source;
      source.addEventListener('ready',()=>{if(live===source){catchup(0,true);if(pairTicket)window.dispatchEvent(new Event('health-device-signal'));}});
      source.addEventListener('change',event=>{if(live===source)catchup(JSON.parse(event.data).cursor);});
      source.addEventListener('devices',()=>window.dispatchEvent(new Event('health-device-signal')));
      source.addEventListener('revoked',()=>disconnected());
      source.addEventListener('workspace',()=>{closeLive();session().then(connectLive).catch(error=>errorMessage(error.message));});
      const retryLive=()=>{if(live!==source)return;closeLive();liveTimer=setTimeout(connectLive,5000);};
      source.addEventListener('unavailable',retryLive);
      source.onerror=()=>{if(source.readyState!==EventSource.CLOSED||live!==source)return;closeLive();sessionAt=0;session().then(()=>{liveTimer=setTimeout(connectLive,5000);}).catch(async error=>{await errorMessage(error.message);if(!(await read()).meta.disconnected)liveTimer=setTimeout(connectLive,5000);});};
    }catch(error){await errorMessage(error.message);if(generation===liveGeneration)liveTimer=setTimeout(connectLive,5000);}
  }
  const pairCall=(action,ticket)=>request('/api/devices/pair',{action,ticket});
  async function cancelPairing(ticket){
    // If an activation response was lost, cancel rolls it back before old edits resume.
    await lock(':sync',async()=>{const p=(await read()).meta.pairingPending;await pairCall(p?.ticket===ticket&&p.phase==='installed'?'finish':'cancel',ticket);await write((data,tx)=>{if(data.meta.pairingPending?.ticket===ticket)tx.objectStore('meta').delete('pairingPending');});});
    pairTicket=null;blockedIdentity=false;sessionAt=0;schedule();connectLive();
  }
  async function recoverPairing(){return lock(':pairing',async()=>{const p=(await read()).meta.pairingPending;if(!p||!navigator.onLine)return;
    try{if(p.phase==='installed'){await pairCall('finish',p.ticket);await write((data,tx)=>tx.objectStore('meta').delete('pairingPending'));pairTicket=null;blockedIdentity=false;sessionAt=0;}else await cancelPairing(p.ticket);}
    catch(error){await errorMessage('Pairing recovery is pending. Your local data is kept. Reconnect and reopen the app. '+error.message);}
  });}
  async function joinPairing(ticket,signal){return lock(':pairing',()=>installPairing(ticket,signal));}
  async function installPairing(ticket,signal){
    const aborted=()=>{if(signal?.aborted)throw new DOMException('Pairing canceled','AbortError');};
    closeLive();catchRequested=false;
    await write((data,tx)=>{if(data.meta.pairingPending)throw new Error('Finish the previous pairing attempt first.');tx.objectStore('meta').put({id:'pairingPending',value:{ticket,phase:'staging',order:data.meta.order||0}});});
    let installed=false;
    try{
      const snapshot=await pairCall('stage',ticket);aborted();
      if(!snapshot||!Array.isArray(snapshot.changes)||!Number.isSafeInteger(snapshot.cursor))throw new Error('The shared data could not be validated.');
      const names=new Set(snapshot.images||[]);for(const c of snapshot.changes)for(const m of JSON.stringify(c.value).matchAll(/\/media\/([a-f0-9]{64}\.png)/g))names.add(m[1]);
      const photos=[];
      for(const name of names){aborted();const response=await fetch('/api/devices/photo?ticket='+encodeURIComponent(ticket)+'&name='+encodeURIComponent(name),{cache:'no-store',signal});if(!response.ok)throw new Error('Could not download a shared photo. Existing data was kept.');const blob=await response.blob();await verifyPhoto(name,blob);photos.push({name,blob});}
      await lock(':action',()=>lock(':sync',async()=>{
        aborted();const before=await read();if(before.meta.order!==before.meta.pairingPending?.order&&(before.meta.order||0)!==before.meta.pairingPending?.order)throw new Error('Local data changed during pairing. Export it and try again.');
        const activated=await pairCall('activate',ticket);aborted();
        await write((data,tx)=>{aborted();if((data.meta.order||0)!==data.meta.pairingPending?.order)throw new Error('Local data changed. Existing data was kept.');
          for(const name of stores)tx.objectStore(name).clear();
          for(const c of snapshot.changes)tx.objectStore('records').put(c);
          for(const p of photos)tx.objectStore('photos').put(p);
          for(const [key,value] of Object.entries({profile:activated.profile,defaults:data.meta.defaults,cursor:snapshot.cursor,images:[...names],order:0,pairingPending:{ticket,phase:'installed'}}))tx.objectStore('meta').put({id:key,value});
        });installed=true;
      }));
      await pairCall('finish',ticket);await write((data,tx)=>tx.objectStore('meta').delete('pairingPending'));
      pairTicket=null;blockedIdentity=false;sessionAt=0;await publish();connectLive();schedule();
    }catch(error){if(!installed){try{await cancelPairing(ticket);}catch{await errorMessage('Pairing canceled; reconnect to finish recovery. Your original data and pending edits are kept.');}throw error;}else{await errorMessage('Shared data is ready locally. Reconnect or use Sync now to finish pairing.');}}
  }
  async function sync(){if(syncing||blockedIdentity)return;const ready=await read(),local=LOOPBACK&&!ready.meta.profile?.hosted;const wants=!document.hidden&&(catchRequested||hintCursor>(ready.meta.cursor||0));if(syncing||!ready.meta.profile||ready.meta.disconnected||ready.meta.pairingPending||(!wants&&(!ready.ops.length||ready.ops[0].conflict))||!navigator.onLine&&!local)return;syncing=true;await publish();let pulling=false;try{await lock(':sync',async()=>{
    // Another tab may have drained the shared outbox while this tab waited.
    const latest=await read(),pending=latest.ops;if(latest.meta.pairingPending||latest.meta.disconnected||(!catchRequested&&hintCursor<=(latest.meta.cursor||0)&&(!pending.length||pending[0].conflict)))return;
    if(Date.now()-sessionAt>30000)await session();
    let data=await read();if(!document.hidden&&(catchRequested||hintCursor>(data.meta.cursor||0)||!data.meta.profile.pairing)){catchRequested=false;pulling=true;readInFlight=true;await merge(await request('/api/sync/pull?since='+(data.meta.cursor||0)));pulling=false;readInFlight=false;}
    while(navigator.onLine||local){data=await read();if(data.meta.pairingPending||data.meta.disconnected)break;let op=data.ops[0];if(!op||op.conflict)break;
      if(!op.wire){await write((latest,tx)=>{const current=latest.ops.find(x=>x.id===op.id);if(!current)return;if((current.changes||[]).some(c=>c.depends_on&&!latest.ops.some(p=>p.id===c.depends_on))){current.conflict={message:'A preceding edit was discarded. Your local edit is retained; review before syncing.'};tx.objectStore('outbox').put(current);return;}current.wire=current.kind==='photo'?{id:current.id,kind:'photo',name:current.name,staged:!!current.staged,since:latest.meta.cursor||0}:current.kind==='restore'?{id:current.id,kind:'restore',base:current.base,document:current.document,since:latest.meta.cursor||0}:{id:current.id,kind:'change',since:latest.meta.cursor||0,changes:current.changes.map(change=>{const copy=clone(change);delete copy.stale;delete copy.depends_on;if(copy.key.startsWith('meals/')&&copy.value){if(copy.value.recipe?.calories)copy.value.calories=copy.value.recipe.calories;delete copy.value.recipe;}return copy;})};tx.objectStore('outbox').put(current);});op=(await read()).ops.find(x=>x.id===op.id);if(!op)continue;if(op.conflict)break;}
      let result;
      if(op.kind==='photo'){const p=data.photos.find(x=>x.name===op.name);if(!p)throw new Error('A pending image is missing. Export a backup before resetting storage.');const image=await base64(p.blob);result=await request('/api/sync/photo',{...op.wire,image});}
      else result=await request('/api/sync/push',op.wire);
      if(result.conflict){await merge(await request('/api/sync/pull?since='+((await read()).meta.cursor||0)));await write((latest,tx)=>{const current=latest.ops.find(x=>x.id===op.id);if(current){current.conflict=result;tx.objectStore('outbox').put(current);}tx.objectStore('meta').put({id:'error',value:result.message});});break;}
      if(result.id!==op.id||!Number.isSafeInteger(result.cursor)||!Array.isArray(result.changes))throw new Error('The server did not acknowledge this operation. Local changes remain pending.');
      await merge(result,op);
    }
    retry=1000;
    if(!document.hidden)await downloadPhotos();
  });}catch(error){if(error.status===403)sessionAt=0;if([400,409].includes(error.status))await write((data,tx)=>{const head=data.ops[0];if(head){head.conflict={message:error.message};tx.objectStore('outbox').put(head);}});catchRequested=false;hintCursor=0;await errorMessage(error.message);retry=Math.min(retry*2,30000);if(pulling&&!pairTicket&&ready.meta.profile.pairing&&!document.hidden&&!blockedIdentity&&error.code!=='device_disconnected'&&![400,409].includes(error.status)){closeLive();liveTimer=setTimeout(connectLive,retry);}}finally{readInFlight=false;syncing=false;await publish();const latest=await read();if(!latest.meta.disconnected&&!latest.meta.pairingPending){if(!document.hidden&&(catchRequested||hintCursor>(latest.meta.cursor||0))){schedule();}else if(latest.ops.length&&!latest.ops[0].conflict)schedule(retry);}}}
  async function downloadPhotos(){const data=await read(),names=new Set(Object.keys(entities(data)).filter(k=>k.startsWith('photos/')).map(k=>k.slice(7)));for(const match of JSON.stringify(entities(data)).matchAll(/\/media\/([a-f0-9]{64}\.png)/g))names.add(match[1]);for(const name of names){if(data.photos.some(p=>p.name===name))continue;const response=await fetch('/media/'+name);if(!response.ok)throw new Error('Some photos are not downloaded yet. Keep this page online to finish saving them for offline use.');const blob=await response.blob();await verifyPhoto(name,blob);await write((latest,tx)=>tx.objectStore('photos').put({name,blob}));}}
  async function verifyPhoto(name,blob){if(!/^[a-f0-9]{64}\.png$/.test(name)||blob.size>4000000)throw new Error('Invalid dish image.');const bytes=new Uint8Array(await blob.arrayBuffer()),hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(b=>b.toString(16).padStart(2,'0')).join('');if(hash+'.png'!==name||bytes[0]!==137||bytes[1]!==80||bytes[2]!==78)throw new Error('Dish image checksum mismatch.');await checkPNG(bytes);const image=await createImageBitmap(blob);const valid=image.width===500&&image.height===500;image.close();if(!valid)throw new Error('Dish images must be 500 by 500 pixels.');}
  async function base64(blob){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(reader.error);reader.readAsDataURL(blob);});}
  async function photo(blob){const name=[...new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer()))].map(b=>b.toString(16).padStart(2,'0')).join('')+'.png';await verifyPhoto(name,blob);await write((data,tx)=>{tx.objectStore('photos').put({name,blob});if(!entities(data)['photos/'+name]){const order=(data.meta.order||0)+1;tx.objectStore('outbox').put({id:id(),order,kind:'photo',name});tx.objectStore('meta').put({id:'order',value:order});}});schedule();return {photo_url:'/media/'+name};}
  function snapshot(recipe,foods){const r=clone(recipe);delete r.source_text;delete r.legacy_groups;delete r.__version;r.calories=calories(r,foods);return r;}
  const positive=value=>typeof value==='number'&&Number.isFinite(value)&&value>0;
  const validHousehold=value=>Number.isSafeInteger(value)&&value>0;
  const decimalQuantity=new Intl.NumberFormat('en-US',{minimumFractionDigits:1,maximumFractionDigits:1,useGrouping:false});
  const displayAmount=value=>decimalQuantity.format(value);
  function scaleQuantity(text,factor,display=false){
    if(typeof text!=='string'||factor===1&&!display||/to taste|as needed|as required|vừa (?:ăn|đủ)|tùy (?:ý|khẩu vị)/iu.test(text))return text;
    const fractions={'¼':.25,'½':.5,'¾':.75,'⅓':1/3,'⅔':2/3,'⅛':.125,'⅜':.375,'⅝':.625,'⅞':.875};
    const pattern=/(?:\d+\s+)?\d+\s*\/\s*\d+|\d+\s*[¼½¾⅓⅔⅛⅜⅝⅞]|\d+(?:[.,]\d+)*|[¼½¾⅓⅔⅛⅜⅝⅞]/gu;
    const leading=/^(?:\s|~|≈|about\b|approximately\b|khoảng\b)*/iu.exec(text)[0].length;
    const first=[...text.matchAll(pattern)][0];if(!first||first.index!==leading)return text;
    function scale(raw){
      let value;
      if(raw.includes('/')){const m=/^(?:(\d+)\s+)?(\d+)\s*\/\s*(\d+)$/.exec(raw);if(!m||!Number(m[3]))return raw;value=Number(m[1]||0)+Number(m[2])/Number(m[3]);}
      else if(fractions[raw.at(-1)]!==undefined)value=Number(raw.slice(0,-1).trim()||0)+fractions[raw.at(-1)];
      else value=Number(/^\d{1,3}(?:,\d{3})+$/.test(raw)?raw.replaceAll(',',''):raw.replace(',','.'));
      return Number.isFinite(value)?(display?displayAmount(value*factor):String(value*factor)):raw;
    }
    // In "2 x 400 g" or "2 cans (400 g each)", scale the count once.
    const rest=text.slice(first.index+first[0].length);
    if(/^\s*(?:[x×]|(?:cans?|packs?|packages?|tins?|hộp|gói)\b).*\d/iu.test(rest))return text.slice(0,first.index)+scale(first[0])+rest;
    return text.replace(pattern,(raw,offset)=>text[offset+raw.length]==='%'?raw:scale(raw));
  }
  function makeServingPlan(recipe,foods,household){
    const servings=validHousehold(household)?household:null,recipe_servings=positive(recipe.servings)?recipe.servings:null;
    const factor=servings&&recipe_servings?servings/recipe_servings:1,c=calories(recipe,foods);
    const ingredients=clone(recipe.ingredients||[]).map(item=>{
      if(factor!==1){if(typeof item.quantity==='string')item.quantity=scaleQuantity(item.quantity,factor);if(typeof item.grams==='number'&&Number.isFinite(item.grams))item.grams=item.grams*factor;}return item;
    });
    return {servings,recipe_servings,ingredients,calories:{...c,total:c.total*factor}};
  }
  function plannedRecipe(recipe,plan){return plan?{...recipe,ingredients:clone(plan.ingredients),servings:plan.servings&&plan.recipe_servings?plan.servings:plan.recipe_servings,calories:clone(plan.calories)}:recipe;}
  function validateServingPlan(plan){
    if(!plan||typeof plan!=='object'||Array.isArray(plan)||!('servings' in plan)||!('recipe_servings' in plan)||plan.servings!==null&&!validHousehold(plan.servings)||plan.recipe_servings!==null&&!positive(plan.recipe_servings)||!Array.isArray(plan.ingredients)||plan.ingredients.length>200)throw new Error('Invalid planned serving quantities.');
    for(const i of plan.ingredients)if(!i||typeof i.name!=='string'||!i.name.trim()||i.quantity!==undefined&&typeof i.quantity!=='string'||i.grams!=null&&(typeof i.grams!=='number'||!Number.isFinite(i.grams)||i.grams<0))throw new Error('Invalid planned ingredient.');
    const c=plan.calories;if(!c||typeof c.complete!=='boolean'||!Array.isArray(c.missing)||['total','per_serving'].some(key=>c[key]!==null&&(typeof c[key]!=='number'||!Number.isFinite(c[key])||c[key]<0)))throw new Error('Invalid planned calorie values.');
  }
  function normal(value){return String(value||'').toLowerCase().replace(/đ/g,'d').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/[^\x00-\x7f]/g,'').trim().replace(/\s+/g,' ');}
  function findFood(item,foods){return item.food_id?foods.find(f=>f.id===item.food_id):foods.find(f=>[f.name,f.vi_name,f.id,...(f.aliases||[])].some(v=>normal(v)===normal(item.name)));}
  function calories(recipe,foods){let total=0;const missing=[];for(const i of recipe.ingredients||[]){const f=findFood(i,foods);if(i.grams==null||!f)missing.push(i.name);else total+=i.grams*f.kcal/100;}return {total,per_serving:recipe.servings>0?total/recipe.servings:null,complete:!missing.length&&!!recipe.ingredients?.length,missing};}
  function date(start,n){const d=new Date(start+'T12:00:00');d.setDate(d.getDate()+n);return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;}
  function monday(week){if(!/^\d{4}-\d{2}-\d{2}$/.test(week)||new Date(week+'T12:00:00').getDay()!==1)throw new Error('Choose a week starting on Monday.');}
  function validateRecipeInput(r){
    r.name=String(r.name||'').trim().slice(0,200);if(!r.name)throw new Error('Enter a dish name.');
    r.source_url=String(r.source_url||'').trim().slice(0,2000);if(r.source_url){let url;try{url=new URL(r.source_url);}catch{throw new Error('Source must be an http or https link.');}if(!['http:','https:'].includes(url.protocol)||!url.hostname)throw new Error('Source must be an http or https link.');}
    for(const key of ['servings','measured_minutes','estimated_minutes']){const v=r[key];if(v!=null&&(!Number.isFinite(v)||v<(key==='servings'?.01:1)||v>(key==='servings'?10000:10080)))throw new Error('Check servings and cooking minutes; the value is outside the supported range.');}
    if(![true,false,null,undefined].includes(r.vegan)||r.good_for_running!==undefined&&typeof r.good_for_running!=='boolean')throw new Error('Check vegan status and Good for Running.');
    r.source_text=String(r.source_text||'').trim().slice(0,30000);r.notes=String(r.notes||'').trim().slice(0,5000);
    if(!Array.isArray(r.ingredients)||r.ingredients.length>200)throw new Error('Use at most 200 ingredients per recipe.');
    for(const i of r.ingredients){i.name=String(i.name||'').trim().slice(0,180);if(!i.name||i.grams!=null&&(!Number.isFinite(i.grams)||i.grams<0||i.grams>1000000))throw new Error('Check ingredient names and weights.');i.quantity=String(i.quantity||'').slice(0,150);i.source_quote=String(i.source_quote||'').slice(0,1500);}
    if(r.photo_url&&!/^\/media\/[a-f0-9]{64}\.png$/.test(r.photo_url))throw new Error('Upload a valid dish photo first.');
  }
  function practical(recipe,foods,household){const minutes=recipe.measured_minutes||recipe.estimated_minutes;return {recipe_id:recipe.id,recipe:snapshot(recipe,foods),serving_plan:makeServingPlan(recipe,foods,household),timing:minutes>25?'night before':'morning',prep:'Review the original source and weigh the listed ingredients ahead of time.',reason:'Selected from your saved dishes.'};}
  async function mutate(path,payload){let result;await lock(':action',async()=>{
    const data=await read(),s=view(data),map=entities(data),changes=[],put=(key,value)=>changes.push({key,value});
    if(path==='/api/plan/result'&&payload.job_id&&(data.meta.completedPlanJobs||[]).includes(payload.job_id)){result={ok:true};return;}
    if(path==='/api/recipes'){const r=clone(payload),base=r.__version,original=r.__original;delete r.__version;delete r.__original;r.id=r.id||id();validateRecipeInput(r);r.calories=calories(r,s.foods);put('recipes/'+r.id,r);if(base!==undefined)changes.at(-1).base=base;if(original!==undefined)changes.at(-1).original=original;result={recipe:r,calories:r.calories,warnings:r.calories.complete?[]:['Unknown calorie values can be entered manually or looked up when online.'],foods:s.foods};}
    else if(path==='/api/recipes/delete'){put('recipes/'+payload.id,null);result={ok:true};}
    else if(path==='/api/recipes/running'){const r=map['recipes/'+payload.id];if(!r)throw new Error('Dish not found.');put('recipes/'+payload.id,{...r,good_for_running:payload.good_for_running});result={recipe:{...r,good_for_running:payload.good_for_running}};}
    else if(path==='/api/foods'){const old=payload.id?s.foods.find(f=>f.id===payload.id):findFood({name:payload.name},s.foods);const food={...old,...payload,id:old?.id||payload.id||normal(payload.name),origin:payload.origin||'manual'};if(!food.name?.trim()||!Number.isFinite(food.kcal)||food.kcal<0||food.kcal>1000)throw new Error('Enter a food name and calories between 0 and 1000.');delete food.base;delete food.original;put('foods/'+food.id,food);result=food;}
    else if(path==='/api/settings'){const settings={...(map.settings||{}),...payload,language:'auto'};if(settings.household_servings!==undefined&&settings.household_servings!==null&&!validHousehold(settings.household_servings))throw new Error('Default household servings must be a positive whole number, or left blank.');delete settings.base;delete settings.original;delete settings.vision_model;if(!payload.usda_api_key){if(map.settings?.usda_api_key)settings.usda_api_key=map.settings.usda_api_key;else delete settings.usda_api_key;}put('settings',settings);result={ok:true};}
    else if(path==='/api/plan/expansion'){monday(payload.week_start);put('expansion/'+payload.week_start,payload.expanded);result={ok:true};}
    else if(path==='/api/plan/delete'){put('plans/'+payload.week_start,null);result={ok:true};}
    else if(path==='/api/plan/add'){const week=payload.week_start;monday(week);if(week<=s.calendar.week_start||week>s.calendar.advance_limit)throw new Error('Choose one of the next eight weeks.');if(map['plans/'+week])throw new Error('This week already exists.');const plan={week_start:week,method:'manual',generated_on:s.calendar.today,vegan_only:false,prioritize_running:false,warnings:[]};put('plans/'+week,plan);const days=Array.from({length:7},(_,i)=>({date:date(week,i),recipe_id:null}));for(const day of days)put('meals/'+week+'/'+day.date,day);result={plan:{...plan,days}};}
    else if(path==='/api/plan/replace'){const week=payload.week_start,key='meals/'+week+'/'+payload.date;if(!map['plans/'+week]||!map[key])throw new Error('This meal slot no longer exists.');const recipe=s.recipes.find(r=>r.id===payload.recipe_id);if(!recipe)throw new Error('Select a saved dish.');const day={date:payload.date,...practical(recipe,s.foods,s.settings.household_servings)};put(key,day);result={ok:true};}
    else if(path==='/api/plan/generate'||path==='/api/plan/result'){
      if(path==='/api/plan/result'&&payload.originals&&Object.entries(payload.originals).some(([key,value])=>!equal(map[key]??null,value)))throw new Error('This week changed while AI was running. Your saved plan was kept; generate again.');
      const week=payload.week_start||s.calendar.week_start;monday(week);if(week<s.calendar.week_start||week>s.calendar.advance_limit)throw new Error('Generate only for the current week or the next eight weeks.');if(week>s.calendar.week_start&&!map['plans/'+week])throw new Error('Add this future week first.');let recipes=s.recipes.filter(r=>!payload.vegan_only||r.vegan===true);if(!recipes.length)throw new Error('Save at least one matching dish before generating.');
      const shuffle=items=>{const a=[...items];for(let i=a.length-1;i>0;i--){const j=crypto.getRandomValues(new Uint32Array(1))[0]%(i+1);[a[i],a[j]]=[a[j],a[i]];}return a;};if(payload.method==='random')recipes=shuffle(recipes);if(payload.prioritize_running)recipes=[...recipes.filter(r=>r.good_for_running),...recipes.filter(r=>!r.good_for_running)];const dates=Array.from({length:7},(_,i)=>date(week,i)).filter(d=>week>s.calendar.week_start||d>=s.calendar.today);
      const plan={...(map['plans/'+week]||{}),week_start:week,generated_on:s.calendar.today,method:payload.method||'AI',vegan_only:!!payload.vegan_only,prioritize_running:!!payload.prioritize_running,warnings:payload.prioritize_running&&!recipes.some(r=>r.good_for_running)?['No matching saved dishes are marked Good for Running. Other saved dishes will be used.']:[]};put('plans/'+week,plan);
      dates.forEach((day,i)=>{const suggestion=payload.days?.find(d=>d.date===day),recipe=suggestion?s.recipes.find(r=>r.id===suggestion.recipe_id):recipes[i%recipes.length];if(!recipe||payload.vegan_only&&recipe.vegan!==true)throw new Error('A suggested dish changed or was deleted; your saved plan was kept.');put('meals/'+week+'/'+day,{...practical(recipe,s.foods,s.settings.household_servings),...suggestion,date:day,recipe:snapshot(recipe,s.foods)});});result={ok:true};
    }else throw new Error('Unknown local action.');
    if(payload.base!==undefined&&changes.length===1)changes[0].base=payload.base;if(payload.original!==undefined&&changes.length===1)changes[0].original=payload.original;
    await commit(changes,{},path==='/api/plan/result'?{planJob:data.meta.planJob?.id===payload.job_id?null:data.meta.planJob||null,lastPlanJob:payload.job_id||null,completedPlanJobs:[...(data.meta.completedPlanJobs||[]),payload.job_id].filter(Boolean).slice(-100)}:{});
  });return result;}
  async function init(changed,status){onChange=changed;onStatus=status;await db();let data=await read();if(!data.meta.profile){const initial=await request('/api/sync/bootstrap');await write((current,tx)=>{for(const c of initial.changes)tx.objectStore('records').put(c);for(const [key,value] of Object.entries({profile:initial.profile,cursor:initial.cursor,defaults:initial.defaults,images:initial.images||[]}))tx.objectStore('meta').put({id:key,value});});}await publish();navigator.storage?.persist?.().catch(()=>{});if('serviceWorker' in navigator){await navigator.serviceWorker.register('/sw.js');await navigator.serviceWorker.ready;}downloadPhotos().catch(error=>errorMessage(error.message));schedule();await recoverPairing();connectLive();}
  async function resolveConflict(keep){await write((data,tx)=>{
    const op=data.ops[0];if(!op?.conflict)return;
    const queue=tx.objectStore('outbox');
    if(!keep)queue.delete(op.id);
    else{
      const versions=Object.fromEntries(data.records.map(r=>[r.key,r]));
      const copies=new Map();
      for(const c of op.changes||[])if(c.key.startsWith('recipes/')&&c.value&&versions[c.key]?.value===null)copies.set(c.key.slice(8),id());
      const previousId=op.id,retryId=id();queue.clear();
      for(const next of data.ops){
        if(next.id===previousId){next.id=retryId;delete next.wire;delete next.conflict;if(next.kind==='restore')next.base=data.meta.cursor;for(const c of next.changes||[]){c.base=versions[c.key]?.version||0;c.depends_on=null;c.stale=false;}}
        for(const c of next.changes||[]){
          if(c.depends_on===previousId)c.depends_on=retryId;
          if(c.key.startsWith('recipes/')&&copies.has(c.key.slice(8))){c.key='recipes/'+copies.get(c.key.slice(8));c.base=0;if(c.value)c.value.id=c.key.slice(8);delete next.wire;}
          if(c.value?.recipe_id&&copies.has(c.value.recipe_id)){c.value.recipe_id=copies.get(c.value.recipe_id);if(c.value.recipe)c.value.recipe.id=c.value.recipe_id;delete next.wire;}
        }
        queue.put(next);
      }
    }
    tx.objectStore('meta').put({id:'error',value:''});
  });schedule();}
  channel?.addEventListener('message',()=>{publish();schedule();});window.addEventListener('online',()=>{sessionAt=0;recoverPairing().then(()=>{schedule();publish();connectLive();});});window.addEventListener('offline',()=>{closeLive();publish();});document.addEventListener('visibilitychange',()=>{if(document.hidden){catchRequested=false;closeLive();}else connectLive();});
  return {sameContent:equal,init,read,view,entities,catchup,joinPairing,cancelPairing,connectLive,watchPairing,mutate,commit,request,sync,schedule,photo,verifyPhoto,base64,calendar,validHousehold,displayAmount,scaleQuantity,makeServingPlan,plannedRecipe,validateServingPlan,resolveConflict,write,id,photoURL:url=>urls.get(String(url).slice(7))||url,downloadPhotos};
})();
