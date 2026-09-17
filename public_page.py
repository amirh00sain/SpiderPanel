# Public subscription page HTML
def get_public_page_html(uuid_key: str) -> str:
    """صفحه پابلیک سابسکریپشن با تم Spider Panel"""
    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Spider Panel · اشتراک</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css">
<style>
:root{{
  --spider-blue:#2B7FFF;--spider-purple:#7B61FF;--spider-red:#FF2352;
  --success:#16A34A;--warning:#F59E0B;--danger:#DC2626;
  --text-primary:#0F172A;--text-secondary:#475569;
  --bg:#F8FAFC;--surface:rgba(255,255,255,.8);--surface-glass:rgba(255,255,255,.75);
  --border:rgba(226,232,240,.7);
  --radius:18px;--radius-lg:24px;--radius-xl:28px;
  --shadow-sm:0 2px 8px rgba(0,0,0,.04);--shadow-md:0 8px 24px rgba(0,0,0,.06);
  --shadow-lg:0 16px 48px rgba(0,0,0,.08);
}}
[data-theme="dark"]{{
  --text-primary:#F1F5F9;--text-secondary:#94A3B8;
  --bg:#0B1121;--surface:rgba(15,23,42,.85);--surface-glass:rgba(15,23,42,.7);
  --border:rgba(226,232,240,.06);
  --shadow-sm:0 2px 8px rgba(0,0,0,.2);--shadow-md:0 8px 24px rgba(0,0,0,.3);
  --shadow-lg:0 16px 48px rgba(0,0,0,.4);
}}
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{min-height:100%;background:var(--bg);font-family:'Vazirmatn',sans-serif;color:var(--text-primary);font-size:14px;transition:background .4s,color .4s}}
.bg-glow{{position:fixed;inset:0;z-index:0;pointer-events:none}}
.bg-orb{{position:absolute;border-radius:50%;filter:blur(120px);opacity:.15;animation:float 14s ease-in-out infinite}}
.bg-orb:nth-child(1){{width:450px;height:450px;background:var(--spider-blue);top:-150px;left:-100px}}
.bg-orb:nth-child(2){{width:350px;height:350px;background:var(--spider-purple);bottom:-100px;right:-80px;animation-delay:-7s}}
@keyframes float{{0%,100%{{transform:translateY(0) scale(1)}}50%{{transform:translateY(-25px) scale(1.04)}}}}
.wrap{{position:relative;z-index:10;max-width:680px;margin:0 auto;padding:28px 16px 60px}}
.top-bar{{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;gap:10px}}
.brand{{display:flex;align-items:center;gap:10px}}
.brand svg{{width:38px;height:38px;filter:drop-shadow(0 3px 8px rgba(43,127,255,.3))}}
.brand-name{{font-size:14px;font-weight:800;color:var(--text-primary)}}
.brand-sub{{font-size:9.5px;color:var(--text-secondary)}}
.theme-toggle{{width:38px;height:38px;border-radius:var(--radius);background:var(--surface);backdrop-filter:blur(12px);border:1px solid var(--border);color:var(--text-secondary);display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:16px;transition:all .2s}}
.theme-toggle:hover{{background:rgba(43,127,255,.1);color:var(--spider-blue)}}
/* Info card */
.info-card{{background:var(--surface);backdrop-filter:blur(20px) saturate(160%);-webkit-backdrop-filter:blur(20px) saturate(160%);border:1px solid var(--border);border-radius:var(--radius-xl);padding:28px 26px 24px;margin-bottom:18px;box-shadow:var(--shadow-lg);position:relative;overflow:hidden}}
.info-card::before{{content:'';position:absolute;top:0;right:0;width:180px;height:180px;background:radial-gradient(circle,rgba(123,97,255,.08),transparent 70%);pointer-events:none}}
.info-name{{font-size:22px;font-weight:800;color:var(--text-primary);position:relative;z-index:1;letter-spacing:-.02em}}
.info-desc{{font-size:12.5px;color:var(--text-secondary);margin-top:6px;position:relative;z-index:1;line-height:1.7}}
.info-stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:18px;position:relative;z-index:1}}
.info-stat{{text-align:center;padding:12px 8px;background:rgba(43,127,255,.04);border-radius:var(--radius);border:1px solid var(--border)}}
.info-s-val{{font-size:18px;font-weight:800;color:var(--text-primary)}}
.info-s-label{{font-size:9px;color:var(--text-secondary);margin-top:3px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}}
/* Config cards */
.section-title{{font-size:12.5px;font-weight:800;color:var(--text-secondary);margin-bottom:12px;display:flex;align-items:center;gap:7px;text-transform:uppercase;letter-spacing:.06em}}
.section-title i{{color:var(--spider-blue);font-size:15px}}
.cfg-card{{background:var(--surface);backdrop-filter:blur(16px);border:1px solid var(--border);border-radius:var(--radius-lg);padding:20px 20px 18px;margin-bottom:12px;box-shadow:var(--shadow-sm);transition:all .3s}}
.cfg-card:hover{{border-color:rgba(43,127,255,.2);transform:translateY(-2px);box-shadow:var(--shadow-md)}}
.cfg-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:12px}}
.cfg-name{{font-size:14px;font-weight:700;color:var(--text-primary)}}
.cfg-status{{font-size:10px;padding:3px 10px;border-radius:20px;font-weight:700;white-space:nowrap}}
.cfg-status.ok{{background:rgba(22,163,74,.1);color:var(--success)}}
.cfg-status.no{{background:rgba(255,35,82,.1);color:var(--spider-red)}}
.cfg-code{{background:rgba(15,23,42,.05);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;font-family:'Inter',monospace;font-size:10px;color:var(--spider-blue);word-break:break-all;line-height:1.7;margin-bottom:10px;max-height:80px;overflow-y:auto}}
[data-theme="dark"] .cfg-code{{background:rgba(0,0,0,.2);color:#60A5FA}}
.cfg-actions{{display:flex;gap:7px;flex-wrap:wrap}}
.btn{{font-family:'Vazirmatn',sans-serif;font-size:11.5px;font-weight:700;border-radius:var(--radius);padding:8px 15px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;border:none;transition:all .2s;white-space:nowrap}}
.btn i{{font-size:13px}}
.btn-p{{background:linear-gradient(90deg,var(--spider-blue),var(--spider-purple));color:#fff;box-shadow:0 4px 14px rgba(43,127,255,.25)}}
.btn-p:hover{{transform:translateY(-1px);box-shadow:0 6px 20px rgba(43,127,255,.35)}}
.btn-ghost{{background:rgba(43,127,255,.06);color:var(--spider-blue);border:1px solid rgba(43,127,255,.15)}}
.btn-ghost:hover{{background:rgba(43,127,255,.12)}}
/* Password lock */
.lock-page{{display:flex;align-items:center;justify-content:center;min-height:70vh}}
.lock-card{{background:var(--surface);backdrop-filter:blur(24px);border:1px solid var(--border);border-radius:var(--radius-xl);padding:40px 32px 34px;text-align:center;max-width:380px;width:100%;box-shadow:var(--shadow-lg)}}
.lock-icon{{width:64px;height:64px;border-radius:18px;background:rgba(43,127,255,.1);border:1px solid rgba(43,127,255,.2);display:flex;align-items:center;justify-content:center;margin:0 auto 18px;font-size:28px;color:var(--spider-blue)}}
.lock-title{{font-size:18px;font-weight:800;margin-bottom:6px}}
.lock-sub{{font-size:12px;color:var(--text-secondary);margin-bottom:20px;line-height:1.7}}
.lock-field{{position:relative;margin-bottom:14px}}
.lock-input{{width:100%;padding:13px 44px;border-radius:var(--radius);border:1.5px solid var(--border);background:rgba(255,255,255,.5);backdrop-filter:blur(8px);color:var(--text-primary);font-family:inherit;font-size:14px;text-align:center;outline:none;transition:all .2s;letter-spacing:.1em}}
.lock-input:focus{{border-color:var(--spider-blue);box-shadow:0 0 0 4px rgba(43,127,255,.1)}}
.lock-icon-left{{position:absolute;right:14px;top:50%;transform:translateY(-50%);color:var(--text-secondary);font-size:16px;pointer-events:none}}
.lock-err{{color:var(--spider-red);font-size:11px;margin-bottom:10px;min-height:18px}}
/* Toast */
.toast{{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(40px);background:var(--surface);backdrop-filter:blur(16px);border:1px solid var(--border);color:var(--text-primary);border-radius:var(--radius);padding:10px 20px;font-size:12px;opacity:0;transition:all .3s;z-index:999;pointer-events:none;white-space:nowrap;box-shadow:var(--shadow-md)}}
.toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
.toast.ok{{border-color:rgba(22,163,74,.3);color:var(--success)}}
/* Empty */
.empty{{text-align:center;padding:60px 20px;color:var(--text-secondary)}}
.empty i{{font-size:40px;opacity:.3;display:block;margin-bottom:12px}}
.footer{{text-align:center;padding-top:30px;font-size:10.5px;color:var(--text-secondary)}}
@media(max-width:500px){{
  .info-stats{{grid-template-columns:1fr 1fr}}
  .wrap{{padding:20px 12px 50px}}
}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
</style>
</head>
<body data-theme="light">
<div class="bg-glow"><div class="bg-orb"></div><div class="bg-orb"></div></div>
<div class="toast" id="toast"></div>
<div class="wrap">
  <div class="top-bar">
    <div class="brand">
      <svg width="38" height="38" viewBox="0 0 100 100"><defs><linearGradient id="bgg" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" style="stop-color:#2B7FFF"/><stop offset="50%" style="stop-color:#7B61FF"/><stop offset="100%" style="stop-color:#FF2352"/></linearGradient></defs><ellipse cx="50" cy="48" rx="17" ry="20" fill="url(#bgg)"/><ellipse cx="50" cy="26" rx="11" ry="9" fill="url(#bgg)"/><circle cx="46" cy="23" r="2.5" fill="#fff"/><circle cx="54" cy="23" r="2.5" fill="#fff"/><path d="M50,50 L12,16 M50,50 L16,50 M50,50 L12,84 M50,50 L88,16 M50,50 L84,50 M50,50 L88,84" stroke="url(#bgg)" stroke-width="2.2" fill="none" stroke-linecap="round"/></svg>
      <div><div class="brand-name">Spider Panel</div><div class="brand-sub">Spider Gateway</div></div>
    </div>
    <button class="theme-toggle" onclick="toggleTheme()" title="تغییر تم"><i class="ti ti-sun" id="theme-icon"></i></button>
  </div>
  <div id="root"><div class="empty"><i class="ti ti-loader-2" style="animation:spin 1s linear infinite"></i><p>در حال بارگذاری...</p></div></div>
  <div class="footer">Spider Panel · Spider Gateway</div>
</div>
<script>
var UUID_KEY='{uuid_key}',savedPw='',currentData=null;
var isDark=localStorage.getItem('spider-pub-theme')==='dark';
function applyTheme(d){{document.documentElement.setAttribute('data-theme',d?'dark':'light');document.getElementById('theme-icon').className='ti '+(d?'ti-sun':'ti-moon')}}
function toggleTheme(){{isDark=!isDark;localStorage.setItem('spider-pub-theme',isDark?'dark':'light');applyTheme(isDark)}}
applyTheme(isDark);
function toast(m,t){{var el=document.getElementById('toast');el.textContent=m;el.className='toast show'+(t?' '+t:'');setTimeout(function(){{el.classList.remove('show')}},2400)}}
function esc(s){{return String(s||'').replace(/[&<>"']/g,function(c){{return{{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]}})}}
function fmtB(b){{if(!b)return'0 B';if(b<1024)return b+' B';if(b<1024**2)return(b/1024).toFixed(1)+' KB';if(b<1024**3)return(b/1024**2).toFixed(2)+' MB';return(b/1024**3).toFixed(2)+' GB'}}
function toFa(n){{return String(n).replace(/\\d/g,function(d){{return'۰۱۲۳۴۵۶۷۸۹'[d]}})}}

async function loadData(pw){{var u='/api/public/sub/'+UUID_KEY+(pw?'?pw='+encodeURIComponent(pw):'');var r=await fetch(u);return r.json()}}

function renderLock(name,err){{document.getElementById('root').innerHTML='<div class="lock-page"><div class="lock-card"><div class="lock-icon"><i class="ti ti-shield-lock"></i></div><div class="lock-title">'+esc(name)+'</div><div class="lock-sub">این گروه با رمز محافظت می‌شود</div><div class="lock-err" id="lock-err">'+(err?'<i class="ti ti-alert-circle"></i> '+esc(err):'')+'</div><div class="lock-field"><i class="ti ti-lock lock-icon-left"></i><input class="lock-input" type="password" id="lock-pw" placeholder="••••••••" autofocus></div><button class="btn btn-p" style="width:100%;justify-content:center;padding:12px" onclick="submitLock()"><i class="ti ti-lock-open"></i> ورود</button></div></div>';document.getElementById('lock-pw').addEventListener('keydown',function(e){{if(e.key==='Enter')submitLock()}})}}

async function submitLock(){{var pw=document.getElementById('lock-pw').value;var d=await loadData(pw);if(d.locked){{renderLock(d.name,'رمز اشتباه است');return}}savedPw=pw;renderContent(d)}}

function renderContent(d){{currentData=d;var active=d.links.filter(function(l){{return l.active}}).length;var subUrl=d.sub_url||(location.protocol+'//'+location.host+'/sub-group/'+UUID_KEY);subUrl+=savedPw?'?pw='+encodeURIComponent(savedPw):'';
document.getElementById('root').innerHTML='<div class="info-card"><div class="info-name">'+esc(d.name)+'</div>'+(d.desc?'<div class="info-desc">'+esc(d.desc)+'</div>':'')+
'<div class="info-stats"><div class="info-stat"><div class="info-s-val">'+toFa(active)+'</div><div class="info-s-label">کانفیگ فعال</div></div><div class="info-stat"><div class="info-s-val">'+toFa(d.links.length)+'</div><div class="info-s-label">کل کانفیگ‌ها</div></div><div class="info-stat"><div class="info-s-val">'+esc(d.total_used_fmt||'0')+'</div><div class="info-s-label">مصرف</div></div></div></div>'+
'<div class="section-title"><i class="ti ti-link"></i> کانفیگ‌ها ('+toFa(d.links.length)+' عدد)</div>'+
(d.links.length?d.links.map(function(l){{return'<div class="cfg-card"><div class="cfg-head"><div class="cfg-name">'+esc(l.label)+'</div><span class="cfg-status '+(l.active?'ok':'no')+'">'+(l.active?'<i class="ti ti-circle-check"></i> فعال':'<i class="ti ti-circle-x"></i> غیرفعال')+'</span></div><div class="cfg-code">'+esc(l.vless_link)+'</div><div class="cfg-actions"><button class="btn btn-p" onclick="navigator.clipboard.writeText(\''+esc(l.vless_link).replace(/'/g,"\\'")+'\').then(function(){{toast(\'کپی شد ✓\',\'ok\')}})"><i class="ti ti-copy"></i> کپی لینک</button><button class="btn btn-ghost" onclick="window.open(\'https://api.qrserver.com/v1/create-qr-code/?size=220x220&data='+encodeURIComponent(l.vless_link)+'\',\'_blank\')"><i class="ti ti-qrcode"></i> QR Code</button></div></div>'}}).join(''):'<div class="empty"><i class="ti ti-link-off"></i><p>کانفیگی در این گروه وجود ندارد</p></div>')+
'<div style="margin-top:16px;text-align:center"><button class="btn btn-ghost" style="justify-content:center" onclick="location.reload()"><i class="ti ti-refresh"></i> بروزرسانی</button></div>';
}}

async function init(){{try{{var d=await loadData();if(d.locked){{renderLock(d.name);return}}renderContent(d)}}catch(e){{document.getElementById('root').innerHTML='<div class="empty"><i class="ti ti-alert-circle" style="color:var(--spider-red)"></i><p>خطا در بارگذاری</p></div>'}}}}
init();
</script>
</body></html>"""
