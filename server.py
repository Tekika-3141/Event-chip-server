"""チップ管理システム - サーバー（Python標準ライブラリのみ）

起動:  python server.py
"""
import argparse
import hmac
import json
import secrets
import socket
import ssl
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from database import Database, AppError, MAX_POINTS, now

BASE_DIR = Path(__file__).resolve().parent
MAX_BODY = 65536
INIT_CONFIRM_TEXT = "INITIALIZE"   # データベース初期化の確認用文字列
PAGES = {"/": "index.html", "/dealer": "dealer.html", "/admin": "admin.html"}
# WindowsのレジストリによってはJSがtext/plainになるため、自前で固定する
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
}


class App:
    def __init__(self, data_dir, static_dir, backups_dir, admin_password=None):
        self.data_dir = Path(data_dir)
        self.static_dir = Path(static_dir).resolve()
        self.backups_dir = Path(backups_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.data_dir / "system.db")
        self.tokens = set()
        self.token_lock = threading.Lock()
        self.log_lines = deque(maxlen=300)
        self.log_lock = threading.Lock()
        self.log_path = self.data_dir / "server.log"
        self.admin_password = admin_password or self._load_password()

    def _load_password(self):
        import os
        env = os.environ.get("ADMIN_PASSWORD")
        if env:
            return env
        f = self.data_dir / "admin_password.txt"
        if f.exists():
            pw = f.read_text(encoding="utf-8").strip()
            if pw:
                return pw
        alphabet = "abcdefghijkmnpqrstuvwxyz23456789"   # 紛らわしい文字(l,o,0,1)を除く
        pw = "".join(secrets.choice(alphabet) for _ in range(8))
        f.write_text(pw + "\n", encoding="utf-8")
        print("管理者パスワードを新規作成しました: %s  （保存先: %s）" % (pw, f))
        return pw

    def log(self, msg):
        line = "[%s] %s" % (now(), msg)
        with self.log_lock:
            self.log_lines.append(line)
            try:
                with open(self.log_path, "a", encoding="utf-8") as fp:
                    fp.write(line + "\n")
            except OSError:
                pass


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PointServer"
        timeout = 30   # 応答のないクライアントがスレッドを占有し続けないようにする

        def setup(self):
            BaseHTTPRequestHandler.setup(self)
            if isinstance(self.request, ssl.SSLSocket):
                self.request.do_handshake()    # ワーカースレッド側でTLSハンドシェイク

        def log_message(self, fmt, *args):
            pass   # 標準のアクセスログは出さない（app.logで必要なものだけ記録）

        # ---------- 送信ヘルパー ----------
        def _send(self, status, body, ctype, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            for k, v in (extra or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj, extra=None):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8", extra)

        def _error(self, status, message, code="error"):
            self._json(status, {"ok": False, "error": message, "code": code})

        # ---------- 認証 ----------
        def _token(self):
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "sid":
                    return v
            return ""

        def _is_admin(self):
            t = self._token()
            with app.token_lock:
                return bool(t) and t in app.tokens

        # ---------- GET ----------
        def do_GET(self):
            try:
                url = urlparse(self.path)
                path = url.path
                if path in PAGES:
                    app.log("GET %s %s" % (path, self.client_address[0]))
                    return self._file(app.static_dir / PAGES[path])
                if path.startswith("/static/"):
                    return self._file(app.static_dir / path[len("/static/"):])
                if path.startswith("/api/"):
                    if path.startswith("/api/admin/") and not self._is_admin():
                        return self._error(401, "管理者ログインが必要です。", "auth")
                    return self._json(200, self._api_get(path, parse_qs(url.query)))
                self._error(404, "ページが見つかりません。", "not_found")
            except AppError as e:
                self._error(e.status, e.message, e.code)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:     # 想定外: 詳細はログへ、利用者には汎用メッセージ
                app.log("ERROR GET %s %r" % (self.path, e))
                self._error(500, "サーバー内部エラーが発生しました。", "internal")

        def _file(self, p):
            try:
                p = p.resolve()
                p.relative_to(app.static_dir)       # static外へ出るパス(../)を拒否
                if not p.is_file():
                    raise ValueError
            except (ValueError, OSError):
                return self._error(404, "ファイルが見つかりません。", "not_found")
            self._send(200, p.read_bytes(), MIME.get(p.suffix.lower(), "application/octet-stream"))

        def _api_get(self, path, q):
            def one(name):
                v = q.get(name)
                return v[0] if v else None
            db = app.db
            if path == "/api/games":
                return {"ok": True, "games": [g["name"] for g in db.list_games()]}
            if path == "/api/participant":
                return {"ok": True, "participant": db.lookup(one("code"))}
            if path == "/api/admin/me":
                return {"ok": True}
            if path == "/api/admin/participants":
                return {"ok": True, "participants": db.list_participants()}
            if path == "/api/admin/sessions":
                return {"ok": True, "sessions": db.list_sessions(one("code"))}
            if path == "/api/admin/history":
                sid = one("session_id")
                if sid not in (None, ""):
                    try:
                        sid = int(sid)
                    except ValueError:
                        raise AppError("セッションIDが正しくありません。", 400, "bad_number")
                else:
                    sid = None
                return {"ok": True, "history": db.history(one("code") or None, sid)}
            if path == "/api/admin/games":
                return {"ok": True, "games": db.list_games(only_active=False)}
            if path == "/api/admin/game-summary":
                return {"ok": True, "summary": db.game_summary()}
            if path == "/api/admin/settings":
                return {"ok": True, "settings": {"default_initial_chips": db.get_default_initial_chips(),
                                                 "max_chips": MAX_POINTS}}
            if path == "/api/admin/backups":
                return {"ok": True, "backups": Database.list_backups(app.backups_dir)}
            if path == "/api/admin/log":
                with app.log_lock:
                    return {"ok": True, "lines": list(app.log_lines)[-150:]}
            raise AppError("APIが見つかりません。", 404, "not_found")

        # ---------- POST ----------
        def do_POST(self):
            try:
                path = urlparse(self.path).path
                body = self._body()
                if path.startswith("/api/admin/") and path != "/api/admin/login" and not self._is_admin():
                    return self._error(401, "管理者ログインが必要です。", "auth")
                result, extra = self._api_post(path, body)
                self._json(200, result, extra)
            except AppError as e:
                self._error(e.status, e.message, e.code)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                app.log("ERROR POST %s %r" % (self.path, e))
                self._error(500, "サーバー内部エラーが発生しました。", "internal")

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise AppError("リクエストが正しくありません。", 400, "bad_request")
            if n < 0 or n > MAX_BODY:
                raise AppError("リクエストが大きすぎます。", 413, "too_large")
            raw = self.rfile.read(n) if n else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                raise AppError("リクエストの形式（JSON）が正しくありません。", 400, "bad_json")
            if not isinstance(data, dict):
                raise AppError("リクエストの形式（JSON）が正しくありません。", 400, "bad_json")
            return data

        def _api_post(self, path, b):
            db = app.db
            dev = self.client_address[0]
            if path == "/api/points/change":
                r = db.change_points(b.get("code"), b.get("delta"), game=b.get("game"),
                                     operator=b.get("operator"), device=dev,
                                     request_id=b.get("request_id"), kind="game")
                if not r["duplicate"]:
                    app.log("CHIP %+d %s %s by %s -> %d" % (r["delta"], r["code"], b.get("game"),
                                                             b.get("operator"), r["points"]))
                return {"ok": True, "result": r}, None
            if path == "/api/admin/login":
                pw = b.get("password")
                ok = isinstance(pw, str) and hmac.compare_digest(pw.encode("utf-8"),
                                                                 app.admin_password.encode("utf-8"))
                if not ok:
                    time.sleep(0.5)     # 総当たりを少し遅くする
                    app.log("ADMIN login failed %s" % dev)
                    raise AppError("パスワードが違います。", 401, "auth")
                token = secrets.token_urlsafe(24)
                with app.token_lock:
                    app.tokens.add(token)
                app.log("ADMIN login %s" % dev)
                return {"ok": True}, [("Set-Cookie", "sid=%s; Path=/; HttpOnly; SameSite=Strict" % token)]
            if path == "/api/admin/logout":
                with app.token_lock:
                    app.tokens.discard(self._token())
                return {"ok": True}, [("Set-Cookie", "sid=; Path=/; Max-Age=0")]
            if path == "/api/admin/participants":
                r = db.create_participant(b.get("code"), b.get("initial_points"))
                app.log("ADMIN add code %s (%d チップ)" % (r["code"], r["initial_points"]))
                return {"ok": True, "result": r}, None
            if path == "/api/admin/settings":
                v = db.set_default_initial_chips(b.get("default_initial_chips"))
                app.log("ADMIN set default initial chips %d（既存の参加者・セッションは変更なし）" % v)
                return {"ok": True, "settings": {"default_initial_chips": v, "max_chips": MAX_POINTS}}, None
            if path == "/api/admin/participants/reset":
                r = db.reset_participant(b.get("code"))
                app.log("ADMIN reset code %s (%s) 次回は %d チップから開始" % (
                    r["code"], "セッション終了 最終 %d チップ" % r["final_points"] if r["session_ended"]
                    else "進行中のセッションなし", r["initial_points"]))
                return {"ok": True, "result": r}, None
            if path == "/api/admin/system/initialize":
                # 画面側の確認に加えて、サーバー側でも確認文字列を必須にする（API直叩きの誤操作対策）
                if b.get("confirm") != INIT_CONFIRM_TEXT:
                    raise AppError("確認のため「%s」と入力してください。初期化は実行していません。" % INIT_CONFIRM_TEXT,
                                   400, "confirm_required")
                app.log("ADMIN database initialize requested %s" % dev)
                try:
                    r = db.reset_event_data(app.backups_dir)
                except AppError as e:
                    app.log("ADMIN database initialize FAILED: %s" % e.message)
                    raise
                app.log("ADMIN database initialized (backup %s; deleted codes=%d sessions=%d history=%d)" % (
                    r["backup"]["name"], r["deleted"]["participants"], r["deleted"]["sessions"],
                    r["deleted"]["history"]))
                return {"ok": True, "result": r}, None
            if path == "/api/admin/participants/active":
                db.set_active(b.get("code"), bool(b.get("active")))
                app.log("ADMIN code %s active=%s" % (b.get("code"), bool(b.get("active"))))
                return {"ok": True}, None
            if path == "/api/admin/sessions/start":
                r = db.start_session(b.get("code"), b.get("initial_points"), operator="admin", device=dev)
                app.log("ADMIN session start %s (%d チップ)" % (r["code"], r["points"]))
                return {"ok": True, "result": r}, None
            if path == "/api/admin/sessions/end":
                r = db.end_session(b.get("code"))
                app.log("ADMIN session end %s (final %d チップ)" % (r["code"], r["final_points"]))
                return {"ok": True, "result": r}, None
            if path == "/api/admin/adjust":
                r = db.change_points(b.get("code"), b.get("delta"), operator="admin", device=dev,
                                     request_id=b.get("request_id"), kind="admin", note=b.get("note"))
                if not r["duplicate"]:
                    app.log("ADJUST %+d %s -> %d" % (r["delta"], r["code"], r["points"]))
                return {"ok": True, "result": r}, None
            if path == "/api/admin/games":
                db.add_game(b.get("name"))
                return {"ok": True}, None
            if path == "/api/admin/games/rename":
                db.rename_game(b.get("id"), b.get("name"))
                return {"ok": True}, None
            if path == "/api/admin/games/active":
                db.set_game_active(b.get("id"), bool(b.get("active")))
                return {"ok": True}, None
            if path == "/api/admin/backup":
                r = db.backup(app.backups_dir)
                app.log("ADMIN backup %s" % r["name"])
                return {"ok": True, "result": r}, None
            raise AppError("APIが見つかりません。", 404, "not_found")

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass    # 切断・TLSハンドシェイク失敗などのノイズを出さない


def create_app(data_dir=None, static_dir=None, backups_dir=None, admin_password=None):
    return App(data_dir or BASE_DIR / "data", static_dir or BASE_DIR / "static",
               backups_dir or BASE_DIR / "backups", admin_password)


def create_server(app, host, port, certfile=None, keyfile=None):
    httpd = Server((host, port), make_handler(app))
    if certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    return httpd


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # UDPなので実際には送信されない
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="文化祭チップ管理システム")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--https", action="store_true", help="data/cert.pem と data/key.pem を使ってHTTPSで起動")
    ap.add_argument("--cert", default=str(BASE_DIR / "data" / "cert.pem"))
    ap.add_argument("--key", default=str(BASE_DIR / "data" / "key.pem"))
    ap.add_argument("--backup", action="store_true", help="DBをbackups/にコピーして終了")
    args = ap.parse_args()

    try:
        app = create_app()
    except AppError as e:
        print("起動できません: " + e.message)
        sys.exit(1)

    if args.backup:
        r = app.db.backup(app.backups_dir)
        print("バックアップを作成しました: backups/%s (%d bytes)" % (r["name"], r["size"]))
        return

    scheme = "https" if args.https else "http"
    try:
        httpd = create_server(app, args.host, args.port,
                              args.cert if args.https else None, args.key if args.https else None)
    except (OSError, ssl.SSLError) as e:
        print("起動できません: %s" % e)
        sys.exit(1)

    ip = lan_ip()
    print("文化祭チップ管理システムを起動しました（止めるには Ctrl+C）")
    print("  このPC      : %s://localhost:%d/" % (scheme, args.port))
    print("  LAN内の端末 : %s://%s:%d/" % (scheme, ip, args.port))
    print("  ディーラー画面: /dealer    管理画面: /admin")
    app.log("SERVER start %s port %d" % (scheme, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.log("SERVER stop")
        httpd.server_close()


if __name__ == "__main__":
    main()
