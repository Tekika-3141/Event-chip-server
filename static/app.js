"use strict";
/* 共通ヘルパー（ディーラー画面・管理画面で共有） */

function $(id) { return document.getElementById(id); }

function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmt(n) { return Number(n).toLocaleString("ja-JP"); }

function signed(n) { return (n > 0 ? "+" : "") + fmt(n); }

/* GET: api(path)  /  POST: api(path, {...})
   通信できない場合は err.network = true の例外にする（成功したようには見せない） */
async function api(path, body) {
  var opt = body === undefined
    ? { cache: "no-store" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  var res;
  try {
    res = await fetch(path, opt);
  } catch (e) {
    var ne = new Error("サーバーに接続できません。操作は実行されていません。");
    ne.network = true;
    throw ne;
  }
  var data = null;
  try { data = await res.json(); } catch (e) { /* JSONでない応答 */ }
  if (!res.ok || !data || data.ok === false) {
    var err = new Error((data && data.error) || ("サーバーエラーが発生しました (" + res.status + ")"));
    err.status = res.status;
    err.code = data && data.code;
    throw err;
  }
  return data;
}

function showMsg(el, text, kind) {
  el.textContent = text;
  el.className = "msg " + (kind || "info");
}

function clearMsg(el) { el.textContent = ""; el.className = "msg"; }

/* 二重送信防止用のID（http環境ではcrypto.randomUUIDが使えないことがあるので自前） */
function newRequestId() {
  return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10) +
         Math.random().toString(36).slice(2, 6);
}

/* localStorageが使えない環境でも落ちないようにする */
function loadPref(key) { try { return localStorage.getItem(key) || ""; } catch (e) { return ""; } }
function savePref(key, v) { try { localStorage.setItem(key, v); } catch (e) { /* 無視 */ } }
