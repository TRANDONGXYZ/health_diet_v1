const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const context={location:{hostname:'test'},window:{addEventListener(){}},structuredClone};
vm.runInNewContext(fs.readFileSync('public/local-store.js','utf8')+';globalThis.testStore=HealthLocal;',context);const S=context.testStore;
for(const [text,want] of [['500 g','300 g'],['2 cups','1.2 cups'],['salt to taste','salt to taste'],['1/2 cup','0.3 cup'],['1 1/2 cups','0.9 cups'],['½ cup','0.3 cup'],['1–2 cups','0.6–1.2 cups'],['2 x 400 g','1.2 x 400 g'],['1,000 g','600 g'],['1,5 l','0.9 l'],['2 muỗng canh','1.2 muỗng canh'],['muối vừa ăn','muối vừa ăn'],['a handful','a handful']])assert.equal(S.scaleQuantity(text,.6),want,text);
const original={name:'Rice',servings:5,ingredients:[{name:'rice',quantity:'500 g',grams:500},{name:'stock',quantity:'2 cups',grams:480},{name:'salt',quantity:'to taste',grams:null}]},before=JSON.stringify(original),plan=S.makeServingPlan(original,[],3);
assert.equal(plan.ingredients[0].grams,300);assert.equal(plan.ingredients[1].quantity,'1.2 cups');assert.equal(plan.ingredients[2].quantity,'to taste');assert.equal(JSON.stringify(original),before);S.validateServingPlan(plan);
const shown=S.plannedRecipe(original,plan);assert.equal(shown.servings,3);assert.equal(S.plannedRecipe(shown,plan).ingredients[0].grams,300);assert.equal(JSON.stringify(original),before);
const measured=S.makeServingPlan(original,[{id:'rice',name:'rice',kcal:100},{id:'stock',name:'stock',kcal:10}],3);assert.equal(measured.calories.total,328.8);assert.equal(measured.calories.per_serving,109.6);
const precise={...original,servings:null,ingredients:[{name:'rice',grams:1.23456789012345}]};assert.equal(S.makeServingPlan(precise,[],3).ingredients[0].grams,precise.ingredients[0].grams);
for(const [yieldValue,household] of [[null,3],[5,null],[0,3],[5,0]]){const next=S.makeServingPlan({...original,servings:yieldValue},[],household);assert.equal(next.ingredients[0].quantity,'500 g');assert.equal(next.ingredients[0].grams,500);}
for(const value of [0,-1,1.5,Infinity,NaN,'3',true])assert.equal(S.validHousehold(value),false);assert.equal(S.validHousehold(3),true);
assert.throws(()=>S.validateServingPlan({...plan,servings:0}));assert.throws(()=>S.validateServingPlan({...plan,ingredients:[{name:'rice',grams:-1}]}));
console.log('PASS serving arithmetic, fractions/ranges/units, package sizes, unmeasured quantities, missing yields/defaults, validation, original preservation and repeated preview without double scaling.');
