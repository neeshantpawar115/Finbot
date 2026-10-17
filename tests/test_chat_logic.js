const fs=require('fs'),assert=require('assert');
const html=fs.readFileSync('index.html','utf8');
const block=html.split('// ==FINLOGIC==')[1].split('// ==/FINLOGIC==')[0];
// syntax check of the whole script too
new Function(html.split('<script>')[1].split('</script>')[0].replace(/applyTheme\(\);go\('home'\);\s*$/,''));
const mem={};const store={get:(k,d)=>d,set(){}};
const S={convos:[],cur:null,page:'assistant'};let saves=0,refreshes=0;
const save=()=>saves++,refresh=()=>refreshes++,$=()=>null,go=()=>{},scrollEnd=()=>{};
const api=new Function('S','store','save','refresh','$','go','scrollEnd','fetch',block+';return{TOOLS,demo,md,hist,send,run,errMsg,findAmount,cur,prov};');
let fetchImpl;const A=api(S,store,save,refresh,$,go,scrollEnd,(...a)=>fetchImpl(...a));
const t=(n,f)=>Promise.resolve().then(f).then(()=>console.log('ok  ',n),e=>{console.log('FAIL',n,e.message);process.exitCode=1});
(async()=>{
await t('amount parsing',()=>{assert.equal(A.findAmount('₹5 lakh loan'),5e5);assert.equal(A.findAmount('2.5 crore'),2.5e7);assert.equal(A.findAmount('Rs 5,00,000'),5e5);assert.equal(A.findAmount('50k'),5e4)});
await t('emi demo uses tool + labels assumptions',()=>{const r=A.demo('Calculate the EMI on a ₹5 lakh loan.');assert(r.text.includes('₹10,623.52'));assert(r.text.includes('Assumptions'));assert.deepEqual(r.used,['emi'])});
await t('emi with explicit inputs has no assumptions',()=>{const r=A.demo('EMI on 5 lakh at 10% for 5 years');assert(r.text.includes('10,623.52')&&!r.text.includes('Assumptions'))});
await t('compound demo',()=>{const r=A.demo('Explain compound interest with an example.');assert(r.text.includes('₹2,21,964.02')&&r.text.includes('guaranteed'))});
await t('sip demo',()=>{const r=A.demo('SIP of 10000 monthly at 12% for 10 years');assert(r.used[0]==='compound'&&r.text.includes('₹12,00,000'))});
await t('expenses demo',()=>{const r=A.demo('Analyse my monthly expenses\nRent 15000\nFood 5000\nTransport 2,500');assert(r.text.includes('₹22,500')&&r.text.includes('Rent'))});
await t('expenses without data asks for input',()=>{assert(A.demo('Analyse my monthly expenses.').text.includes('one per line'))});
await t('market question refuses to invent data',()=>{const r=A.demo('Compare the historical performance of two stocks.');assert(r.text.includes("isn't available")&&!/\d{2,}%/.test(r.text))});
await t('markdown table + escaping',()=>{const h=A.md('| a | b |\n|---|---|\n| 1 | **2** |\n\n<script>x</script>\n- one\n- two');assert(h.includes('<table>')&&h.includes('<b>2</b>')&&!h.includes('<script>')&&h.includes('<ul>'))});
await t('tool validation',()=>{assert.throws(()=>A.TOOLS.emi.run({principal:-5,annual_rate_pct:5,years:1}));assert.throws(()=>A.TOOLS.market_data.run())});
// conversation state
await t('send creates conversation, saves reply (demo)',async()=>{S.provider='demo';await A.send('Calculate the EMI on a ₹5 lakh loan.');const c=A.cur();assert(c&&c.msgs.length===2&&c.msgs[1].r==='a'&&!c.msgs[1].err&&c.msgs[1].src.startsWith('Demo')&&S.busy===null&&c.title.startsWith('Calculate'))});
await t('follow-up keeps context in same conversation',async()=>{const c=A.cur();await A.send('What about 12% for 3 years on 5 lakh?');assert(c.msgs.length===4);const h=A.hist(c);assert(h.length===1||h[0].role==='user');assert(h.map(m=>m.content).join().includes('EMI'))});
await t('empty / duplicate send ignored while busy',async()=>{const c=A.cur(),n=c.msgs.length;await A.send('   ');assert.equal(c.msgs.length,n);S.busy=c.id;await A.send('x');assert.equal(c.msgs.length,n);S.busy=null});
await t('failure produces retryable error, retry succeeds',async()=>{await A.send('/fail');const c=A.cur();let l=c.msgs[c.msgs.length-1];assert(l.err&&l.t.includes('Simulated'));assert(!A.hist(c).map(m=>m.content).join().includes('Simulated'));
 c.msgs.pop();c.msgs[c.msgs.length-1].t='Calculate the EMI on 1 lakh';await A.run(c);l=c.msgs[c.msgs.length-1];assert(!l.err&&l.t.includes('EMI'))});
await t('new conversation is separate',async()=>{const first=A.cur().id;S.cur=null;await A.send('Explain compound interest');assert.notEqual(A.cur().id,first);assert.equal(S.convos.length,2)});
// platform provider
await t('platform: tools executed, text returned, history sent',async()=>{S.provider='platform';let seen;S.sample=async(msgs,o)=>{seen=msgs;const r=await o.tools.find(x=>x.name==='emi').execute({principal:5e5,annual_rate_pct:10,years:5});o.onText({text:'partial'});return{text:'EMI '+r.monthly_emi}};
 S.cur=null;await A.send('emi?');const m=A.cur().msgs[1];assert.equal(m.t,'EMI 10623.52');assert.deepEqual(m.tools,['emi']);assert.equal(m.src,'Live AI');assert(seen[0].content.includes('FinBot')&&seen[0].content.includes('emi?'))});
await t('platform: rate limit and not_granted messages',async()=>{for(const [code,txt] of [['rate_limited','Too many'],['not_granted','Permission']]){S.sample=async()=>{throw {code,message:'x'}};S.cur=null;await A.send('hi');const m=A.cur().msgs[1];assert(m.err&&m.t.includes(txt),m.t)}});
await t('platform unavailable explained',async()=>{S.sample=null;S.cur=null;await A.send('hi');const m=A.cur().msgs[1];assert(m.err&&m.t.includes('not available'))});
// backend provider
await t('backend: success',async()=>{S.provider='backend';S.backendUrl='/api/chat';fetchImpl=async(u,o)=>({ok:true,json:async()=>({text:'hello',tools_used:['compound']})});S.cur=null;await A.send('hi');const m=A.cur().msgs[1];assert(m.t==='hello'&&m.tools[0]==='compound')});
await t('backend: HTTP error message shown',async()=>{fetchImpl=async()=>({ok:false,status:503,json:async()=>({error:'ANTHROPIC_API_KEY is not set'})});S.cur=null;await A.send('hi');assert(A.cur().msgs[1].t.includes('ANTHROPIC_API_KEY'))});
await t('backend: unreachable',async()=>{fetchImpl=async()=>{throw new TypeError('fail')};S.cur=null;await A.send('hi');assert(A.cur().msgs[1].t.includes('Could not reach'))});
console.log('saves',saves,'refreshes',refreshes);
})();
