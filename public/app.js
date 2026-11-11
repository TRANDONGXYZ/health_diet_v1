'use strict';
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const normalize = v => String(v || '').toLowerCase().replace(/đ/g,'d').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/[^\x00-\x7F]/g,'').trim().replace(/\s+/g,' ');
const safeLink = v => { try { const u = new URL(v); return ['http:','https:'].includes(u.protocol) ? esc(u.href) : '#'; } catch { return '#'; } };
const fmt = n => Number.isFinite(n) ? Math.round(n).toLocaleString() : '—';
let state = {recipes:[],foods:[],plan:null,settings:{}}, token = '', draft = null, photo = null, matches = [], busy = false, notifyTimer;
let cameraStream = null;
const weekPreferences=new Map(), weekExpansion=new Map();
let expansionSaves=Promise.resolve();
const dayNames = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-App-Token':token},body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
function notify(message, error = false) {
  const node = $('#notification'); node.textContent = message; node.classList.toggle('error',error); node.hidden = false;
  clearTimeout(notifyTimer); notifyTimer = setTimeout(() => node.hidden = true, error ? 14000 : 6000);
}
async function work(label, callback, {pollAI=true}={}) {
  if (busy||planJobId) { notify('A task is already running. Wait or cancel AI generation.'); return; }
  busy = true; $('#busy-text').textContent = label; $('#busy-indicator').hidden = false;
  // A dialog is in the browser's top layer; include the progress message inside it too.
  const dialog = $$('dialog[open]').at(-1);
  const progress = document.createElement('p'); progress.className = 'inline-message'; progress.setAttribute('role','status'); progress.textContent = label + ' — please wait…';
  if (dialog) dialog.prepend(progress);
  const workStarted = Date.now();
  let polling = true, pollPending = false;
  const progressTimer = pollAI?setInterval(async () => {
    if (!polling || pollPending) return;
    pollPending = true;
    try {
      const status = await api('/api/ai-progress');
      if (!polling) return;
      const seconds = Math.max(status.elapsed_seconds || 0, Math.round((Date.now() - workStarted) / 1000));
      const elapsed = `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
      const message = status.phase === 'loading' ? `${state.hosted?'Hosted':'Local'} AI is reading the source · ${elapsed}` : status.phase === 'generating' ? `${state.hosted?'Hosted':'Local'} AI is generating a response · ${elapsed}` : label;
      $('#busy-text').textContent = message;
      if (!progress.dataset.persist) progress.textContent = message + ' — please wait…';
    } catch { /* Progress availability does not decide whether the main request succeeds. */ }
    finally { pollPending = false; }
  }, 2000):null;
  $$('button[type=submit], [data-action=generate], [data-action=import], [data-action=identify]').forEach(b => b.disabled = true);
  try { return await callback(); }
  catch (error) { notify(error.message,true); if (dialog?.open) { progress.textContent = error.message; progress.classList.add('danger'); progress.dataset.persist = 'true'; } }
  finally { polling = false; clearInterval(progressTimer); busy = false; $('#busy-indicator').hidden = true; if (!progress.dataset.persist) progress.remove(); $$('button[type=submit], [data-action=generate], [data-action=import], [data-action=identify]').forEach(b => b.disabled = false); $('#identify-button').disabled = !photo; }
}
let pageScrollLock=null;
const dialogOrigins=new WeakMap();
function open(id,initialFocus=null) {
  const dialog=$(id);if(dialog.open)return;
  if(!pageScrollLock){pageScrollLock={x:scrollX,y:scrollY};document.documentElement.classList.add('modal-open');document.body.classList.add('modal-open');}
  dialogOrigins.set(dialog,$$('dialog[open]').map(parent=>({parent,top:parent.scrollTop,left:parent.scrollLeft})));
  dialog.returnValue='';dialog.showModal();dialog.scrollTop=0;
  if(initialFocus)$(initialFocus,dialog).focus({preventScroll:true});
  window.scrollTo(pageScrollLock.x,pageScrollLock.y);
}
function finishDialogClose(dialog) {
  if(dialog.open)return;
  for(const entry of dialogOrigins.get(dialog)||[]){if(entry.parent.open){entry.parent.scrollTop=entry.top;entry.parent.scrollLeft=entry.left;}}
  dialogOrigins.delete(dialog);
  if(!$$('dialog[open]').length&&pageScrollLock){
    const saved=pageScrollLock;pageScrollLock=null;document.documentElement.classList.remove('modal-open');document.body.classList.remove('modal-open');window.scrollTo({left:saved.x,top:saved.y,behavior:'instant'});
  }
}
function closeDialog(dialog){if(!dialog?.open)return;dialog.close();finishDialogClose(dialog);}
document.addEventListener('close',event=>{if(event.target.matches('dialog'))finishDialogClose(event.target);},true);
document.addEventListener('wheel',event=>{const active=$$('dialog[open]').at(-1);if(active&&!active.contains(event.target))event.preventDefault();},{passive:false});

function tab(name) {
  ['weekly','dishes'].forEach(id => { $('#'+id).hidden = name !== id; $(`[data-tab=${id}]`).setAttribute('aria-selected',String(name === id)); });
}
let refreshVersion=0;
async function refresh() {
  const version=++refreshVersion;
  const next=await api('/api/state?timezone_offset='+new Date().getTimezoneOffset());if(version!==refreshVersion)return;state=next;token=state.token;
  $('#dish-count').textContent = state.recipes.length; $('#food-count').textContent = `${state.foods.length} food references`;
  $('#food-options').innerHTML = state.foods.map(f => `<option value="${esc(f.name)}">${esc(f.name)} · ${esc(f.aliases?.join(', '))}</option>`).join('');
  renderDishes(); renderWeek();
  $('#storage-label').textContent=state.hosted?'Private beta workspace · Keep browser cookies or export a backup':'Stored on this laptop';
  $('#settings-form').hidden=!!state.hosted;
  if(state.hosted&&!$('#hosted-settings-info'))$('#settings-form').insertAdjacentHTML('beforebegin','<p id="hosted-settings-info" class="footnote">AI is provided by the beta operator through Groq. No personal API key is needed. Keep browser cookies to return to this private workspace, and export backups regularly.</p>');
  if(state.hosted){$('#busy-indicator small').textContent='Hosted AI may be limited during the public beta.';$('#photo-modal>.muted').textContent='Identify a food with hosted AI, confirm the name, then retrieve nutrition and guidance online. A photo cannot reveal exact ingredients or portion weight.';}
  if(state.hosted)$('#import-box .import-content>p.footnote:last-of-type').textContent='Vietnamese and English stay in the source language. Hosted video retrieval may be blocked. Paste a transcript or upload an audio/video file you are allowed to use (24 MB, ten minutes maximum). Review extracted amounts before saving.';
}
async function checkStatus() {
  const node = $('#model-status');
  try {
    const status = await api('/api/status');
    if(status.provider==='groq'){
      node.classList.toggle('ready',status.connected);node.textContent=status.connected?'Hosted AI · Groq':'Hosted AI unavailable · manual tools still work';
      $('#settings-status').textContent='Hosted AI is managed by the beta operator. No personal API key is needed. Keep browser cookies to return to this private workspace, and export backups regularly.';
      return;
    }
    const ready=status.vietnamese_ready && status.english_ready;
    node.classList.toggle('ready',ready);
    const missing=[!status.vietnamese_ready && state.settings.vietnamese_model,!status.english_ready && state.settings.english_model].filter(Boolean);
    node.textContent=!status.connected?'Ollama is offline - open Settings':ready?'Local AI ready - Vietnamese + English':`Install ${missing.join(', ')} - Settings`;
    $('#settings-status').textContent=`Ollama: ${status.connected?'connected':'offline'}. Vietnamese: ${status.vietnamese_ready?'installed':'not installed'}. English: ${status.english_ready?'installed':'not installed'}. Planning: ${status.text_ready?'installed':'not installed'}. Photo: ${status.vision_ready?'installed':'not installed'}.`;

  } catch { node.textContent = 'AI status unavailable'; }
}
function foodFor(ingredient) {
  if (ingredient.food_id) return state.foods.find(f => f.id === ingredient.food_id);
  const name = normalize(ingredient.name);
  return state.foods.find(f => [f.name,f.vi_name,f.id,...(f.aliases || [])].filter(Boolean).some(a => normalize(a) === name));
}
function calculate(recipe) {
  let total = 0, count = 0; const missing = [];
  for (const ingredient of recipe.ingredients || []) {
    count++; const food = foodFor(ingredient);
    if (ingredient.grams == null || !food) missing.push(ingredient.name || 'Unnamed ingredient');
    else total += ingredient.grams * food.kcal / 100;
  }
  return {total,per_serving:recipe.servings > 0 ? total / recipe.servings : null,complete:missing.length === 0 && count > 0,missing};
}
function calorieMarkup(recipe, frozen=false) {
  const c = frozen&&recipe.calories?recipe.calories:calculate(recipe);
  return `<div><strong>${fmt(c.total)}</strong><small>kcal / whole recipe${c.complete ? '' : ' (partial)'}</small></div><div><strong>${fmt(c.per_serving)}</strong><small>kcal / serving${recipe.servings ? '' : ' · set servings'}</small></div>${!c.complete ? `<div class="warning">Incomplete estimate. ${c.missing.length ? 'Check weight or food value: '+esc((c.missing||[]).map(item=>typeof item==='string'?item:item.name).join(', ')) : 'Add ingredients to calculate calories.'}</div>` : '<span class="pill">All ingredients counted</span>'}`;
}
function veganPill(value) { return `<span class="pill ${value === true ? '' : 'neutral'}">${value === true ? 'Vegan' : value === false ? 'Non-vegan' : 'Vegan status unsure'}</span>`; }
function timeLabel(r) { return r.measured_minutes ? `${r.measured_minutes} min measured` : r.estimated_minutes ? (r.time_source==='source'?`${r.estimated_minutes} min from source`:`~${r.estimated_minutes} min estimated`) : 'Time not entered'; }
function recipeMeta(r) { const c = calculate(r); return `${fmt(c.per_serving)} kcal / serving${c.complete ? '' : ' · incomplete'} <span aria-hidden="true">·</span> ${esc(timeLabel(r))}`; }
function renderDishes() {
  const query = normalize($('#dish-search').value), filter = $('#vegan-filter').value, running = $('#running-filter').value;
  const recipes = state.recipes.filter(r => normalize(r.name).includes(query) && (filter === 'all' || filter === 'yes' && r.vegan === true || filter === 'no' && r.vegan === false || filter === 'unknown' && r.vegan == null) && (running === 'all' || running === 'yes' && r.good_for_running || running === 'no' && !r.good_for_running));
  $('#dish-list').innerHTML = recipes.length ? recipes.map(r => `<article class="dish-card"><div class="dish-main"><h3><button data-action="details" data-id="${esc(r.id)}">${esc(r.name)}</button></h3><div class="badge-row">${veganPill(r.vegan)}${runningPill(r)}<span class="dish-meta">${r.servings ? esc(r.servings)+' servings' : 'Servings not stated'}</span></div><div class="dish-meta">${recipeMeta(r)}</div></div><div class="actions"><label class="check"><input type="checkbox" data-running-id="${esc(r.id)}" ${r.good_for_running ? 'checked' : ''}> Good for Running</label><button data-action="edit" data-id="${esc(r.id)}" class="secondary">Edit recipe</button><button data-action="delete" data-id="${esc(r.id)}" class="quiet danger">Delete</button></div></article>`).join('') : `<div class="empty"><div class="empty-icon" aria-hidden="true">＋</div><h3>${state.recipes.length ? 'No matching dishes' : 'A good week starts with a recipe.'}</h3><p>${state.recipes.length ? 'Try another search or vegan filter.' : 'Add your own dishes from a video, an article, or a recipe you already know. Your meal plan will only use these dishes.'}</p><button data-action="new">+ Create your first recipe</button></div>`;
}
function dateLabel(start, offset, options) { const d = new Date(start+'T12:00:00'); d.setDate(d.getDate()+offset); return d.toLocaleDateString('en-GB',options); }
function weekRange(start) { return dateLabel(start,0,{day:'numeric',month:'short'})+' - '+dateLabel(start,6,{day:'numeric',month:'short',year:'numeric'}); }
function mealRecipe(day,past) { if(day.deleted)return {name:'Dish deleted'};return (past||day.date<state.calendar.today?day.recipe:(state.recipes.find(r=>r.id===day.recipe_id)||day.recipe))||{id:day.recipe_id,name:'Dish unavailable',ingredients:[],notes:'The original recipe was removed before this plan was archived.',calories:{total:null,per_serving:null,complete:false,missing:['Recipe details unavailable']}}; }
function mealCard(day,index,plan,past) {
  if(!day.recipe_id)return `<button class="meal-card empty-meal${day.date===state.calendar.today?' is-today':''}" data-action="edit-meal" data-week="${esc(plan.week_start)}" data-day="${index}" aria-label="Add dish for ${esc(dateLabel(day.date,0,{weekday:'long',day:'numeric',month:'long'}))}"><span class="empty-slot"><span class="slot-plus" aria-hidden="true">+</span></span><span class="meal-content"><span class="meal-date">${dateLabel(day.date,0,{weekday:'long',day:'numeric',month:'short'})}</span><span class="muted">Add dish</span></span></button>`;
  const r=mealRecipe(day,past),frozen=past||day.date<state.calendar.today||!state.recipes.some(item=>item.id===day.recipe_id),c=day.deleted?null:frozen?r?.calories:calculate(r||{}),isToday=day.date===state.calendar.today;
  const image=day.deleted?'<span class="meal-placeholder" aria-hidden="true">Dish deleted</span>':r?.photo_url?`<img src="${esc(r.photo_url)}" alt="" width="500" height="500" loading="lazy">`:'<span class="meal-placeholder" aria-hidden="true">No photo</span>';
  return `<article class="meal-card${isToday?' is-today':''}" data-action="meal-details" data-week="${esc(plan.week_start)}" data-day="${index}"><button class="meal-open" data-action="meal-details" data-week="${esc(plan.week_start)}" data-day="${index}" ${isToday?'aria-current="date"':''} aria-label="${esc(r?.name||'Unavailable dish')} - ${esc(dateLabel(day.date,0,{weekday:'long',day:'numeric',month:'long'}))}"><span class="meal-image">${image}</span><span class="meal-content"><span class="meal-date">${dateLabel(day.date,0,{weekday:'long',day:'numeric',month:'short'})}</span>${day.deleted?'':`<span class="meal-time">${esc(timeLabel(r||{}))}</span>`}<strong>${esc(r?.name||'Dish unavailable')}</strong>${day.deleted?'':`<span class="meal-calories">${fmt(c?.per_serving)} kcal / serving${c&&!c.complete?' (partial)':''}</span>`}${r?.good_for_running?'<span class="pill">Good for Running</span>':''}</span></button><div class="meal-actions"><button data-action="edit-meal" data-week="${esc(plan.week_start)}" data-day="${index}" class="secondary" aria-label="Edit meal for ${esc(dateLabel(day.date,0,{weekday:'long',day:'numeric',month:'long'}))}">Edit</button></div></article>`;
}
function offsetDate(start,offset){const d=new Date(start+'T12:00:00');d.setDate(d.getDate()+offset);return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;}
function availableWeeks(){return Array.from({length:8},(_,i)=>offsetDate(state.calendar.week_start,7*(i+1))).filter(week=>!(state.plans||[]).some(p=>p.week_start===week));}
function weekControls(plan) {
  const week=plan.week_start,pref=weekPreferences.get(week)||plan;
  return `<div class="toolbar panel weekly-controls future-controls" data-week="${esc(week)}"><label class="check"><input type="checkbox" data-week="${esc(week)}" data-pref="vegan_only" ${pref.vegan_only?'checked':''}> Vegan dishes only</label><label class="check"><input type="checkbox" data-week="${esc(week)}" data-pref="prioritize_running" ${pref.prioritize_running?'checked':''}> Prioritize Good for Running</label><div class="spacer"></div><button class="secondary plan-control" data-action="rotation" data-week="${esc(week)}">Simple Rotation</button><button class="secondary plan-control" data-action="randomize" data-week="${esc(week)}">Randomize</button><button class="plan-control" data-action="generate" data-week="${esc(week)}">Generate with AI</button><button class="secondary" data-action="cancel-plan" data-week="${esc(week)}" hidden>Cancel</button></div><p class="inline-message" data-plan-progress data-week="${esc(week)}" role="status" hidden></p>`;
}
function weekIsExpanded(week){return week===state.calendar.week_start?true:(weekExpansion.has(week)?weekExpansion.get(week):(state.week_expansion?.[week]??true));}
function renderWeek() {
  if(!state.calendar)return;
  const plan=state.plan,calendar=state.calendar;
  $('#current-week-range').textContent='Current week: '+weekRange(calendar.week_start);
  $('#current-week-method').textContent=plan?({AI:'AI plan',rotation:'Simple Rotation',random:'Randomized',manual:'Manual plan'}[plan.method]||'Saved plan'):'No plan yet';
  $('#generate-plan').textContent='Generate with AI';
  $('#delete-current-week').hidden=!plan;$('#delete-current-week').dataset.week=calendar.week_start;
  $$('#weekly>.weekly-controls button,#plan-progress').forEach(node=>node.dataset.week=calendar.week_start);
  for(const [id,key] of [['plan-vegan','vegan_only'],['plan-running','prioritize_running']]){
    const input=$('#'+id);if(input.dataset.week!==calendar.week_start)input.checked=!!(weekPreferences.get(calendar.week_start)||plan||{})[key];input.dataset.week=calendar.week_start;input.dataset.pref=key;
  }
  $('#week-notice').textContent=plan?(plan.warnings||[]).join(' '):'New plans cover '+dateLabel(calendar.today,0,{weekday:'long',day:'numeric',month:'short'})+' through Sunday.';
  $('#week-grid').innerHTML=plan?plan.days.map((day,i)=>mealCard(day,i,plan,false)).join(''):`<div class="empty"><h3>Your remaining week, ready to plan.</h3><p>${state.recipes.length?'Choose a generation option above. Your plan will be saved automatically.':'Add a dish in Dish List to start planning.'}</p>${!state.recipes.length?'<button data-action="go-dishes">Go to Dish List</button>':''}</div>`;
  const future=(state.plans||[]).filter(p=>p.week_start>calendar.week_start).sort((a,b)=>a.week_start.localeCompare(b.week_start));
  $('#future-weeks').innerHTML=future.length?'<h2 class="history-title">Upcoming weeks</h2>'+future.map(p=>`<details class="future-week" data-week="${esc(p.week_start)}" ${weekIsExpanded(p.week_start)?'open':''}><summary class="week-summary"><span class="past-arrow" aria-hidden="true">&#9656;</span><div class="grow"><h3>${weekRange(p.week_start)}</h3><p class="muted">${p.method==='manual'?'Plan ahead':esc(p.method==='AI'?'AI plan':p.method==='random'?'Randomized':'Simple Rotation')}</p></div><button data-action="delete-week" data-week="${esc(p.week_start)}" class="quiet danger" aria-label="Delete week ${esc(weekRange(p.week_start))}">Delete</button></summary>${p.week_start<=calendar.advance_limit?weekControls(p):''}<div class="meal-grid">${p.days.map((day,i)=>mealCard(day,i,p,false)).join('')}</div></details>`).join(''):'';
  const past=(state.plans||[]).filter(p=>p.week_start<calendar.week_start);
  $('#plan-history').innerHTML=past.length?'<h2 class="history-title">Past weeks</h2>'+past.map(p=>`<details class="past-week" data-week="${esc(p.week_start)}" ${weekIsExpanded(p.week_start)?'open':''}><summary class="week-summary"><span class="past-arrow" aria-hidden="true">&#9656;</span><div class="grow"><h3>${weekRange(p.week_start)}</h3><p class="muted">${esc(p.method==='AI'?'AI plan':p.method==='random'?'Randomized':p.method==='manual'?'Manual plan':'Simple Rotation')}</p></div><button data-action="delete-week" data-week="${esc(p.week_start)}" class="quiet danger" aria-label="Delete week ${esc(weekRange(p.week_start))}">Delete</button></summary><div class="meal-grid">${p.days.map((day,i)=>mealCard(day,i,p,true)).join('')}</div></details>`).join(''):'';
  $('#add-week').disabled=!availableWeeks().length;$('#add-week').title=availableWeeks().length?'Add one of the next eight weeks':'All eight upcoming weeks have been added.';
  setPlanBusy(!!planJobId||!!pendingPlanWeek);
}
async function openAddWeek(){
  const weeks=availableWeeks();if(!weeks.length){notify('All eight upcoming weeks have been added.');return;}
  $('#add-week-select').innerHTML=weeks.map(week=>`<option value="${week}">${weekRange(week)}</option>`).join('');$('#add-week-message').textContent='';open('#add-week-modal','#create-week');
}
async function createWeek(){
  if(weekMutationPending)return;weekMutationPending=true;++refreshVersion;
  const button=$('#create-week');button.disabled=true;
  try{
    const result=await api('/api/plan/add',{week_start:$('#add-week-select').value,timezone_offset:new Date().getTimezoneOffset()});
    ++refreshVersion;state.plans=[...(state.plans||[]).filter(plan=>plan.week_start!==result.plan.week_start),result.plan];
    renderWeek();closeDialog($('#add-week-modal'));notify('Week added with seven empty slots.');
  }
  catch(error){$('#add-week-message').textContent=error.message;}
  finally{weekMutationPending=false;button.disabled=false;if($('#add-week-modal').open)button.focus({preventScroll:true});}
}
function showMeal(week,index) {
  const plan=(state.plans||[]).find(p=>p.week_start===week);const day=plan?.days[index];if(!day)return;
  if(day.deleted){$('#dish-detail').innerHTML=`<article class="recipe-detail"><h2>Dish deleted</h2><p class="dish-meta">${dateLabel(day.date,0,{weekday:'long',day:'numeric',month:'short'})}</p></article>`;open('#detail-modal');return;}
  const past=week<state.calendar.week_start,r=mealRecipe(day,past);if(!r){notify('This dish is unavailable.',true);return;}
  const frozen=past||day.date<state.calendar.today||!state.recipes.some(item=>item.id===day.recipe_id);
  $('#dish-detail').innerHTML=(past?'<p class="footnote">Past week - saved dish details.</p>':'')+detailsMarkup(r,frozen)+`<h3>Preparation for ${dateLabel(day.date,0,{weekday:'long',day:'numeric',month:'short'})}</h3><p>${esc(day.prep)}</p><p>${day.timing==='night before'?'Cook the night before.':'Cook fresh in the morning.'} ${esc(day.reason)}</p>`;
  open('#detail-modal');
}
function runningPill(recipe) { return recipe.good_for_running ? '<span class="pill">Good for Running</span>' : ''; }
function blankRecipe() { return {name:'',vegan:null,good_for_running:false,servings:null,source_url:'',source_text:'',notes:'',measured_minutes:null,estimated_minutes:null,ingredients:[]}; }
function editRecipe(recipe) {
  if(busy){notify("Wait for the current task to finish.");return;}
  draft = structuredClone(recipe || blankRecipe());
  $('#recipe-title').textContent = recipe ? 'Edit recipe' : 'Create recipe';
  $('#recipe-message').textContent = ''; fillRecipe(); $('#import-box').open = !recipe; open('#recipe-modal');
}
let sourceLanguageVersion=0, languageTimer;
async function showSourceLanguage() {
  const version=++sourceLanguageVersion, value=$('#recipe-form').elements.source_text.value;
  if (!value.trim()) { $('#source-language').textContent='Source language is detected automatically: Vietnamese or English.'; return; }
  try {
    const result=await api('/api/source-language',{text:value});
    if (version===sourceLanguageVersion) $('#source-language').textContent=`Detected ${result.language==='vi'?'Vietnamese':'English'} source. Model: ${result.model}. Content stays in the source language.`;
  } catch { if(version===sourceLanguageVersion) $('#source-language').textContent='Language will be detected when extracting.'; }
}
function fillRecipe() {
  const f=$('#recipe-form');
  ['name','source_url','source_text','servings','measured_minutes','notes'].forEach(k=>f.elements[k].value=draft[k]??'');
  f.elements.vegan.value=draft.vegan===true?'yes':draft.vegan===false?'no':'unknown';
  f.elements.good_for_running.checked=!!draft.good_for_running;
  $('#time-estimate').textContent=draft.estimated_minutes ? `${draft.time_source==='source'?'Source cooking time':'Previous time estimate'}: ${draft.estimated_minutes} minutes` : 'Enter your measured cooking time, if known.';
  renderIngredients(); updateCalories(); showSourceLanguage(); showDishPhoto();
}
function readRecipe() {
  const f=$('#recipe-form'), num=value=>value===''?null:Number(value), recipe={...draft};
  ['name','source_url','source_text','notes'].forEach(k=>recipe[k]=f.elements[k].value.trim());
  recipe.servings=num(f.elements.servings.value); recipe.measured_minutes=num(f.elements.measured_minutes.value);
  recipe.vegan=f.elements.vegan.value==='yes'?true:f.elements.vegan.value==='no'?false:null;
  recipe.good_for_running=f.elements.good_for_running.checked;
  recipe.ingredients=$$('.ingredient-row').map((row,i)=>{
    const name=$('.name-input',row).value.trim(), prior=draft.ingredients[i], same=prior?.name===name, grams=num($('.grams-input',row).value);
    return {name,quantity:$('.quantity-input',row).value,grams,food_id:same?prior?.food_id||'':'',source_quote:same?prior?.source_quote||'':'',weight_estimated:!!(same&&prior?.weight_estimated&&prior.grams===grams)};
  });
  return recipe;
}
function renderIngredients() {
  $('#ingredient-list').innerHTML='<div class="ingredient-header"><span>Ingredient (raw / cooked)</span><span class="quantity-label">Original quantity</span><span>Weight (g)</span><span>kcal</span><span></span></div>'+draft.ingredients.map((item,i)=>`<div class="ingredient-row" data-ingredient="${i}"><input class="name-input" list="food-options" aria-label="Ingredient name" title="${esc(item.source_quote||'')}" value="${esc(item.name)}" placeholder="Food name" required><input class="quantity-input" aria-label="Original ingredient quantity" placeholder="e.g. 1 tbsp" value="${esc(item.quantity)}"><input class="grams-input" aria-label="Ingredient weight in grams" type="number" min="0" step="any" value="${item.grams??''}" placeholder="grams"><span class="ingredient-kcal"></span><button type="button" class="remove-ingredient" data-action="remove-ingredient" data-ingredient="${i}" aria-label="Remove ${esc(item.name||'ingredient')}">×</button></div>`).join('');
}
function updateCalories() {
  if(!draft) return;
  const recipe=readRecipe(); $('#calorie-summary').innerHTML=calorieMarkup(recipe);
  $$('.ingredient-row').forEach((row,i)=>{
    const ingredient=recipe.ingredients[i],food=foodFor(ingredient),value=food&&ingredient.grams!=null?fmt(ingredient.grams*food.kcal/100):'—';
    $('.ingredient-kcal',row).innerHTML=`${value}${ingredient.weight_estimated?'<small class="estimated-mark">~ weight</small>':''}`;
    $('.ingredient-kcal',row).title=food?`${food.kcal} kcal/100 g ${food.matched_name||food.name}`:'Unknown food. Look up or enter its calorie value.';
  });
}
const resolving = new Set();
async function resolveIngredient(name) {
  if (!name || foodFor({name}) || resolving.has(name)) return;
  resolving.add(name); $('#recipe-message').textContent = `Looking up ${name} online…`;
  try {
    const result = await api('/api/resolve',{names:[name]});
    for (const item of result.results) {
      if (item.food) { state.foods = state.foods.filter(f => f.id !== item.food.id).concat(item.food); $('#recipe-message').textContent = item.food.note; }
      else $('#recipe-message').textContent = item.error;
    }
    updateCalories();
  } catch (e) { $('#recipe-message').textContent = e.message; }
  finally { resolving.delete(name); }
}
function detailsMarkup(r, frozen=false) {
  return `<article class="recipe-detail"><div class="detail-heading"><h2>${esc(r.name)}</h2>${frozen?'':`<button data-action="edit" data-id="${esc(r.id)}" class="secondary">Edit</button>`}</div>${r.photo_url?`<img class="detail-dish-photo" src="${esc(r.photo_url)}" alt="${esc(r.name)}" width="500" height="500">`:''}<div class="badge-row">${veganPill(r.vegan)}${runningPill(r)}<span class="dish-meta">${r.servings?esc(r.servings)+' servings':'Servings not stated'} · ${esc(timeLabel(r))}</span></div>${r.source_url?`<p class="source-link"><a href="${safeLink(r.source_url)}" target="_blank" rel="noopener noreferrer">View original source ↗</a></p>`:''}<div class="calorie-summary">${calorieMarkup(r,frozen)}</div><section class="detail-ingredients"><h3>Ingredients</h3><ul>${r.ingredients.map(item=>`<li>${esc(item.name)} — ${esc(item.quantity||'')}${item.quantity&&item.grams!=null?' · ':''}${item.grams!=null?`${item.weight_estimated?'~':''}${item.grams} g`:' (weight unknown)'}${item.weight_estimated?' (estimated)':''}</li>`).join('')}</ul></section>${r.ingredients.some(i=>i.source_quote)?`<details><summary>Source evidence</summary><ul>${r.ingredients.filter(i=>i.source_quote).map(i=>`<li>${esc(i.source_quote)}</li>`).join('')}</ul></details>`:''}${r.notes?`<h3 class="compact">Notes</h3><p class="inline-message">${esc(r.notes)}</p>`:''}</article>`;
}
function showDetails(id) { const r = state.recipes.find(r => r.id === id); if (!r) return; $('#dish-detail').innerHTML = detailsMarkup(r); open('#detail-modal'); }
let planJobId=null,planJobWeek=null,pendingPlanWeek=null,planPollTimer=null, planPollPending=false, requestedPlanCancel=false;
function jobWeek(){return planJobWeek||pendingPlanWeek||state.calendar?.week_start;}
function planProgress(message){$$('[data-plan-progress]').forEach(node=>{if(node.dataset.week===jobWeek())node.textContent=message;});}
function cancelDisabled(value){$$('[data-action=cancel-plan]').forEach(button=>button.disabled=value);}
function setPlanBusy(active) {
  $$('.plan-control').forEach(button=>button.disabled=active);
  $$('input[data-pref],[data-action=edit-meal],[data-action=delete-week]').forEach(node=>node.disabled=active&&node.dataset.week===jobWeek());
  $$('[data-action=cancel-plan],[data-plan-progress]').forEach(node=>{node.hidden=!(active&&node.dataset.week===jobWeek());});
}
async function finishPlanJob(job) {
  if(job.status==='running'||job.status==='cancelling')return;
  clearInterval(planPollTimer);planPollTimer=null;planJobId=null;planJobWeek=null;pendingPlanWeek=null;setPlanBusy(false);cancelDisabled(false);
  await refresh();
  if(job.status==='completed')notify('Your weekly plan is saved.');
  else if(job.status==='cancelled')notify('AI generation cancelled. Your saved plan was kept.');
  else notify(job.error||'Generation failed. Your saved plan was kept.',true);
}
async function pollPlanJob() {
  if(!planJobId||planPollPending)return;planPollPending=true;
  try {
    const job=await api('/api/plan/job?id='+encodeURIComponent(planJobId));
    if(job.status==='cancelling')planProgress('Stopping AI generation...');
    if(job.status==='running'){
      const progress=await api('/api/ai-progress');
      planProgress(`${progress.phase==='loading'?'Loading the model or reading recipes':'Generating your meal plan'} - ${Math.floor(progress.elapsed_seconds/60)}m ${progress.elapsed_seconds%60}s. Your saved plan stays visible.`);
    }
    await finishPlanJob(job);
  } catch(error){planProgress('Connection interrupted. Use Cancel to stop the task, or reload to reconnect.');}
  finally {planPollPending=false;}
}
function followPlanJob(job) {
  planJobId=job.id;planJobWeek=job.week_start;pendingPlanWeek=null;setPlanBusy(true);cancelDisabled(job.status==='cancelling');planProgress(job.status==='cancelling'?'Stopping AI generation...':'Starting local AI generation...');
  clearInterval(planPollTimer);planPollTimer=setInterval(pollPlanJob,800);pollPlanJob();
}
async function generate(method='AI',week=state.calendar.week_start) {
  if(planJobId||busy){notify('Wait for the current task or cancel AI generation.');return;}
  const panel=week===state.calendar.week_start?$('#weekly>.weekly-controls'):$(`.future-controls[data-week="${week}"]`);
  const data={method,week_start:week,timezone_offset:new Date().getTimezoneOffset(),vegan_only:$('input[data-pref=vegan_only]',panel).checked,prioritize_running:$('input[data-pref=prioritize_running]',panel).checked};
  if(method==='AI'){
    requestedPlanCancel=false;pendingPlanWeek=week;setPlanBusy(true);
    try {const job=await api('/api/plan/start',data);if(job.status==='completed'){await refresh();setPlanBusy(false);}else {followPlanJob(job);if(requestedPlanCancel)await cancelPlan();}}
    catch(error){pendingPlanWeek=null;setPlanBusy(false);notify(error.message,true);}
  }else await work('Saving your remaining week',async()=>{await api('/api/plan/generate',data);await refresh();notify('Your weekly plan is saved.');});
}
async function cancelPlan() {
  if(!planJobId){requestedPlanCancel=true;cancelDisabled(true);planProgress('Stopping AI generation...');return;}cancelDisabled(true);planProgress('Stopping AI generation...');
  try {const job=await api('/api/plan/cancel',{id:planJobId});await finishPlanJob(job);}
  catch(error){cancelDisabled(false);notify(error.message,true);}
}
let replacement=null,weekMutationPending=false;
function openReplacement(week,index) {
  const day=(state.plans||[]).find(p=>p.week_start===week)?.days[index];if(!day)return;
  replacement={week,date:day.date,recipeId:null,empty:!day.recipe_id};$('#replace-search').value='';$('#replace-title').textContent=replacement.empty?'Add dish':'Replace meal';$('#replace-confirm').textContent=replacement.empty?'Add':'Replace';
  $('#replace-date').textContent=dateLabel(day.date,0,{weekday:'long',day:'numeric',month:'long',year:'numeric'});
  $('#replace-message').textContent=replacement.empty?'Select a saved dish, review its details, then click Add.':'Select any saved dish, review its details, then click Replace.';
  $('#replacement-details').hidden=true;$('#replacement-details').innerHTML='';$('#replace-modal').classList.remove('expanded');$('#replace-confirm').disabled=true;
  renderReplacementList();open('#replace-modal');
}
function renderReplacementList() {
  const query=normalize($('#replace-search').value),dishes=state.recipes.filter(r=>normalize(r.name).includes(query));
  $('#replace-list').innerHTML=dishes.map(r=>`<button class="replacement-choice${replacement?.recipeId===r.id?' selected':''}" data-action="choose-replacement" data-id="${esc(r.id)}" aria-pressed="${replacement?.recipeId===r.id}">${r.photo_url?`<img src="${esc(r.photo_url)}" alt="" width="64" height="64" loading="lazy">`:''}<span><strong>${esc(r.name)}</strong><small>${esc(timeLabel(r))} - ${fmt(calculate(r).per_serving)} kcal / serving</small></span></button>`).join('')||`<p class="muted">${state.recipes.length?'No matching dishes.':'No saved dishes yet. Create a recipe in Dish List first.'}</p>`;
}
function selectReplacement(id) {
  if(weekMutationPending)return;const recipe=state.recipes.find(r=>r.id===id);if(!replacement||!recipe)return;
  replacement.recipeId=id;renderReplacementList();$('#replace-modal').classList.add('expanded');
  $('#replacement-details').hidden=false;$('#replacement-details').innerHTML=detailsMarkup(recipe,true);$('#replace-confirm').disabled=false;
  $('#replacement-details').scrollTop=0;
  if(innerWidth<800){const dialog=$('#replace-modal');dialog.scrollTo({top:Math.max(0,$('#replacement-details').offsetTop-dialog.clientHeight/3),behavior:'instant'});}
}
async function replaceMeal() {
  if(!replacement?.recipeId||weekMutationPending)return;weekMutationPending=true;$('#replace-confirm').disabled=true;
  const target={...replacement};$('#replace-message').textContent='Saving replacement...';
  try {
    await api('/api/plan/replace',{week_start:target.week,date:target.date,recipe_id:target.recipeId});
    await refresh();closeDialog($('#replace-modal'));replacement=null;notify(target.empty?'Dish added.':'Meal replaced.');
  }catch(error){$('#replace-message').textContent=error.message;$('#replace-confirm').disabled=false;}
  finally {weekMutationPending=false;}
}
async function deleteWeek(button) {
  if(weekMutationPending||busy)return;
  const week=button.dataset.week;weekMutationPending=true;
  if(!confirm(`Delete the meal plan for ${weekRange(week)}? Your saved dishes will be kept.`)){weekMutationPending=false;return;}
  button.disabled=true;++refreshVersion;
  try {
    await api('/api/plan/delete',{week_start:week});
    // The server has durably committed before acknowledging this response.
    // Remove only that week; a full state reload adds an avoidable storage trip.
    ++refreshVersion;state.plans=(state.plans||[]).filter(plan=>plan.week_start!==week);
    if(state.plan?.week_start===week)state.plan=null;
    renderWeek();notify('Weekly plan deleted. Your saved recipes were kept.');
  }
  catch(error){button.disabled=false;notify(error.message,true);}
  finally {weekMutationPending=false;}
}
async function uploadDishPhoto(file) {
  if(!file)return;
  $('#dish-photo-message').textContent='';
  try {
    if(!/\.(jpe?g|png)$/i.test(file.name))throw new Error('Choose a .jpg, .jpeg, or .png dish photo.');
    if(file.size>20000000)throw new Error('Choose a dish photo under 20 MB.');
    const bytes=new Uint8Array(await file.slice(0,12).arrayBuffer());
    if(!(bytes[0]===255&&bytes[1]===216&&bytes[2]===255)&&!(bytes[0]===137&&bytes[1]===80&&bytes[2]===78&&bytes[3]===71))throw new Error('The file must contain a real JPEG or PNG image.');
    await work('Cropping and resizing your dish photo',async()=>{
      const image=await createImageBitmap(file,{imageOrientation:'from-image'});
      const side=Math.min(image.width,image.height),canvas=document.createElement('canvas');canvas.width=500;canvas.height=500;
      canvas.getContext('2d').drawImage(image,(image.width-side)/2,(image.height-side)/2,side,side,0,0,500,500);image.close();
      const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(blob);});
      const result=await api('/api/dish-photo',{filename:file.name,image:data});draft=readRecipe();draft.photo_url=result.photo_url;showDishPhoto();$('#dish-photo-message').textContent='Photo ready: 500 by 500 pixels.';
    });
  }catch(error){$('#dish-photo-message').textContent=error.message;notify(error.message,true);}
  finally {$('#dish-photo-upload').value='';}
}
function showDishPhoto() {const url=draft?.photo_url||'';$('#dish-photo-preview').hidden=!url;if(url)$('#dish-photo-preview').src=url;else $('#dish-photo-preview').removeAttribute('src');$('#remove-dish-photo').hidden=!url;}
function renderFoods() {
  const query = normalize($('#food-search').value);
  const foods = state.foods.filter(f => normalize(f.name+' '+(f.vi_name || '')+' '+(f.aliases || []).join(' ')).includes(query)).sort((a,b) => a.name.localeCompare(b.name));
  $('#foods-table').innerHTML = foods.map(f => `<tr><td>${esc(f.name)}<small>${esc((f.aliases || []).join(', '))}</small></td><td>${esc(f.kcal)}</td><td>${f.origin === 'manual' ? 'Your value' : f.origin === 'online' ? `<a href="${safeLink(f.source)}" target="_blank" rel="noopener noreferrer">USDA match ↗</a>` : 'Approximate starter value'}<small>${esc(f.note)}</small></td><td><button class="secondary" data-action="edit-food" data-id="${esc(f.id)}">Edit</button></td></tr>`).join('');
}
function editFood(id) {
  const food = state.foods.find(f => f.id === id); if (!food) return;
  const f = $('#food-form'); ['id','name','kcal','note'].forEach(k => f.elements[k].value = food[k] ?? ''); f.elements.aliases.value = (food.aliases || []).join(', '); f.elements.name.focus();
}
async function loadPhoto(file) {
  if (!file) return;
  if (file.size > 20_000_000) { notify('Choose an image smaller than 20 MB.',true); return; }
  try {
    const bitmap = await createImageBitmap(file), canvas = document.createElement('canvas');
    const scale = Math.min(1,448/Math.max(bitmap.width,bitmap.height)); canvas.width = Math.round(bitmap.width*scale); canvas.height = Math.round(bitmap.height*scale);
    canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height); bitmap.close();
    const data = canvas.toDataURL('image/jpeg',.82); photo = data.split(',')[1]; $('#photo-preview').src = data; $('#photo-preview').hidden = false; $('#identify-button').disabled = false;
    $('#identification').textContent = 'Photo ready. Identify the food, then confirm or correct its name.';
    $('#nutrition-results').innerHTML = ''; $('#nutrition-detail').innerHTML = ''; $('#nutrition-guidance').innerHTML = '';
  } catch { notify('Could not read this photo. Use a JPEG, PNG, or WebP image.',true); }
}
function stopCamera() {
  cameraStream?.getTracks().forEach(track => track.stop()); cameraStream = null;
  $('#camera-feed').srcObject = null; $('#camera-area').hidden = true;
}
async function startCamera() {
  stopCamera();
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'},width:{ideal:640},height:{ideal:480}},audio:false});
    if (!$('#photo-modal').open) { stopCamera(); return; }
    $('#camera-feed').srcObject = cameraStream; $('#camera-area').hidden = false; await $('#camera-feed').play();
  } catch { stopCamera(); $('#identification').textContent = 'Camera access is unavailable. Allow camera access in your browser, or upload a photo instead.'; }
}
async function captureCamera() {
  const video = $('#camera-feed'); if (!video.videoWidth) return;
  const canvas = document.createElement('canvas'); const scale = Math.min(1,448/Math.max(video.videoWidth,video.videoHeight));
  canvas.width = Math.round(video.videoWidth*scale); canvas.height = Math.round(video.videoHeight*scale);
  canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);
  const blob = await new Promise(resolve => canvas.toBlob(resolve,'image/jpeg',.82));
  stopCamera(); if (blob) await loadPhoto(blob);
}
async function nutritionSearch() {
  const query = $('#nutrition-query').value.trim();
  if (!query) { notify('Enter or confirm a food name first.',true); return; }
  await work('Retrieving nutrition from USDA', async () => {
    const result = await api('/api/nutrition/search',{query}); matches = result.matches;
    $('#nutrition-detail').innerHTML = ''; $('#nutrition-guidance').innerHTML = '';
    $('#nutrition-results').innerHTML = matches.length ? `<p class="muted">Choose the closest food and preparation state. All values are per 100 g.</p>${matches.map((m,i) => `<div class="nutrition-match"><span>${esc(m.name)}<br><small>${fmt(m.kcal)} kcal / 100 g</small></span><button class="secondary" data-action="choose-nutrition" data-index="${i}">Use this match</button></div>`).join('')}` : '<p>No USDA match found. Try another English name or a single ingredient.</p>';
  });
}
async function chooseNutrition(index) {
  const match = matches[index]; if (!match) return;
  $('#nutrition-results').innerHTML = '';
  const micronutrients = match.nutrients.filter(n => /vitamin|calcium|iron|magnesium|phosphorus|potassium|sodium|zinc|copper|manganese|selenium|thiamin|riboflavin|niacin|folate|choline/i.test(n.name));
  $('#nutrition-detail').innerHTML = `<div class="panel"><h3>${esc(match.name)}</h3><p><strong>${fmt(match.kcal)} kcal / 100 g</strong> · <a href="${safeLink(match.source)}" target="_blank" rel="noopener noreferrer">USDA source ↗</a></p><p class="footnote">Reference food estimate; your photographed food may differ. Retrieved ${esc(new Date(match.retrieved_at).toLocaleDateString())}.</p><h3>Vitamins & minerals per 100 g</h3>${micronutrients.length ? `<div class="table-scroll"><table><thead><tr><th>Nutrient</th><th>Amount / 100 g</th></tr></thead><tbody>${micronutrients.map(n => `<tr><td>${esc(n.name)}</td><td>${esc(n.amount)} ${esc(n.unit)}</td></tr>`).join('')}</tbody></table></div>` : '<p>Micronutrient data was not available for this record.</p>'}</div>`;
  $('#nutrition-guidance').innerHTML = '<p class="inline-message">Fetching primary-source guidance online…</p>';
  await work('Reading online daily-amount and precaution guidance', async () => {
    const data = await api('/api/nutrition/guidance',{query:$('#nutrition-query').value.trim()});
    $('#nutrition-guidance').innerHTML = `<h3>Daily amounts & precautions</h3><p class="footnote">These quotations come from retrieved sources. General information, not a personal diet prescription. Food-group targets do not specify how much of this single food you should eat.</p>${data.items.map(item => `<div class="guidance-item"><span class="pill neutral">${esc(item.kind)}</span><blockquote>“${esc(item.quote)}”</blockquote><a href="${safeLink(item.url)}" target="_blank" rel="noopener noreferrer">Read full source and context ↗</a></div>`).join('')}${!data.items.some(i => i.kind === 'daily amount') ? '<p class="inline-message">No verified daily amount was found for this specific food.</p>' : ''}${!data.items.some(i => i.kind === 'precaution') ? '<p class="inline-message">Food-specific precautions could not be verified. This does not establish that the food is safe for everyone.</p>' : ''}${data.warnings.map(w => `<p class="inline-message">${esc(w)}</p>`).join('')}<details><summary>Retrieved online references</summary>${data.sources.map(s => `<div class="guidance-source"><a href="${safeLink(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.url)}</a><p>${esc(s.text.slice(0,600))}…</p></div>`).join('') || '<p>No sources could be retrieved.</p>'}</details>`;
  });
}

document.addEventListener('click', async event => {
  const button = event.target.closest('button,[data-action=meal-details]'); if (!button) return;
  if (button.hasAttribute('data-close')) {if((busy&&button.closest('#recipe-modal'))||(weekMutationPending&&button.closest('#replace-modal'))){notify('Wait for the current task to finish.');return;} closeDialog(button.closest('dialog')); return; }
  if (button.dataset.tab) { tab(button.dataset.tab); return; }
  const action = button.dataset.action, id = button.dataset.id;
  if (!action) return;
  if (action === 'new') editRecipe();
  if (action === 'go-dishes') tab('dishes');
  if (action === 'edit') { closeDialog($('#detail-modal')); editRecipe(state.recipes.find(r => r.id === id)); }
  if (action === 'details') showDetails(id);
  if (action === 'delete') {
    const recipe = state.recipes.find(r => r.id === id);
    if (!recipe)return;
    if (confirm(`Delete “${recipe.name}”? This will mark the dish as “Dish deleted” everywhere it appears in Weekly Meals, including past, current, and upcoming weeks.`)) await work('Deleting recipe',async () => { await api('/api/recipes/delete',{id}); await refresh(); notify('Recipe deleted.'); });
  }
  if (action === 'retrieve') await work(state.hosted?'Retrieving source; if the host is blocked, paste a transcript or upload a file you are allowed to use':'Retrieving source; videos without captions are downloaded as audio and transcribed locally. This can take several minutes',async () => {
    const f = $('#recipe-form'); const source = await api('/api/extract',{url:f.elements.source_url.value,kind:$('#source-kind').value});
    f.elements.source_text.value = source.text; showSourceLanguage(); $('#recipe-message').textContent = `Retrieved ${source.kind}. ${source.method === 'audio' ? 'Speech recognition can mishear ingredient names and amounts. ' : ''}Review the text, then click Extract recipe with AI.`;
  });
  if (action === 'import') await work('Extracting the dish and ingredient list',async () => {
    const f = $('#recipe-form'), result = await api('/api/import',{text:f.elements.source_text.value,url:f.elements.source_url.value});
    const previous=readRecipe(); draft = {...result.recipe,id:draft.id,good_for_running:previous.good_for_running,photo_url:previous.photo_url,measured_minutes:previous.measured_minutes}; await refresh(); fillRecipe();
    const warnings = result.resolved.filter(r => r.error).map(r => `${r.name}: ${r.error}`);
    $('#recipe-message').textContent = 'Ingredients extracted in the source language. Review quantities, servings, and estimated weights before saving. '+(result.recipe.extraction_warnings||[]).join(' ') + (warnings.length ? '\n'+warnings.join('\n') : '');
  });
  if(action==='add-ingredient'||action==='remove-ingredient') {
    draft=readRecipe();
    if(action==='add-ingredient') draft.ingredients.push({name:'',quantity:'',grams:null,weight_estimated:false});
    else draft.ingredients.splice(Number(button.dataset.ingredient),1);
    renderIngredients(); updateCalories();
  }
  if (action === 'generate') await generate('AI',button.dataset.week||state.calendar.week_start);
  if(action==='randomize')await generate('random',button.dataset.week||state.calendar.week_start);
  if(action==='cancel-plan')await cancelPlan();
  if(action==='meal-details')showMeal(button.dataset.week,Number(button.dataset.day));
  if(action==='edit-meal')openReplacement(button.dataset.week,Number(button.dataset.day));
  if(action==='choose-replacement')selectReplacement(button.dataset.id);
  if(action==='replace-meal')await replaceMeal();
  if(action==='delete-week'){event.preventDefault();await deleteWeek(button);}
  if(action==='add-week')await openAddWeek();
  if(action==='create-week')await createWeek();
  if(action==='export-backup')await exportBackup();
  if(action==='import-backup')$('#backup-file').click();
  if(action==='remove-dish-photo'){draft=readRecipe();draft.photo_url='';showDishPhoto();}
  if (action === 'rotation') await generate('rotation',button.dataset.week||state.calendar.week_start);
  if (action === 'foods') { renderFoods(); open('#foods-modal'); }
  if (action === 'edit-food') editFood(id);
  if (action === 'clear-food') $('#food-form').reset();
  if (action === 'photo') open('#photo-modal');
  if (action === 'start-camera') await startCamera();
  if (action === 'stop-camera') stopCamera();
  if (action === 'capture-camera') await captureCamera();
  if (action === 'identify') await work(state.hosted?'Identifying the food with hosted AI':'Identifying the food locally',async () => { const result = await api('/api/nutrition/identify',{image:photo}); $('#nutrition-query').value = result.name; $('#identification').textContent = `${result.name} · ${result.confidence} confidence. ${result.explanation}\nConfirm or correct the name below before looking it up.`; });
  if (action === 'nutrition-search') await nutritionSearch();
  if (action === 'choose-nutrition') await chooseNutrition(Number(button.dataset.index));
  if (action === 'settings') { const f = $('#settings-form'); ['text_model','vision_model','vietnamese_model','english_model'].forEach(k => f.elements[k].value = state.settings[k]); f.elements.usda_api_key.value = ''; open('#settings-modal'); await checkStatus(); }
  if (action === 'export') await exportBackup();
});
$('#recipe-form').addEventListener('submit',async event => {
  event.preventDefault();
  await work('Saving recipe and checking calorie values',async () => {
    ++refreshVersion;const result=await api('/api/recipes',readRecipe());++refreshVersion;
    const recipe={...result.recipe,calories:result.calories},index=state.recipes.findIndex(item=>item.id===recipe.id);
    if(index<0)state.recipes.push(recipe);else state.recipes[index]=recipe;
    if(result.foods)state.foods=result.foods;
    $('#dish-count').textContent=state.recipes.length;$('#food-count').textContent=`${state.foods.length} food references`;
    $('#food-options').innerHTML=state.foods.map(food=>`<option value="${esc(food.name)}">${esc(food.name)} · ${esc(food.aliases?.join(', '))}</option>`).join('');
    renderDishes();renderWeek();closeDialog($('#recipe-modal'));tab('dishes');
    notify(result.warnings.length?'Recipe saved. Some estimates are unavailable: '+result.warnings.join(' '):'Recipe saved.');
  },{pollAI:false});
});
$('#recipe-form').addEventListener('input',event=>{updateCalories();if(event.target.name==='source_text'){clearTimeout(languageTimer);languageTimer=setTimeout(showSourceLanguage,250);}});
$('#recipe-form').addEventListener('change',event => { if (event.target.matches('.name-input')) resolveIngredient(event.target.value.trim()); });
document.addEventListener('change',async event=>{
  const input=event.target;
  if(!input.matches('[data-running-id]')) return;
  const value=input.checked;
  try { await api('/api/recipes/running',{id:input.dataset.runningId,good_for_running:value});await refresh(); }
  catch(error){input.checked=!value;notify(error.message,true);}
});
$('#food-form').addEventListener('submit',async event => {
  event.preventDefault(); const f = event.target;
  await work('Saving your food value',async () => { await api('/api/foods',{id:f.elements.id.value,name:f.elements.name.value,kcal:Number(f.elements.kcal.value),aliases:f.elements.aliases.value.split(',').map(s => s.trim()).filter(Boolean),note:f.elements.note.value}); await refresh(); renderFoods(); if ($('#recipe-modal').open) updateCalories(); f.reset(); notify('Food value saved. Recipe calories have been updated.'); });
});
$('#settings-form').addEventListener('submit',async event => {
  event.preventDefault(); const data = Object.fromEntries(new FormData(event.target));
  await work('Saving local settings',async () => { await api('/api/settings',data); await refresh(); await checkStatus(); closeDialog($('#settings-modal')); notify('Settings saved.'); });
});
$('#dish-search').addEventListener('input',renderDishes); $('#vegan-filter').addEventListener('change',renderDishes); $('#running-filter').addEventListener('change',renderDishes); $('#food-search').addEventListener('input',renderFoods);
$('#dish-photo-upload').addEventListener('change',e=>uploadDishPhoto(e.target.files[0]));
$('#photo-upload').addEventListener('change',e => loadPhoto(e.target.files[0])); $('#photo-camera').addEventListener('change',e => loadPhoto(e.target.files[0]));
$('#photo-modal').addEventListener('close',stopCamera);
$$('[role=tab]').forEach((button,index,buttons) => button.addEventListener('keydown',event => { if (['ArrowLeft','ArrowRight'].includes(event.key)) { event.preventDefault(); const next = buttons[(index+1)%buttons.length]; next.focus(); tab(next.dataset.tab); } }));
$('#recipe-modal').addEventListener('cancel',event => { if (busy) { event.preventDefault(); notify('Wait for the current task to finish before closing.'); } });
$('#import-box .import-content').insertAdjacentHTML('beforeend','<label class="upload-label">Upload a transcript or audio/video file you are allowed to use<input id="transcript-upload" type="file" accept=".txt,.mp3,.mp4,.mpeg,.mpga,.m4a,.ogg,.wav,.webm,.flac"></label>');
$('#transcript-upload').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;
  try{
    if(file.size>24*1024*1024)throw new Error('Choose a file under 24 MB. Hosted audio is limited to ten minutes.');
    await work('Reading your file and preparing its transcript',async()=>{
      let text;
      if(/\.txt$/i.test(file.name))text=await file.text();
      else{
        const response=await fetch('/api/transcribe',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-App-Token':token,'X-File-Name':file.name.replace(/[^a-zA-Z0-9._-]/g,'_')},body:file});
        const data=await response.json();if(!response.ok)throw new Error(data.error||'Could not transcribe this file. Paste a transcript instead.');text=data.text;
      }
      $('#recipe-form').elements.source_text.value=text;showSourceLanguage();$('#recipe-message').textContent='Transcript loaded in its original language. Review it, then click Extract recipe with AI.';
    });
  }catch(error){notify(error.message,true);}
  finally{event.target.value='';}
});
(async () => {
  try {
    await refresh();
    $('#plan-vegan').checked=!!state.plan?.vegan_only;$('#plan-running').checked=!!state.plan?.prioritize_running;
    if(state.plan_job)followPlanJob(state.plan_job);
    await checkStatus();
    setInterval(async()=>{if(!busy&&!planJobId&&!weekMutationPending){try{await refresh();}catch{}}},60000);
  } catch (error) { notify('Could not load the app: '+error.message,true); }
})();

window.addEventListener('focus',async()=>{if(!busy&&!planJobId&&!weekMutationPending){try{await refresh();}catch{}}});

$('#replace-search').addEventListener('input',renderReplacementList);
$('#replace-modal').addEventListener('cancel',event=>{if(weekMutationPending)event.preventDefault();});

document.addEventListener('change',event=>{const input=event.target;if(!input.matches('input[data-pref]'))return;const week=input.dataset.week,panel=input.closest('.weekly-controls');weekPreferences.set(week,{vegan_only:$('input[data-pref=vegan_only]',panel).checked,prioritize_running:$('input[data-pref=prioritize_running]',panel).checked});});
function rememberWeekExpansion(event){
  const node=event.target,week=node.dataset.week;
  if(!node.isConnected||!node.matches('details[data-week]')||week===state.calendar?.week_start||node.open===weekIsExpanded(week))return;
  const expanded=node.open;weekExpansion.set(week,expanded);
  expansionSaves=expansionSaves.then(()=>api('/api/plan/expansion',{week_start:week,expanded})).then(()=>{state.week_expansion={...(state.week_expansion||{}),[week]:expanded};}).catch(error=>notify(error.message,true));
}
$('#plan-history').addEventListener('toggle',rememberWeekExpansion,true);
$('#future-weeks').addEventListener('toggle',rememberWeekExpansion,true);
function clearBackupFeedback(){$$('#settings-modal [data-persist=true]').forEach(node=>node.remove());$('#backup-message').textContent='';}
async function exportBackup(){clearBackupFeedback();await work('Exporting full ZIP backup',async()=>{await expansionSaves;const response=await fetch('/api/backup/export');if(!response.ok){const data=await response.json();throw new Error(data.error||'Backup export failed.');}const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;link.download='meal-notebook-backup.zip';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);$('#backup-message').textContent='Backup exported.';});}
$('#backup-file').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;clearBackupFeedback();
  try{
    if(!/\.zip$/i.test(file.name))throw new Error('Choose a ZIP backup.');
    if(file.size>256*1024*1024)throw new Error('Choose a ZIP backup under 256 MB.');
    if(!confirm('Importing this backup will replace all current data with the contents of the backup file. Do you want to continue?'))return;
    await expansionSaves;
    await work('Validating and restoring backup',async()=>{
      const response=await fetch('/api/backup/import',{method:'POST',headers:{'Content-Type':'application/zip','X-App-Token':token},body:file});const data=await response.json();
      if(!response.ok){$('#backup-message').textContent=data.error;throw new Error(data.error||'Backup import failed. Existing data was kept.');}
      weekPreferences.clear();weekExpansion.clear();$('#plan-vegan').dataset.week='';$('#plan-running').dataset.week='';
      await refresh();await checkStatus();closeDialog($('#settings-modal'));notify('Backup restored: recipes, weeks, images and settings.');
    });
  }catch(error){$('#backup-message').textContent=error.message;notify(error.message,true);}
  finally{event.target.value='';}
});
