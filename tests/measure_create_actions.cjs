// Live/local UI benchmarks in disposable anonymous sessions; no AI calls.
const {chromium}=require('playwright'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const args=process.argv.slice(2),option=name=>args.includes(name)?args[args.indexOf(name)+1]:null;
const local=args.includes('--local'),origin=(option('--url')||'http://127.0.0.1:8776').replace(/\/$/,'');
const idleSeconds=Number(option('--idle-seconds')||0),output=option('--output');
let browser,server,folder;
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function run(){
  if(local){folder=fs.mkdtempSync(path.join(os.tmpdir(),'meal-create-timing-'));server=spawn(process.env.PYTHON||'python',['tests/browser_server.py'],{cwd:path.resolve(__dirname,'..'),env:{...process.env,MEAL_APP_DATA:folder,MEAL_TEST_PORT:'8776'},stdio:'pipe',windowsHide:true});for(let i=0;i<80;i++){try{if((await fetch(origin)).ok)break;}catch{}await wait(100);}}
  browser=await chromium.launch({channel:'msedge',headless:true});
  const pages=[];const boots=[];
  for(const action of ['week','recipe']){
    const context=await browser.newContext({viewport:{width:1440,height:1000},timezoneId:'Asia/Bangkok'}),page=await context.newPage();
    await page.addInitScript(()=>{
      window.__createTiming={};
      document.addEventListener('keydown',event=>{if(event.key==='Enter'&&event.target.closest('#add-week-modal'))window.__createTiming.start=performance.now();},true);
      document.addEventListener('click',event=>{if(event.target.closest('#save-recipe'))window.__createTiming.start=performance.now();},true);
      const observer=new MutationObserver(()=>{
        const m=window.__createTiming;if(!m.start||m.end)return;
        if(m.action==='week'&&document.querySelector('.future-week[data-week="'+m.week+'"]'))m.end=performance.now();
        if(m.action==='recipe'&&!document.querySelector('#recipe-modal').open&&document.querySelector('#notification')?.textContent.startsWith('Recipe saved.'))m.end=performance.now();
      });
      document.addEventListener('DOMContentLoaded',()=>observer.observe(document.body,{childList:true,subtree:true,attributes:true}));
    });
    const started=performance.now();await page.goto(origin,{waitUntil:'domcontentloaded',timeout:120000});
    await page.waitForFunction(()=>typeof state!=='undefined'&&state.calendar,{timeout:120000});
    boots.push({action,ms:Math.round(performance.now()-started)});pages.push({page,action});
  }
  async function prepare(item,index){
    const {page,action}=item;
    if(action==='week'){
      await page.getByRole('button',{name:'Add Week',exact:true}).click();
      const week=await page.locator('#add-week-select').inputValue();item.week=week;
      await page.evaluate(week=>{performance.clearResourceTimings();window.__createTiming={action:'week',week};},week);
    }else{
      await page.getByRole('tab',{name:/Dish List/}).click();
      await page.getByRole('button',{name:'+ Create recipe',exact:true}).click();
      item.name='Disposable save timing '+index;
      await page.locator('#recipe-form [name=name]').fill(item.name);
      await page.evaluate(()=>{performance.clearResourceTimings();window.__createTiming={action:'recipe'};});
    }
  }
  const results=[];
  async function act(item,index){
    const {page,action}=item;
    if(action==='week')await page.keyboard.press('Enter');
    else await page.getByRole('button',{name:'Save recipe',exact:true}).click();
    await page.waitForFunction(()=>window.__createTiming.end,{timeout:120000});
    const result=await page.evaluate(()=>{
      const m=window.__createTiming;
      return {ms:Math.round(m.end-m.start),requests:performance.getEntriesByType('resource').filter(r=>r.name.includes('/api/')).map(r=>({path:new URL(r.name).pathname,ms:Math.round(r.duration),ttfbMs:Math.round(r.responseStart-r.startTime),serverTiming:r.serverTiming.map(t=>({name:t.name,ms:Math.round(t.duration)}))}))};
    });results.push({action,attempt:index+1,...result});
  }
  for(const item of pages)await prepare(item,0);
  if(idleSeconds){
    for(const {page} of pages){await page.waitForLoadState('networkidle');await page.evaluate(()=>{for(let id=1;id<10000;id++)clearInterval(id);});}
    console.log('Both test pages idle; no test traffic for '+idleSeconds+' seconds.');
    for(let remaining=idleSeconds;remaining>0;remaining-=30){await wait(Math.min(30,remaining)*1000);if(remaining>30)console.log('Idle remaining: '+Math.max(0,remaining-30)+'s');}
  }
  // First operations start together after inactivity, then repeated warm actions.
  await Promise.all(pages.map(item=>act(item,0)));
  for(let index=1;index<3;index++)for(const item of pages){await prepare(item,index);await act(item,index);}
  for(const item of pages){await item.page.reload();await item.page.waitForFunction(()=>typeof state!=='undefined'&&state.calendar);
    const present=await item.page.evaluate(({action,week,name})=>action==='week'?state.plans.some(p=>p.week_start===week):state.recipes.some(r=>r.name===name),{action:item.action,week:item.week||null,name:item.name||null});assert.equal(present,true);}
  const report={origin,mode:local?'local':'deployed',idleSeconds,boots,results,persisted:true};
  console.log(JSON.stringify(report,null,2));
  if(output){fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');}
}
run().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await browser?.close();if(server){server.kill();await new Promise(resolve=>server.once('exit',resolve));}if(folder){assert.ok(path.resolve(folder).startsWith(path.join(os.tmpdir(),'meal-create-timing-')));fs.rmSync(folder,{recursive:true,force:true});}});
