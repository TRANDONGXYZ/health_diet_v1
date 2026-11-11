// Hosted interface smoke check; provider calls are explicitly mocked.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const folder=fs.mkdtempSync(path.join(os.tmpdir(),'meal-hosted-ui-'));
const origin='http://127.0.0.1:8772';let browser;
const server=spawn(process.env.PYTHON||'python',['tests/browser_server.py'],{cwd:path.resolve(__dirname,'..'),env:{...process.env,MEAL_APP_DATA:folder,MEAL_TEST_PORT:'8772'},stdio:'pipe',windowsHide:true});
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function run(){
  for(let i=0;i<80;i++){try{if((await fetch(origin)).ok)break;}catch{}await wait(150);}
  browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/state*',async route=>{const response=await route.fetch();const data=await response.json();data.hosted=true;await route.fulfill({response,json:data});});
  await page.route('**/api/status',route=>route.fulfill({json:{provider:'groq',connected:true,text_ready:true,vision_ready:true,vietnamese_ready:true,english_ready:true}}));
  await page.goto(origin);await page.locator('#model-status').filter({hasText:'Groq'}).waitFor();
  assert.match(await page.locator('#storage-label').innerText(),/Private beta workspace/);
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  assert.equal(await page.locator('#settings-form').isVisible(),false);
  assert.match(await page.locator('#hosted-settings-info').innerText(),/No personal API key/);
  assert.equal(await page.getByRole('button',{name:'Export Backup',exact:true}).isVisible(),true);
  await page.getByRole('button',{name:'Close settings',exact:true}).click();
  await page.getByRole('tab',{name:/Dish List/}).click();await page.getByRole('button',{name:'+ Create recipe',exact:true}).click();
  const source=page.locator('#recipe-form [name=source_text]');
  await page.locator('#transcript-upload').setInputFiles({name:'recipe.txt',mimeType:'text/plain',buffer:Buffer.from('Fried tofu. Ingredients: 200 g tofu. Fry until golden.')});
  await page.waitForFunction(()=>document.querySelector('#recipe-message').textContent.includes('Transcript loaded'));
  assert.match(await source.inputValue(),/Fried tofu/);assert.match(await page.locator('#source-language').innerText(),/English/);
  await page.route('**/api/transcribe',route=>route.fulfill({json:{text:'Đậu hũ chiên. Nguyên liệu: 200 g đậu hũ. Chiên cho vàng.'}}));
  await page.locator('#transcript-upload').setInputFiles({name:'allowed.m4a',mimeType:'audio/mp4',buffer:Buffer.from('test fixture')});
  await page.waitForFunction(()=>document.querySelector('#recipe-form [name=source_text]').value.startsWith('Đậu'));
  assert.match(await page.locator('#source-language').innerText(),/Vietnamese/);
  await page.route('**/api/import',route=>route.fulfill({status:400,json:{error:'The Groq quota is exhausted. Please try again later. Manual editing, Simple Rotation and Randomize are still available.'}}));
  await page.getByRole('button',{name:'Extract recipe with AI',exact:true}).click();
  await page.locator('#recipe-modal .danger').filter({hasText:'Groq quota'}).waitFor();
  assert.match(await source.inputValue(),/Đậu hũ/);
  await page.locator('#recipe-form [name=name]').fill('Manual beta soup');
  await page.getByRole('button',{name:'+ Add ingredient',exact:true}).click();
  await page.locator('.name-input').fill('tofu');await page.locator('.grams-input').fill('200');
  await page.getByRole('button',{name:'Save recipe',exact:true}).click();await page.locator('#recipe-modal').waitFor({state:'hidden'});
  await page.getByRole('tab',{name:'Weekly Meals',exact:true}).click();
  await page.locator('#weekly>.weekly-controls').getByRole('button',{name:'Simple Rotation',exact:true}).click();
  await page.locator('#week-grid .meal-card').first().waitFor();
  await page.locator('#weekly>.weekly-controls').getByRole('button',{name:'Randomize',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#current-week-method').textContent==='Randomized');
  assert.deepEqual(errors,[]);
  console.log('PASS: existing hosted UI hides personal keys/model controls, keeps backups visible, accepts text/audio transcripts in native language, preserves sources on Groq quota errors, and supports manual editing/rotation/randomization (explicit provider mocks).');
}
run().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await browser?.close();server.kill();await new Promise(resolve=>server.once('exit',resolve));assert.ok(path.resolve(folder).startsWith(path.join(os.tmpdir(),'meal-hosted-ui-')));fs.rmSync(folder,{recursive:true,force:true});});
