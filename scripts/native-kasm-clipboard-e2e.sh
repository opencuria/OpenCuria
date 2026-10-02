#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARTIFACTS="/workspace/.opencuria/playwright"
RUN_ID="native-vnc-$$"
RUNTIME="$ARTIFACTS/$RUN_ID"
DISPLAY_ID=""
HTTP_PORT=""
XVNC_PID=""
HTTP_PID=""
NATIVE_UI_PID=""
VM_TEXT_FILE=""

cleanup() {
  local pid
  for pid in "$HTTP_PID" "$NATIVE_UI_PID" "$XVNC_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  if [[ -n "$DISPLAY_ID" ]]; then
    for _ in $(seq 1 50); do
      [[ ! -S "/tmp/.X11-unix/X$DISPLAY_ID" ]] && break
      sleep 0.1
    done
  fi
}
trap cleanup EXIT INT TERM

for candidate in $(seq 120 180); do
  http=$((8100 + candidate))
  if [[ ! -S "/tmp/.X11-unix/X$candidate" ]] &&
     ! ss -ltnH "sport = :$((5900 + candidate))" | grep -q . &&
     ! ss -ltnH "sport = :$((6900 + candidate))" | grep -q . &&
     ! ss -ltnH "sport = :$http" | grep -q .; then
    DISPLAY_ID="$candidate"
    HTTP_PORT="$http"
    break
  fi
done
if [[ -z "$DISPLAY_ID" ]]; then
  echo "No free isolated Xvnc/websocket/http port tuple in display range :120-:180" >&2
  exit 1
fi

mkdir -p "$RUNTIME/site/dist" "$ARTIFACTS"
VM_TEXT_FILE="$RUNTIME/native-text.txt"
cp /usr/share/kasmvnc/www/vnc.html "$RUNTIME/site/vnc.html"
cp /usr/share/kasmvnc/www/dist/*.bundle.js /usr/share/kasmvnc/www/dist/*.bundle.css "$RUNTIME/site/dist/"
cp -r /usr/share/kasmvnc/www/dist/fonts /usr/share/kasmvnc/www/dist/images "$RUNTIME/site/dist/"
cp -r /usr/share/kasmvnc/www/app /usr/share/kasmvnc/www/vendor "$RUNTIME/site/"
printf '{"version":"1.3.3"}\n' > "$RUNTIME/site/package.json"
cat > "$RUNTIME/site/host.html" <<HTML
<!doctype html><meta charset="utf-8"><title>OpenCuria native clipboard transport lab</title>
<style>body{margin:0;background:#111;color:white;font:14px sans-serif}header{height:28px;padding:6px 12px;background:#222}iframe{width:1024px;height:768px;border:0;display:block}</style>
<header>Isolated OpenCuria clipboard transport lab · KasmVNC 1.3.3 · display :$DISPLAY_ID</header>
<iframe id="desktop" allow="clipboard-read; clipboard-write" src="/vnc.html?host=127.0.0.1&port=$((6900 + DISPLAY_ID))&path=websockify&encrypt=0&autoconnect=true&clipboard_up=true&clipboard_down=true&clipboard_seamless=true&resize=scale"></iframe>
<textarea id="parent-user-input" aria-label="Host dashboard input"></textarea>
<script>
let sequence=0,connected=false;window.clipboardMessages=[];window.sentContexts=[];
const testContext={enabled:true,visible:true,parentFocused:true,computerUseActive:false,workspaceId:'native-isolated-test'};
function sendContext(){const frame=document.getElementById('desktop');if(!frame.contentWindow)return;const message={action:'opencuria_clipboard',version:1,kind:'context',sequence:++sequence,...testContext,connected};window.sentContexts.push(message);frame.contentWindow.postMessage(message,location.origin)}
window.setTestContext=(values)=>{Object.assign(testContext,values);sendContext()};
window.requestNativeClipboardPanel=()=>{document.getElementById('desktop').contentWindow.postMessage({action:'opencuria_clipboard',version:1,kind:'open-panel'},location.origin)};
addEventListener('message',event=>{const frame=document.getElementById('desktop');if(event.source!==frame.contentWindow||event.origin!==location.origin)return;window.clipboardMessages.push(event.data);if(event.data?.action==='connection_state'){connected=event.data.value==='connected';sendContext()}if(event.data?.action==='opencuria_clipboard'&&event.data.kind==='ready')sendContext()});
addEventListener('focus',sendContext);addEventListener('blur',()=>setTimeout(()=>window.setTestContext({parentFocused:document.visibilityState==='visible'&&document.hasFocus()}),0));document.addEventListener('focusin',event=>{if(event.target===document.getElementById('desktop'))window.setTestContext({parentFocused:true})});document.addEventListener('visibilitychange',()=>window.setTestContext({parentFocused:document.visibilityState==='visible'&&document.hasFocus()}));
</script>
HTML
cat > "$RUNTIME/proxy.py" <<'PY'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import os, sys
sys.path.insert(0, os.environ['OPENCURIA_BACKEND'])
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from apps.runners.desktop_proxy import apply_vnc_client_patches
root=Path(os.environ['NATIVE_SITE']).resolve()
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  path=urlsplit(self.path).path
  target='/host.html' if path in ('/','/host.html') else path
  file=(root/target.lstrip('/')).resolve()
  if root not in file.parents and file != root: self.send_error(404); return
  if not file.is_file(): self.send_error(404); return
  body=file.read_bytes()
  content=b'text/html; charset=utf-8' if target.endswith('.html') else b'application/javascript' if target.endswith('.js') else b'application/json' if target.endswith('.json') else b'text/css'
  headers=[[b'content-type',content]]
  try: headers,body=apply_vnc_client_patches(target,headers,body)
  except ValueError: self.send_error(502); return
  self.send_response(200)
  for key,value in headers:self.send_header(key.decode(),value.decode())
  self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
ThreadingHTTPServer(('127.0.0.1',int(os.environ['NATIVE_HTTP_PORT'])),Handler).serve_forever()
PY
export OPENCURIA_BACKEND="$ROOT/backend" NATIVE_SITE="$RUNTIME/site" NATIVE_HTTP_PORT="$HTTP_PORT" NATIVE_HTTP_URL="http://127.0.0.1:$HTTP_PORT/host.html" NATIVE_X_DISPLAY=":$DISPLAY_ID" NATIVE_WS_PORT="$((6900 + DISPLAY_ID))" NATIVE_ARTIFACT="$ARTIFACTS/native-kasm-clipboard.png" NATIVE_VM_TEXT_FILE="$VM_TEXT_FILE"
Xvnc ":$DISPLAY_ID" -geometry 1024x768 -depth 24 -rfbport "$((5900 + DISPLAY_ID))" -SecurityTypes None -disableBasicAuth -websocketPort "$((6900 + DISPLAY_ID))" -httpd /usr/share/kasmvnc/www -interface 127.0.0.1 -AlwaysShared -AcceptKeyEvents -AcceptPointerEvents -SendCutText -AcceptCutText -AcceptSetDesktopSize=0 -DLP_ClipTypes text/plain,text/html,image/png > "$RUNTIME/xvnc.log" 2>&1 &
XVNC_PID=$!
for _ in $(seq 1 100); do
  if [[ -S "/tmp/.X11-unix/X$DISPLAY_ID" ]] && ss -ltnH "sport = :$((6900 + DISPLAY_ID))" | grep -q .; then break; fi
  if ! kill -0 "$XVNC_PID" 2>/dev/null; then cat "$RUNTIME/xvnc.log" >&2; exit 1; fi
  sleep 0.1
done
[[ -S "/tmp/.X11-unix/X$DISPLAY_ID" ]] || { echo "Xvnc did not start" >&2; exit 1; }
DISPLAY=":$DISPLAY_ID" python3 "$ROOT/e2e/fixtures/native-clipboard-desktop.py" "$VM_TEXT_FILE" > "$RUNTIME/native-ui.log" 2>&1 &
NATIVE_UI_PID=$!
for _ in $(seq 1 100); do
  [[ -s "$VM_TEXT_FILE" ]] && break
  if ! kill -0 "$NATIVE_UI_PID" 2>/dev/null; then cat "$RUNTIME/native-ui.log" >&2; exit 1; fi
  sleep 0.1
done
[[ -s "$VM_TEXT_FILE" ]] || { echo "Native GUI text fixture did not start" >&2; exit 1; }
"$ROOT/backend/.venv/bin/python" "$RUNTIME/proxy.py" > "$RUNTIME/http.log" 2>&1 &
HTTP_PID=$!
for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:$HTTP_PORT/host.html" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$HTTP_PID" 2>/dev/null; then cat "$RUNTIME/http.log" >&2; exit 1; fi
  sleep 0.1
done
curl -fsS "http://127.0.0.1:$HTTP_PORT/dist/main.bundle.js" -o "$RUNTIME/patched-main.bundle.js"
node --check "$RUNTIME/patched-main.bundle.js"
EXPECTED_PATCHED_SHA="$($ROOT/backend/.venv/bin/python - <<'PY'
import hashlib
import os
import sys

sys.path.insert(0, os.environ['OPENCURIA_BACKEND'])
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from apps.runners.desktop_proxy import (
    _KASM_133_PATCHED_BUNDLE_SIZE,
    _KASM_133_PATCHED_SHA256,
    apply_vnc_client_patches,
    patch_kasm_133_clipboard_bundle,
)
source = open('/usr/share/kasmvnc/www/dist/main.bundle.js', 'rb').read()
_, patched = apply_vnc_client_patches('/dist/main.bundle.js', [], source)
assert len(patched) == _KASM_133_PATCHED_BUNDLE_SIZE
assert hashlib.sha256(patched).hexdigest() == _KASM_133_PATCHED_SHA256
patch_kasm_133_clipboard_bundle.cache_clear()
assert patch_kasm_133_clipboard_bundle(patched) == patched
print(hashlib.sha256(patched).hexdigest())
PY
)"
[[ "$(sha256sum "$RUNTIME/patched-main.bundle.js" | cut -d' ' -f1)" == "$EXPECTED_PATCHED_SHA" ]]
cd "$ROOT/e2e"
NATIVE_KASM_CLIPBOARD_E2E=1 NATIVE_VM_TEXT_FILE="$VM_TEXT_FILE" E2E_BASE_URL="${E2E_BASE_URL:-http://127.0.0.1:5173}" npx playwright test --config=playwright.native-clipboard.config.ts 2>&1 | tee "$RUNTIME/playwright.log"
printf 'Isolated real KasmVNC 1.3.3 clipboard E2E passed; artifact: %s\n' "$NATIVE_ARTIFACT"
