// Free Cloudflare Worker + D1: comments, votes, articles and publishing. Everything else is static files.
const CORS={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'GET,POST,DELETE,OPTIONS','Access-Control-Allow-Headers':'Content-Type,x-admin-key','Access-Control-Max-Age':'86400'};
const J=(d,s=200)=>new Response(JSON.stringify(d),{status:s,headers:{'Content-Type':'application/json','Cache-Control':'no-store',...CORS}});
const hex=async s=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s)))].slice(0,10).map(x=>x.toString(16).padStart(2,'0')).join('');
const now=()=>Math.floor(Date.now()/1000);
const body=async req=>{try{const d=await req.json();return d&&typeof d==='object'?d:{}}catch(e){return {}}};
function isAdmin(req,env){const a=req.headers.get('x-admin-key')||'',k=env.ADMIN_KEY||'';if(!k||a.length!==k.length)return false;let d=0;for(let i=0;i<k.length;i++)d|=a.charCodeAt(i)^k.charCodeAt(i);return d===0}
async function limited(env,req,what,n,secs){ // n actions per visitor per window; CF-Connecting-IP is set by Cloudflare, visitors can't fake it
  const ip=await hex((req.headers.get('CF-Connecting-IP')||'?')+'|nx');
  const r=await env.DB.prepare('select count(*) c from hits where ip=? and what=? and t>?').bind(ip,what,now()-secs).first();
  if(r.c>=n)return true;
  await env.DB.prepare('insert into hits(ip,what,t) values(?,?,?)').bind(ip,what,now()).run();
  if(Math.random()<0.02)await env.DB.prepare('delete from hits where t<?').bind(now()-3600).run();
  return false;
}
const poll=env=>env.DB.prepare('select option,votes from poll order by rowid').all().then(r=>r.results);
export default{async fetch(req,env,ctx){
  const p=new URL(req.url).pathname,M=req.method;let m;
  if(M==='OPTIONS')return new Response(null,{status:204,headers:CORS});
  try{
    if(p==='/api/gql'&&M==='POST'){ // fallback only: lets the page reach AniList through us if a visitor's browser can't
      const b=await req.text();if(b.length>20000)return J({detail:'too big'},413);
      const ck=new Request('https://nx.cache/'+await hex(b)),c=caches.default,hit=await c.match(ck);
      if(hit)return new Response(hit.body,{status:200,headers:{'Content-Type':'application/json',...CORS}});
      const r=await fetch('https://graphql.anilist.co',{method:'POST',headers:{'Content-Type':'application/json',Accept:'application/json'},body:b}),t=await r.text();
      if(r.ok)ctx.waitUntil(c.put(ck,new Response(t,{headers:{'Content-Type':'application/json','Cache-Control':'max-age=600'}})));
      return new Response(t,{status:r.status,headers:{'Content-Type':'application/json',...CORS}});
    }
    if(p==='/api/export'&&M==='GET'){ // read by the 15-minute GitHub job to build the static copy of articles, comments and poll
      const A=(await env.DB.prepare('select id,kind,title,body,anime_id,created from articles order by id desc limit 60').all()).results;
      const N={};(await env.DB.prepare('select article_id,count(*) n from comments group by article_id').all()).results.forEach(r=>N[r.article_id]=r.n);
      const C={};(await env.DB.prepare('select article_id,name,text,created from comments order by id desc limit 1500').all()).results.forEach(r=>{const l=C[r.article_id]=C[r.article_id]||[];if(l.length<30)l.push({name:r.name,text:r.text,created:r.created})});
      return J({articles:A.map(a=>({...a,comments:N[a.id]||0})),comments:C,poll:await poll(env)});
    }
    if(p==='/api/poll'&&M==='GET')return J(await poll(env));
    if(p==='/api/poll/vote'&&M==='POST'){
      if(await limited(env,req,'vote',3,60))return J({detail:'slow down'},429);
      const d=await body(req);await env.DB.prepare('update poll set votes=votes+1 where option=?').bind(String(d.option||'')).run();
      return J(await poll(env));
    }
    if(p==='/api/articles'){
      if(M==='GET')return J((await env.DB.prepare('select id,kind,title,anime_id,created,substr(body,1,160) excerpt,(select count(*) from comments where article_id=articles.id) comments from articles order by id desc limit 200').all()).results);
      if(M==='POST'){
        if(!isAdmin(req,env))return J({detail:'wrong or missing admin key'},403);
        const d=await body(req),t=String(d.title||'').trim(),b=String(d.body||'').trim();
        if(!t||!b)return J({detail:'title and body needed'},400);
        const r=await env.DB.prepare('insert into articles(kind,title,body,anime_id) values(?,?,?,?)').bind(String(d.kind||'News').slice(0,30),t.slice(0,200),b,d.anime_id||null).run();
        return J({id:r.meta.last_row_id});
      }
    }
    if(m=p.match(/^\/api\/articles\/(\d+)$/)){
      const id=+m[1];
      if(M==='GET'){const r=await env.DB.prepare('select * from articles where id=?').bind(id).first();return r?J(r):J({detail:'not found'},404)}
      if(M==='DELETE'){
        if(!isAdmin(req,env))return J({detail:'wrong or missing admin key'},403);
        await env.DB.batch([env.DB.prepare('delete from comments where article_id=?').bind(id),env.DB.prepare('delete from articles where id=?').bind(id)]);
        return J({ok:1});
      }
    }
    if(m=p.match(/^\/api\/articles\/(\d+)\/comments$/)){
      const id=+m[1];
      if(M==='GET')return J((await env.DB.prepare('select name,text,created from comments where article_id=? order by id desc limit 200').bind(id).all()).results);
      if(M==='POST'){
        if(await limited(env,req,'comment',5,60))return J({detail:'slow down'},429);
        const d=await body(req),name=String(d.name||'').trim().slice(0,40),text=String(d.text||'').trim().slice(0,1000);
        if(!name||!text)return J({detail:'name and text needed'},400);
        if(!await env.DB.prepare('select 1 x from articles where id=?').bind(id).first())return J({detail:'not found'},404);
        await env.DB.prepare('insert into comments(article_id,name,text) values(?,?,?)').bind(id,name,text).run();
        return J({ok:1});
      }
    }
    return J({detail:'not found'},404);
  }catch(e){return J({detail:'server error'},500)}
}};
