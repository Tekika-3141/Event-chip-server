"""テスト:  python tests.py   （標準ライブラリのunittestのみ。一時フォルダを使うので本番DBには触れない）"""
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import server
from database import Database, AppError

BASE = Path(__file__).resolve().parent


def tmp_db(**kw):
    d = tempfile.mkdtemp()
    return Database(Path(d) / "system.db", **kw), Path(d)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db, self.dir = tmp_db()
        self.db.create_participant("2026_A_001", 100)
        self.db.start_session("2026_A_001")

    def chg(self, delta, **kw):
        args = dict(game="Blackjack", operator="tester")
        args.update(kw)
        return self.db.change_points("2026_A_001", delta, **args)

    def test_duplicate_code_rejected(self):
        with self.assertRaises(AppError) as cm:
            self.db.create_participant("2026_A_001", 50)
        self.assertEqual(cm.exception.code, "duplicate")

    def test_arbitrary_codes_accepted(self):
        for code in ["JO1chip1", "CHIP34798hjrek", "A001", "TEST-001", "日本語コード"]:
            self.db.create_participant(code, 10)
        with self.assertRaises(AppError):
            self.db.create_participant("   ", 10)
        with self.assertRaises(AppError):
            self.db.create_participant("a" * 65, 10)

    def test_code_is_stripped(self):      # USBスキャナーが改行や空白を付けても同じコードになる
        self.assertEqual(self.db.lookup(" 2026_A_001\n")["code"], "2026_A_001")

    def test_add_and_subtract_with_history(self):
        self.assertEqual(self.chg(20)["points"], 120)
        self.assertEqual(self.chg(-30)["points"], 90)
        h = self.db.history("2026_A_001")
        self.assertEqual([r["delta"] for r in h], [-30, 20, 100])
        self.assertEqual([r["points_after"] for r in h], [90, 120, 100])
        self.assertEqual(h[0]["game"], "Blackjack")
        self.assertEqual(h[0]["operator"], "tester")

    def test_insufficient_points_rejected_and_nothing_written(self):
        with self.assertRaises(AppError) as cm:
            self.chg(-101)
        self.assertEqual(cm.exception.code, "insufficient")
        self.assertEqual(self.db.lookup("2026_A_001")["session"]["current_points"], 100)
        self.assertEqual(len(self.db.history("2026_A_001")), 1)

    def test_invalid_numbers(self):
        for bad in [0, 1.5, "10", None, True, 10 ** 9]:
            with self.assertRaises(AppError, msg=repr(bad)):
                self.chg(bad)

    def test_unknown_game_and_missing_operator(self):
        with self.assertRaises(AppError):
            self.chg(10, game="NoSuchGame")
        with self.assertRaises(AppError):
            self.chg(10, operator="")

    def test_unknown_and_inactive_code(self):
        with self.assertRaises(AppError) as cm:
            self.db.change_points("nope", 10, game="Poker", operator="x")
        self.assertEqual(cm.exception.code, "not_found")
        self.db.set_active("2026_A_001", False)
        with self.assertRaises(AppError) as cm:
            self.chg(10)
        self.assertEqual(cm.exception.code, "inactive")
        with self.assertRaises(AppError):
            self.db.lookup("2026_A_001")

    def test_no_session_and_ended_session(self):
        self.db.create_participant("B", 10)
        with self.assertRaises(AppError) as cm:
            self.db.change_points("B", 1, game="Poker", operator="x")
        self.assertEqual(cm.exception.code, "no_session")
        self.db.end_session("2026_A_001")
        with self.assertRaises(AppError) as cm:
            self.chg(1)
        self.assertEqual(cm.exception.code, "session_ended")
        with self.assertRaises(AppError):
            self.db.end_session("2026_A_001")     # 二重終了

    def test_session_reuse_keeps_old_session(self):
        self.chg(150)
        self.db.end_session("2026_A_001")
        self.db.start_session("2026_A_001")          # 同じコードで新しいセッション
        sessions = self.db.list_sessions("2026_A_001")
        self.assertEqual(len(sessions), 2)
        s1, s2 = sessions[1], sessions[0]
        self.assertEqual((s1["status"], s1["initial_points"], s1["current_points"]), ("ended", 100, 250))
        self.assertEqual((s2["status"], s2["current_points"]), ("active", 100))
        self.assertEqual(len(self.db.history(session_id=s1["id"])), 2)   # 旧セッションの履歴は残る

    def test_no_two_active_sessions(self):
        with self.assertRaises(AppError) as cm:
            self.db.start_session("2026_A_001")
        self.assertEqual(cm.exception.code, "already_active")

    def test_admin_adjust_adds_history_row(self):
        self.chg(10)
        r = self.db.change_points("2026_A_001", -110, operator="admin", kind="admin", note="誤操作の訂正")
        self.assertEqual(r["points"], 0)
        h = self.db.history("2026_A_001")
        self.assertEqual((h[0]["kind"], h[0]["delta"], h[0]["note"]), ("admin", -110, "誤操作の訂正"))
        self.assertEqual(len(h), 3)           # 既存行は変更されず、行が追加される

    def test_history_is_append_only(self):
        self.chg(10)
        con = sqlite3.connect(str(self.db.path))
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("UPDATE point_history SET delta=999")
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("DELETE FROM point_history")
        con.close()

    def test_request_id_prevents_double_apply(self):
        a = self.chg(50, request_id="req-1")
        b = self.chg(50, request_id="req-1")
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(self.db.lookup("2026_A_001")["session"]["current_points"], 150)
        self.assertEqual(len(self.db.history("2026_A_001")), 2)
        with self.assertRaises(AppError):      # 同じIDで中身が違う
            self.chg(60, request_id="req-1")

    def test_concurrent_updates_do_not_lose_changes(self):
        self.chg(100000 - 100)                 # 十分な残高
        threads, per = 8, 40

        def work(i):
            for n in range(per):
                self.chg(3 if (n + i) % 2 == 0 else -1, operator="t%d" % i)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        expected = 100000 + sum(3 if (n + i) % 2 == 0 else -1 for i in range(threads) for n in range(per))
        self.assertEqual(self.db.lookup("2026_A_001")["session"]["current_points"], expected)
        h = self.db.history("2026_A_001", limit=10000)
        self.assertEqual(len(h), 2 + threads * per)
        self.assertEqual(sum(r["delta"] for r in h), expected)    # 履歴の合計 = 現在値
        ordered = sorted(h, key=lambda r: r["id"])
        run = 0
        for r in ordered:                                          # 各行のpoints_afterが連続している
            run += r["delta"]
            self.assertEqual(r["points_after"], run)

    def test_game_rename_keeps_history_and_summary(self):
        self.chg(50)
        self.chg(-20)
        gid = [g for g in self.db.list_games() if g["name"] == "Blackjack"][0]["id"]
        self.db.rename_game(gid, "BJ")
        self.assertEqual(self.db.history("2026_A_001")[1]["game"], "Blackjack")   # 履歴は書き換わらない
        s = {r["game"]: r for r in self.db.game_summary()}
        self.assertEqual((s["Blackjack"]["plus"], s["Blackjack"]["minus"], s["Blackjack"]["net"]), (50, 20, 30))
        self.assertEqual(self.chg(1, game="BJ")["points"], 131)
        with self.assertRaises(AppError):
            self.db.add_game("BJ")

    def test_busy_database_gives_clear_error(self):
        db, _ = tmp_db(busy_timeout=0.2)
        db.create_participant("X", 10)
        db.start_session("X")
        blocker = sqlite3.connect(str(db.path), isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(AppError) as cm:
                db.change_points("X", 1, game="Poker", operator="t")
            self.assertEqual(cm.exception.code, "busy")
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        self.assertEqual(db.lookup("X")["session"]["current_points"], 10)   # 失敗時は何も書かれていない

    def test_backup_does_not_touch_original(self):
        self.chg(5)
        r = self.db.backup(self.dir / "backups")
        copy = Database(self.dir / "backups" / r["name"])
        self.assertEqual(copy.lookup("2026_A_001")["session"]["current_points"], 105)
        self.assertEqual(self.db.lookup("2026_A_001")["session"]["current_points"], 105)
        self.assertEqual(len(Database.list_backups(self.dir / "backups")), 1)

    def test_data_survives_reopen(self):
        self.chg(7)
        again = Database(self.db.path)
        self.assertEqual(again.lookup("2026_A_001")["session"]["current_points"], 107)


class WebHelpers:
    """HTTPテスト共通のヘルパー（self.port が必要）"""

    def req(self, method, path, body=None, cookie=None, raw=None, headers=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        r = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        if cookie:
            r.add_header("Cookie", cookie)
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw_body = resp.read()
                return resp.status, raw_body, resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def jreq(self, method, path, body=None, cookie=None, **kw):
        s, b, h = self.req(method, path, body, cookie, **kw)
        return s, json.loads(b.decode("utf-8")), h

    def login(self):
        s, j, h = self.jreq("POST", "/api/admin/login", {"password": "pw-test"})
        self.assertEqual(s, 200)
        return h["Set-Cookie"].split(";")[0]


class WebTests(WebHelpers, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = Path(tempfile.mkdtemp())
        cls.app = server.create_app(d / "data", BASE / "static", d / "backups", admin_password="pw-test")
        cls.httpd = server.create_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.cookie = None

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_01_pages_and_static(self):
        for p, needle in [("/", "チップ"), ("/dealer", "dealer.js"), ("/admin", "admin.js")]:
            s, b, h = self.req("GET", p)
            self.assertEqual(s, 200, p)
            self.assertIn("text/html", h["Content-Type"])
            self.assertIn(needle, b.decode("utf-8"), p)
        for f in ["app.js", "dealer.js", "admin.js", "qr-reader.js", "jsQR.js", "style.css"]:
            s, b, h = self.req("GET", "/static/" + f)
            self.assertEqual(s, 200, f)
        self.assertIn("javascript", self.req("GET", "/static/app.js")[2]["Content-Type"])

    def test_02_path_traversal_blocked(self):
        for p in ["/static/../server.py", "/static/..%2fserver.py", "/static/%2e%2e/database.py"]:
            s, _, _ = self.req("GET", p)
            self.assertEqual(s, 404, p)

    def test_03_admin_required_and_dealer_separated(self):
        for m, p in [("GET", "/api/admin/participants"), ("POST", "/api/admin/participants"),
                     ("POST", "/api/admin/adjust"), ("POST", "/api/admin/backup"),
                     ("GET", "/api/admin/history"), ("GET", "/api/admin/log")]:
            s, j, _ = self.jreq(m, p, {} if m == "POST" else None)
            self.assertEqual(s, 401, p)
        s, j, _ = self.jreq("POST", "/api/admin/login", {"password": "wrong"})
        self.assertEqual(s, 401)
        s, j, _ = self.jreq("GET", "/api/admin/participants", cookie="sid=forged")
        self.assertEqual(s, 401)

    def test_04_full_flow(self):
        ck = self.login()
        for code in ["2026_A_001", "CHIP34798hjrek", "JO1chip1", "TEST-001"]:
            s, j, _ = self.jreq("POST", "/api/admin/participants", {"code": code, "initial_points": 100}, ck)
            self.assertEqual(s, 200, code)
        s, j, _ = self.jreq("POST", "/api/admin/participants", {"code": "TEST-001", "initial_points": 5}, ck)
        self.assertEqual((s, j["code"]), (409, "duplicate"))

        # セッション開始前はチップ変更できない
        s, j, _ = self.jreq("POST", "/api/points/change",
                            {"code": "2026_A_001", "delta": 10, "game": "Poker", "operator": "d1"})
        self.assertEqual((s, j["code"]), (409, "no_session"))

        s, j, _ = self.jreq("POST", "/api/admin/sessions/start", {"code": "2026_A_001"}, ck)
        self.assertEqual(s, 200)

        # ディーラー: 検索（日本語・記号を含むURLエンコード）
        s, j, _ = self.jreq("GET", "/api/participant?code=2026_A_001")
        self.assertEqual((s, j["participant"]["session"]["current_points"]), (200, 100))
        s, j, _ = self.jreq("GET", "/api/participant?code=" + urllib.request.quote("JO1chip1"))
        self.assertEqual(s, 200)
        s, j, _ = self.jreq("GET", "/api/participant?code=NOPE")
        self.assertEqual((s, j["code"]), (404, "not_found"))

        s, j, _ = self.jreq("POST", "/api/points/change",
                            {"code": "2026_A_001", "delta": 100, "game": "Blackjack",
                             "operator": "dealer1", "request_id": "r1"})
        self.assertEqual((s, j["result"]["points"]), (200, 200))
        s, j, _ = self.jreq("POST", "/api/points/change",
                            {"code": "2026_A_001", "delta": 100, "game": "Blackjack",
                             "operator": "dealer1", "request_id": "r1"})
        self.assertTrue(j["result"]["duplicate"])
        self.assertEqual(j["result"]["points"], 200)

        # クライアントが「現在チップ」を送っても無視される
        s, j, _ = self.jreq("POST", "/api/points/change",
                            {"code": "2026_A_001", "delta": -50, "game": "Roulette", "operator": "d2",
                             "points": 99999, "current_points": 99999})
        self.assertEqual(j["result"]["points"], 150)

        for bad in [{"delta": "abc"}, {"delta": 1.5}, {"delta": 0}, {"delta": 10 ** 9}, {"delta": None}]:
            body = {"code": "2026_A_001", "game": "Poker", "operator": "d1"}
            body.update(bad)
            s, j, _ = self.jreq("POST", "/api/points/change", body)
            self.assertEqual(s, 400, bad)
        s, j, _ = self.jreq("POST", "/api/points/change",
                            {"code": "2026_A_001", "delta": -9999, "game": "Poker", "operator": "d1"})
        self.assertEqual((s, j["code"]), (409, "insufficient"))

        # 管理者修正
        s, j, _ = self.jreq("POST", "/api/admin/adjust", {"code": "2026_A_001", "delta": 20, "note": "訂正"}, ck)
        self.assertEqual(j["result"]["points"], 170)

        s, j, _ = self.jreq("GET", "/api/admin/history?code=2026_A_001", cookie=ck)
        self.assertEqual([r["delta"] for r in j["history"]], [20, -50, 100, 100])
        self.assertEqual(j["history"][0]["kind"], "admin")
        s, j, _ = self.jreq("GET", "/api/admin/participants", cookie=ck)
        row = [r for r in j["participants"] if r["code"] == "2026_A_001"][0]
        self.assertEqual((row["status"], row["current_points"]), ("active", 170))
        s, j, _ = self.jreq("GET", "/api/admin/game-summary", cookie=ck)
        self.assertEqual({r["game"]: r["net"] for r in j["summary"]}, {"Blackjack": 100, "Roulette": -50})

        # セッション終了 → 変更不可 → 再開始で新セッション
        s, j, _ = self.jreq("POST", "/api/admin/sessions/end", {"code": "2026_A_001"}, ck)
        self.assertEqual(j["result"]["final_points"], 170)
        s, j, _ = self.jreq("POST", "/api/points/change",
                            {"code": "2026_A_001", "delta": 1, "game": "Poker", "operator": "d1"})
        self.assertEqual((s, j["code"]), (409, "session_ended"))
        self.jreq("POST", "/api/admin/sessions/start", {"code": "2026_A_001"}, ck)
        s, j, _ = self.jreq("GET", "/api/admin/sessions?code=2026_A_001", cookie=ck)
        self.assertEqual([x["status"] for x in j["sessions"]], ["active", "ended"])

        # バックアップ・ログ
        s, j, _ = self.jreq("POST", "/api/admin/backup", {}, ck)
        self.assertEqual(s, 200)
        s, j, _ = self.jreq("GET", "/api/admin/backups", cookie=ck)
        self.assertEqual(len(j["backups"]), 1)
        s, j, _ = self.jreq("GET", "/api/admin/log", cookie=ck)
        text = "\n".join(j["lines"])
        self.assertIn("CHIP +100 2026_A_001", text)

    def test_05_bad_requests(self):
        s, j, _ = self.jreq("POST", "/api/points/change", raw=b"{not json")
        self.assertEqual((s, j["code"]), (400, "bad_json"))
        s, j, _ = self.jreq("POST", "/api/points/change", raw=b"[1,2]")
        self.assertEqual((s, j["code"]), (400, "bad_json"))
        s, j, _ = self.jreq("POST", "/api/points/change", raw=b"x" * 70000)
        self.assertEqual(s, 413)
        s, j, _ = self.jreq("POST", "/api/points/change", {"code": 123, "delta": 1})
        self.assertEqual(s, 400)
        s, j, _ = self.jreq("GET", "/api/nothing")
        self.assertEqual(s, 404)
        # SQLインジェクション風の文字列は単なるコードとして扱われる
        s, j, _ = self.jreq("GET", "/api/participant?code=" + urllib.request.quote("' OR 1=1 --"))
        self.assertEqual((s, j["code"]), (404, "not_found"))
        ck = self.login()
        s, j, _ = self.jreq("POST", "/api/admin/participants", {"code": "x'); DROP TABLE participants;--",
                                                                "initial_points": 1}, ck)
        self.assertEqual(s, 200)
        s, j, _ = self.jreq("GET", "/api/admin/participants", cookie=ck)
        self.assertEqual(s, 200)

    def test_06_logout(self):
        ck = self.login()
        self.assertEqual(self.jreq("GET", "/api/admin/me", cookie=ck)[0], 200)
        self.jreq("POST", "/api/admin/logout", {}, ck)
        self.assertEqual(self.jreq("GET", "/api/admin/me", cookie=ck)[0], 401)


# ======================================================================
# チップ設定・プレイヤーのリセット・データベース初期化
# ======================================================================
def raw_counts(path):
    con = sqlite3.connect(str(path))
    try:
        return {t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                for t in ("participants", "sessions", "point_history", "games")}
    finally:
        con.close()


class ChipSettingsTests(unittest.TestCase):
    def setUp(self):
        self.db, self.dir = tmp_db()

    def test_default_value_available(self):
        self.assertEqual(self.db.get_default_initial_chips(), 100)

    def test_default_can_be_changed(self):
        self.assertEqual(self.db.set_default_initial_chips(500), 500)
        self.assertEqual(self.db.get_default_initial_chips(), 500)
        self.assertEqual(self.db.set_default_initial_chips(0), 0)       # 0も許可
        self.assertEqual(self.db.get_default_initial_chips(), 0)

    def test_invalid_values_rejected_and_value_unchanged(self):
        self.db.set_default_initial_chips(300)
        for bad in [-1, 1.5, "100", None, True, 10 ** 12]:
            with self.assertRaises(AppError, msg=repr(bad)):
                self.db.set_default_initial_chips(bad)
        self.assertEqual(self.db.get_default_initial_chips(), 300)

    def test_setting_survives_reopen(self):
        self.db.set_default_initial_chips(500)
        again = Database(self.db.path)          # 再オープンしても既存の値を上書きしない
        self.assertEqual(again.get_default_initial_chips(), 500)

    def test_new_participant_uses_setting(self):
        self.db.set_default_initial_chips(500)
        r = self.db.create_participant("NEW1")                 # 省略 → 設定値
        self.assertEqual(r["initial_points"], 500)
        self.db.start_session("NEW1")
        self.assertEqual(self.db.lookup("NEW1")["session"]["current_points"], 500)
        r = self.db.create_participant("NEW2", 30)             # 個別指定は従来どおり優先
        self.assertEqual(r["initial_points"], 30)

    def test_existing_participants_and_sessions_untouched(self):
        self.db.create_participant("OLD", 100)
        self.db.start_session("OLD")
        self.db.change_points("OLD", 50, game="Poker", operator="t")
        before = (self.db.lookup("OLD"), self.db.history("OLD"), self.db.list_sessions("OLD"))
        self.db.set_default_initial_chips(999)
        after = (self.db.lookup("OLD"), self.db.history("OLD"), self.db.list_sessions("OLD"))
        self.assertEqual(before, after)
        self.assertEqual(self.db.lookup("OLD")["initial_points"], 100)
        self.assertEqual(self.db.lookup("OLD")["session"]["current_points"], 150)

    def test_old_database_without_settings_table_is_upgraded_in_place(self):
        self.db.create_participant("KEEP", 77)
        self.db.start_session("KEEP")
        con = sqlite3.connect(str(self.db.path))
        con.execute("DROP TABLE system_settings")          # 旧バージョンのDBを再現
        con.commit()
        con.close()
        again = Database(self.db.path)                      # 起動時に自動でスキーマ追加。データは保持
        self.assertEqual(again.get_default_initial_chips(), 100)
        self.assertEqual(again.lookup("KEEP")["session"]["current_points"], 77)
        self.assertEqual(len(again.history("KEEP")), 1)


class PlayerResetTests(unittest.TestCase):
    def setUp(self):
        self.db, self.dir = tmp_db()
        self.db.create_participant("2026_A_001", 100)
        self.db.create_participant("OTHER", 100)
        self.db.start_session("2026_A_001")
        self.db.start_session("OTHER")
        self.db.change_points("2026_A_001", 800, game="Poker", operator="t")
        self.db.change_points("2026_A_001", -50, game="Poker", operator="t")      # 現在 850
        self.db.change_points("OTHER", 5, game="Poker", operator="t")

    def test_reset_ends_session_and_keeps_code(self):
        self.assertEqual(self.db.lookup("2026_A_001")["session"]["current_points"], 850)
        r = self.db.reset_participant("2026_A_001")
        self.assertTrue(r["session_ended"])
        self.assertEqual((r["final_points"], r["initial_points"]), (850, 100))
        s = self.db.lookup("2026_A_001")                    # コードは残る
        self.assertEqual(s["session"]["status"], "ended")
        with self.assertRaises(AppError) as cm:             # 終了済みなのでチップ変更は不可
            self.db.change_points("2026_A_001", 1, game="Poker", operator="t")
        self.assertEqual(cm.exception.code, "session_ended")

    def test_next_session_starts_from_initial_chips(self):
        self.db.reset_participant("2026_A_001")
        self.db.start_session("2026_A_001")
        self.assertEqual(self.db.lookup("2026_A_001")["session"]["current_points"], 100)
        self.assertEqual(self.db.change_points("2026_A_001", 10, game="Poker", operator="t")["points"], 110)

    def test_past_sessions_and_history_are_kept(self):
        hist_before = self.db.history("2026_A_001")
        self.db.reset_participant("2026_A_001")
        self.db.start_session("2026_A_001")
        sessions = self.db.list_sessions("2026_A_001")
        self.assertEqual(len(sessions), 2)
        old = sessions[1]
        self.assertEqual((old["status"], old["initial_points"], old["current_points"]), ("ended", 100, 850))
        old_hist = self.db.history(session_id=old["id"])
        self.assertEqual(old_hist, hist_before)                              # 過去の履歴は1行も変わらない
        self.assertEqual([r["delta"] for r in old_hist], [-50, 800, 100])

    def test_reset_without_active_session_is_ok(self):
        self.db.create_participant("NOSESSION", 40)
        r = self.db.reset_participant("NOSESSION")             # まだ一度も開始していない
        self.assertFalse(r["session_ended"])
        self.db.start_session("NOSESSION")
        self.assertEqual(self.db.lookup("NOSESSION")["session"]["current_points"], 40)
        self.db.end_session("NOSESSION")
        r = self.db.reset_participant("NOSESSION")             # すでに終了済み
        self.assertFalse(r["session_ended"])
        self.assertEqual(len(self.db.list_sessions("NOSESSION")), 1)

    def test_unknown_code_is_error(self):
        with self.assertRaises(AppError) as cm:
            self.db.reset_participant("NOPE")
        self.assertEqual(cm.exception.code, "not_found")
        with self.assertRaises(AppError):
            self.db.reset_participant("   ")

    def test_other_participants_not_affected(self):
        self.db.reset_participant("2026_A_001")
        self.assertEqual(self.db.lookup("OTHER")["session"]["status"], "active")
        self.assertEqual(self.db.lookup("OTHER")["session"]["current_points"], 105)

    def test_reset_uses_participant_initial_not_current_setting(self):
        self.db.set_default_initial_chips(999)               # 設定を変えても既存プレイヤーの初期チップは100のまま
        self.db.reset_participant("2026_A_001")
        self.db.start_session("2026_A_001")
        self.assertEqual(self.db.lookup("2026_A_001")["session"]["current_points"], 100)


class DatabaseInitTests(unittest.TestCase):
    def setUp(self):
        self.db, self.dir = tmp_db()
        self.bak = self.dir / "backups"
        self.db.set_default_initial_chips(500)
        self.db.add_game("My Game")
        for code in ["A1", "B2", "C3"]:
            self.db.create_participant(code, 100)
            self.db.start_session(code)
            self.db.change_points(code, 25, game="Poker", operator="t")
        self.db.end_session("C3")
        self.db.start_session("C3")

    def test_event_data_deleted(self):
        before = raw_counts(self.db.path)
        self.assertEqual((before["participants"], before["sessions"]), (3, 4))
        r = self.db.reset_event_data(self.bak)
        after = raw_counts(self.db.path)
        self.assertEqual((after["participants"], after["sessions"], after["point_history"]), (0, 0, 0))
        self.assertEqual(r["deleted"], {"participants": 3, "sessions": 4, "history": before["point_history"]})
        self.assertEqual(self.db.list_participants(), [])
        self.assertEqual(self.db.history(), [])

    def test_games_and_chip_setting_are_kept(self):
        games_before = self.db.list_games(only_active=False)
        gid = [g for g in games_before if g["name"] == "Poker"][0]["id"]
        self.db.set_game_active(gid, False)                    # 停止状態も維持されること
        games_before = self.db.list_games(only_active=False)
        self.db.reset_event_data(self.bak)
        self.assertEqual(self.db.list_games(only_active=False), games_before)
        self.assertIn("My Game", [g["name"] for g in games_before])
        self.assertEqual(self.db.get_default_initial_chips(), 500)
        self.assertEqual(Database(self.db.path).get_default_initial_chips(), 500)   # 再オープン後も

    def test_backup_is_created_before_init_and_contains_old_data(self):
        before = raw_counts(self.db.path)
        r = self.db.reset_event_data(self.bak)
        files = Database.list_backups(self.bak)
        self.assertEqual([f["name"] for f in files], [r["backup"]["name"]])
        self.assertEqual(raw_counts(self.bak / r["backup"]["name"]), before)       # 初期化前の状態が入っている
        self.assertEqual(sorted(f.name for f in self.bak.iterdir()), [r["backup"]["name"]])   # -wal/-shm等の残骸が無い
        self.assertEqual(raw_counts(self.db.path)["participants"], 0)

    def test_backup_failure_means_no_init(self):
        blocked = self.dir / "not_a_dir"
        blocked.write_text("x")                                # フォルダを作れない場所を指定
        before = raw_counts(self.db.path)
        with self.assertRaises(AppError) as cm:
            self.db.reset_event_data(blocked)
        self.assertEqual(cm.exception.code, "backup_failed")
        self.assertEqual(raw_counts(self.db.path), before)     # 何も消えていない
        self.assertEqual(len(self.db.list_participants()), 3)

    def test_failure_in_the_middle_rolls_back_everything(self):
        con = sqlite3.connect(str(self.db.path))               # 参加者の削除で必ず失敗する罠
        con.execute("CREATE TRIGGER boom BEFORE DELETE ON participants "
                    "BEGIN SELECT RAISE(ABORT, 'boom'); END")
        con.commit()
        con.close()
        before = raw_counts(self.db.path)
        with self.assertRaises(AppError):
            self.db.reset_event_data(self.bak)
        self.assertEqual(raw_counts(self.db.path), before)     # 履歴・セッション削除も取り消されている
        self.assertEqual(self.db.get_default_initial_chips(), 500)
        con = sqlite3.connect(str(self.db.path))               # 追記専用トリガーも元のまま
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertTrue({"hist_no_update", "hist_no_delete"} <= names)
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("DELETE FROM point_history")
        finally:
            con.close()
        self.assertEqual(self.db.lookup("A1")["session"]["current_points"], 125)   # 運用も継続できる

    def test_append_only_protection_remains_after_init_and_system_usable(self):
        self.db.reset_event_data(self.bak)
        con = sqlite3.connect(str(self.db.path))
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertTrue({"hist_no_update", "hist_no_delete"} <= names)
        self.db.create_participant("NEW")                       # 設定値500で登録できる
        self.db.start_session("NEW")
        self.db.change_points("NEW", -1, game="Poker", operator="t")
        self.assertEqual(self.db.lookup("NEW")["session"]["current_points"], 499)
        self.assertEqual(self.db.lookup("NEW")["session"]["id"], 1)    # IDも1から振り直される
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("DELETE FROM point_history")
        con.close()

    def test_init_twice_is_fine(self):
        self.db.reset_event_data(self.bak)
        r = self.db.reset_event_data(self.bak)
        self.assertEqual(r["deleted"], {"participants": 0, "sessions": 0, "history": 0})


class AdminChipWebTests(WebHelpers, unittest.TestCase):
    """管理者API（チップ設定・プレイヤーリセット・DB初期化）。テストごとに新しいサーバーとDBを使う。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.app = server.create_app(self.dir / "data", BASE / "static", self.dir / "backups",
                                     admin_password="pw-test")
        self.httpd = server.create_server(self.app, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.ck = self.login()

    def post(self, path, body=None, cookie=True):
        return self.jreq("POST", path, {} if body is None else body, self.ck if cookie else None)

    def seed(self):
        for code in ["P1", "P2"]:
            self.assertEqual(self.post("/api/admin/participants", {"code": code, "initial_points": 100})[0], 200)
            self.assertEqual(self.post("/api/admin/sessions/start", {"code": code})[0], 200)
        self.jreq("POST", "/api/points/change",
                  {"code": "P1", "delta": 750, "game": "Poker", "operator": "d1"})        # P1 = 850

    # ---- 認証 ----
    def test_unauthenticated_calls_are_rejected_and_change_nothing(self):
        self.seed()
        for cookie in (None, "sid=forged"):
            s, j, _ = self.jreq("GET", "/api/admin/settings", cookie=cookie)
            self.assertEqual((s, j["code"]), (401, "auth"))
            for path, body in [("/api/admin/settings", {"default_initial_chips": 1}),
                               ("/api/admin/participants/reset", {"code": "P1"}),
                               ("/api/admin/system/initialize", {"confirm": "INITIALIZE"})]:
                s, j, _ = self.jreq("POST", path, body, cookie)
                self.assertEqual((s, j["code"]), (401, "auth"), path)
        s, j, _ = self.jreq("GET", "/api/admin/settings", cookie=self.ck)
        self.assertEqual(j["settings"]["default_initial_chips"], 100)                      # 設定は変わっていない
        s, j, _ = self.jreq("GET", "/api/participant?code=P1")
        self.assertEqual((j["participant"]["session"]["status"], j["participant"]["session"]["current_points"]),
                         ("active", 850))                                                   # リセットされていない
        self.assertEqual(len(self.jreq("GET", "/api/admin/participants", cookie=self.ck)[1]["participants"]), 2)
        self.assertEqual(Database.list_backups(self.dir / "backups"), [])                  # 初期化もバックアップも走っていない

    # ---- チップ設定 ----
    def test_chip_setting_api_and_new_registration(self):
        s, j, _ = self.jreq("GET", "/api/admin/settings", cookie=self.ck)
        self.assertEqual((s, j["settings"]["default_initial_chips"]), (200, 100))
        self.seed()
        s, j, _ = self.post("/api/admin/settings", {"default_initial_chips": 500})
        self.assertEqual((s, j["settings"]["default_initial_chips"]), (200, 500))
        for bad in [-1, 1.5, "abc", None, True]:
            s, j, _ = self.post("/api/admin/settings", {"default_initial_chips": bad})
            self.assertEqual((s, j["code"]), (400, "bad_number"), repr(bad))
        s, j, _ = self.jreq("GET", "/api/admin/settings", cookie=self.ck)
        self.assertEqual(j["settings"]["default_initial_chips"], 500)
        s, j, _ = self.post("/api/admin/participants", {"code": "N1"})                    # 初期チップ省略 → 設定値
        self.assertEqual(j["result"]["initial_points"], 500)
        s, j, _ = self.post("/api/admin/participants", {"code": "N2", "initial_points": 20})   # 個別指定は維持
        self.assertEqual(j["result"]["initial_points"], 20)
        rows = {r["code"]: r for r in self.jreq("GET", "/api/admin/participants", cookie=self.ck)[1]["participants"]}
        self.assertEqual((rows["P1"]["initial_points"], rows["P1"]["current_points"]), (100, 850))   # 既存は不変
        self.assertEqual(rows["P2"]["initial_points"], 100)

    # ---- プレイヤーのリセット ----
    def test_player_reset_api(self):
        self.seed()
        s, j, _ = self.post("/api/admin/participants/reset", {"code": "P1"})
        self.assertEqual(s, 200)
        self.assertEqual((j["result"]["session_ended"], j["result"]["final_points"], j["result"]["initial_points"]),
                         (True, 850, 100))
        s, j, _ = self.jreq("GET", "/api/admin/history?code=P1", cookie=self.ck)
        self.assertEqual([r["delta"] for r in j["history"]], [750, 100])                  # 履歴は残る
        self.post("/api/admin/sessions/start", {"code": "P1"})
        s, j, _ = self.jreq("GET", "/api/participant?code=P1")
        self.assertEqual(j["participant"]["session"]["current_points"], 100)
        s, j, _ = self.jreq("GET", "/api/admin/sessions?code=P1", cookie=self.ck)
        self.assertEqual([x["status"] for x in j["sessions"]], ["active", "ended"])
        s, j, _ = self.post("/api/admin/participants/reset", {"code": "NOPE"})
        self.assertEqual((s, j["code"]), (404, "not_found"))
        text = "\n".join(self.jreq("GET", "/api/admin/log", cookie=self.ck)[1]["lines"])
        self.assertIn("ADMIN reset code P1", text)                                         # サーバーログに記録

    # ---- DB初期化 ----
    def test_initialize_requires_confirmation_string(self):
        self.seed()
        for body in [{}, {"confirm": ""}, {"confirm": "initialize"}, {"confirm": "yes"}]:
            s, j, _ = self.post("/api/admin/system/initialize", body)
            self.assertEqual((s, j["code"]), (400, "confirm_required"), body)
        self.assertEqual(len(self.jreq("GET", "/api/admin/participants", cookie=self.ck)[1]["participants"]), 2)
        self.assertEqual(Database.list_backups(self.dir / "backups"), [])

    def test_initialize_keeps_admin_password_games_and_settings(self):
        pw_file = self.dir / "data" / "admin_password.txt"
        pw_file.write_text("keep-me\n", encoding="utf-8")
        self.seed()
        self.post("/api/admin/settings", {"default_initial_chips": 500})
        self.post("/api/admin/games", {"name": "Dice"})
        games_before = self.jreq("GET", "/api/admin/games", cookie=self.ck)[1]["games"]
        s, j, _ = self.post("/api/admin/system/initialize", {"confirm": "INITIALIZE"})
        self.assertEqual(s, 200, j)
        result = j["result"]
        self.assertEqual(result["deleted"]["participants"], 2)
        # 0件になっている
        self.assertEqual(self.jreq("GET", "/api/admin/participants", cookie=self.ck)[1]["participants"], [])
        self.assertEqual(self.jreq("GET", "/api/admin/history", cookie=self.ck)[1]["history"], [])
        self.assertEqual(self.jreq("GET", "/api/admin/game-summary", cookie=self.ck)[1]["summary"], [])
        counts = raw_counts(self.dir / "data" / "system.db")
        self.assertEqual((counts["participants"], counts["sessions"], counts["point_history"]), (0, 0, 0))
        # 維持されるもの
        self.assertEqual(self.jreq("GET", "/api/admin/games", cookie=self.ck)[1]["games"], games_before)
        self.assertEqual(self.jreq("GET", "/api/admin/settings", cookie=self.ck)[1]["settings"]["default_initial_chips"], 500)
        self.assertEqual(pw_file.read_text(encoding="utf-8"), "keep-me\n")
        s, j, h = self.jreq("POST", "/api/admin/login", {"password": "pw-test"})          # 同じパスワードでログインできる
        self.assertEqual(s, 200)
        # バックアップが作られ、一覧にも出る
        baks = self.jreq("GET", "/api/admin/backups", cookie=self.ck)[1]["backups"]
        self.assertEqual([b["name"] for b in baks], [result["backup"]["name"]])
        self.assertEqual(raw_counts(self.dir / "backups" / baks[0]["name"])["participants"], 2)
        # 初期化後は通常どおり運用できる（新規登録は設定値500）
        s, j, _ = self.post("/api/admin/participants", {"code": "AFTER"})
        self.assertEqual(j["result"]["initial_points"], 500)
        self.assertEqual(self.post("/api/admin/sessions/start", {"code": "AFTER"})[0], 200)
        text = "\n".join(self.jreq("GET", "/api/admin/log", cookie=self.ck)[1]["lines"])
        self.assertIn("ADMIN database initialized", text)

    def test_initialize_backup_failure_via_api_keeps_data(self):
        self.seed()
        (self.dir / "backups").write_text("blocked")          # backups/ をフォルダにできない状態にする
        s, j, _ = self.post("/api/admin/system/initialize", {"confirm": "INITIALIZE"})
        self.assertEqual((s, j["code"]), (500, "backup_failed"))
        self.assertEqual(len(self.jreq("GET", "/api/admin/participants", cookie=self.ck)[1]["participants"]), 2)

    # ---- 画面側 ----
    def test_ui_wording_is_chip_and_new_controls_exist(self):
        for name in ["index.html", "dealer.html", "admin.html", "app.js", "dealer.js", "admin.js"]:
            text = (BASE / "static" / name).read_text(encoding="utf-8")
            self.assertNotIn("ポイント", text, name)             # 利用者向け表記に「ポイント」が残っていない
        admin = self.req("GET", "/admin")[1].decode("utf-8")
        for needle in ["チップ設定", "初期チップ数", "データベース初期化", "INITIALIZE", "現在チップ"]:
            self.assertIn(needle, admin, needle)
        self.assertNotIn('value="100"', admin)                   # 初期チップはHTMLに固定しない
        js = self.req("GET", "/static/admin.js")[1].decode("utf-8")
        for needle in ["初期状態に戻す", "/api/admin/participants/reset", "/api/admin/system/initialize",
                       "/api/admin/settings", "過去の履歴は保持されます", "この操作は元に戻せません"]:
            self.assertIn(needle, js, needle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
