"use strict";
/* ディーラー画面。QRカメラ・USBスキャナー・手入力はすべて lookupCode() に合流する。 */

var current = null;       // { code, session } 表示中の参加者
var pending = null;       // 通信エラー時の再送用 { key, id }
var busy = false;

var elOp = $("operator"), elCode = $("codeInput"), elDelta = $("delta"), elGame = $("game");

elOp.value = loadPref("dealerOperator");
elOp.addEventListener("input", function () { savePref("dealerOperator", elOp.value.trim()); });

/* ---- ゲーム一覧 ---- */
async function loadGames() {
  try {
    var r = await api("/api/games");
    var keep = elGame.value;
    elGame.innerHTML = r.games.map(function (g) { return "<option>" + esc(g) + "</option>"; }).join("");
    if (keep && r.games.indexOf(keep) >= 0) { elGame.value = keep; }
    else { var saved = loadPref("dealerGame"); if (r.games.indexOf(saved) >= 0) { elGame.value = saved; } }
  } catch (e) {
    showMsg($("lookupMsg"), e.message, "err");
  }
}
elGame.addEventListener("change", function () { savePref("dealerGame", elGame.value); });

/* ---- 表示 ---- */
function render() {
  var box = $("partBox");
  if (!current) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  $("partCode").textContent = current.code;
  var s = current.session;
  var active = !!(s && s.status === "active");
  $("points").textContent = s ? fmt(s.current_points) + " チップ" : "—";
  if (!s) {
    $("sessionInfo").textContent = "セッション未開始（管理者に開始を依頼してください）";
  } else if (!active) {
    $("sessionInfo").textContent = "セッション終了済み（最終 " + fmt(s.current_points) + " チップ）。変更できません。";
  } else {
    $("sessionInfo").textContent = "セッション進行中（開始 " + s.started_at + "）";
  }
  $("changeBox").classList.toggle("hidden", !active);
  updatePreview();
}

function updatePreview() {
  var el = $("preview");
  var d = parseInt(elDelta.value, 10);
  var s = current && current.session;
  if (!s || s.status !== "active" || isNaN(d) || d === 0) { el.textContent = ""; return; }
  var after = s.current_points + d;
  el.textContent = fmt(s.current_points) + " → " + fmt(after) + " チップ" + (after < 0 ? "（チップ不足）" : "");
  el.className = "status " + (after < 0 ? "minus" : "");
}

/* ---- コード検索（3経路の合流点） ---- */
async function lookupCode(raw, keepResult) {
  var code = (raw || "").trim();
  if (!keepResult) { clearMsg($("resultMsg")); }
  if (!code) { showMsg($("lookupMsg"), "参加者コードを入力してください。", "err"); return; }
  try {
    var r = await api("/api/participant?code=" + encodeURIComponent(code));
    current = { code: r.participant.code, session: r.participant.session };
    pending = null;
    elDelta.value = "";
    clearMsg($("lookupMsg"));
    loadGames();
    render();
  } catch (e) {
    current = null;
    render();
    showMsg($("lookupMsg"), e.message, "err");
  }
  elCode.value = "";
  elCode.focus();
}

elCode.addEventListener("keydown", function (ev) {
  if (ev.key === "Enter") { ev.preventDefault(); lookupCode(elCode.value); }   // USBスキャナーは通常Enterを送る
});
$("searchBtn").addEventListener("click", function () { lookupCode(elCode.value); });

/* ---- QRカメラ ---- */
$("camBtn").addEventListener("click", function () {
  $("camBox").classList.remove("hidden");
  clearMsg($("lookupMsg"));
  QrReader.start($("video"), function (text) {
    $("camBox").classList.add("hidden");
    lookupCode(text);
  }, function (err) {
    $("camBox").classList.add("hidden");
    showMsg($("lookupMsg"), err.message, "err");
  });
});
$("camStop").addEventListener("click", function () {
  QrReader.stop();
  $("camBox").classList.add("hidden");
});

/* ---- チップ変更 ---- */
document.querySelectorAll(".presets button").forEach(function (b) {
  b.addEventListener("click", function () { elDelta.value = b.getAttribute("data-d"); updatePreview(); });
});
elDelta.addEventListener("input", updatePreview);

$("applyBtn").addEventListener("click", async function () {
  if (busy || !current) { return; }
  var res = $("resultMsg");
  var delta = parseInt(elDelta.value, 10);
  var op = elOp.value.trim();
  if (!op) { showMsg(res, "操作者名を入力してください。", "err"); elOp.focus(); return; }
  if (isNaN(delta) || delta === 0 || String(delta) !== elDelta.value.trim().replace(/^\+/, "")) {
    showMsg(res, "チップ変更量は0以外の整数で入力してください。", "err"); return;
  }
  var game = elGame.value;
  var key = [current.code, delta, game].join("|");
  if (!pending || pending.key !== key) { pending = { key: key, id: newRequestId() }; }
  busy = true;
  $("applyBtn").disabled = true;
  try {
    var r = await api("/api/points/change", {
      code: current.code, delta: delta, game: game, operator: op, request_id: pending.id
    });
    pending = null;
    current.session.current_points = r.result.points;
    elDelta.value = "";
    render();
    showMsg(res, (r.result.duplicate ? "この操作は既に実行済みです。" : "変更しました: ") +
                 signed(r.result.delta) + " チップ（" + game + "）→ 現在 " + fmt(r.result.points) + " チップ", "ok");
  } catch (e) {
    if (e.network) {
      /* 応答が届かなかっただけで実行済みの可能性もあるため、再送しても二重にはならない（同じ request_id） */
      showMsg(res, e.message + " 通信が途中で切れた場合に備え、結果は再検索して現在チップを確認してください。", "err");
    } else {
      pending = null;
      showMsg(res, e.message, "err");
      if (e.code === "session_ended" || e.code === "no_session" || e.code === "inactive") {
        lookupCode(current.code, true);
      }
    }
  } finally {
    busy = false;
    $("applyBtn").disabled = false;
  }
});

$("nextBtn").addEventListener("click", function () {
  current = null; pending = null; elDelta.value = "";
  clearMsg($("resultMsg")); clearMsg($("lookupMsg"));
  render();
  elCode.focus();
});

loadGames();
render();
