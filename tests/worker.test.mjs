import {DatabaseSync} from 'node:sqlite';
import fs from 'node:fs';
// Run from the repository root with Node 22 or newer:  node --no-warnings tests/worker.test.mjs
// Tests worker/worker.js against an in-memory SQLite copy of worker/schema.sql (no network, no Cloudflare account needed).
const REPO=new URL('../',import.meta.url).pathname;
let pass=0,fail=0;const ok=(c,m)=>{c?pass++:(fail++,console.log('  FAIL:',m))};
function mk(repo,extra={},LAT=0){const lat=()=>LAT?new Promise(r=>setTimeout(r,LAT)):0;
  return import(repo+'worker/worker.js?'+Math.random()).then(mod=>{
    const worker=mod.default,db=new DatabaseSync(':memory:');db.exec(fs.readFileSync(repo+'worker/schema.sql','utf8'));
    const ops={reads:0,writes:0},all=[];
    const prep=sql=>{const st=db.prepare(sql);let a=[];const o={bind(...x){a=x;return o},async first(){await lat();ops.reads++;return st.get(...a)??null},async all(){await lat();ops.reads++;return{results:st.all(...a)}},async run(){await lat();ops.writes++;const r=st.run(...a);return{meta:{last_row_id:Number(r.lastInsertRowid),changes:Number(r.changes)}}},_run(){const r=st.run(...a);return{meta:{changes:Number(r.changes)}}}};return o};
    const env={ADMIN_KEY:'sekret',DB:{prepare:prep,async batch(l){ops.writes+=l.length;return l.map(s=>s._run())}},...extra};
    const call=async(M,p,b,h={},raw)=>{const body=raw!==undefined?raw:(b?JSON.stringify(b):undefined);const r=await worker.fetch(new Request('https://w'+p,{method:M,headers:{'Content-Type':'application/json','CF-Connecting-IP':'1.1.1.1',...h},body}),env,{waitUntil(){}});const t=await r.text();all.push(t);let j=null;try{j=JSON.parse(t)}catch(e){}return[r.status,j,r.headers]};
    return{env,call,ops,all,db};
  });
}
const A={'x-admin-key':'sekret'};
async function suite(name,repo){
  console.log('\n== '+name);const{env,call,ops,all}=await mk(repo);let s,j,h;
  [s,j]=await call('GET','/api/articles');ok(s===200&&j.length===1,'list');
  [s]=await call('POST','/api/articles',{title:'t',body:'b'});ok(s===403,'publish needs key');
  [s,j]=await call('POST','/api/articles',{title:'Hello',body:'Body',kind:'Review',anime_id:'5'},A);ok(s===200,'publish with numeric string anime_id');const ID=j.id;
  [s,j]=await call('GET','/api/articles');ok(j.find(a=>a.id===ID).anime_id==5,'anime id stored');
  if(true){[s]=await call('POST','/api/articles',{title:'t',body:'b',anime_id:'x"><script>'},A);ok(s===400,'anime_id must be a number');[s]=await call('POST','/api/articles',{title:' ',body:'b'},A);ok(s===400,'empty title')}
  // rate limit + spoof
  for(let i=0;i<5;i++){[s]=await call('POST','/api/articles/'+ID+'/comments',{name:'a',text:'hi'+i});ok(s===200,'comment '+i)}
  [s]=await call('POST','/api/articles/'+ID+'/comments',{name:'a',text:'6th'});ok(s===429,'6th limited');
  [s]=await call('POST','/api/articles/'+ID+'/comments',{name:'a',text:'spoof'},{'X-Forwarded-For':'9.9.9.9'});ok(s===429,'fake X-Forwarded-For ignored');
  [s]=await call('POST','/api/articles/'+ID+'/comments',{name:'b',text:'other'},{'CF-Connecting-IP':'2.2.2.2'});ok(s===200,'other visitor ok');
  [s]=await call('POST','/api/articles/999/comments',{name:'a',text:'x'},{'CF-Connecting-IP':'4.4.4.4'});ok(s===404,'missing article 404');
  // concurrency: 20 at once from one visitor, with a 2 ms round trip per query like a real database
  {const c2=await mk(repo,{},2);await c2.call('POST','/api/articles',{title:'T',body:'B'},A);
   const res=await Promise.all(Array.from({length:20},(_,i)=>c2.call('POST','/api/articles/1/comments',{name:'c',text:'burst'+i},{'CF-Connecting-IP':'7.7.7.7'})));
   const okc=res.filter(r=>r[0]===200).length;console.log('  20 simultaneous comments from one visitor (2 ms per query): '+okc+' accepted, limit is 5');
   if(true)ok(okc===5,'burst must be capped at exactly 5 (got '+okc+')');else ok(okc>5,'old check-then-insert expected to leak (got '+okc+')');}
  // votes
  for(let i=0;i<3;i++)await call('POST','/api/poll/vote',{option:'Horror Collector'},{'CF-Connecting-IP':'5.5.5.5'});
  [s]=await call('POST','/api/poll/vote',{option:'Horror Collector'},{'CF-Connecting-IP':'5.5.5.5'});ok(s===429,'4th vote limited');
  [s,j]=await call('GET','/api/poll');ok(j.find(x=>x.option==='Horror Collector').votes===3,'votes counted');
  if(true){
    [s]=await call('POST','/api/poll/vote',{option:'nope'},{'CF-Connecting-IP':'6.6.6.6'});ok(s===404,'unknown option 404');
    for(let i=0;i<6;i++)await call('POST','/api/articles/'+ID+'/comments',{name:'',text:'x'},{'CF-Connecting-IP':'8.8.8.8'});
    for(let i=0;i<5;i++){[s]=await call('POST','/api/articles/'+ID+'/comments',{name:'v',text:'ok'+i},{'CF-Connecting-IP':'8.8.8.8'});ok(s===200,'invalid requests do not use up the limit '+i)}
    // body limits
    [s]=await call('POST','/api/articles/'+ID+'/comments',null,{'CF-Connecting-IP':'3.3.3.3'},JSON.stringify({name:'a',text:'x'.repeat(5000)}));ok(s===413,'oversize comment 413');
    [s]=await call('POST','/api/poll/vote',null,{'CF-Connecting-IP':'3.3.3.4'},JSON.stringify({option:'x'.repeat(2000)}));ok(s===413,'oversize vote 413');
    [s]=await call('POST','/api/articles',null,A,JSON.stringify({title:'t',body:'x'.repeat(70000)}));ok(s===413,'oversize article 413');
    [s]=await call('POST','/api/articles/'+ID+'/comments',null,{'CF-Connecting-IP':'3.3.3.5'},'not json');ok(s===400,'bad json 400');
    // admin brute force
    let last=0;for(let i=0;i<12;i++)[last]=await call('DELETE','/api/articles/'+ID,null,{'x-admin-key':'guess'+i,'CF-Connecting-IP':'9.9.9.9'});ok(last===429,'wrong-key guesses get throttled');
    [s]=await call('DELETE','/api/articles/'+ID,null,{...A,'CF-Connecting-IP':'10.9.9.9'});ok(s===200,'right key from another visitor still works');
    // pagination
    [s,j]=await call('POST','/api/articles',{title:'P',body:'pg'},A);const P=j.id;
    for(let i=0;i<35;i++)await call('POST','/api/articles/'+P+'/comments',{name:'u'+i,text:'c'+i},{'CF-Connecting-IP':'11.0.0.'+i});
    [s,j]=await call('GET','/api/articles/'+P+'/comments');ok(j.length===35&&j[0].text==='c34'&&j[0].id,'default returns all 35 newest first with ids');
    [s,j]=await call('GET','/api/articles/'+P+'/comments?limit=10');const p1=j;ok(p1.length===10&&p1[0].text==='c34','limit=10');
    [s,j]=await call('GET','/api/articles/'+P+'/comments?limit=10&before='+p1[9].id);ok(j.length===10&&j[0].id<p1[9].id&&j[0].text==='c24','before= next page without overlap');
    [s,j]=await call('GET','/api/articles/'+P+'/comments?limit=9999');ok(j.length===35,'limit clamps to 200');
    // export + fingerprint
    let ex;[s,ex]=await call('GET','/api/export');ok(s===200&&ex.fp&&ex.articles.length===2&&ex.comments[P].length===30&&ex.articles.find(a=>a.id===P).comments===35&&ex.poll.length===3,'export shape');
    [s,j]=await call('GET','/api/export?fp='+ex.fp);ok(j.unchanged===true,'unchanged fingerprint short-circuits');
    await call('POST','/api/articles/'+P+'/comments',{name:'n',text:'new'},{'CF-Connecting-IP':'12.0.0.1'});[s,j]=await call('GET','/api/export?fp='+ex.fp);ok(!j.unchanged,'new comment changes fingerprint');
    let fp2=j.fp;await call('POST','/api/poll/vote',{option:'Horror Collector'},{'CF-Connecting-IP':'12.0.0.2'});[s,j]=await call('GET','/api/export?fp='+fp2);ok(!j.unchanged,'vote changes fingerprint');
    fp2=j.fp;[s,j]=await call('POST','/api/articles',{title:'Q',body:'q'},A);[s,j]=await call('GET','/api/export?fp='+fp2);ok(!j.unchanged,'new article changes fingerprint');
    fp2=j.fp;await call('DELETE','/api/articles/'+P,null,A);[s,j]=await call('GET','/api/export?fp='+fp2);ok(!j.unchanged,'delete changes fingerprint');
    [s,j]=await call('GET','/api/articles/'+P+'/comments');ok(j.length===0,'delete removed comments');
    // gql proxy
    [s]=await call('POST','/api/gql',null,{},'nope');ok(s===400,'gql bad json');
    [s]=await call('POST','/api/gql',{query:'mutation{x}'});ok(s===400,'gql mutation refused');
    [s]=await call('POST','/api/gql',null,{},JSON.stringify({query:'x'.repeat(21000)}));ok(s===413,'gql too big');
    let calls=0;const store=new Map();globalThis.caches={default:{async match(k){const v=store.get(k.url);return v?v.clone():undefined},async put(k,v){store.set(k.url,v)}}};
    const rf=globalThis.fetch;globalThis.fetch=async()=>{calls++;return new Response('{"data":{"ok":1}}')};
    const wk=(await import(repo+'worker/worker.js?q')).default;const pend=[];const ctx={waitUntil(p){pend.push(p)}};
    const g=async()=>{const r=await wk.fetch(new Request('https://w/api/gql',{method:'POST',body:JSON.stringify({query:'query{Media{id}}'}),headers:{'CF-Connecting-IP':'13.0.0.1'}}),env,ctx);await Promise.all(pend);return r.status};
    ok(await g()===200&&await g()===200&&calls===1,'gql proxy caches (AniList called '+calls+' time for 2 requests)');globalThis.fetch=rf;
  }
  // CORS
  [s,,h]=await call('OPTIONS','/api/articles');ok(s===204&&h.get('access-control-allow-origin')==='*','CORS default *');
  if(true){
    const e2=await mk(repo,{ALLOWED_ORIGINS:'https://a.example, https://b.example'});
    let r;[s,,r]=await e2.call('GET','/api/poll',null,{Origin:'https://b.example'});ok(r.get('access-control-allow-origin')==='https://b.example'&&r.get('vary')==='Origin','CORS echoes an allowed origin');
    [s,,r]=await e2.call('GET','/api/poll',null,{Origin:'https://evil.example'});ok(r.get('access-control-allow-origin')==='https://a.example','CORS never echoes an unknown origin');
    // error path hides internals
    const e3=await mk(repo);e3.env.DB.prepare=()=>{throw new Error('secret db detail sekret')};
    [s,j]=await e3.call('GET','/api/poll');ok(s===500&&j.detail==='server error'&&!JSON.stringify(j).includes('secret'),'500 hides internals');
  }
  ok(!all.join('').includes('sekret'),'admin key never appears in any response');
  // D1 operations per request (reads+writes counted as queries)
  const m2=await mk(repo);await m2.call('POST','/api/articles',{title:'T',body:'B'},A);const cnt=async(f)=>{m2.ops.reads=m2.ops.writes=0;await f();return m2.ops.reads+'r/'+m2.ops.writes+'w'};
  console.log('  D1 queries per request  comment POST:',await cnt(()=>m2.call('POST','/api/articles/1/comments',{name:'a',text:'b'},{'CF-Connecting-IP':'20.0.0.1'})),'| vote POST:',await cnt(()=>m2.call('POST','/api/poll/vote',{option:'Horror Collector'},{'CF-Connecting-IP':'20.0.0.2'})),'| GET comments:',await cnt(()=>m2.call('GET','/api/articles/1/comments')),'| GET poll:',await cnt(()=>m2.call('GET','/api/poll')));
}
await suite('worker/worker.js',REPO);
console.log('\nworker tests: pass',pass,'fail',fail);process.exit(fail?1:0);
