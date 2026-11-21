/* marimoblog without a server. The page still calls api('/trending') etc.; this file answers from
   static JSON (rebuilt every ~15 min by GitHub), AniList (asked straight from the visitor's browser,
   so every visitor has their own rate limit) or the Cloudflare Worker (comments, votes, articles). */
(()=>{
const API=/^https?:\/\//.test(window.NX_API||'')?window.NX_API.replace(/\/$/,''):'';
const AL='https://graphql.anilist.co',LS=window.localStorage;
const CARD='id title{english romaji} coverImage{large} averageScore genres seasonYear format popularity startDate{month day} nextAiringEpisode{airingAt episode}';
// one AniList call per anime page: detail + recommendations + links + art + names for the news matcher
const DETAIL='query($id:Int){Media(id:$id,type:ANIME){'+CARD+' coverImage{extraLarge} synonyms description episodes status bannerImage siteUrl idMal externalLinks{site url type} studios(isMain:true){nodes{name}} tags{name rank isMediaSpoiler} characters(perPage:8,sort:[ROLE,RELEVANCE]){edges{role node{name{full} image{medium}}}} staff(perPage:8){edges{role node{name{full}}}} relations{edges{relationType node{type '+CARD+'}}} stats{statusDistribution{status amount}} recommendations(perPage:10,sort:RATING_DESC){nodes{mediaRecommendation{'+CARD+'}}}}}';
const TIT='query($ids:[Int]){Page(perPage:50){media(id_in:$ids,type:ANIME){id title{romaji english} synonyms}}}';
const ART='query($i:[Int]){Page(perPage:40){media(id_in:$i,type:ANIME){id bannerImage coverImage{extraLarge}}}}';
const h=s=>{let x=5381;for(let i=0;i<s.length;i++)x=(x*33^s.charCodeAt(i))>>>0;return x.toString(36)};
const lsget=(k,ttl)=>{try{const c=JSON.parse(LS.getItem(k)||'null');if(c&&Date.now()-c.t<ttl*1000)return c.d}catch(e){}return null};
const lsold=k=>{try{const c=JSON.parse(LS.getItem(k)||'null');return c?c.d:null}catch(e){return null}};
const lsset=(k,d)=>{const v=JSON.stringify({t:Date.now(),d});try{LS.setItem(k,v)}catch(e){try{Object.keys(LS).filter(x=>x.startsWith('nx:')).forEach(x=>LS.removeItem(x));LS.setItem(k,v)}catch(e2){}}};
const inflight={};
async function post(url,q,v){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json',Accept:'application/json'},body:JSON.stringify({query:q,variables:v})})}
function gql(q,v,ttl=600){
  const k='nx:'+h(q+JSON.stringify(v||{})),c=lsget(k,ttl);if(c)return Promise.resolve(c);
  return inflight[k]||(inflight[k]=(async()=>{
    try{
      let r=null;try{r=await post(AL,q,v)}catch(e){}
      if((!r||!r.ok)&&API){try{r=await post(API+'/api/gql',q,v)}catch(e){}}
      if(!r||!r.ok)throw new Error('anilist '+(r?r.status:'unreachable'));
      const d=(await r.json()).data;lsset(k,d);return d;
    }catch(e){const o=lsold(k);if(o)return o;throw e}finally{delete inflight[k]}
  })());
}
const jget=async p=>{const r=await fetch(p);if(!r.ok)throw new Error(r.status);return r.json()};
const B=(()=>{try{const e=document.getElementById('boot');return e?JSON.parse(e.textContent):null}catch(e){return null}})(); // first-screen data the build embeds in index.html
const J={};function jc(p,ttl=6e5){ // JSON files: one request per ttl; if a refresh fails the last good copy keeps being served
  const c=J[p];if(c&&c.p&&Date.now()-c.t<ttl)return c.p;
  const q=jget(p).then(d=>{J[p].d=d;return d},e=>{if(J[p]&&J[p].d)return J[p].d;delete J[p];throw e});
  J[p]={t:Date.now(),p:q,d:c&&c.d};return q}
const home=()=>jc('data/home.json'),newsData=()=>jc('data/news.json'),idx=()=>jc('data/anime/index.json',36e5),nmatch=()=>jc('data/anime/matches.json',6e5);
const norm=s=>(s||'').toLowerCase().replace(/[^a-z0-9]+/g,' ').trim();
const keys=names=>{const ks=[];for(const n of names)for(const v of [n||'',(n||'').split(/\s*[:\u2013\u2014]\s*|\s+-\s+/)[0]]){const k=' '+norm(v)+' ';if(k.length>=7&&!ks.includes(k))ks.push(k)}return ks};
const strip=m=>JSON.parse(JSON.stringify(m));
const recs=m=>m.recommendations.nodes.map(n=>n.mediaRecommendation).filter(Boolean);
const sid=B&&B.anime?new Set(B.anime):null,RAW={};
async function raw(id){
  if(B&&B.feat&&B.feat.id===id)return RAW[id]=B.feat.m;
  if(!sid||sid.has(id)){try{const d=await jc('data/anime/'+id+'.json',36e5);if(d&&d.m)return RAW[id]=d.m}catch(e){}}
  const d=await gql(DETAIL,{id},3600);if(!d||!d.Media)throw new Error(404);return RAW[id]=d.Media;
}
const memo={},live={};
const isGet=o=>!o||!o.method||o.method==='GET';
async function worker(p,o){
  if(!API)throw new Error('NX_API not set in site/config.js');
  const r=await fetch(API+'/api'+p,o);if(!r.ok)throw new Error(r.status);return r.json();
}
const artData=()=>jc('data/articles.json'),aFile=id=>jc('data/a/'+id+'.json');
const mine=id=>lsget('nx:mine'+id,86400)||[];
async function comments(id,fresh,force){ // static copy for everyone; live (one Worker call, cached 20 s) only after this visitor posted or pressed Refresh
  if(fresh&&API&&(force||mine(id).length)){
    const c=live[id];if(!force&&c&&Date.now()-c.t<20000)return c.d;
    try{const d=await worker('/articles/'+id+'/comments');live[id]={t:Date.now(),d};return d}catch(e){}
  }
  let st=[];try{st=(await aFile(id)).comments||[]}catch(e){}
  return[...mine(id).filter(c=>!st.some(x=>x.name===c.name&&x.text===c.text)),...st];
}
async function postComment(id,o){
  const r=await worker('/articles/'+id+'/comments',o);
  try{const b=JSON.parse(o.body);lsset('nx:mine'+id,[{name:String(b.name||'').slice(0,40),text:String(b.text||'').slice(0,1000),created:new Date().toISOString().slice(0,19).replace('T',' ')},...mine(id)].slice(0,20))}catch(e){}
  delete live[id];return r;
}
async function anime(id){
  const m=strip(await raw(id));
  m.description=(m.description||'').replace(/<br>/g,'\n').replace(/<[^>]+>/g,'');
  m.relations.edges=m.relations.edges.filter(e=>e.node.type==='ANIME');
  m.recs=recs(m).slice(0,8);['recommendations','siteUrl','idMal','externalLinks','synonyms'].forEach(k=>delete m[k]);
  try{m.articles=(await api('/articles')).filter(a=>a.anime_id===id).map(a=>({id:a.id,title:a.title,kind:a.kind,created:a.created,comments:a.comments}))}catch(e){m.articles=[]}
  return m;
}
const similar=async id=>recs(strip(await raw(id))).slice(0,10).map(r=>({...r,why:'Fans of this also liked it'}));
const okurl=u=>typeof u==='string'&&/^https?:\/\//.test(u)?u:'';
async function links(id){
  const m=await raw(id),name=m.title.english||m.title.romaji||'';
  const out=[{label:'AniList',url:m.siteUrl,kind:'database'}];
  if(m.idMal)out.push({label:'MyAnimeList',url:'https://myanimelist.net/anime/'+m.idMal,kind:'database'});
  out.push({label:'Fandom wiki search',url:'https://www.fandom.com/search?q='+encodeURIComponent(name),kind:'wiki'});
  for(const e of m.externalLinks||[])if(okurl(e.url))out.push({label:({Twitter:'X / Twitter'})[e.site]||e.site,url:e.url,kind:(e.type||'INFO').toLowerCase()});
  return out;
}
async function art(ids){
  const n=ids.split(',').filter(x=>/^\d+$/.test(x.trim())).map(Number).slice(0,40);if(!n.length)return[];
  const mk=(i,b,c)=>({id:i,best:b||c,stills:[c]}),out=[],need=[];
  for(const i of n){const e=B&&B.art&&B.art[i];if(e)out.push(mk(i,e.b,e.c));else need.push(i)}
  if(need.length){
    const ix=await idx().catch(()=>({})),rest=[];
    for(const i of need){const e=ix[i];if(e)out.push(mk(i,e.b,e.c));else rest.push(i)}
    if(rest.length===1){const m=await raw(rest[0]);out.push(mk(m.id,m.bannerImage,m.coverImage.extraLarge))}
    else if(rest.length)(await gql(ART,{i:rest},3600)).Page.media.forEach(m=>out.push(mk(m.id,m.bannerImage,m.coverImage.extraLarge)));
  }
  return out;
}
async function browse(q){
  const sort=q.get('sort')||'popular',s=(q.get('q')||'').trim().slice(0,100),genre=(q.get('genre')||'').trim().slice(0,30),year=+q.get('year')||0,page=Math.max(1,+q.get('page')||1);
  if(!s&&!genre&&!year&&page===1){try{const H=await home();if(H['browse_'+sort])return H['browse_'+sort]}catch(e){}}
  const S={popular:'POPULARITY_DESC',trending:'TRENDING_DESC',score:'SCORE_DESC',new:'START_DATE_DESC'}[sort]||'POPULARITY_DESC';
  const d=['$p:Int','$n:Int'],a=['sort:'+(s&&sort==='popular'?'SEARCH_MATCH':S)],v={p:page,n:24};
  [[s,'s','String','search'],[genre,'g','String','genre'],[year,'y','Int','seasonYear']].forEach(([val,n,t,arg])=>{if(val){d.push('$'+n+':'+t);a.push(arg+':$'+n);v[n]=val}});
  return(await gql('query('+d.join(',')+'){Page(page:$p,perPage:$n){media('+a.join(',')+',type:ANIME,isAdult:false){'+CARD+'}}}',v,600)).Page.media;
}
async function titles(ids){
  const out={},need=[];let ix;
  for(const i of ids){
    const c=lsget('nx:t'+i,43200);if(c){out[i]=c;continue}
    const r=RAW[i];if(r){const ks=keys([r.title.english,r.title.romaji,...(r.synonyms||[]).slice(0,4)]);if(ks.length){out[i]=ks;lsset('nx:t'+i,ks)}continue}
    if(ix===undefined)ix=await idx().catch(()=>({}));
    const e=ix[i];if(e){const ks=keys(e.n);if(ks.length){out[i]=ks;lsset('nx:t'+i,ks)}}else need.push(i)}
  if(need.length)try{for(const m of(await gql(TIT,{ids:need},43200)).Page.media){const ks=keys([m.title.english,m.title.romaji,...(m.synonyms||[]).slice(0,4)]);lsset('nx:t'+m.id,ks);if(ks.length)out[m.id]=ks}}catch(e){}
  return out;
}
async function news(q){
  const types=(q.get('type')||'').split(',').filter(Boolean).slice(0,5),src=q.get('source')||'',s=(q.get('q')||'').trim().toLowerCase();
  const idl=(q.get('ids')||'').split(',').filter(x=>/^\d+$/.test(x.trim())).map(Number).slice(0,50);
  const page=Math.max(1,+q.get('page')||1),per=Math.max(1,Math.min(+q.get('per')||12,48));
  const bn=B&&B.news&&B.news[types.join(',')];if(bn&&bn.per===per&&page===1&&!src&&!s&&!idl.length)return bn.resp;
  const N=await newsData();
  let L=N.items.filter(r=>(!types.length||types.includes(r.kind))&&(!src||r.source===src)&&(!s||(r.title+' '+r.excerpt).toLowerCase().includes(s)));
  if(idl.length){ // news that mentions the anime: prebuilt lists first, matching in the browser only for anime without one
    const mm=await nmatch().catch(()=>null),pre=mm?idl.filter(i=>mm[i]):[],rest=idl.filter(i=>!pre.includes(i)),sets={};
    pre.forEach(i=>sets[i]=new Set(mm[i]));
    const T=rest.length?Object.entries(await titles(rest)):[];
    L=L.map(r=>{
      const m=pre.filter(i=>sets[i].has(r.id));
      if(T.length){const t=r._t||(r._t=' '+norm(r.title+' '+r.excerpt)+' ');for(const[i,ks]of T)if(ks.some(k=>t.includes(k)))m.push(+i)}
      return m.length?{...r,anime:m}:null}).filter(Boolean);
  }
  const total=L.length;
  return{items:L.slice((page-1)*per,page*per).map(({_t,...r})=>r),total,page,pages:Math.max(1,Math.ceil(total/per)),updated:N.updated};
}
async function newsItem(id){
  const N=await newsData(),r=N.items.find(x=>x.id===id);if(!r)throw new Error(404);
  return{...r,more:N.items.filter(x=>x.source===r.source&&x.id!==id).slice(0,4)};
}
async function api(p,o){
  const u=new URL(p,'http://x'),path=u.pathname,q=u.searchParams;let m;
  if(path==='/trending')return B&&B.trending?B.trending:(await home()).trending;
  if(path==='/season')return B&&B.season?B.season:(await home()).season;
  if(path==='/upcoming')return(await home()).upcoming;
  if(path==='/calendar'){const end=Date.now()/1000+7*86400;return(await home()).calendar.filter(x=>x.nextAiringEpisode&&x.nextAiringEpisode.airingAt<end)}
  if(path==='/browse')return browse(q);
  if(m=path.match(/^\/anime\/(\d+)$/))return anime(+m[1]);
  if(m=path.match(/^\/similar\/(\d+)$/))return similar(+m[1]);
  if(m=path.match(/^\/links\/(\d+)$/))return links(+m[1]);
  if(path==='/art')return art(q.get('ids')||'');
  if(path==='/news')return news(q);
  if(m=path.match(/^\/news\/item\/(.+)$/))return newsItem(decodeURIComponent(m[1]));
  if(path==='/sources')return(await newsData()).sources;
  if(path==='/poll'&&isGet(o))return memo.poll||(B&&B.poll)||artData().then(d=>d.poll,()=>worker('/poll'));
  if(path==='/poll/vote'){const d=await worker(p,o);memo.poll=d;return d}
  if(path==='/articles'&&isGet(o))return(B&&B.articles)||artData().then(d=>d.list,()=>worker('/articles'));
  if((m=path.match(/^\/articles\/(\d+)$/))&&isGet(o))return aFile(m[1]).then(d=>d.article,()=>worker('/articles/'+m[1]));
  if(m=path.match(/^\/articles\/(\d+)\/comments$/))return isGet(o)?comments(m[1],q.get('live')==='1',q.get('force')==='1'):postComment(m[1],o);
  return worker(p,o);
}
window.NX={api,live:B?Promise.resolve(true):jget('data/home.json').then(()=>true,()=>false)};
})();
