#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clipboard Bridge  -  iPhone <-> Windows / Mac のクリップボード共有
================================================================
同じ Wi-Fi 上で、PC のクリップボードと iPhone を双方向で同期します。

使い方:
    python clipboard_bridge.py
起動すると LAN 上の URL (例: http://192.168.1.23:8765) が表示されます。
iPhone の Safari でその URL を開き「ホーム画面に追加」すれば即利用できます。

依存ライブラリ不要（Python 標準ライブラリのみ）。
pyperclip / qrcode が入っていれば自動で利用します（任意）。
"""

import os
import sys
import json
import time
import queue
import socket
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
MAX_BODY = 5 * 1024 * 1024  # 5MB まで

# ---------------------------------------------------------------------------
# クリップボードのバックエンド（OS ごとに get / set を用意）
# ---------------------------------------------------------------------------

def _make_backend():
    """利用可能な最良のクリップボード backend を返す: (get, set, name)"""

    # 1) pyperclip があれば最優先（最も堅牢・全OS対応）
    try:
        import pyperclip  # type: ignore
        return (lambda: pyperclip.paste(),
                lambda t: pyperclip.copy(t),
                "pyperclip")
    except Exception:
        pass

    # 2) macOS: pbpaste / pbcopy（UTF-8 ロケールを強制して文字化けを防ぐ）
    if sys.platform == "darwin":
        env = {**os.environ, "LC_CTYPE": "UTF-8", "LANG": "en_US.UTF-8"}
        def get():
            r = subprocess.run(["pbpaste"], capture_output=True, env=env)
            return r.stdout.decode("utf-8", "replace")
        def setc(t):
            subprocess.run(["pbcopy"], input=t.encode("utf-8"), env=env)
        return (get, setc, "pbcopy/pbpaste")

    # 3) Windows: Win32 API を ctypes で直接叩く（追加インストール不要）
    if sys.platform == "win32":
        return _win_backend()

    # 4) Linux: xclip / xsel / wl-clipboard のいずれか
    for getcmd, setcmd, name in (
        (["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard"], "xclip"),
        (["xsel", "-b", "-o"], ["xsel", "-b", "-i"], "xsel"),
        (["wl-paste", "-n"], ["wl-copy"], "wl-clipboard"),
    ):
        try:
            subprocess.run(getcmd, capture_output=True, check=False)
            def get(_c=getcmd):
                r = subprocess.run(_c, capture_output=True)
                return r.stdout.decode("utf-8", "replace")
            def setc(t, _c=setcmd):
                subprocess.run(_c, input=t.encode("utf-8"))
            return (get, setc, name)
        except FileNotFoundError:
            continue

    raise RuntimeError("対応するクリップボードツールが見つかりませんでした。")


def _win_backend():
    """Windows 用: ctypes による CF_UNICODETEXT 読み書き（64bit 安全）。"""
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # ポインタ切り捨て防止のため restype / argtypes を明示
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    def _open(retries=5):
        for _ in range(retries):
            if user32.OpenClipboard(None):
                return True
            time.sleep(0.01)
        return False

    def get():
        if not _open():
            return ""
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def setc(text):
        data = text.encode("utf-16-le") + b"\x00\x00"
        if not _open():
            return
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            # 成功したら所有権が OS に移るので解放しない
            user32.SetClipboardData(CF_UNICODETEXT, handle)
        finally:
            user32.CloseClipboard()

    return (get, setc, "win32-ctypes")


clip_get, clip_set, BACKEND = _make_backend()

# ---------------------------------------------------------------------------
# 受信時の自動ペースト（任意・Windows のみ）。
#   POST /clip でテキストを受け取り、クリップボードへ入れた直後に、
#   今フォーカスのあるウィンドウへ Ctrl+V を送って自動で貼り付ける。
#   ・既定 OFF（誤爆防止）。Web UI のチェック、または起動時 --paste で ON。
#   ・既存のクリップボード共有は一切変えない（後方互換・追加機能）。
# ---------------------------------------------------------------------------
AUTO_PASTE = {"on": False}

def _win_paste():
    """Windows: 今フォーカスのある入力欄へ Ctrl+V を送る（標準ライブラリ ctypes のみ）。"""
    try:
        import ctypes, time as _t
        _t.sleep(0.12)  # 直前の応答・フォーカス安定を待つ
        u = ctypes.windll.user32
        VK_CONTROL, VK_V, KEYUP = 0x11, 0x56, 0x0002
        u.keybd_event(VK_CONTROL, 0, 0, 0)       # Ctrl ↓
        u.keybd_event(VK_V, 0, 0, 0)             # V ↓
        u.keybd_event(VK_V, 0, KEYUP, 0)         # V ↑
        u.keybd_event(VK_CONTROL, 0, KEYUP, 0)   # Ctrl ↑
    except Exception as e:
        print("[autopaste] 失敗:", e)

def maybe_autopaste():
    """設定が ON かつ Windows のときだけ、別スレッドで自動ペーストする。"""
    if AUTO_PASTE["on"] and sys.platform == "win32":
        threading.Thread(target=_win_paste, daemon=True).start()


# ---------------------------------------------------------------------------
# Hub: 最新クリップボード状態の保持と SSE 配信
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.text = ""
        self.ts = time.time()
        self.origin = "pc"        # "pc" or "phone"
        self.subs = set()         # SSE 購読者の Queue 集合

    def snapshot(self):
        with self.lock:
            return {"text": self.text, "ts": self.ts, "origin": self.origin}

    def _broadcast_locked(self):
        payload = {"text": self.text, "ts": self.ts, "origin": self.origin}
        dead = []
        for q in self.subs:
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            self.subs.discard(q)

    def update_from_pc(self, text):
        """ポーラーが OS クリップボードの変化を検知したとき。"""
        with self.lock:
            if text == self.text:
                return False
            self.text = text
            self.ts = time.time()
            self.origin = "pc"
            self._broadcast_locked()
            return True

    def push_from_remote(self, text, setter):
        """iPhone から送られてきたとき。OS への書き込みも lock 内で行い競合を防ぐ。"""
        with self.lock:
            if text == self.text:
                return False
            try:
                setter(text)
            except Exception as e:
                print("clipboard write error:", e)
            self.text = text
            self.ts = time.time()
            self.origin = "phone"
            self._broadcast_locked()
            return True

    def subscribe(self):
        q = queue.Queue(maxsize=16)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)


hub = Hub()


def poller(stop_event):
    """OS のクリップボードを定期的に読み、変化を Hub に反映する。"""
    # 起動時の現在値を取り込む
    try:
        hub.text = clip_get() or ""
    except Exception:
        pass
    while not stop_event.is_set():
        try:
            t = clip_get()
            if t is not None:
                hub.update_from_pc(t)
        except Exception:
            pass
        stop_event.wait(0.4)


# ---------------------------------------------------------------------------
# Web UI (静的 HTML。データは JS が /clip, /events から取得)
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Clip Bridge">
<meta name="theme-color" content="#0b0f1a">
<title>Clipboard Bridge</title>
<style>
  :root{ --bg:#0b0f1a; --card:#161c2d; --card2:#1d2540; --line:#2a3350;
         --fg:#eef2ff; --mut:#8b97c4; --accent:#5b8cff; --accent2:#7c5bff;
         --ok:#2ecc71; --warn:#ff5b6e; }
  *{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html,body{ margin:0; height:100%; }
  body{ background:
          radial-gradient(1200px 600px at 80% -10%, #1a2f5a 0, transparent 60%),
          radial-gradient(900px 500px at -10% 110%, #20184a 0, transparent 55%),
          var(--bg);
        color:var(--fg);
        font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Yu Gothic",system-ui,sans-serif;
        padding:max(16px,env(safe-area-inset-top)) 16px calc(16px + env(safe-area-inset-bottom));
        max-width:640px; margin:0 auto; }
  header{ display:flex; align-items:center; gap:10px; padding:6px 2px 16px; }
  .logo{ width:34px; height:34px; border-radius:10px;
         background:linear-gradient(135deg,var(--accent),var(--accent2));
         display:grid; place-items:center; font-size:18px; box-shadow:0 6px 20px #5b8cff55; }
  h1{ font-size:18px; margin:0; font-weight:700; letter-spacing:.2px; }
  .status{ margin-left:auto; display:flex; align-items:center; gap:7px; font-size:12px; color:var(--mut); }
  .dot{ width:9px; height:9px; border-radius:50%; background:var(--warn); transition:.3s; box-shadow:0 0 0 0 #2ecc7100;}
  .dot.on{ background:var(--ok); box-shadow:0 0 0 4px #2ecc7122; }
  .card{ background:linear-gradient(180deg,var(--card),var(--card2));
         border:1px solid var(--line); border-radius:18px; padding:16px;
         margin-bottom:14px; box-shadow:0 10px 30px #00000055; }
  .label{ font-size:12px; color:var(--mut); display:flex; align-items:center; gap:8px; margin-bottom:10px; }
  .tag{ font-size:10px; padding:2px 8px; border-radius:999px; border:1px solid var(--line); color:var(--mut); }
  .pcbox{ font-size:16px; line-height:1.5; white-space:pre-wrap; word-break:break-word;
          background:#0d1322; border:1px solid var(--line); border-radius:12px;
          padding:14px; min-height:56px; max-height:34vh; overflow:auto; }
  .pcbox.empty{ color:var(--mut); }
  textarea{ width:100%; font-size:16px; line-height:1.5; color:var(--fg);
            background:#0d1322; border:1px solid var(--line); border-radius:12px;
            padding:14px; min-height:96px; resize:vertical; font-family:inherit; }
  textarea:focus{ outline:none; border-color:var(--accent); }
  .row{ display:flex; gap:10px; margin-top:12px; }
  button{ flex:1; font-size:16px; font-weight:700; color:#fff; border:none; cursor:pointer;
          padding:15px; border-radius:13px; transition:transform .06s, filter .2s; font-family:inherit; }
  button:active{ transform:scale(.97); }
  .primary{ background:linear-gradient(135deg,var(--accent),var(--accent2)); box-shadow:0 8px 20px #5b8cff44; }
  .ghost{ background:#222b45; color:var(--fg); border:1px solid var(--line); }
  .ts{ font-size:11px; color:var(--mut); margin-top:8px; }
  .hint{ font-size:12px; color:var(--mut); text-align:center; padding:6px 0 2px; line-height:1.6; }
  .toast{ position:fixed; left:50%; bottom:calc(26px + env(safe-area-inset-bottom));
          transform:translateX(-50%) translateY(20px);
          background:rgba(20,26,46,.96); border:1px solid var(--line); color:var(--fg);
          padding:12px 20px; border-radius:13px; font-size:14px; opacity:0; pointer-events:none;
          transition:.25s; box-shadow:0 12px 30px #000a; z-index:9; }
  .toast.show{ opacity:1; transform:translateX(-50%) translateY(0); }
</style>
</head>
<body>
  <header>
    <div class="logo">📋</div>
    <h1>Clipboard Bridge</h1>
    <div class="status"><span id="dot" class="dot"></span><span id="stat">接続中…</span></div>
  </header>
  <div style="text-align:center;margin:6px 0 2px;font-size:13px;color:var(--fg)">
    <label><input type="checkbox" id="ap" onchange="toggleAP()"> 受信したら自動で貼り付け（Windowsのみ）</label>
  </div>

  <div class="card">
    <div class="label">PC のクリップボード <span id="origin" class="tag">PC</span></div>
    <div id="pcbox" class="pcbox empty">（まだ何もありません）</div>
    <div class="row">
      <button class="primary" onclick="copyPC()">📥 iPhone にコピー</button>
    </div>
    <div id="ts" class="ts"></div>
  </div>

  <div class="card">
    <div class="label">iPhone → PC へ送信</div>
    <textarea id="box" placeholder="ここに貼り付け / 入力して送信"></textarea>
    <div class="row">
      <button class="ghost" onclick="pasteHere()">📋 貼り付け</button>
      <button class="primary" onclick="send()">📤 PC へ送信</button>
    </div>
  </div>

  <div class="hint">
    ホーム画面に追加するとアプリのように使えます。<br>
    背面タップにショートカットを割り当てると、開かずに送受信できます。
  </div>
  <div id="toast" class="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
let current = "";

function relTime(ts){
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if(s < 5) return "たった今";
  if(s < 60) return s + "秒前";
  if(s < 3600) return Math.floor(s/60) + "分前";
  return Math.floor(s/3600) + "時間前";
}
function render(d){
  current = d.text || "";
  const box = $("pcbox");
  if(current){ box.textContent = current; box.classList.remove("empty"); }
  else { box.textContent = "（まだ何もありません）"; box.classList.add("empty"); }
  $("origin").textContent = d.origin === "phone" ? "iPhone" : "PC";
  $("ts").textContent = d.ts ? "更新: " + relTime(d.ts) : "";
  $("ts").dataset.ts = d.ts || 0;
}
function setStatus(on){
  $("dot").classList.toggle("on", on);
  $("stat").textContent = on ? "接続中" : "再接続中…";
}
let toastT;
function toast(msg){
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toastT); toastT = setTimeout(()=>t.classList.remove("show"), 1600);
}

async function copyPC(){
  if(!current){ toast("コピーする内容がありません"); return; }
  try{
    await navigator.clipboard.writeText(current);
    toast("iPhone にコピーしました");
  }catch(e){
    const ta=document.createElement("textarea"); ta.value=current;
    ta.style.position="fixed"; ta.style.opacity="0"; document.body.appendChild(ta);
    ta.focus(); ta.select(); ta.setSelectionRange(0, current.length);
    try{ document.execCommand("copy"); toast("iPhone にコピーしました"); }
    catch(_){ toast("コピーできませんでした"); }
    document.body.removeChild(ta);
  }
}
async function pasteHere(){
  try{ $("box").value = await navigator.clipboard.readText(); }
  catch(e){ $("box").focus(); toast("長押しして手動で貼り付けてください"); }
}
async function send(){
  const t = $("box").value;
  if(!t){ toast("送信する内容がありません"); return; }
  try{
    await fetch("/clip", {method:"POST",
      headers:{"Content-Type":"text/plain; charset=utf-8"}, body:t});
    toast("PC へ送信しました");
  }catch(e){ toast("送信に失敗しました"); }
}

function connect(){
  const es = new EventSource("/events");
  es.onmessage = (e)=>{ render(JSON.parse(e.data)); setStatus(true); };
  es.onopen = ()=> setStatus(true);
  es.onerror = ()=>{ setStatus(false); es.close(); setTimeout(connect, 2000); };
}
connect();
// 自動貼り付けトグル
function toggleAP(){
  fetch("/autopaste?on="+(document.getElementById("ap").checked?1:0))
    .then(r=>r.json()).then(d=>{ if(!d.supported){ toast("自動貼り付けは Windows のみ対応です"); document.getElementById("ap").checked=false; } });
}
fetch("/autopaste").then(r=>r.json()).then(d=>{ var ap=document.getElementById("ap"); if(ap) ap.checked=!!d.auto_paste; }).catch(function(){});
// 経過時間を定期更新
setInterval(()=>{ const ts=+$("ts").dataset.ts; if(ts) $("ts").textContent="更新: "+relTime(ts); }, 10000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP ハンドラ
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # アクセスログは抑制

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/clip":
            self._send(200, json.dumps(hub.snapshot(), ensure_ascii=False))
        elif path == "/events":
            self._sse()
        elif path == "/health":
            self._send(200, json.dumps({"ok": True, "backend": BACKEND}))
        elif path == "/autopaste":
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            if "on=1" in q:
                AUTO_PASTE["on"] = True
            elif "on=0" in q:
                AUTO_PASTE["on"] = False
            self._send(200, json.dumps({"auto_paste": AUTO_PASTE["on"],
                                        "supported": sys.platform == "win32"}))
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/clip":
            self._send(404, json.dumps({"error": "not found"}))
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY:
            self._send(413, json.dumps({"error": "too large"}))
            return
        body = self.rfile.read(length) if length > 0 else b""
        ctype = self.headers.get("Content-Type", "")
        text = None
        if "application/json" in ctype:
            try:
                text = json.loads(body.decode("utf-8")).get("text")
            except Exception:
                text = None
        if text is None:
            text = body.decode("utf-8", "replace")
        hub.push_from_remote(text, clip_set)
        maybe_autopaste()
        self._send(200, json.dumps({"ok": True}))

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()
        q = hub.subscribe()
        try:
            self._sse_write(hub.snapshot())  # 接続直後に現在値を送る
            while True:
                try:
                    payload = q.get(timeout=15)
                    self._sse_write(payload)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(q)

    def _sse_write(self, payload):
        data = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
        self.wfile.flush()


# ---------------------------------------------------------------------------
# 起動
# ---------------------------------------------------------------------------

def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def print_qr(url):
    """qrcode が入っていれば端末に QR を表示（任意）。"""
    try:
        import qrcode  # type: ignore
        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        pass


def main():
    # 起動時に --paste / --auto-paste を付けると自動貼り付けを ON で開始（誤爆防止で既定 OFF）。
    if "--paste" in sys.argv or "--auto-paste" in sys.argv:
        AUTO_PASTE["on"] = True

    ip = get_lan_ip()
    url = "http://%s:%d" % (ip, PORT)

    stop_event = threading.Event()
    t = threading.Thread(target=poller, args=(stop_event,), daemon=True)
    t.start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True

    line = "=" * 52
    print("\n" + line)
    print("  📋  Clipboard Bridge が起動しました")
    print(line)
    print("  iPhone の Safari で次の URL を開いてください:\n")
    print("      \033[1;36m%s\033[0m\n" % url)
    print("  （PC とこの iPhone を同じ Wi-Fi につないでください）")
    print("  クリップボード backend: %s" % BACKEND)
    if sys.platform == "win32":
        print("  自動貼り付け: %s（Webの『受信したら自動で貼り付け』で切替）"
              % ("ON" if AUTO_PASTE["on"] else "OFF"))
    print(line)
    print_qr(url)
    print("  停止するには Ctrl+C\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します…")
    finally:
        stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()
