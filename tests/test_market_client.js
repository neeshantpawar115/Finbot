const fs=require('fs'),assert=require('assert');
const html=fs.readFileSync('index.html','utf8');
const script=html.split('<script>')[1].split('</script>')[0];
new Function(script.replace(/applyTheme\(\);go\('home'\);\s*$/,'')); console.log('ok   whole script parses');
const blk=html.split('// ==MKTLOGIC==')[1].split('// ==/MKTLOGIC==')[0];
let fx;const S={backendUrl:'/api/chat'};
const A=new Function('S','fetch',blk+';return{M,mget,mktErr,px,spark}')(S,(...a)=>fx(...a));
const t=async(n,f)=>{try{await f();console.log('ok  ',n)}catch(e){console.log('FAIL',n,e.message||e);process.exitCode=1}};
(async()=>{
await t('mget builds /api/market URL and returns JSON',async()=>{let u;fx=async(url)=>{u=url;return{ok:true,json:async()=>({results:[]})}};await A.mget('search',{q:'a b',market:'all'});assert.equal(u,'/api/market/search?q=a+b&market=all')});
await t('custom backend URL respected',async()=>{S.backendUrl='http://h:9/api/chat';let u;fx=async(url)=>{u=url;return{ok:true,json:async()=>({})}};await A.mget('status',{});assert(u.startsWith('http://h:9/api/market/status'));S.backendUrl='/api/chat'});
await t('unreachable backend -> explained',async()=>{fx=async()=>{throw new TypeError('x')};try{await A.mget('status',{});assert.fail('no throw')}catch(e){assert.equal(e.code,'backend_unreachable');assert(e.message.includes('python app.py'))}});
await t('non-JSON (static host) -> bad_response',async()=>{fx=async()=>({ok:false,status:404,json:async()=>{throw new Error('x')}});try{await A.mget('status',{});assert.fail()}catch(e){assert.equal(e.code,'bad_response')}});
await t('API error object passed through',async()=>{fx=async()=>({ok:false,status:404,json:async()=>({error:{code:'invalid_symbol',message:'No instrument found for X.'}})});try{await A.mget('stock',{id:'X'});assert.fail()}catch(e){assert.equal(e.code,'invalid_symbol')}});
await t('abort not masked',async()=>{fx=async()=>{const e=new Error('a');e.name='AbortError';throw e};try{await A.mget('s',{});assert.fail()}catch(e){assert.equal(e.name,'AbortError')}});
await t('error messages per code',()=>{assert(A.mktErr({code:'rate_limited',message:'m'}).includes('Wait'));assert(A.mktErr({code:'no_data',message:'m'}).includes('delisted')||A.mktErr({code:'no_data',message:'m'}).includes('still listed'));assert(A.mktErr({code:'timeout',message:'m'}).includes('timed out'));assert.equal(A.mktErr({code:'missing_credentials',message:'Set KEY'}),'Set KEY')});
await t('price formatting: INR grouping, USD, unknown currency, null',()=>{assert.equal(A.px(1234567.5,'INR'),'₹12,34,567.5');assert.equal(A.px(1234.5,'USD'),'$1,234.5');assert.equal(A.px(10,'JPY'),'10 JPY');assert.equal(A.px(10,null),'10');assert.equal(A.px(null,'INR'),'—')});
await t('sparkline: points, colour, degenerate',()=>{const s=A.spark([['2026-01-01',1],['2026-01-02',3]],false);assert(s.includes('polyline')&&s.includes('var(--g)'));assert(A.spark([['d',1],['e',0]],true).includes('var(--red)'));assert.equal(A.spark([['d',1]]),'');assert(A.spark([['a',5],['b',5]],false).includes('polyline'))});
})();
