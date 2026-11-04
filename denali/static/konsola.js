// Konsola maszyny: noVNC (static/novnc) na WebSocket panelu, który przekazuje połączenie do Proxmoxa.
// Hasło VNC to jednorazowy bilet od Proxmoxa — przychodzi z /start i nigdzie nie zostaje.
import RFB from "./novnc/core/rfb.js";

var page = document.getElementById("konsola");
var screen = document.getElementById("console-screen");
var statusEl = document.getElementById("console-status");
var cad = document.getElementById("console-cad");
var end = document.getElementById("console-end");
var rfb = null;

function say(text) {
  statusEl.textContent = text;
}

function finished(text) {
  say(text);
  cad.disabled = true;
  end.textContent = "Wróć";
  rfb = null;
}

// noVNC wie tylko, że połączenie padło. Powód zna panel; chwila na to, żeby zdążył go zapisać.
async function finishedWithReason(text) {
  finished(text);
  await new Promise(function (ok) { setTimeout(ok, 700); });
  var res = await fetch(page.dataset.end).catch(function () { return null; });
  var body = res && res.ok ? await res.json().catch(function () { return {}; }) : {};
  if (body.reason && body.reason !== "Rozłączono.") { say(body.reason); }
}

async function start() {
  var res = await fetch(page.dataset.start, { method: "POST", headers: { "X-CSRF-Token": page.dataset.csrf } });
  var body = await res.json().catch(function () { return {}; });
  if (!res.ok || !body.password) {
    finished(body.error || "Nie udało się otworzyć konsoli.");
    return;
  }
  var scheme = location.protocol === "https:" ? "wss:" : "ws:";
  rfb = new RFB(screen, scheme + "//" + location.host + page.dataset.ws, { credentials: { password: body.password } });
  rfb.scaleViewport = true;
  rfb.background = "#07090b";
  rfb.addEventListener("connect", function () {
    say("Połączono");
    cad.disabled = false;
    rfb.focus();
  });
  var rejected = "";
  rfb.addEventListener("securityfailure", function (e) {
    rejected = "Proxmox odrzucił bilet konsoli" + (e.detail.reason ? ": " + e.detail.reason : "");
  });
  rfb.addEventListener("disconnect", function (e) {
    if (rejected) { finished(rejected); } else { finishedWithReason(e.detail.clean ? "Rozłączono" : "Połączenie przerwane"); }
  });
}

cad.addEventListener("click", function () {
  if (rfb) { rfb.sendCtrlAltDel(); }
});
end.addEventListener("click", function () {
  if (rfb) { rfb.disconnect(); } else { location.href = page.dataset.back; }
});

start().catch(function () {
  finished("Nie udało się otworzyć konsoli.");
});
