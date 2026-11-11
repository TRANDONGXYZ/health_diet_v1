// Week confirmation, durable acknowledgement and stale-refresh regression checks.
const {chromium}=require('playwright'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const folder=fs.mkdtempSync(path.join(os.tmpdir(),'meal-week-delete-'));
const origin='http://127.0.0.1:8775';let browser;
const server=spawn(process.env.PYTHON||'python',['tests/browser_server.py'],{cwd:path.resolve(__dirname,'..'),env:{...process.env,MEAL_APP_DATA:folder,MEAL_TEST_PORT:'8775'},stdio:'pipe',windowsHide:true});
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function run(){
  for(let i=0;i<80;i++){try{if((await fetch(origin)).ok)break;}catch{}await wait(100);}
  browser=await chromium.launch({channel:'msedge',headless:true});const page=await browser.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(origin);await page.waitForFunction(()=>state.calendar);
  const weeks=await page.evaluate(async()=>{const values=availableWeeks().slice(0,2);for(const week of values)await api('/api/plan/add',{week_start:week});await refresh();return values;});
  const entry=week=>page.locator(`.future-week[data-week="${week}"]`);
  let answer=false,dialogCount=0,deletes=0;
  page.on('dialog',async dialog=>{dialogCount++;assert.equal(dialog.type(),'confirm');assert.match(dialog.message(),/saved dishes will be kept/);await dialog[answer?'accept':'dismiss']();});
  page.on('request',request=>{if(request.url().endsWith('/api/plan/delete'))deletes++;});
  const before=await page.evaluate(()=>structuredClone(state.plans));
  await entry(weeks[0]).getByRole('button',{name:/Delete week/}).click();
  assert.equal(dialogCount,1);assert.equal(deletes,0);assert.deepEqual(await page.evaluate(()=>state.plans),before);
  answer=true;
  await page.route('**/api/plan/delete',route=>route.fulfill({status:503,json:{error:'Test: durable storage unavailable'}}));
  await entry(weeks[0]).getByRole('button',{name:/Delete week/}).click();
  await page.locator('#notification').filter({hasText:'durable storage unavailable'}).waitFor();
  assert.equal(await entry(weeks[0]).count(),1);assert.deepEqual(await page.evaluate(()=>state.plans),before);
  await page.unroute('**/api/plan/delete');
  // A state request started before Delete must not put the deleted week back.
  let releaseStale,receivedStale;const staleHeld=new Promise(resolve=>receivedStale=resolve),staleGate=new Promise(resolve=>releaseStale=resolve);
  await page.route('**/api/state*',async route=>{const response=await route.fetch();receivedStale();await staleGate;await route.fulfill({response});});
  await page.evaluate(()=>{window.__staleRefresh=refresh();});await staleHeld;
  let releaseDelete,receivedDelete;const deleteHeld=new Promise(resolve=>receivedDelete=resolve),deleteGate=new Promise(resolve=>releaseDelete=resolve);
  await page.route('**/api/plan/delete',async route=>{receivedDelete();await deleteGate;await route.continue();});
  const click=entry(weeks[0]).getByRole('button',{name:/Delete week/}).click();await deleteHeld;
  assert.equal(await entry(weeks[0]).count(),1,'Keep week visible until durable success');
  const stateRequests=[];page.on('request',request=>{if(request.url().includes('/api/state'))stateRequests.push(request.url());});
  releaseDelete();await click;await entry(weeks[0]).waitFor({state:'detached'});
  assert.equal(await entry(weeks[1]).count(),1);assert.equal(stateRequests.length,0,'No full-state reload on successful Delete');
  releaseStale();await page.evaluate(()=>window.__staleRefresh);
  assert.equal(await entry(weeks[0]).count(),0,'Ignore stale state response after acknowledgement');
  await page.unroute('**/api/state*');await page.reload();await page.waitForFunction(()=>state.calendar);
  assert.equal(await entry(weeks[0]).count(),0);assert.equal(await entry(weeks[1]).count(),1);
  assert.deepEqual(errors,[]);
  console.log('PASS: immediate confirmation; cancel/dismiss sends no delete; failed/pending writes keep the week visible; one successful delete removes only its target without a state reload; stale responses cannot resurrect it; reload verifies persistence.');
}
run().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await browser?.close();server.kill();await new Promise(resolve=>server.once('exit',resolve));assert.ok(path.resolve(folder).startsWith(path.join(os.tmpdir(),'meal-week-delete-')));fs.rmSync(folder,{recursive:true,force:true});});
