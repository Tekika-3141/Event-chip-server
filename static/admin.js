"use strict";
/* 管理画面。サーバーは管理者Cookieが無い要求を401で拒否する（画面を隠すだけではない）。 */

var timer = null;

function gmsg(text, kind) { showMsg($("globalMsg"), text, kind); }

function rows(tableId, html) { document.querySelector("#" + tableId + " tbody").innerHTML = html; }

/* 失敗してもメッセージを出して続行。401ならログイン画面へ戻す */
async function call(path, body, okText) {
  try {
    var r = await api(path, body);
    if (okText) { gmsg(okText, "ok"); }
    return r;
  } catch (e) {
    if (e.status === 401) { showLogin(); }
    else { gmsg(e.message, "err"); }
    return null;
  }
}

function showLogin() {
  $("main").classList.add("hidden");
  $("loginBox").classList.remove("hidden");
  if (timer) { clearInterval(timer); timer = null; $("auto").checked = false; }
}

async function enter() {
  try {
    await api("/api/admin/me");
  } catch (e) {
    showLogin();
    return;
  }
  $("loginBox").classList.add("hidden");
  $("main").classList.remove("hidden");
  refreshAll();
}

$("loginBtn").addEventListener("click", async function () {
  try {
    await api("/api/admin/login", { password: $("pw").value });
    $("pw").value = "";
    clearMsg($("loginMsg"));
    enter();
  } catch (e) {
    showMsg($("loginMsg"), e.message, "err");
  }
});
$("pw").addEventListener("keydown", function (ev) { if (ev.key === "Enter") { $("loginBtn").click(); } });
$("logoutBtn").addEventListener("click", async function () {
  await call("/api/admin/logout", {});
  showLogin();
});

/* ---- 一覧 ---- */
async function loadParticipants() {
  var r = await call("/api/admin/participants");
  if (!r) { return; }
  rows("partTable", r.participants.map(function (p) {
    var st, pts = "—", started = "—", upd = "—";
    if (!p.active) { st = "無効"; }
    else if (p.status === "active") { st = "セッション中"; }
    else if (p.status === "ended") { st = "終了"; }
    else { st = "未開始"; }
    if (p.session_id) { pts = fmt(p.current_points); started = p.started_at; upd = p.updated_at; }
    var btns = "";
    if (p.active && p.status !== "active") { btns += '<button data-act="start">セッション開始</button> '; }
    if (p.status === "active") { btns += '<button data-act="end">セッション終了</button> '; }
    btns += '<button data-act="reset" class="reset">初期状態に戻す</button> ';
    btns += '<button data-act="adjust">修正</button> <button data-act="hist">履歴</button> ' +
            '<button data-act="toggle">' + (p.active ? "無効にする" : "有効にする") + "</button>";
    return '<tr class="' + (p.active ? "" : "inactive") + '" data-code="' + esc(p.code) + '" data-active="' + p.active +
      '"><td>' + esc(p.code) + "</td><td>" + st + '</td><td class="num">' + pts + " チップ</td><td>" + esc(started) +
      "</td><td>" + esc(upd) + "</td><td>" + btns + "</td></tr>";
  }).join(""));
}

$("partTable").addEventListener("click", async function (ev) {
  var b = ev.target.closest("button");
  if (!b) { return; }
  var tr = b.closest("tr"), code = tr.getAttribute("data-code"), act = b.getAttribute("data-act");
  if (act === "start") {
    if (await call("/api/admin/sessions/start", { code: code }, "セッションを開始しました: " + code)) { loadParticipants(); loadLog(); }
  } else if (act === "end") {
    if (!confirm("コード「" + code + "」のセッションを終了しますか？（履歴は残ります）")) { return; }
    var r = await call("/api/admin/sessions/end", { code: code });
    if (r) { gmsg("終了しました: " + code + "（最終 " + fmt(r.result.final_points) + " チップ）", "ok"); loadParticipants(); loadLog(); }
  } else if (act === "reset") {
    if (!confirm("コード「" + code + "」を初期状態に戻します。\n現在のセッションは終了し、次回は初期チップから開始されます。\n過去の履歴は保持されます。\n本当に実行しますか？")) { return; }
    var rr = await call("/api/admin/participants/reset", { code: code });
    if (rr) {
      var x = rr.result;
      gmsg("初期状態に戻しました: " + code + "（" +
           (x.session_ended ? "進行中のセッションを終了しました。最終 " + fmt(x.final_points) + " チップ" : "進行中のセッションはありませんでした") +
           "）。次回は " + fmt(x.initial_points) + " チップから開始されます。", "ok");
      loadParticipants(); loadLog();
    }
  } else if (act === "adjust") {
    $("adjCode").value = code;
    $("adjDelta").focus();
  } else if (act === "hist") {
    $("histCode").value = code;
    showHistory(true);
  } else if (act === "toggle") {
    var to = tr.getAttribute("data-active") !== "1";
    if (await call("/api/admin/participants/active", { code: code, active: to }, (to ? "有効にしました: " : "無効にしました: ") + code)) { loadParticipants(); }
  }
});

/* ---- チップ設定（初期チップ数のデフォルト） ---- */
var newPtsDirty = false;      // 管理者が登録欄の初期チップを手で変えたか（変えた値は自動更新で上書きしない）
$("newPts").addEventListener("input", function () { newPtsDirty = true; });

async function loadSettings() {
  var r = await call("/api/admin/settings");
  if (!r) { return; }
  var v = r.settings.default_initial_chips;
  $("defCurrent").textContent = fmt(v) + " チップ";
  if (document.activeElement !== $("defChips")) { $("defChips").value = v; }
  if (!newPtsDirty) { $("newPts").value = v; }
}
$("defSaveBtn").addEventListener("click", async function () {
  var raw = $("defChips").value.trim();
  if (!/^\d+$/.test(raw)) { gmsg("初期チップ数は0以上の整数で入力してください。", "err"); return; }
  var r = await call("/api/admin/settings", { default_initial_chips: parseInt(raw, 10) },
                     "初期チップ数を " + fmt(parseInt(raw, 10)) + " に設定しました（以降に新しく登録するプレイヤーから適用されます）。");
  if (r) {
    $("defChips").blur();
    newPtsDirty = false;
    loadSettings();
  }
});

$("addBtn").addEventListener("click", async function () {
  var pts = parseInt($("newPts").value, 10);
  if (isNaN(pts)) { gmsg("初期チップは整数で入力してください。", "err"); return; }
  var r = await call("/api/admin/participants", { code: $("newCode").value, initial_points: pts },
                     "登録しました: " + $("newCode").value.trim());
  if (r) { $("newCode").value = ""; $("newCode").focus(); newPtsDirty = false; loadSettings(); loadParticipants(); }
});
$("newCode").addEventListener("keydown", function (ev) { if (ev.key === "Enter") { $("addBtn").click(); } });  // USBスキャナーでも登録できる

/* ---- 手動修正 ---- */
var adjId = null;
$("adjBtn").addEventListener("click", async function () {
  var delta = parseInt($("adjDelta").value, 10), code = $("adjCode").value.trim();
  if (!code || isNaN(delta) || delta === 0) { gmsg("コードと、0以外の整数のチップ変更量を入力してください。", "err"); return; }
  if (!confirm("コード「" + code + "」のチップを " + signed(delta) + " 修正します。よろしいですか？")) { return; }
  if (!adjId) { adjId = newRequestId(); }
  var r = await call("/api/admin/adjust", { code: code, delta: delta, note: $("adjNote").value, request_id: adjId });
  if (r) {
    adjId = null;
    gmsg("修正しました: " + signed(delta) + " チップ → 現在 " + fmt(r.result.points) + " チップ", "ok");
    $("adjDelta").value = ""; $("adjNote").value = "";
    loadParticipants(); loadLog();
  }
});
["adjCode", "adjDelta"].forEach(function (id) { $(id).addEventListener("input", function () { adjId = null; }); });

/* ---- 履歴 ---- */
async function showHistory(loadSessions) {
  var code = $("histCode").value.trim();
  var sel = $("histSession");
  if (loadSessions) {
    sel.innerHTML = '<option value="">すべて</option>';
    if (code) {
      var s = await call("/api/admin/sessions?code=" + encodeURIComponent(code));
      if (!s) { return; }
      sel.innerHTML += s.sessions.map(function (x) {
        return '<option value="' + x.id + '">#' + x.id + " " + esc(x.started_at) +
               (x.status === "active" ? "（進行中）" : "（終了）") + "</option>";
      }).join("");
    }
  }
  var q = "/api/admin/history?code=" + encodeURIComponent(code) + "&session_id=" + encodeURIComponent(sel.value);
  var r = await call(q);
  if (!r) { return; }
  var kinds = { start: "開始", game: "ゲーム", admin: "管理者修正" };
  rows("histTable", r.history.map(function (h) {
    return "<tr><td>" + esc(h.changed_at) + "</td><td>" + esc(h.code) + "</td><td>#" + h.session_id + "</td><td>" +
      (kinds[h.kind] || esc(h.kind)) + "</td><td>" + esc(h.game) + '</td><td class="num ' + (h.delta < 0 ? "minus" : "plus") + '">' +
      signed(h.delta) + '</td><td class="num">' + fmt(h.points_after) + "</td><td>" + esc(h.operator) + "</td><td>" +
      esc(h.device) + "</td><td>" + esc(h.note) + "</td></tr>";
  }).join(""));
}
$("histBtn").addEventListener("click", function () { showHistory(false); });
$("histCode").addEventListener("change", function () { showHistory(true); });
$("histSession").addEventListener("change", function () { showHistory(false); });

/* ---- ゲーム ---- */
async function loadGames() {
  var r = await call("/api/admin/games");
  if (!r) { return; }
  rows("gameTable", r.games.map(function (g) {
    return '<tr class="' + (g.active ? "" : "inactive") + '" data-id="' + g.id + '" data-name="' + esc(g.name) +
      '" data-active="' + g.active + '"><td>' + esc(g.name) + "</td><td>" + (g.active ? "使用中" : "停止中") +
      '</td><td><button data-act="rename">名前変更</button> <button data-act="toggle">' +
      (g.active ? "停止" : "再開") + "</button></td></tr>";
  }).join(""));
}
$("gameTable").addEventListener("click", async function (ev) {
  var b = ev.target.closest("button");
  if (!b) { return; }
  var tr = b.closest("tr"), id = parseInt(tr.getAttribute("data-id"), 10);
  if (b.getAttribute("data-act") === "rename") {
    var n = prompt("新しいゲーム名", tr.getAttribute("data-name"));
    if (n === null) { return; }
    if (await call("/api/admin/games/rename", { id: id, name: n }, "名前を変更しました。")) { loadGames(); }
  } else {
    var to = tr.getAttribute("data-active") !== "1";
    if (await call("/api/admin/games/active", { id: id, active: to })) { loadGames(); }
  }
});
$("addGameBtn").addEventListener("click", async function () {
  if (await call("/api/admin/games", { name: $("newGame").value }, "ゲームを追加しました。")) { $("newGame").value = ""; loadGames(); }
});

async function loadSummary() {
  var r = await call("/api/admin/game-summary");
  if (!r) { return; }
  rows("sumTable", r.summary.map(function (s) {
    return "<tr><td>" + esc(s.game) + '</td><td class="num">' + s.count + '</td><td class="num">' + fmt(s.plus) +
      '</td><td class="num">' + fmt(s.minus) + '</td><td class="num">' + signed(s.net) + "</td></tr>";
  }).join(""));
}

/* ---- バックアップ・ログ ---- */
async function loadBackups() {
  var r = await call("/api/admin/backups");
  if (!r) { return; }
  rows("bakTable", r.backups.map(function (b) {
    return "<tr><td>" + esc(b.name) + '</td><td class="num">' + fmt(b.size) + " bytes</td></tr>";
  }).join(""));
}
$("backupBtn").addEventListener("click", async function () {
  var r = await call("/api/admin/backup", {});
  if (r) { gmsg("バックアップを作成しました: backups/" + r.result.name, "ok"); loadBackups(); }
});

async function loadLog() {
  var r = await call("/api/admin/log");
  if (!r) { return; }
  var box = $("logBox");
  box.textContent = r.lines.join("\n");
  box.scrollTop = box.scrollHeight;
}

/* ---- システム管理: データベース初期化 ---- */
$("initConfirm").addEventListener("input", function () {
  $("initBtn").disabled = $("initConfirm").value.trim() !== "INITIALIZE";
});
$("initBtn").addEventListener("click", async function () {
  if ($("initConfirm").value.trim() !== "INITIALIZE") { return; }
  if (!confirm("【重要】データベースを初期化します。\n\n参加者、セッション、チップ履歴などのイベントデータがすべて削除されます。\n初期化前に自動バックアップを作成します。\nこの操作は元に戻せません。\n\n本当に初期化しますか？")) { return; }
  $("initBtn").disabled = true;
  var r = await call("/api/admin/system/initialize", { confirm: $("initConfirm").value.trim() });
  $("initConfirm").value = "";
  if (r) {
    var d = r.result.deleted;
    gmsg("初期化が完了しました。バックアップ: backups/" + r.result.backup.name + "（削除: 参加者コード " + fmt(d.participants) +
         "件 / セッション " + fmt(d.sessions) + "件 / チップ履歴 " + fmt(d.history) + "件）。", "ok");
    $("histCode").value = ""; $("histSession").innerHTML = '<option value="">すべて</option>';
    rows("histTable", "");
    refreshAll();
  }
});

function refreshAll() { loadSettings(); loadParticipants(); loadGames(); loadSummary(); loadBackups(); loadLog(); }
$("reloadBtn").addEventListener("click", refreshAll);
$("auto").addEventListener("change", function () {
  if (timer) { clearInterval(timer); timer = null; }
  if ($("auto").checked) { timer = setInterval(function () { loadParticipants(); loadSummary(); loadLog(); }, 5000); }
});

enter();
