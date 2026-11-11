// Isolated real HTTP/storage test for confirmed deletion across weekly history.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const folder=fs.mkdtempSync(path.join(os.tmpdir(),'meal-deletion-'));
const origin='http://127.0.0.1:8771';let browser,serverErrors='';
const server=spawn(process.env.PYTHON||'python',['tests/browser_server.py'],{cwd:path.resolve(__dirname,'..'),env:{...process.env,MEAL_APP_DATA:folder,MEAL_TEST_PORT:'8771',MEAL_TEST_TODAY:'2026-11-11'},stdio:'pipe',windowsHide:true});
server.stderr.on('data',data=>serverErrors+=data);
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function run(){
  for(let i=0;i<80;i++){try{if((await fetch(origin)).ok)break;}catch{}await wait(150);}
  browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const state=async()=>await(await fetch(origin+'/api/state')).json(),token=(await state()).token;
  const api=async(route,data)=>{const response=await fetch(origin+route,{method:'POST',headers:{'Content-Type':'application/json','X-App-Token':token},body:JSON.stringify(data)});const body=await response.json();assert.equal(response.status,200,JSON.stringify(body));return body;};
  await page.goto(origin);await page.locator('.past-week').first().waitFor();
  const image=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=500;c.height=500;c.getContext('2d').fillRect(0,0,500,500);return c.toDataURL('image/png').split(',')[1];});
  const photo=(await api('/api/dish-photo',{filename:'dish.png',image})).photo_url;
  const target={id:'target',name:'Delete this soup',servings:2,measured_minutes:25,good_for_running:true,photo_url:photo,notes:'Private source details',ingredients:[{name:'tofu',grams:200}]};
  const other={...target,id:'keep',name:'Keep this soup'};
  await api('/api/recipes',target);await api('/api/recipes',other);
  await api('/test/date',{date:'2026-11-02'});
  await api('/api/plan/generate',{method:'rotation'});
  await api('/api/plan/add',{week_start:'2026-11-09'});
  await api('/api/plan/replace',{week_start:'2026-11-09',date:'2026-11-09',recipe_id:'target'});
  await api('/test/date',{date:'2026-11-11'});
  await api('/api/plan/generate',{method:'rotation'});
  await api('/api/plan/add',{week_start:'2026-11-16'});
  await api('/api/plan/generate',{week_start:'2026-11-16',method:'rotation'});
  await api('/api/plan/add',{week_start:'2026-11-23'});
  await page.reload();await page.locator('#week-grid .meal-card').first().waitFor();
  await page.getByRole('tab',{name:/Dish List/}).click();
  const dish=page.locator('.dish-card').filter({hasText:'Delete this soup'}),before=await state();
  let deleteRequests=0;page.on('request',request=>{if(request.url().endsWith('/api/recipes/delete'))deleteRequests++;});
  // Both Cancel and dismiss use the browser confirmation's non-accept path.
  for(let i=0;i<2;i++){
    const notice=page.waitForEvent('dialog'),action=dish.getByRole('button',{name:'Delete',exact:true}).click(),dialog=await notice;
    assert.equal(dialog.type(),'confirm');assert.match(dialog.message(),/Dish deleted/);assert.match(dialog.message(),/Weekly Meals/);
    assert.match(dialog.message(),/past, current, and upcoming/);
    assert.equal(deleteRequests,0);assert.deepEqual(await state(),before);
    await dialog.dismiss();await action;
    assert.deepEqual(await state(),before);assert.equal(await dish.count(),1);assert.equal(deleteRequests,0);
  }
  const notice=page.waitForEvent('dialog'),action=dish.getByRole('button',{name:'Delete',exact:true}).click(),dialog=await notice;
  assert.equal(deleteRequests,0);await dialog.accept();await action;await dish.waitFor({state:'hidden'});
  assert.equal(deleteRequests,1);
  const after=await state(),expected=structuredClone(before.plans);
  for(const plan of expected)for(let i=0;i<plan.days.length;i++)if(plan.days[i].recipe_id==='target')plan.days[i]={date:plan.days[i].date,recipe_id:'target',deleted:true};
  assert.deepEqual(after.plans,expected);assert.deepEqual(after.recipes,before.recipes.filter(r=>r.id!=='target'));
  assert.equal((await page.request.get(origin+photo)).status(),200,'Other dishes can still use the photo');
  await page.getByRole('tab',{name:'Weekly Meals'}).click();
  const deleted=expected.flatMap(plan=>plan.days.map((day,index)=>({week:plan.week_start,day,index}))).filter(item=>item.day.deleted);
  assert.ok(deleted.length>7,'Check repeated occurrences across all three week categories');
  const card=item=>page.locator(`.meal-card[data-week="${item.week}"][data-day="${item.index}"]`);
  async function checkCards(){
    for(const item of deleted){
      const entry=card(item);assert.equal(await entry.count(),1);assert.equal(await entry.locator('strong').innerText(),'Dish deleted');
      assert.equal(await entry.locator('.meal-image').innerText(),'Dish deleted');assert.equal(await entry.locator('img,.meal-time,.meal-calories,.pill').count(),0);
      assert.ok(await entry.locator('.meal-date').innerText());assert.equal(await entry.getByRole('button',{name:/Edit meal/}).count(),1);
    }
  }
  await checkCards();
  for(const week of ['2026-11-02','2026-11-09','2026-11-16']){
    await card(deleted.find(item=>item.week===week)).locator('.meal-open').click();
    const details=page.locator('#dish-detail');assert.equal(await details.locator('h2').innerText(),'Dish deleted');
    assert.doesNotMatch(await details.innerText(),/Delete this soup|Private source details|Ingredients|kcal|preparation/i);
    assert.equal(await details.locator('img').count(),0);await page.getByRole('button',{name:'Close recipe details'}).click();
  }
  await page.reload();await page.locator('#week-grid .meal-card').first().waitFor();
  assert.deepEqual((await state()).plans,expected);await checkCards();
  await page.getByRole('tab',{name:/Dish List/}).click();assert.equal(await dish.count(),0);
  await page.getByRole('tab',{name:'Weekly Meals'}).click();
  const chosen=deleted.find(item=>item.week==='2026-11-16');await card(chosen).getByRole('button',{name:/Edit meal/}).click();
  const picker=page.locator('#replace-modal');assert.equal(await picker.locator('.replacement-choice').count(),1);
  await picker.getByRole('button',{name:/Keep this soup/}).click();await picker.getByRole('button',{name:'Replace',exact:true}).click();await picker.waitFor({state:'hidden'});
  assert.match(await card(chosen).innerText(),/Keep this soup/);assert.doesNotMatch(await card(chosen).innerText(),/Dish deleted/);
  assert.deepEqual(errors,[]);
  console.log('PASS: confirmation before mutation; cancel/dismiss keep both pages and saved data intact; all repeated past/current/upcoming occurrences become persistent deleted slots without old photos/details; other dishes and empty slots unchanged; deleted slots remain replaceable.');
}
run().catch(error=>{console.error(error);console.error(serverErrors);process.exitCode=1;}).finally(async()=>{await browser?.close();server.kill();await new Promise(resolve=>server.once('exit',resolve));assert.ok(path.resolve(folder).startsWith(path.join(os.tmpdir(),'meal-deletion-')));fs.rmSync(folder,{recursive:true,force:true});});
