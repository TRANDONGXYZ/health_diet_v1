// Real browser checks; --url targets a live service using a fresh private workspace.
const {chromium}=require('playwright'),assert=require('node:assert/strict'),{spawn}=require('node:child_process'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const supplied=process.argv.indexOf('--url'),live=supplied>=0,origin=live?process.argv[supplied+1].replace(/\/$/,''):'http://127.0.0.1:8788';
const folder=live?null:fs.mkdtempSync(path.join(os.tmpdir(),'meal-dialog-sync-'));
const server=live?null:spawn(process.env.PYTHON||'python',['tests/browser_server.py'],{env:{...process.env,MEAL_APP_DATA:folder,MEAL_TEST_PORT:'8788'},windowsHide:true,stdio:'pipe'});
let browser,page;const wait=ms=>new Promise(r=>setTimeout(r,ms)),requests=[],errors=[];
async function drained(){for(let n=0;n<1200;n++){const result=await page.evaluate(async()=>({pending:(await HealthLocal.read()).ops.length,syncing:document.querySelector('#connection-status').textContent.includes('Syncing')}));if(!result.pending&&!result.syncing)return;await wait(100);}throw new Error('Sync did not drain: '+await page.locator('#connection-status').getAttribute('title'));}
async function create(){await page.getByRole('tab',{name:/Dish List/}).click();await page.getByRole('button',{name:'+ Create recipe',exact:true}).click();}
async function close(mode){if(mode==='Escape')await page.keyboard.press('Escape');else await page.locator('#recipe-modal').getByRole('button',{name:mode,exact:true}).click();await page.locator('#recipe-modal').waitFor({state:'hidden'});}
async function run(){
  if(!live)for(let n=0;n<100;n++){try{if((await fetch(origin)).ok)break;}catch{}await wait(100);}
  browser=await chromium.launch({channel:'msedge',headless:true});const context=await browser.newContext({viewport:{width:1280,height:900}});page=await context.newPage();
  page.on('pageerror',e=>errors.push(e.message));await page.goto(origin);await page.waitForFunction(()=>state.calendar);await page.evaluate(()=>navigator.serviceWorker.ready);await drained();
  // Wait beyond the former polling interval. Exercise actual browser tab focus too.
  page.on('request',r=>{if(new URL(r.url()).pathname.startsWith('/api/sync/'))requests.push({path:new URL(r.url()).pathname,body:r.postDataJSON()});});
  for(let n=0;n<3;n++){await page.getByRole('tab',{name:/Dish List/}).click();await page.mouse.wheel(0,600);await page.getByRole('tab',{name:'Weekly Meals',exact:true}).click();}
  const other=await context.newPage();await other.goto('about:blank');await other.bringToFront();await page.bringToFront();await other.close();
  const idle=process.env.MEAL_TEST_IDLE_MS?Number(process.env.MEAL_TEST_IDLE_MS):35000;await wait(idle);assert.deepEqual(requests,[],'Empty outbox must produce no session/pull/push requests on idle, scroll, tabs or focus');
  console.log(`PASS Network: ${idle/1000} seconds idle, scrolling, navigation and browser focus: 0 sync requests.`);
  await create();const dialog=page.locator('#recipe-modal');
  for(const kind of ['auto','video']){await page.locator('#source-kind').selectOption(kind);assert.equal(await dialog.getByRole('button',{name:'Retrieve source',exact:true}).isDisabled(),true);}
  let retrievals=0;await page.route('**/api/online/extract',async r=>{retrievals++;await r.fulfill({status:500,json:{error:'Unexpected retrieval'}});});
  await page.evaluate(()=>document.querySelector('[data-action=retrieve]').click());assert.equal(retrievals,0);
  await page.locator('[name=source_url]').focus();await page.keyboard.press('Tab');await page.keyboard.press('Tab');assert.notEqual(await page.evaluate(()=>document.activeElement.dataset.action),'retrieve');
  // Header geometry stays fixed while only the form scrolls.
  await page.evaluate(()=>{draft.ingredients=Array.from({length:45},()=>({name:'tofu',grams:100,quantity:'100 g'}));renderIngredients();});
  const top=await dialog.locator('.modal-head').boundingBox();await page.locator('#recipe-form').evaluate(e=>e.scrollTop=e.scrollHeight);const after=await dialog.locator('.modal-head').boundingBox();assert.deepEqual(after,top);assert.ok(await dialog.evaluate(e=>e.scrollHeight<=e.clientHeight+1));assert.equal(await page.locator('#save-recipe').isVisible(),true);assert.equal(await dialog.locator('.modal-actions').count(),0);
  // Each dismissal method clears all work errors. No real AI invocation is needed.
  for(const mode of ['Cancel','Close recipe editor','Escape']){
    await page.evaluate(()=>work('Test error',async()=>{throw new Error('Old dialog failure');},{pollAI:false}));assert.match(await dialog.innerText(),/Old dialog failure/);
    await close(mode);await create();assert.doesNotMatch(await dialog.innerText(),/Old dialog failure/);assert.equal(await dialog.locator('[data-dialog-feedback]').count(),0);
  }
  const transcript='Tofu, 100 g. Cook for five minutes.';await page.locator('#transcript-upload').setInputFiles({name:'recipe.txt',mimeType:'text/plain',buffer:Buffer.from(transcript)});await page.waitForFunction(text=>document.querySelector('[name=source_text]').value===text,transcript);
  await close('Cancel');
  for(const id of ['foods-modal','settings-modal','add-week-modal','replace-modal']){
    await page.evaluate(async id=>{open('#'+id);await work('Test dialog feedback',async()=>{throw new Error('Dismissed feedback');},{pollAI:false});},id);
    await page.keyboard.press('Escape');await page.locator('#'+id).waitFor({state:'hidden'});await page.evaluate(id=>open('#'+id),id);assert.doesNotMatch(await page.locator('#'+id).innerText(),/Dismissed feedback/);await page.keyboard.press('Escape');await page.locator('#'+id).waitFor({state:'hidden'});
  }
  await create();
  let release,received;const arrived=new Promise(r=>received=r),held=new Promise(r=>release=r);
  await page.route('**/api/online/import',async route=>{received();await held;await route.fulfill({status:503,json:{error:'Late failure from closed editor'}}).catch(()=>{});});
  await page.locator('[name=source_text]').fill('Tofu, 100 g. Cook for five minutes.');await page.getByRole('button',{name:'Extract recipe with AI',exact:true}).click();await arrived;
  await close('Escape');await create();release();await wait(600);assert.doesNotMatch(await dialog.innerText(),/Late failure|Old dialog failure/);assert.equal(await page.locator('#save-recipe').isEnabled(),true);
  console.log('PASS disabled retrieval, fixed recipe header, Cancel/close/Escape error cleanup and late-request isolation.');
  // Offline manual save commits locally; reconnect sends only its queued record.
  await context.setOffline(true);await dialog.locator('[name=name]').fill('Offline dialog sync test');const before=Date.now();await page.locator('#save-recipe').click();await dialog.waitFor({state:'hidden'});assert.ok(Date.now()-before<1800,'Manual save must finish locally');
  const pending=await page.evaluate(async()=>{const d=await HealthLocal.read();return d.ops.map(o=>({id:o.id,changes:o.changes}));});assert.equal(pending.length,1);assert.equal(pending[0].changes.length,1);assert.match(pending[0].changes[0].key,/^recipes\//);
  await page.reload();await page.waitForFunction(()=>state.recipes.some(r=>r.name==='Offline dialog sync test'));assert.ok(await page.evaluate(async()=>(await HealthLocal.read()).ops.length>0));
  const pushes=[];await page.route('**/api/sync/push',async route=>{pushes.push(route.request().postDataJSON());await wait(3000);await route.continue();});
  await context.setOffline(false);await drained();assert.equal(pushes.length,1);assert.equal(pushes[0].id,pending[0].id);assert.equal(pushes[0].changes.length,1);assert.equal(pushes[0].changes[0].key,pending[0].changes[0].key);
  const recipeId=await page.evaluate(()=>state.recipes.find(r=>r.name==='Offline dialog sync test').id);await page.evaluate(id=>showDetails(id),recipeId);
  const details=page.locator('#detail-modal');const detailTop=await details.locator('.modal-head').boundingBox();await page.locator('#dish-detail').evaluate(e=>{e.insertAdjacentHTML('beforeend','<p>'+('Long recipe. '.repeat(2000))+'</p>');e.scrollTop=e.scrollHeight;});assert.deepEqual(await details.locator('.modal-head').boundingBox(),detailTop);assert.equal(await details.locator('#detail-edit').isVisible(),true);assert.equal(await details.locator('#dish-detail [data-action=edit]').count(),0);await page.locator('#detail-edit').click();assert.equal(await page.locator('#recipe-title').innerText(),'Edit recipe');
  await page.locator('#save-recipe').click();await dialog.waitFor({state:'hidden'});await drained();assert.equal(pushes.length,1,'Saving an unchanged recipe must not upload a no-op');
  const count=requests.length;await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await page.evaluate(()=>HealthLocal.sync());await wait(1500);assert.equal(requests.length,count);
  const fresh=await browser.newContext({storageState:await context.storageState()});const check=await fresh.newPage();await check.goto(origin);try{await check.waitForFunction(id=>state.recipes.some(r=>r.id===id),recipeId);}catch(error){console.error('Fresh context diagnostic:',await check.evaluate(()=>({recipes:state.recipes.map(r=>r.id),notification:document.querySelector('#notification').textContent,profile:!!state.calendar})));throw error;}await fresh.close();
  await page.setViewportSize({width:390,height:844});await create();assert.ok(await dialog.evaluate(e=>e.scrollHeight<=e.clientHeight+1));assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));assert.equal(await page.locator('#save-recipe').isVisible(),true);await close('Cancel');
  assert.deepEqual(errors,[]);console.log('PASS offline save/reload, automatic reconnect with a 3-second delayed ACK, exactly one recipe-only upload, no-op save, durable fresh-browser reload, fixed Details header and mobile controls.');
}
run().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await browser?.close();if(server){server.kill();await new Promise(r=>server.once('exit',r));}if(folder){assert.ok(path.resolve(folder).startsWith(path.join(os.tmpdir(),'meal-dialog-sync-')));fs.rmSync(folder,{recursive:true,force:true});}});
