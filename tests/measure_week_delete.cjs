// Isolated browser timing of real delete requests. Never uses an existing login.
// node tests/measure_week_delete.cjs --url https://health-diet-v1.onrender.com
// node tests/measure_week_delete.cjs --local
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const args=process.argv.slice(2),option=name=>args.includes(name)?args[args.indexOf(name)+1]:null;
const local=args.includes('--local'),simulation=args.includes('--hosted-sim'),origin=(option('--url')||(simulation?'http://localhost:8774':'http://127.0.0.1:8773')).replace(/\/$/,'');
const output=option('--output'),idleSeconds=Number(option('--idle-seconds')||0);
let browser,server,folder;
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function run(){
  if(local||simulation){
    folder=fs.mkdtempSync(path.join(os.tmpdir(),'meal-delete-timing-'));
    const env={...process.env,MEAL_APP_DATA:folder,MEAL_TEST_PORT:'8773',...(simulation?{APP_MODE:'hosted',PORT:'8774',PUBLIC_ORIGIN:'https://localhost:8774',SUPABASE_URL:'https://project.supabase.co',SUPABASE_PUBLISHABLE_KEY:'sb_publishable_test',SESSION_SECRET:'s'.repeat(64)}:{})};
    server=spawn(process.env.PYTHON||'python',[simulation?'tests/delete_timing_server.py':'tests/browser_server.py'],{cwd:path.resolve(__dirname,'..'),env,stdio:'pipe',windowsHide:true});
    for(let i=0;i<100;i++){try{if((await fetch(origin)).ok)break;}catch{}await wait(100);}
  }
  browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000},timezoneId:'Asia/Bangkok'});
  let page=await context.newPage();
  if(option('--override-script'))await context.route('**/app.js',route=>route.fulfill({path:path.resolve(option('--override-script')),contentType:'text/javascript'}));
  const install=async page=>{
    page.on('dialog',async dialog=>{assert.equal(dialog.type(),'confirm');await dialog.accept();});
    await page.addInitScript(()=>{
      window.__deleteMeasure={};
      const native=window.confirm;
      window.confirm=function(...args){window.__deleteMeasure.confirmAt=performance.now();const answer=native.apply(this,args);window.__deleteMeasure.confirmReturnAt=performance.now();return answer;};
      document.addEventListener('click',event=>{if(event.target.closest('[data-action="delete-week"]'))window.__deleteMeasure.clickAt=performance.now();},true);
    });
  };
  await install(page);
  const boot=performance.now();await page.goto(origin,{waitUntil:'domcontentloaded',timeout:120000});
  await page.waitForFunction(()=>typeof state!=='undefined'&&state.calendar,{timeout:120000});
  const bootMs=performance.now()-boot;
  const setup=async()=>page.evaluate(async()=>{
    const weeks=availableWeeks().slice(0,2);
    for(const week of weeks)await api('/api/plan/add',{week_start:week,timezone_offset:new Date().getTimezoneOffset()});
    await refresh();return weeks;
  });
  const weeks=await setup();
  if(args.includes('--images')){
    await page.evaluate(async()=>{
      for(let i=0;i<7;i++){
        const c=document.createElement('canvas');c.width=500;c.height=500;c.getContext('2d').fillStyle=`rgb(${i*30},130,60)`;c.getContext('2d').fillRect(0,0,500,500);
        const photo=await api('/api/dish-photo',{filename:'timing.png',image:c.toDataURL('image/png').split(',')[1]});
        await api('/api/recipes',{name:'Disposable timing dish '+i,photo_url:photo.photo_url,servings:2,ingredients:[{name:'tofu',grams:100}]});
      }
      const week=availableWeeks()[0];await api('/api/plan/add',{week_start:week});await api('/api/plan/generate',{week_start:week,method:'rotation'});
      await refresh();
    });
  }
  if(idleSeconds){
    // Suspend only this test page's automatic refresh; retain the actual DOM and
    // session, so the first click itself can encounter a cold Render service.
    await page.waitForLoadState('networkidle');
    await page.evaluate(()=>{for(let id=1;id<10000;id++)clearInterval(id);});
    console.log('Idle measurement: no test-page traffic for '+idleSeconds+' seconds.');
    for(let remaining=idleSeconds;remaining>0;remaining-=30){await wait(Math.min(30,remaining)*1000);if(remaining>30)console.log('Idle wait remaining: '+Math.max(0,remaining-30)+'s');}
  }
  const results=[];
  for(const [index,week] of weeks.entries()){
    const button=page.locator(`.future-week[data-week="${week}"] [data-action="delete-week"]`);
    await button.scrollIntoViewIfNeeded();
    await page.evaluate(week=>{
      window.__deleteMeasure={week};performance.clearResourceTimings();
      window.__deleteObserver=new MutationObserver(()=>{if(!document.querySelector('.future-week[data-week="'+week+'"]')&&window.__deleteMeasure.clickAt){window.__deleteMeasure.goneAt=performance.now();window.__deleteObserver.disconnect();}});
      window.__deleteObserver.observe(document.querySelector('#future-weeks'),{childList:true,subtree:true});
    },week);
    await button.click();await button.waitFor({state:'detached',timeout:120000});
    const measured=await page.evaluate(()=>{
      const m=window.__deleteMeasure;
      return {label:m.confirmAt?'confirmed':'no_confirmation',clickToConfirmMs:m.confirmAt==null?null:Math.round(m.confirmAt-m.clickAt),
        confirmToGoneMs:m.confirmReturnAt==null?null:Math.round(m.goneAt-m.confirmReturnAt),clickToGoneMs:Math.round(m.goneAt-m.clickAt),
        requests:performance.getEntriesByType('resource').filter(r=>r.name.includes('/api/')||r.name.includes('/media/')).map(r=>({path:new URL(r.name).pathname.replace(/\/media\/.*/,'/media/[image]'),ms:Math.round(r.duration),ttfbMs:Math.round(r.responseStart-r.startTime),serverTiming:r.serverTiming.map(t=>({name:t.name,ms:Math.round(t.duration),description:t.description}))}))};
    });
    results.push({attempt:index+1,...measured});
  }
  await page.reload({waitUntil:'domcontentloaded'});await page.waitForFunction(()=>typeof state!=='undefined'&&state.calendar);
  const persisted=await page.evaluate(weeks=>weeks.every(week=>!state.plans.some(plan=>plan.week_start===week)),weeks);
  assert.equal(persisted,true);
  const result={origin,mode:local?'local':simulation?'hosted-simulation':option('--override-script')?'live-backend-local-script':'deployed',initialBootMs:Math.round(bootMs),idleSeconds,results,persisted};
  console.log(JSON.stringify(result,null,2));
  if(output){fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,JSON.stringify(result,null,2)+'\n');}
}
run().catch(error=>{console.error(error.message);process.exitCode=1;}).finally(async()=>{
  await browser?.close();if(server){server.kill();await new Promise(resolve=>server.once('exit',resolve));}
  if(folder){assert.ok(path.resolve(folder).startsWith(path.join(os.tmpdir(),'meal-delete-timing-')));fs.rmSync(folder,{recursive:true,force:true});}
});
