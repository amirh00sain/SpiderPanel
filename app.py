from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from main import app

_HIDDEN_TABS = """
<style>
  [onclick*="switchTab('autoconfig'"] ,
  [onclick*="switchTab('tunnel'"] ,
  #tab-autoconfig,
  #tab-tunnel { display: none !important; }
</style>
"""

_SETTINGS_TOOLS = """
<div id="copilot-settings-tools" class="panel" style="margin-top:14px">
  <div class="panel-h"><span class="glow">IP و API Key</span></div>
  <div class="set-row"><span class="set-lbl">Public IP</span><span id="copilot-public-ip" dir="ltr">در حال دریافت...</span></div>
  <div class="set-row"><span class="set-lbl">API Key</span><code id="copilot-api-key" dir="ltr">--</code></div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
    <button class="btn btn-g" type="button" id="copilot-refresh-ip">دریافت IP</button>
    <button class="btn btn-p" type="button" id="copilot-rotate-key">ساخت API Key جدید</button>
    <button class="btn btn-g" type="button" id="copilot-copy-key">کپی API Key</button>
  </div>
  <div id="copilot-tools-message" style="margin-top:10px;color:var(--txt2);font-size:11px"></div>
</div>
<script>
(function(){
  function authFetch(url, opts){ return fetch(url, opts || {}).then(function(r){
    if(r.status === 401){ location.href='/login'; throw new Error('نشست منقضی شده است'); }
    return r;
  }); }
  function msg(t){ var e=document.getElementById('copilot-tools-message'); if(e)e.textContent=t; }
  function loadIp(){
    authFetch('/api/tools/my-ip').then(function(r){return r.json()}).then(function(d){
      var e=document.getElementById('copilot-public-ip');
      var v=d.ips && (d.ips.ipify || d.ips.icanhazip || d.ips.ipinfo) || 'نامشخص';
      if(e)e.textContent=v;
    }).catch(function(e){msg(e.message || 'دریافت IP ناموفق بود');});
  }
  function loadKey(){
    authFetch('/api/settings/security-token/rotate',{method:'POST'}).then(function(r){return r.json()}).then(function(d){
      var e=document.getElementById('copilot-api-key'); if(e)e.textContent=d.security_token || '--';
      msg('API Key ساخته شد و در تنظیمات ذخیره شد.');
    }).catch(function(e){msg(e.message || 'ساخت API Key ناموفق بود');});
  }
  document.addEventListener('DOMContentLoaded',function(){
    var ip=document.getElementById('copilot-refresh-ip'), key=document.getElementById('copilot-rotate-key'), copy=document.getElementById('copilot-copy-key');
    if(ip)ip.onclick=loadIp; if(key)key.onclick=loadKey;
    if(copy)copy.onclick=function(){var v=document.getElementById('copilot-api-key').textContent; navigator.clipboard.writeText(v).then(function(){msg('API Key کپی شد.');});};
    loadIp();
  });
})();
</script>
"""

@app.middleware("http")
async def deployment_ui_fixes(request: Request, call_next):
    if request.url.path == "/":
        return RedirectResponse("/spider", status_code=307)
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        return response
    body = b""
    async for chunk in response.body_iterator:
        body += chunk
    html = body.decode("utf-8", errors="replace")
    html = html.replace("</head>", _HIDDEN_TABS + "</head>", 1)
    if 'id="tab-settings"' in html:
        html = html.replace("</div>\n</body>", _SETTINGS_TOOLS + "</div>\n</body>", 1)
    # The HTML body was modified above, so headers tied to the original body
    # (especially Content-Length / Content-Encoding / ETag) are no longer valid.
    # Forward only safe response headers; HTMLResponse will calculate a fresh
    # Content-Length for the rewritten body. Keeping the old Content-Length is
    # what triggers Starlette/Uvicorn's "Response content longer than
    # Content-Length" runtime error on /login and /spider.
    safe_headers = {
        k: v for k, v in response.headers.items()
        if k.lower() not in {"content-length", "content-encoding", "etag", "transfer-encoding"}
    }
    return HTMLResponse(html, status_code=response.status_code, headers=safe_headers, media_type="text/html")
