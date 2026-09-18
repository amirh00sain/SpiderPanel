// Spider Panel — single-location VLESS Worker for Cloudflare Pages Advanced Mode
// Exact route: /ws/{uuid}. UUID must match the VLESS header and KV record.

import { connect } from "cloudflare:sockets";

const PANEL_TOKEN = __PANEL_TOKEN__;
const PANEL_DOMAIN = __PANEL_DOMAIN__;
const WORKER_DOMAIN = __WORKER_DOMAIN__;
const USAGE_FLUSH_BYTES = 256 * 1024;
const USAGE_FLUSH_MS = 250;
const IP_TTL = 900;
const IP_HEARTBEAT_MS = 300000;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {status, headers:{"content-type":"application/json","cache-control":"no-store","access-control-allow-origin":"*"}});
}
function authorized(request) { return (request.headers.get("Authorization") || "") === "Bearer " + PANEL_TOKEN; }
function uuidRe() { return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i; }
function workerPath(uuid) { return `/ws/${String(uuid || "").toLowerCase()}`; }
function clientIp(request) { const cf=request.headers.get("CF-Connecting-IP"); if(cf)return cf.trim(); const f=request.headers.get("x-forwarded-for"); return f?f.split(",")[0].trim():"unknown"; }

async function getUser(env, uuid) {
  uuid=String(uuid||"").toLowerCase(); if(!uuidRe().test(uuid)) return null;
  try {
    const raw=await env.SPIDER_KV.get("user:"+uuid); if(!raw)return null;
    const u=JSON.parse(raw); u.uuid=String(u.uuid||uuid).toLowerCase(); u.path=workerPath(u.uuid);
    if(u.expire && Date.now()/1000>Number(u.expire))return null;
    if(Number(u.limit_bytes||0)>0 && Number(u.used_bytes||0)>=Number(u.limit_bytes))return null;
    return u;
  } catch(_){return null;}
}
async function setUser(env,uuid,u){ uuid=String(uuid||"").toLowerCase(); u.uuid=uuid; u.path=workerPath(uuid); await env.SPIDER_KV.put("user:"+uuid,JSON.stringify(u)); }

async function flushUsage(env, uuid, holder) {
  if(!holder||holder.flushing||!holder.p)return true; holder.flushing=true; const amount=holder.p; holder.p=0;
  try { const u=await getUser(env,uuid); if(!u){holder.p+=amount;return false;} u.used_bytes=Number(u.used_bytes||0)+amount; await setUser(env,uuid,u); holder.last=Date.now(); return !(Number(u.limit_bytes||0)>0 && u.used_bytes>=Number(u.limit_bytes)); }
  catch(_){holder.p+=amount;return true;} finally {holder.flushing=false;}
}
async function addUsage(env,uuid,n,holder){ if(!n)return true; holder.p=(holder.p||0)+n; const now=Date.now(); if(holder.p>=USAGE_FLUSH_BYTES || now-(holder.last||now)>=USAGE_FLUSH_MS) return flushUsage(env,uuid,holder); return true; }

async function getIps(env,uuid){try{const r=await env.SPIDER_KV.get("ips:"+uuid);return r?JSON.parse(r):{ips:[]};}catch(_){return {ips:[]};}}
async function saveIps(env,uuid,rec){try{await env.SPIDER_KV.put("ips:"+uuid,JSON.stringify(rec));}catch(_){}}
async function touchIp(env,uuid,ip,max){ if(!ip||ip==="unknown"||ip==="127.0.0.1"||!max||max<1)return true; const now=Date.now()/1000; const rec=await getIps(env,uuid); const live=(rec.ips||[]).filter(x=>x&&Number(x.exp||0)>now); const ex=live.find(x=>x.ip===ip); if(ex)ex.exp=now+IP_TTL; else if(live.length>=max)return false; else live.push({ip,exp:now+IP_TTL}); await saveIps(env,uuid,{ips:live}); return true; }
async function removeIp(env,uuid,ip){if(!uuid||!ip||ip==="unknown")return;const rec=await getIps(env,uuid);const now=Date.now()/1000;rec.ips=(rec.ips||[]).filter(x=>x&&x.ip!==ip&&Number(x.exp||0)>now);await saveIps(env,uuid,rec);}

function formatUuid(b){if(!b||b.length!==16)return"";let h="";for(const x of b)h+=x.toString(16).padStart(2,"0");return h.slice(0,8)+"-"+h.slice(8,12)+"-"+h.slice(12,16)+"-"+h.slice(16,20)+"-"+h.slice(20);}
function parseVless(data){if(!(data instanceof Uint8Array)||data.length<24)return null;let p=0;const version=data[p++];const userId=formatUuid(data.subarray(p,p+16)).toLowerCase();p+=16;if(!uuidRe().test(userId)||p>=data.length)return null;const al=data[p++];if(p+al+4>data.length)return null;p+=al;const cmd=data[p++];if(cmd!==1)return {version,userId,command:cmd,unsupported:true};const port=(data[p]<<8)|data[p+1];p+=2;const at=data[p++];let address="";if(at===1){if(p+4>data.length)return null;address=Array.from(data.subarray(p,p+4)).join(".");p+=4;}else if(at===2){if(p>=data.length)return null;const n=data[p++];if(p+n>data.length)return null;address=new TextDecoder().decode(data.subarray(p,p+n));p+=n;}else if(at===3){if(p+16>data.length)return null;const b=data.subarray(p,p+16);p+=16;const a=[];for(let i=0;i<16;i+=2)a.push(((b[i]<<8)|b[i+1]).toString(16));address=a.join(":");}else{return null;}return {version,userId,command:cmd,address,port,payload:data.subarray(p)};}
async function openSocket(hostname,port){try{const sock=await connect({hostname,port});if(!sock?.readable||!sock?.writable)return null;return {socket:sock,reader:sock.readable.getReader(),writer:sock.writable.getWriter()};}catch(_){return null;}}

async function handleVlessWs(request,env,uuid){
  uuid=String(uuid||"").toLowerCase(); if(!uuidRe().test(uuid))return json({error:"bad uuid"},400);
  if(new URL(request.url).pathname!==workerPath(uuid))return json({error:"bad path"},404);
  const user=await getUser(env,uuid); if(!user)return json({error:"unauthorized or expired"},403); if(user.path!==workerPath(uuid))return json({error:"path mismatch"},403);
  const pair=new WebSocketPair(); const client=pair[0],server=pair[1]; server.accept(); server.binaryType="arraybuffer";
  const ip=clientIp(request); if(!await touchIp(env,uuid,ip,Number(user.concurrent_connections||0))){try{server.close(4031,"ip limit reached");}catch(_){}return new Response(null,{status:101,webSocket:client});}
  const usage={p:0,last:Date.now(),flushing:false}; let conn=null,closed=false,hb=null,writeChain=Promise.resolve(),header=null;
  const cleanup=async()=>{if(closed)return;closed=true;if(hb)clearInterval(hb);try{if(conn)conn.socket.close();}catch(_){}try{await flushUsage(env,uuid,usage);}catch(_){}try{await removeIp(env,uuid,ip);}catch(_){}};
  hb=setInterval(async()=>{try{await touchIp(env,uuid,ip,Number(user.concurrent_connections||0));}catch(_){}},IP_HEARTBEAT_MS);
  server.addEventListener("message",async(ev)=>{
    if(closed)return;const data=ev.data instanceof ArrayBuffer?new Uint8Array(ev.data):(ev.data instanceof Uint8Array?ev.data:new TextEncoder().encode(String(ev.data||"")));if(!data.length)return;
    if(!header){
      header=parseVless(data);if(!header||header.userId!==uuid){try{server.close(4002,"uuid mismatch");}catch(_){}await cleanup();return;}
      if(header.unsupported||header.command!==1){try{server.close(4004,"unsupported command");}catch(_){}await cleanup();return;}
      conn=await openSocket(header.address,header.port);if(!conn){try{server.close(4001,"outbound connect failed");}catch(_){}await cleanup();return;}
      writeChain=writeChain.then(()=>header.payload.length?conn.writer.write(header.payload):undefined);if(header.payload.length)await addUsage(env,uuid,header.payload.length,usage);
      (async()=>{let first=true;try{while(!closed){const {done,value}=await conn.reader.read();if(done)break;if(!value?.length)continue;await addUsage(env,uuid,value.length,usage);let frame=value;if(first){frame=new Uint8Array(value.length+2);frame[0]=header.version&255;frame[1]=0;frame.set(value,2);first=false;}try{server.send(frame);}catch(_){break;}}}catch(_){}try{server.close(1000,"closed");}catch(_){}await cleanup();})();
      return;
    }
    if(!conn)return;writeChain=writeChain.then(async()=>{if(closed)return;await conn.writer.write(data);await addUsage(env,uuid,data.length,usage);});try{await writeChain;}catch(_){try{server.close(4003,"write failed");}catch(__){}await cleanup();}
  });
  server.addEventListener("close",cleanup);server.addEventListener("error",cleanup);
  return new Response(null,{status:101,webSocket:client});
}
function buildConfig(uuid,remark){const u=String(uuid||"").toLowerCase();const host=WORKER_DOMAIN;const path=workerPath(u);const q=`encryption=none&security=tls&type=ws&host=${encodeURIComponent(host)}&path=${encodeURIComponent(path)}&sni=${encodeURIComponent(host)}&fp=chrome&alpn=http/1.1`;return `vless://${u}@${host}:443?${q}#${encodeURIComponent(remark||"Spider")}`;}

export default {async fetch(request,env){const url=new URL(request.url);const path=url.pathname;
  if(path==="/health"||path==="/")return new Response("Spider VLESS Worker online",{headers:{"content-type":"text/plain"}});
  if(path==="/panel/config"&&request.method==="POST"){if(!authorized(request))return json({error:"Forbidden"},403);let body;try{body=await request.json();}catch(_){return json({error:"bad json"},400);}const users=Array.isArray(body.users)?body.users:[];let written=0,traffic=0,online=0;const now=Date.now()/1000;const existing=await env.SPIDER_KV.list({prefix:"user:"});const keep=new Set();for(const src of users){const uuid=String(src.uuid||"").toLowerCase();if(!uuidRe().test(uuid)||src.disabled)continue;const rec={uuid,path:workerPath(uuid),remark:String(src.remark||"user"),limit_bytes:Number(src.limit_bytes)||0,expire:Number(src.expire)||0,used_bytes:Number(src.used_bytes)||0,concurrent_connections:Number(src.concurrent_connections)||0,created:Date.now()};keep.add("user:"+uuid);await setUser(env,uuid,rec);written++;traffic+=rec.used_bytes;if(rec.expire&&now>rec.expire)continue;if(rec.limit_bytes>0&&rec.used_bytes>=rec.limit_bytes)continue;online++;}for(const k of existing.keys)if(!keep.has(k.name))await env.SPIDER_KV.delete(k.name);await env.SPIDER_KV.put("settings",JSON.stringify({mode:"single-location",path_pattern:"/ws/{uuid}",panel_domain:PANEL_DOMAIN}));await env.SPIDER_KV.put("heartbeat",JSON.stringify({at:Date.now(),users:written}));return json({ok:true,users:written,traffic,online});}
  if(path==="/panel/status"&&request.method==="GET"){if(!authorized(request))return json({error:"Forbidden"},403);let users=0,traffic=0,online=0;const list=await env.SPIDER_KV.list({prefix:"user:"});const now=Date.now()/1000;for(const k of list.keys){try{const u=JSON.parse(await env.SPIDER_KV.get(k.name));if(!u)continue;users++;traffic+=Number(u.used_bytes||0);if(u.expire&&now>u.expire)continue;if(u.limit_bytes>0&&u.used_bytes>=u.limit_bytes)continue;online++;}catch(_){}}return json({ok:true,users,traffic,online,mode:"single-location",path_pattern:"/ws/{uuid}"});}
  if(path.startsWith("/api/")){if(!authorized(request))return json({error:"Forbidden"},403);if(path==="/api/users"&&request.method==="GET"){const out=[];const list=await env.SPIDER_KV.list({prefix:"user:"});for(const k of list.keys){const raw=await env.SPIDER_KV.get(k.name);if(raw)out.push(JSON.parse(raw));}return json({ok:true,users:out});}if(path==="/api/users"&&request.method==="POST"){const body=await request.json();const uuid=String(body.uuid||"").toLowerCase();if(!uuidRe().test(uuid))return json({error:"bad uuid"},400);const u={uuid,path:workerPath(uuid),remark:String(body.remark||"user"),limit_bytes:Number(body.limit_bytes)||0,expire:Number(body.expire)||0,used_bytes:Number(body.used_bytes)||0,concurrent_connections:Number(body.concurrent_connections)||0,created:Date.now()};await setUser(env,uuid,u);return json({ok:true,user:{...u,config:buildConfig(uuid,u.remark)}});}if(path.startsWith("/api/user/")){const uuid=path.split("/").pop().toLowerCase();if(!uuidRe().test(uuid))return json({error:"bad uuid"},400);if(request.method==="DELETE"){await env.SPIDER_KV.delete("user:"+uuid);return json({ok:true});}const u=await getUser(env,uuid);if(!u)return json({error:"not found"},404);return json({ok:true,user:{...u,config:buildConfig(uuid,u.remark)}});}return json({error:"Not Found"},404);}
  const seg=path.split("/").filter(Boolean);const first=(seg[0]||"").toLowerCase();if(first==="ws"&&seg.length===2&&uuidRe().test(seg[1])){if((request.headers.get("Upgrade")||"").toLowerCase()!=="websocket")return json({error:"websocket upgrade required"},400);return handleVlessWs(request,env,seg[1]);}
  if(env?.ASSETS?.fetch)return env.ASSETS.fetch(request);return json({error:"Not Found"},404);
}};
