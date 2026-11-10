"use strict";
/* QRコード読み取り（カメラ映像 → jsQR でデコード）。QRの生成機能はありません。
   jsQR.js（同梱, Apache-2.0）以外のライブラリは使いません。 */
var QrReader = (function () {
  var stream = null, timer = null, video = null;
  var canvas = document.createElement("canvas");
  var ctx = canvas.getContext("2d");

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
    if (video) { video.srcObject = null; }
  }

  function describe(e) {
    var n = e && e.name;
    if (n === "NotAllowedError" || n === "SecurityError") {
      return "カメラの使用が許可されていません。ブラウザの設定でカメラを許可してください。";
    }
    if (n === "NotFoundError" || n === "OverconstrainedError") { return "カメラが見つかりません。"; }
    if (n === "NotReadableError") { return "カメラを使用できません（他のアプリが使用中の可能性があります）。"; }
    return (e && e.message) || "カメラを起動できませんでした。";
  }

  /* onCode(文字列) は読み取り成功時に1回だけ呼ばれ、カメラは自動で止まる */
  function start(videoEl, onCode, onError) {
    stop();
    if (typeof jsQR !== "function") {
      onError(new Error("QR読み取りライブラリ(jsQR.js)が読み込まれていません。"));
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      onError(new Error("このブラウザ／接続ではカメラを使用できません。" +
        "カメラはHTTPSまたはlocalhostでのみ使用できます。手入力かUSBスキャナーを使用してください。"));
      return;
    }
    video = videoEl;
    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false })
      .then(function (s) {
        stream = s;
        video.srcObject = s;
        video.setAttribute("playsinline", "true");
        return video.play();
      })
      .then(function () {
        timer = setInterval(function () {
          if (!stream || video.readyState < 2 || !video.videoWidth) { return; }
          var w = Math.min(video.videoWidth, 640);
          var h = Math.round(video.videoHeight * w / video.videoWidth);
          canvas.width = w;
          canvas.height = h;
          ctx.drawImage(video, 0, 0, w, h);
          var img = ctx.getImageData(0, 0, w, h);
          var r = jsQR(img.data, w, h, { inversionAttempts: "dontInvert" });
          if (r && r.data) {
            stop();
            onCode(r.data);
          }
        }, 250);
      })
      .catch(function (e) { stop(); onError(new Error(describe(e))); });
  }

  return { start: start, stop: stop };
})();
