"""文化祭チップ管理システム - データベース層（標準ライブラリのsqlite3のみ使用）

方針:
- チップ変更は必ずここ（サーバー側）で、BEGIN IMMEDIATE のトランザクション内で行う。
- チップ更新と履歴INSERTは同一トランザクション。どちらかが失敗すれば両方ロールバック。
- 履歴(point_history)は追記専用。トリガーでUPDATE/DELETEを禁止している。
  （例外: イベント全体のDB初期化のみ、同一トランザクション内でDELETE禁止トリガーを一時的に外して履歴を消し、
  必ずトリガーを作り直してからCOMMITする。途中で失敗すればDDLごとROLLBACKされ、トリガーも元のまま残る）
- 内部のテーブル名・カラム名（point_history / initial_points / current_points など）は既存DBとの互換性のため
  「point」のまま。利用者に見える表記だけを「チップ」にしている。
"""
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

MAX_DELTA = 1000000          # 1回のチップ変更量の上限
MAX_POINTS = 100000000       # 保有チップの上限
DEFAULT_GAMES = ["Blackjack", "Roulette", "Poker", "Game A", "Game B"]
SETTING_DEFAULT_CHIPS = "default_initial_chips"
FALLBACK_DEFAULT_CHIPS = 100   # 設定が未保存・壊れている場合の値（従来の画面の初期値と同じ）


class AppError(Exception):
    """利用者に表示してよい日本語メッセージ付きのエラー"""

    def __init__(self, message, status=400, code="error"):
        Exception.__init__(self, message)
        self.message = message
        self.status = status
        self.code = code


HIST_TRIGGER_UPDATE = """CREATE TRIGGER IF NOT EXISTS hist_no_update BEFORE UPDATE ON point_history
BEGIN SELECT RAISE(ABORT, 'point_history is append-only'); END"""
HIST_TRIGGER_DELETE = """CREATE TRIGGER IF NOT EXISTS hist_no_delete BEFORE DELETE ON point_history
BEGIN SELECT RAISE(ABORT, 'point_history is append-only'); END"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    initial_points INTEGER NOT NULL CHECK(initial_points >= 0),
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    initial_points INTEGER NOT NULL,
    current_points INTEGER NOT NULL CHECK(current_points >= 0),
    status TEXT NOT NULL CHECK(status IN ('active','ended')),
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_session
    ON sessions(participant_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS games(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS point_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    changed_at TEXT NOT NULL,
    delta INTEGER NOT NULL,
    points_after INTEGER NOT NULL,
    game TEXT,
    operator TEXT,
    device TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('start','game','admin')),
    note TEXT,
    request_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS hist_session ON point_history(session_id);
CREATE TABLE IF NOT EXISTS system_settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""" + HIST_TRIGGER_UPDATE + ";\n" + HIST_TRIGGER_DELETE + ";\n"


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_code(code):
    if not isinstance(code, str):
        raise AppError("参加者コードが正しくありません。", 400, "bad_code")
    code = code.strip()
    if not code:
        raise AppError("参加者コードを入力してください。", 400, "bad_code")
    if len(code) > 64:
        raise AppError("参加者コードは64文字以内にしてください。", 400, "bad_code")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in code):
        raise AppError("参加者コードに使えない文字が含まれています。", 400, "bad_code")
    return code


def to_int(value, label, minimum=None, maximum=None):
    # bool は int のサブクラスなので明示的に除外。小数・文字列も受け付けない。
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppError("%sは整数で指定してください。" % label, 400, "bad_number")
    if minimum is not None and value < minimum:
        raise AppError("%sは%d以上にしてください。" % (label, minimum), 400, "bad_number")
    if maximum is not None and value > maximum:
        raise AppError("%sは%d以下にしてください。" % (label, maximum), 400, "bad_number")
    return value


def clean_text(value, label, limit, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AppError("%sが正しくありません。" % label, 400, "bad_text")
    value = value.strip()
    if required and not value:
        raise AppError("%sを入力してください。" % label, 400, "bad_text")
    if len(value) > limit:
        raise AppError("%sは%d文字以内にしてください。" % (label, limit), 400, "bad_text")
    return value


def _wrap(e):
    msg = str(e).lower()
    if "locked" in msg or "busy" in msg:
        return AppError("データベースが混み合っています。もう一度お試しください。", 503, "busy")
    sys.stderr.write("DB error: %r\n" % (e,))
    return AppError("データベースエラーが発生しました。操作は実行されていません。", 500, "db_error")


class Database:
    def __init__(self, path, busy_timeout=10.0):
        self.path = Path(path)
        self.busy_timeout = busy_timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            c = self._connect()
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(SCHEMA)
                if c.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0:
                    for i, name in enumerate(DEFAULT_GAMES):
                        c.execute("INSERT INTO games(name, sort_order) VALUES(?,?)", (name, i))
                # 設定が無いときだけ既定値を入れる（既存DBの値は上書きしない）
                c.execute("INSERT OR IGNORE INTO system_settings(key, value) VALUES(?,?)",
                          (SETTING_DEFAULT_CHIPS, str(FALLBACK_DEFAULT_CHIPS)))
            finally:
                c.close()
        except sqlite3.Error as e:
            raise _wrap(e)

    def _connect(self):
        c = sqlite3.connect(str(self.path), timeout=self.busy_timeout, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    @contextmanager
    def read(self):
        try:
            c = self._connect()
            try:
                yield c
            finally:
                c.close()
        except sqlite3.Error as e:
            raise _wrap(e)

    @contextmanager
    def tx(self):
        """書き込みトランザクション。例外が出れば必ずROLLBACK。"""
        try:
            c = self._connect()
            try:
                c.execute("BEGIN IMMEDIATE")
                try:
                    yield c
                    c.execute("COMMIT")
                except BaseException:
                    try:
                        c.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            finally:
                c.close()
        except sqlite3.Error as e:
            raise _wrap(e)

    # ---------- 参加者コード ----------
    def create_participant(self, code, initial_points=None):
        """initial_points を省略（None）すると「チップ設定」の初期チップ数が使われる。
        既存の参加者・セッションには一切影響しない。"""
        code = normalize_code(code)
        if initial_points is not None:
            initial_points = to_int(initial_points, "初期チップ", 0, MAX_POINTS)
        with self.tx() as c:
            if initial_points is None:
                initial_points = self._default_chips(c)
            try:
                c.execute("INSERT INTO participants(code, initial_points, created_at) VALUES(?,?,?)",
                          (code, initial_points, now()))
            except sqlite3.IntegrityError:
                raise AppError("コード「%s」は既に登録されています。" % code, 409, "duplicate")
        return {"code": code, "initial_points": initial_points}

    # ---------- チップ設定（システム設定） ----------
    @staticmethod
    def _default_chips(c):
        row = c.execute("SELECT value FROM system_settings WHERE key=?", (SETTING_DEFAULT_CHIPS,)).fetchone()
        try:
            v = int(row["value"]) if row else FALLBACK_DEFAULT_CHIPS
        except (TypeError, ValueError):
            return FALLBACK_DEFAULT_CHIPS
        return v if 0 <= v <= MAX_POINTS else FALLBACK_DEFAULT_CHIPS

    def get_default_initial_chips(self):
        with self.read() as c:
            return self._default_chips(c)

    def set_default_initial_chips(self, value):
        """新規登録時の初期チップ数だけを変える。登録済みの参加者・セッション・履歴は変更しない。"""
        value = to_int(value, "初期チップ数", 0, MAX_POINTS)
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO system_settings(key, value) VALUES(?,?)",
                      (SETTING_DEFAULT_CHIPS, str(value)))
        return value

    def set_active(self, code, active):
        code = normalize_code(code)
        with self.tx() as c:
            cur = c.execute("UPDATE participants SET active=? WHERE code=?", (1 if active else 0, code))
            if cur.rowcount == 0:
                raise AppError("参加者コード「%s」は登録されていません。" % code, 404, "not_found")

    def _participant(self, c, code):
        p = c.execute("SELECT * FROM participants WHERE code=?", (code,)).fetchone()
        if p is None:
            raise AppError("参加者コード「%s」は登録されていません。" % code, 404, "not_found")
        return p

    def lookup(self, code):
        """ディーラー画面用: コードから参加者と現在のセッションを返す"""
        code = normalize_code(code)
        with self.read() as c:
            p = self._participant(c, code)
            if not p["active"]:
                raise AppError("コード「%s」は無効になっています。" % code, 409, "inactive")
            s = c.execute("SELECT * FROM sessions WHERE participant_id=? "
                          "ORDER BY (status='active') DESC, id DESC LIMIT 1", (p["id"],)).fetchone()
            return {"code": p["code"], "initial_points": p["initial_points"],
                    "session": dict(s) if s else None}

    def list_participants(self):
        with self.read() as c:
            rows = c.execute(
                "SELECT p.code, p.initial_points, p.active, p.created_at, "
                "s.id AS session_id, s.status, s.started_at, s.ended_at, s.current_points, s.updated_at "
                "FROM participants p LEFT JOIN sessions s ON s.id = ("
                "  SELECT id FROM sessions WHERE participant_id=p.id "
                "  ORDER BY (status='active') DESC, id DESC LIMIT 1) "
                "ORDER BY p.code").fetchall()
            return [dict(r) for r in rows]

    # ---------- セッション ----------
    def start_session(self, code, initial_points=None, operator="admin", device=""):
        code = normalize_code(code)
        if initial_points is not None:
            initial_points = to_int(initial_points, "初期チップ", 0, MAX_POINTS)
        with self.tx() as c:
            p = self._participant(c, code)
            if not p["active"]:
                raise AppError("コード「%s」は無効です。" % code, 409, "inactive")
            if c.execute("SELECT 1 FROM sessions WHERE participant_id=? AND status='active'",
                         (p["id"],)).fetchone():
                raise AppError("コード「%s」のセッションは既に進行中です。" % code, 409, "already_active")
            pts = p["initial_points"] if initial_points is None else initial_points
            t = now()
            cur = c.execute("INSERT INTO sessions(participant_id, started_at, initial_points, "
                            "current_points, status, updated_at) VALUES(?,?,?,?,?,?)",
                            (p["id"], t, pts, pts, "active", t))
            sid = cur.lastrowid
            c.execute("INSERT INTO point_history(session_id, changed_at, delta, points_after, operator, "
                      "device, kind, note) VALUES(?,?,?,?,?,?,?,?)",
                      (sid, t, pts, pts, operator, device, "start", "セッション開始"))
            return {"session_id": sid, "code": code, "points": pts}

    def end_session(self, code):
        code = normalize_code(code)
        with self.tx() as c:
            p = self._participant(c, code)
            s = c.execute("SELECT * FROM sessions WHERE participant_id=? AND status='active'",
                          (p["id"],)).fetchone()
            if not s:
                raise AppError("コード「%s」には進行中のセッションがありません。" % code, 409, "no_session")
            t = now()
            c.execute("UPDATE sessions SET status='ended', ended_at=?, updated_at=? WHERE id=?",
                      (t, t, s["id"]))
            return {"session_id": s["id"], "code": code, "final_points": s["current_points"]}

    def reset_participant(self, code):
        """プレイヤーを「初期状態に戻す」。
        進行中のセッションがあれば終了する（履歴・過去セッションは一切消さない・書き換えない）。
        参加者コード自体も残す。次回のセッション開始は participants.initial_points から始まる。
        進行中のセッションが無い（未開始・終了済み）場合もエラーにしない。"""
        code = normalize_code(code)
        with self.tx() as c:
            p = self._participant(c, code)
            s = c.execute("SELECT * FROM sessions WHERE participant_id=? AND status='active'",
                          (p["id"],)).fetchone()
            ended = s is not None
            if ended:
                t = now()
                c.execute("UPDATE sessions SET status='ended', ended_at=?, updated_at=? WHERE id=?",
                          (t, t, s["id"]))
            return {"code": code, "initial_points": p["initial_points"], "session_ended": ended,
                    "session_id": s["id"] if ended else None,
                    "final_points": s["current_points"] if ended else None}

    def list_sessions(self, code):
        code = normalize_code(code)
        with self.read() as c:
            p = self._participant(c, code)
            rows = c.execute("SELECT * FROM sessions WHERE participant_id=? ORDER BY id DESC",
                             (p["id"],)).fetchall()
            return [dict(r) for r in rows]

    # ---------- チップ変更（最重要） ----------
    def change_points(self, code, delta, game=None, operator="", device="",
                      request_id=None, kind="game", note=None):
        code = normalize_code(code)
        delta = to_int(delta, "チップ変更量", -MAX_DELTA, MAX_DELTA)
        if delta == 0:
            raise AppError("チップ変更量が0です。", 400, "bad_number")
        operator = clean_text(operator, "操作者名", 40, required=True)
        note = clean_text(note, "備考", 200) or None
        if kind not in ("game", "admin"):
            raise AppError("操作種別が正しくありません。", 400, "bad_kind")
        if request_id is not None:
            request_id = clean_text(request_id, "リクエストID", 64) or None
        if kind == "game":
            game = clean_text(game, "ゲーム名", 40, required=True)
        else:
            game = None
        with self.tx() as c:
            # 二重送信対策: 同じrequest_idが既に処理済みなら、再適用せず前回の結果を返す
            if request_id:
                prev = c.execute("SELECT h.delta, h.points_after, s.id AS sid, p.code AS code "
                                 "FROM point_history h JOIN sessions s ON s.id=h.session_id "
                                 "JOIN participants p ON p.id=s.participant_id "
                                 "WHERE h.request_id=?", (request_id,)).fetchone()
                if prev:
                    if prev["code"] != code or prev["delta"] != delta:
                        raise AppError("同じリクエストIDで内容の異なる操作が送信されました。", 409, "request_conflict")
                    return {"code": code, "delta": delta, "points": prev["points_after"],
                            "session_id": prev["sid"], "duplicate": True}
            p = self._participant(c, code)
            if not p["active"]:
                raise AppError("コード「%s」は無効になっています。" % code, 409, "inactive")
            s = c.execute("SELECT * FROM sessions WHERE participant_id=? AND status='active'",
                          (p["id"],)).fetchone()
            if not s:
                ever = c.execute("SELECT 1 FROM sessions WHERE participant_id=?", (p["id"],)).fetchone()
                if ever:
                    raise AppError("このコードのセッションは終了しています。管理者に新しいセッションの開始を依頼してください。",
                                   409, "session_ended")
                raise AppError("セッションが開始されていません。管理者にセッション開始を依頼してください。",
                               409, "no_session")
            if kind == "game":
                if not c.execute("SELECT 1 FROM games WHERE name=? AND active=1", (game,)).fetchone():
                    raise AppError("ゲーム「%s」は登録されていません。画面を更新してください。" % game,
                                   400, "bad_game")
            cur_pts = s["current_points"]
            new_pts = cur_pts + delta
            if new_pts < 0:
                raise AppError("チップが不足しています（現在 %d チップ）。" % cur_pts, 409, "insufficient")
            if new_pts > MAX_POINTS:
                raise AppError("チップの上限を超えます。", 409, "too_many")
            t = now()
            c.execute("UPDATE sessions SET current_points=?, updated_at=? WHERE id=?",
                      (new_pts, t, s["id"]))
            c.execute("INSERT INTO point_history(session_id, changed_at, delta, points_after, game, "
                      "operator, device, kind, note, request_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (s["id"], t, delta, new_pts, game, operator, device, kind, note, request_id))
            return {"code": code, "delta": delta, "points": new_pts,
                    "session_id": s["id"], "duplicate": False}

    # ---------- 履歴・集計 ----------
    def history(self, code=None, session_id=None, limit=500):
        sql = ("SELECT h.id, h.session_id, p.code, h.changed_at, h.delta, h.points_after, h.game, "
               "h.operator, h.device, h.kind, h.note FROM point_history h "
               "JOIN sessions s ON s.id=h.session_id JOIN participants p ON p.id=s.participant_id")
        where, args = [], []
        if code:
            where.append("p.code=?")
            args.append(normalize_code(code))
        if session_id is not None:
            where.append("h.session_id=?")
            args.append(to_int(session_id, "セッションID", 1))
        if where:
            sql += " WHERE " + " AND ".join(where)   # 固定文字列のみ連結（値は?で渡す）
        sql += " ORDER BY h.id DESC LIMIT ?"
        args.append(int(limit))
        with self.read() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def game_summary(self):
        with self.read() as c:
            rows = c.execute(
                "SELECT game, COUNT(*) AS count, "
                "SUM(CASE WHEN delta>0 THEN delta ELSE 0 END) AS plus, "
                "SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END) AS minus, "
                "SUM(delta) AS net FROM point_history WHERE kind='game' "
                "GROUP BY game ORDER BY game").fetchall()
            return [dict(r) for r in rows]

    # ---------- ゲーム名 ----------
    def list_games(self, only_active=True):
        with self.read() as c:
            sql = "SELECT id, name, active FROM games"
            if only_active:
                sql += " WHERE active=1"
            sql += " ORDER BY sort_order, id"
            return [dict(r) for r in c.execute(sql).fetchall()]

    def add_game(self, name):
        name = clean_text(name, "ゲーム名", 40, required=True)
        with self.tx() as c:
            try:
                n = c.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM games").fetchone()[0]
                c.execute("INSERT INTO games(name, sort_order) VALUES(?,?)", (name, n))
            except sqlite3.IntegrityError:
                raise AppError("ゲーム「%s」は既に登録されています。" % name, 409, "duplicate")

    def rename_game(self, game_id, name):
        """名前を変えても過去の履歴は書き換えない（履歴には当時の名前が残る）"""
        game_id = to_int(game_id, "ゲームID", 1)
        name = clean_text(name, "ゲーム名", 40, required=True)
        with self.tx() as c:
            try:
                cur = c.execute("UPDATE games SET name=? WHERE id=?", (name, game_id))
            except sqlite3.IntegrityError:
                raise AppError("ゲーム「%s」は既に登録されています。" % name, 409, "duplicate")
            if cur.rowcount == 0:
                raise AppError("ゲームが見つかりません。", 404, "not_found")

    def set_game_active(self, game_id, active):
        game_id = to_int(game_id, "ゲームID", 1)
        with self.tx() as c:
            cur = c.execute("UPDATE games SET active=? WHERE id=?", (1 if active else 0, game_id))
            if cur.rowcount == 0:
                raise AppError("ゲームが見つかりません。", 404, "not_found")

    # ---------- バックアップ ----------
    def backup(self, backups_dir):
        """sqlite3のバックアップAPIでコピーする（稼働中でも一貫した状態で取れる。元DBは変更しない）"""
        backups_dir = Path(backups_dir)
        dest = None
        try:
            backups_dir.mkdir(parents=True, exist_ok=True)
            base = "system-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = backups_dir / (base + ".db")
            n = 1
            while dest.exists():
                n += 1
                dest = backups_dir / ("%s-%d.db" % (base, n))
            src = self._connect()
            try:
                dst = sqlite3.connect(str(dest))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            return {"name": dest.name, "size": dest.stat().st_size}
        except sqlite3.Error as e:
            self._discard(dest)
            raise _wrap(e)
        except OSError as e:     # 保存先フォルダを作れない・書き込めない・ディスクが一杯 など
            self._discard(dest)
            sys.stderr.write("Backup error: %r\n" % (e,))
            raise AppError("バックアップを保存できません。保存先フォルダ（backups/）を確認してください。",
                           500, "backup_failed")

    @staticmethod
    def _discard(path):
        try:
            if path is not None and Path(path).exists():
                Path(path).unlink()
        except OSError:
            pass

    @staticmethod
    def _verify_backup(path):
        """作成したバックアップが開けて、壊れておらず、必要なテーブルを持つことを確認する"""
        try:
            # 読み取り専用(mode=ro)で開くとWALモードのDBに -shm/-wal が残るため、通常接続で開く
            # （integrity_check と SELECT しか実行しないのでバックアップの中身は変わらない。閉じると残骸も消える）
            con = sqlite3.connect(str(path))
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    return False
                names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                return {"participants", "sessions", "point_history", "games"} <= names
            finally:
                con.close()
        except sqlite3.Error:
            return False

    # ---------- データベース初期化（イベントデータの全消去） ----------
    EVENT_TABLES = ("point_history", "sessions", "participants")   # 外部キーの都合で、この順に消す

    def reset_event_data(self, backups_dir):
        """イベント運用データ（参加者・セッション・チップ履歴）をすべて消す。
        1) 先にバックアップを作り、開けることを確認する。失敗したら何も消さずにエラーにする。
        2) 1つのトランザクション（BEGIN IMMEDIATE）で削除する。途中で失敗すれば全部ROLLBACK。
        消さないもの: ゲーム設定、チップ設定（system_settings）、DBファイル自体。
        管理者パスワード（data/admin_password.txt）はDBの外にあり、ここでは触れない。"""
        try:
            bak = self.backup(backups_dir)
        except AppError as e:
            raise AppError("バックアップに失敗したため、初期化は実行していません。（%s）" % e.message,
                           500, "backup_failed")
        if not self._verify_backup(Path(backups_dir) / bak["name"]):
            self._discard(Path(backups_dir) / bak["name"])
            raise AppError("バックアップを確認できなかったため、初期化は実行していません。", 500, "backup_failed")

        deleted = {}
        with self.tx() as c:
            # 履歴はDELETE禁止トリガーで守られている。同じトランザクション内で外して消し、必ず作り直す。
            # DDLもトランザクションの一部なので、途中で例外が出ればトリガーごと元に戻る。
            c.execute("DROP TRIGGER IF EXISTS hist_no_delete")
            for t in self.EVENT_TABLES:
                deleted[t] = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]   # テーブル名は固定値
                c.execute("DELETE FROM %s" % t)
            c.execute("DELETE FROM sqlite_sequence WHERE name IN ('participants','sessions','point_history')")
            c.execute(HIST_TRIGGER_DELETE)
            for t in self.EVENT_TABLES:
                if c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] != 0:
                    raise AppError("初期化の確認に失敗しました。変更は取り消されました。", 500, "init_failed")
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='hist_no_delete'").fetchone():
                raise AppError("初期化の確認に失敗しました。変更は取り消されました。", 500, "init_failed")
        return {"backup": bak, "deleted": {"participants": deleted["participants"],
                                           "sessions": deleted["sessions"],
                                           "history": deleted["point_history"]}}

    @staticmethod
    def list_backups(backups_dir):
        d = Path(backups_dir)
        if not d.is_dir():
            return []
        files = sorted(d.glob("system-*.db"), reverse=True)
        return [{"name": f.name, "size": f.stat().st_size} for f in files]
