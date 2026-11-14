// The six requested behavior regressions. Uses the existing isolated AI/calendar fixture.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const folder=fs.mkdtempSync(path.join(os.tmpdir(),'meal-behavior-'));
const origin='http://127.0.0.1:8770';let browser,serverErrors='';
const server=spawn(process.env.PYTHON||'python',['tests/browser_server.py'],{cwd:path.resolve(__dirname,'..'),env:{...process.env,MEAL_APP_DATA:folder,MEAL_TEST_PORT:'8770',MEAL_TEST_TODAY:'2026-11-11'},stdio:'pipe',windowsHide:true});
server.stderr.on('data',data=>serverErrors+=data);
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function run(){
  for(let i=0;i<80;i++){try{if((await fetch(origin)).ok)break;}catch{}await wait(150);}
  browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];page.on('pageerror',error=>errors.push(error.message));
  const state=async()=>await(await fetch(origin+'/api/state')).json();
  const token=(await state()).token;
  const api=async(route,data)=>{const response=await fetch(origin+route,{method:'POST',headers:{'Content-Type':'application/json','X-App-Token':token},body:JSON.stringify(data)});return {status:response.status,body:await response.json()};};
  const until=async(test)=>{for(let i=0;i<150;i++){if(await test())return;await wait(30);}throw new Error('State did not settle');};
  await page.goto(origin);await page.locator('.past-week').first().waitFor();
  const image=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=500;c.height=500;c.getContext('2d').fillRect(0,0,500,500);return c.toDataURL('image/png').split(',')[1];});
  const photo=(await api('/api/dish-photo',{filename:'dish.png',image})).body.photo_url;
  const short={id:'short',name:'Short dish',servings:2,measured_minutes:15,photo_url:photo,ingredients:[{name:'tofu',grams:200}]};
  const long={id:'long',name:'Long dish',servings:2,measured_minutes:45,photo_url:photo,ingredients:Array.from({length:80},()=>({name:'tofu',grams:1}))};
  await api('/api/recipes',short);await api('/api/recipes',long);
  await api('/test/date',{date:'2026-11-02'});await api('/api/plan/add',{week_start:'2026-11-09'});
  await api('/api/plan/replace',{week_start:'2026-11-09',date:'2026-11-09',recipe_id:'short'});
  const earlier=structuredClone((await state()).plans.find(p=>p.week_start==='2026-11-09').days.slice(0,2));
  await api('/test/date',{date:'2026-11-11'});await api('/api/recipes',{...short,name:'Updated short dish',ingredients:[{name:'tofu',grams:100}]});
  await page.reload();await page.locator('#week-grid .meal-card').first().waitFor();
  for(const name of ['Simple Rotation','Randomize','Generate with AI']){
    await page.locator('#weekly>.weekly-controls').getByRole('button',{name,exact:true}).click();
    await until(async()=>{const s=await state();return !s.plan_job&&s.plan.method===({'Simple Rotation':'rotation','Randomize':'random','Generate with AI':'AI'}[name]);});
    await page.waitForFunction(()=>document.querySelector('#busy-indicator').hidden&&document.querySelector('#cancel-plan').hidden);
    assert.deepEqual((await state()).plan.days.slice(0,2),earlier);
    assert.equal((await state()).plan.days.length,7);
    assert.match(await page.locator('#week-grid .meal-card').first().innerText(),/Short dish/);
    assert.doesNotMatch(await page.locator('#week-grid .meal-card').first().innerText(),/Updated/);
  }
  // Enter must take the exact Add action, including server validation failures.
  await page.getByRole('button',{name:'Add Week',exact:true}).click();
  assert.equal(await page.evaluate(()=>document.activeElement.id),'create-week');
  await page.keyboard.press('Enter');await page.locator('#add-week-modal').waitFor({state:'hidden'});
  assert.ok((await state()).plans.some(p=>p.week_start==='2026-11-16'));
  await page.getByRole('button',{name:'Add Week',exact:true}).click();
  await page.locator('#add-week-select').evaluate(select=>{select.add(new Option('Already saved','2026-11-16'));select.value='2026-11-16';});
  await page.keyboard.press('Enter');await page.waitForFunction(()=>document.querySelector('#add-week-message').textContent.includes('already exists'));
  assert.equal(await page.locator('#add-week-modal').isVisible(),true);
  await page.getByRole('button',{name:'Close Add Week'}).click();
  await page.getByRole('button',{name:'Add Week',exact:true}).click();await page.keyboard.press('Enter');await page.locator('#add-week-modal').waitFor({state:'hidden'});
  const future=week=>page.locator(`.future-week[data-week="${week}"]`),past=week=>page.locator(`.past-week[data-week="${week}"]`);
  assert.equal(await future('2026-11-16').evaluate(e=>e.open),true);assert.equal(await future('2026-11-23').evaluate(e=>e.open),true);
  await future('2026-11-16').locator('summary .grow').click();await until(async()=>(await state()).week_expansion['2026-11-16']===false);
  await past('2026-11-02').locator('summary .grow').click();await until(async()=>(await state()).week_expansion['2026-11-02']===false);
  await page.reload();await future('2026-11-16').waitFor();
  assert.equal(await future('2026-11-16').evaluate(e=>e.open),false);assert.equal(await future('2026-11-23').evaluate(e=>e.open),true);
  assert.equal(await past('2026-11-02').evaluate(e=>e.open),false);assert.equal(await past('2026-10-26').evaluate(e=>e.open),true);
  await future('2026-11-16').locator('summary .grow').click();await until(async()=>(await state()).week_expansion['2026-11-16']===true);
  // Long/short recipe selection must not change dialog geometry or move the page.
  const slot=future('2026-11-16').locator('.empty-meal').first();await slot.scrollIntoViewIfNeeded();
  const before=await page.evaluate(()=>scrollY);await slot.click();
  const picker=page.locator('#replace-modal');await picker.waitFor();
  await picker.getByRole('button',{name:/Long dish/}).click();const longBox=await picker.boundingBox();
  assert.equal(await page.evaluate(()=>scrollY),before);
  await picker.getByRole('button',{name:/Updated short dish/}).click();assert.deepEqual(await picker.boundingBox(),longBox);
  await picker.getByRole('button',{name:/Long dish/}).click();assert.deepEqual(await picker.boundingBox(),longBox);
  for(const position of ['top','bottom']){
    await picker.evaluate((el,position)=>{el.scrollTop=position==='top'?0:el.scrollHeight;},position);
    await page.mouse.move(longBox.x+15,longBox.y+20);await page.mouse.wheel(0,position==='top'?-3000:3000);await wait(80);
    assert.equal(await page.evaluate(()=>scrollY),before);
  }
  await picker.getByRole('button',{name:'Add',exact:true}).click();await picker.waitFor({state:'hidden'});
  assert.equal(await page.evaluate(()=>scrollY),before);
  const edit=future('2026-11-16').locator('[data-action=edit-meal]').first();await edit.scrollIntoViewIfNeeded();
  const beforeReplace=await page.evaluate(()=>scrollY);await edit.click();
  await picker.getByRole('button',{name:/Updated short dish/}).click();await picker.getByRole('button',{name:'Replace',exact:true}).click();await picker.waitFor({state:'hidden'});
  assert.equal(await page.evaluate(()=>scrollY),beforeReplace);
  // The same height, internal overflow and scroll lock apply to every HTML dialog.
  for(const id of ['recipe-modal','detail-modal','foods-modal','settings-modal','add-week-modal','replace-modal']){
    const savedY=await page.evaluate(()=>scrollY);
    await page.evaluate(id=>open('#'+id),id);const modal=page.locator('#'+id),box=await modal.boundingBox();
    await modal.evaluate(el=>{const probe=document.createElement('div');probe.dataset.probe='yes';probe.style.height='2400px';el.append(probe);});
    assert.deepEqual(await modal.boundingBox(),box);assert.ok(box.y>=0&&box.y+box.height<=1050);
    await modal.evaluate(el=>el.scrollTop=el.scrollHeight);await page.mouse.move(box.x+20,box.y+20);await page.mouse.wheel(0,3500);await wait(50);
    assert.equal(await page.evaluate(()=>scrollY),savedY);
    await modal.evaluate(el=>el.querySelector('[data-probe]').remove());await page.keyboard.press('Escape');await modal.waitFor({state:'hidden'});
    assert.equal(await page.evaluate(()=>scrollY),savedY);
  }
  // Earlier empty dates remain manually editable.
  const tuesday=page.locator('#week-grid .empty-meal');await tuesday.scrollIntoViewIfNeeded();await tuesday.click();
  await picker.getByRole('button',{name:/Updated short dish/}).click();await picker.getByRole('button',{name:'Add',exact:true}).click();await picker.waitFor({state:'hidden'});
  assert.equal((await state()).plan.days[1].recipe_id,'short');
  // Store both true and false choices and include an ignored collapsed-current choice.
  for(const [week,value] of [['2026-11-16',false],['2026-11-23',true],['2026-11-02',false],['2026-10-26',true],['2026-11-09',false]])await api('/api/plan/expansion',{week_start:week,expanded:value});
  await page.reload();await future('2026-11-16').waitFor();
  const choices=(await state()).week_expansion;
  assert.equal(await page.locator('#week-grid .meal-card').count(),7);assert.equal(await page.locator('#week-grid').evaluate(el=>el.closest('details')),null);
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  const settings=page.locator('#settings-modal'),[download]=await Promise.all([page.waitForEvent('download'),settings.getByRole('button',{name:'Export Backup',exact:true}).click()]);
  const zipPath=path.join(folder,'states.zip');await download.saveAs(zipPath);await settings.getByRole('button',{name:'Close settings'}).click();
  await api('/api/recipes',{...short,name:'Changed after backup'});
  await api('/api/settings',{text_model:'changed',vietnamese_model:'changed-vi',english_model:'changed-en',usda_api_key:'changed-key'});
  await api('/api/plan/expansion',{week_start:'2026-11-16',expanded:true});await api('/api/plan/expansion',{week_start:'2026-11-23',expanded:false});
  await page.reload();await page.locator('#week-grid .meal-card').first().waitFor();await page.getByRole('button',{name:'Settings',exact:true}).click();
  const images=()=>Object.fromEntries(fs.readdirSync(path.join(folder,'photos')).map(name=>[name,fs.readFileSync(path.join(folder,'photos',name)).toString('base64')]));
  const unchanged=await state(),imageBytes=images(),configPath=path.join(folder,'config.local.json');const config=fs.existsSync(configPath)?fs.readFileSync(configPath).toString():null;
  let imports=0;page.on('request',r=>{if(r.url().endsWith('/api/backup/import'))imports++;});
  for(let i=0;i<2;i++){
    const notice=page.waitForEvent('dialog'),action=page.locator('#backup-file').setInputFiles(zipPath),dialog=await notice;
    assert.equal(dialog.type(),'confirm');assert.equal(dialog.message(),'Importing this backup will replace all current data with the contents of the backup file. Do you want to continue?');
    assert.equal(imports,0);await dialog.dismiss();await action;await until(async()=>await page.locator('#backup-file').inputValue()==='');
    assert.deepEqual(await state(),unchanged);assert.deepEqual(images(),imageBytes);assert.equal(fs.existsSync(configPath)?fs.readFileSync(configPath).toString():null,config);
  }
  const notice=page.waitForEvent('dialog'),action=page.locator('#backup-file').setInputFiles(zipPath),dialog=await notice;
  assert.equal(imports,0);await dialog.accept();await action;await settings.waitFor({state:'hidden'});assert.equal(imports,1);
  assert.deepEqual((await state()).week_expansion,choices);
  assert.equal(await future('2026-11-16').evaluate(e=>e.open),false);assert.equal(await future('2026-11-23').evaluate(e=>e.open),true);
  await page.reload();await future('2026-11-16').waitFor();assert.equal(await future('2026-11-16').evaluate(e=>e.open),false);
  await api('/test/date',{date:'2026-11-18'});await page.reload();await page.locator('#week-grid .meal-card').first().waitFor();
  assert.equal((await state()).week_expansion['2026-11-16'],false);
  assert.equal(await page.locator('.future-week[data-week="2026-11-16"]').count(),0);
  assert.equal(await page.locator('#week-grid .meal-card').count(),7);
  assert.equal(await page.locator('#week-grid').evaluate(el=>el.closest('details')),null);
  assert.deepEqual(errors,[]);
  console.log('PASS: initial Add focus/Enter and validation; independent persisted upcoming/past collapse; always-expanded current week; stable heights and scroll containment for every dialog; exact Add/Replace scroll restoration; earlier slots preserved for all three methods and manually editable; backup collapse roundtrip and confirmation cancel/close safety.');
}
run().catch(error=>{console.error(error);console.error(serverErrors);process.exitCode=1;}).finally(async()=>{await browser?.close();server.kill();await new Promise(resolve=>server.once('exit',resolve));assert.ok(path.resolve(folder).startsWith(path.join(os.tmpdir(),'meal-behavior-')));fs.rmSync(folder,{recursive:true,force:true});});
