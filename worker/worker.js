// marimoblog API on Cloudflare Workers + D1.
// Public pages are static files; this Worker only handles writes, optional live comments, the build export and an AniList fallback.
const LIM={comment:4096,article:60000,vote:1024,gql:20000};
function cors(req,env){
  const list=String(env.ALLOWED_ORIGINS||'*').split(',').map(s=>s.trim()).filter(Boolean),o=req.headers.get('Origin');
  const allow=list.includes('*')?'*':(o&&list.includes(o)?o:list[0]||'*');
  const h={'Access-Control-Allow-Origin':allow,'Access-Control-Allow-Methods':'GET,POST,DELETE,OPTIONS','Access-Control-Allow-Headers':'Content-Type,x-admin-key','Access-Control-Max-Age':'86400','X-Content-Type-Options':'nosniff'};
  if(allow!=='*')h.Vary='Origin';
  return h;
}
const hex=async s=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s)))].slice(0,10).map(x=>x.toString(16).padStart(2,'0')).join('');
const now=()=>Math.floor(Date.now()/1000);
async function body(req,max){ // null = too big
  const n=+req.headers.get('content-length')||0;if(n>max)return null;
  const t=await req.text();if(t.length>max)return null;
  try{const d=JSON.parse(t);return d&&typeof d==='object'?d:{}}catch(e){return {}}
}
const same=(a,k)=>{if(!k||a.length!==k.length)return false;let d=0;for(let i=0;i<k.length;i++)d|=a.charCodeAt(i)^k.charCodeAt(i);return d===0};
// One atomic statement: records the hit only while the visitor is under the limit, so concurrent requests cannot slip past it.
// CF-Connecting-IP is set by Cloudflare and cannot be faked by visitors. Returns true when the visitor is over the limit.
async function hit(env,req,what,max,secs){
  const ip=await hex((req.headers.get('CF-Connecting-IP')||'?')+'|nx'),t=now();
  const r=await env.DB.prepare('insert into hits(ip,what,t) select ?1,?2,?3 where (select count(*) from hits where ip=?1 and what=?2 and t>?4)<?5').bind(ip,what,t,t-secs,max).run();
  if(Math.random()<0.02)await env.DB.prepare('delete from hits where t<?').bind(t-3600).run();
  return !r.meta.changes;
}
async function adminCheck(req,env){ // 0 = ok, 403 = wrong key, 429 = too many wrong tries from this visitor
  if(same(req.headers.get('x-admin-key')||'',env.ADMIN_KEY||''))return 0;
  return(await hit(env,req,'adminfail',10,60))?429:403;
}
const poll=env=>env.DB.prepare('select option,votes from poll order by rowid').all().then(r=>r.results);
const intOrNull=v=>{if(v==null||v==='')return null;const n=Number(v);return Number.isInteger(n)&&n>0&&n<2e9?n:NaN};
export default{async fetch(req,env,ctx){
  const u=new URL(req.url),p=u.pathname,M=req.method,h=cors(req,env);let m;
  const R=(d,s,cc)=>new Response(JSON.stringify(d),{status:s||200,headers:{'Content-Type':'application/json','Cache-Control':cc||'no-store',...h}});
  if(M==='OPTIONS')return new Response(null,{status:204,headers:h});
  try{
    if(p==='/api/gql'&&M==='POST'){ // fallback only: lets the page reach AniList through us if a visitor's browser cannot
      const b=await req.text();if(b.length>LIM.gql)return R({detail:'too big'},413);
      let q;try{q=JSON.parse(b)}catch(e){return R({detail:'bad request'},400)}
      if(!q||typeof q.query!=='string'||q.query.length>6000||/\bmutation\b/i.test(q.query))return R({detail:'bad request'},400);
      const ck=new Request('https://nx.cache/'+await hex(b)),c=caches.default,cached=await c.match(ck);
      if(cached)return new Response(cached.body,{status:200,headers:{'Content-Type':'application/json',...h}});
      if(await hit(env,req,'gql',30,60))return R({detail:'slow down'},429);
      const r=await fetch('https://graphql.anilist.co',{method:'POST',headers:{'Content-Type':'application/json',Accept:'application/json'},body:b}),t=await r.text();
      if(r.ok)ctx.waitUntil(c.put(ck,new Response(t,{headers:{'Content-Type':'application/json','Cache-Control':'max-age=600'}})));
      return new Response(t,{status:r.status,headers:{'Content-Type':'application/json',...h}});
    }
    if(p==='/api/export'&&M==='GET'){ // read by the 15-minute GitHub job; ?fp= lets it skip all the heavy reads when nothing changed
      const f=await env.DB.prepare("select (select coalesce(max(id),0) from comments)||'.'||(select coalesce(max(id),0) from articles)||'.'||(select count(*) from articles)||'.'||(select coalesce(sum(votes),0) from poll) fp").first();
      if(u.searchParams.get('fp')===f.fp)return R({unchanged:true,fp:f.fp});
      const last='where article_id in (select id from articles order by id desc limit 60)';
      const A=(await env.DB.prepare('select id,kind,title,body,anime_id,created from articles order by id desc limit 60').all()).results;
      const N={};(await env.DB.prepare('select article_id,count(*) n from comments '+last+' group by article_id').all()).results.forEach(r=>N[r.article_id]=r.n);
      const C={};(await env.DB.prepare('select article_id,name,text,created from comments '+last+' order by id desc limit 1500').all()).results.forEach(r=>{const l=C[r.article_id]=C[r.article_id]||[];if(l.length<30)l.push({name:r.name,text:r.text,created:r.created})});
      let size=0;const out=[];for(const a of A){size+=(a.body||'').length;if(out.length&&size>600000)break;out.push({...a,comments:N[a.id]||0})} // keeps the response (and Worker CPU) bounded; older ones fall back to live reads
      return R({fp:f.fp,articles:out,comments:C,poll:await poll(env)});
    }
    if(p==='/api/poll'&&M==='GET')return R(await poll(env));
    if(p==='/api/poll/vote'&&M==='POST'){
      const d=await body(req,LIM.vote);if(d===null)return R({detail:'too big'},413);
      if(await hit(env,req,'vote',3,60))return R({detail:'slow down'},429);
      const r=await env.DB.prepare('update poll set votes=votes+1 where option=?').bind(String(d.option||'').slice(0,100)).run();
      if(!r.meta.changes)return R({detail:'unknown option'},404);
      return R(await poll(env));
    }
    if(p==='/api/articles'){
      if(M==='GET')return R((await env.DB.prepare('select id,kind,title,anime_id,created,substr(body,1,160) excerpt,(select count(*) from comments where article_id=articles.id) comments from articles order by id desc limit 200').all()).results);
      if(M==='POST'){
        const a=await adminCheck(req,env);if(a)return R({detail:a===429?'slow down':'wrong or missing admin key'},a);
        const d=await body(req,LIM.article);if(d===null)return R({detail:'too big'},413);
        const t=String(d.title||'').trim().slice(0,200),b=String(d.body||'').trim(),aid=intOrNull(d.anime_id);
        if(!t||!b)return R({detail:'title and body needed'},400);
        if(aid!==aid)return R({detail:'anime_id must be a positive whole number'},400);
        const r=await env.DB.prepare('insert into articles(kind,title,body,anime_id) values(?,?,?,?)').bind(String(d.kind||'News').trim().slice(0,30)||'News',t,b,aid).run();
        return R({id:r.meta.last_row_id});
      }
    }
    if(m=p.match(/^\/api\/articles\/(\d+)$/)){
      const id=+m[1];
      if(M==='GET'){const r=await env.DB.prepare('select * from articles where id=?').bind(id).first();return r?R(r):R({detail:'not found'},404)}
      if(M==='DELETE'){
        const a=await adminCheck(req,env);if(a)return R({detail:a===429?'slow down':'wrong or missing admin key'},a);
        await env.DB.batch([env.DB.prepare('delete from comments where article_id=?').bind(id),env.DB.prepare('delete from articles where id=?').bind(id)]);
        return R({ok:1});
      }
    }
    if(m=p.match(/^\/api\/articles\/(\d+)\/comments$/)){
      const id=+m[1];
      if(M==='GET'){ // newest first; ?limit= (1-200, default 200) and ?before=<comment id> page backwards
        const lim=Math.min(200,Math.max(1,parseInt(u.searchParams.get('limit'))||200)),bf=parseInt(u.searchParams.get('before'))||0;
        const q=bf?env.DB.prepare('select id,name,text,created from comments where article_id=? and id<? order by id desc limit ?').bind(id,bf,lim):env.DB.prepare('select id,name,text,created from comments where article_id=? order by id desc limit ?').bind(id,lim);
        return R((await q.all()).results);
      }
      if(M==='POST'){
        const d=await body(req,LIM.comment);if(d===null)return R({detail:'too big'},413);
        const name=String(d.name||'').trim().slice(0,40),text=String(d.text||'').trim().slice(0,1000);
        if(!name||!text)return R({detail:'name and text needed'},400);
        if(await hit(env,req,'comment',5,60))return R({detail:'slow down'},429);
        const r=await env.DB.prepare('insert into comments(article_id,name,text) select ?1,?2,?3 where exists(select 1 from articles where id=?1)').bind(id,name,text).run();
        return r.meta.changes?R({ok:1}):R({detail:'not found'},404);
      }
    }
    return R({detail:'not found'},404);
  }catch(e){console.error('worker error:',e&&e.message);return R({detail:'server error'},500)}
}};
