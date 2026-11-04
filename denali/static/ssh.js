// Terminal SSH: xterm.js na WebSocket panelu. Klawisze idą binarnie, rozmiar okna tekstem (JSON).
import { Terminal } from "./xterm/xterm.mjs";
import { FitAddon } from "./xterm/addon-fit.mjs";

var page = document.getElementById("ssh");
var statusEl = document.getElementById("console-status");
var end = document.getElementById("console-end");
var encoder = new TextEncoder();

var term = new Terminal({
  cursorBlink: true,
  fontFamily: "Consolas, 'Cascadia Mono', 'DejaVu Sans Mono', Menlo, monospace",
  fontSize: 14,
  scrollback: 10000,
  theme: { background: "#07090b", foreground: "#e6e9ed", cursor: "#6ea8ff", selectionBackground: "#3a5a8a" }
});
var fit = new FitAddon();
term.loadAddon(fit);
term.open(document.getElementById("ssh-screen"));
fit.fit();
term.focus();

var scheme = location.protocol === "https:" ? "wss:" : "ws:";
var ws = new WebSocket(scheme + "//" + location.host + page.dataset.ws);
ws.binaryType = "arraybuffer";

function open() {
  return ws.readyState === WebSocket.OPEN;
}

function sendSize() {
  if (open()) { ws.send(JSON.stringify({ cols: term.cols, rows: term.rows })); }
}

ws.onopen = function () {
  statusEl.textContent = "Połączono";
  sendSize();
};
ws.onmessage = function (e) {
  term.write(new Uint8Array(e.data));
};
ws.onclose = async function () {
  end.textContent = "Wróć";
  statusEl.textContent = "Rozłączono";
  await new Promise(function (ok) { setTimeout(ok, 500); });
  var res = await fetch(page.dataset.end).catch(function () { return null; });
  var body = res && res.ok ? await res.json().catch(function () { return {}; }) : {};
  if (body.reason) { statusEl.textContent = body.reason; }
};

term.onData(function (data) {
  if (open()) { ws.send(encoder.encode(data)); }
});
term.onResize(sendSize);
new ResizeObserver(function () { fit.fit(); }).observe(document.getElementById("ssh-screen"));

function paste() {
  navigator.clipboard.readText().then(function (text) { term.paste(text); }).catch(function () {});
}

// Jak w cmd: Ctrl+C kopiuje, gdy coś jest zaznaczone, a bez zaznaczenia przerywa program.
term.attachCustomKeyEventHandler(function (e) {
  if (e.type !== "keydown" || !e.ctrlKey) { return true; }
  var key = e.key.toLowerCase();
  if (key === "c" && term.hasSelection()) {
    navigator.clipboard.writeText(term.getSelection());
    term.clearSelection();
    e.preventDefault();
    return false;
  }
  if (key === "v") {
    e.preventDefault();
    paste();
    return false;
  }
  return true;
});

// Prawy przycisk też jak w cmd: kopiuje zaznaczenie albo wkleja.
document.getElementById("ssh-screen").addEventListener("contextmenu", function (e) {
  e.preventDefault();
  if (term.hasSelection()) {
    navigator.clipboard.writeText(term.getSelection());
    term.clearSelection();
  } else {
    paste();
  }
});

document.querySelectorAll("[data-preset]").forEach(function (b) {
  b.addEventListener("click", function () {
    term.paste(b.dataset.preset);
    term.focus();
  });
});

end.addEventListener("click", function () {
  if (open()) { ws.close(); } else { location.href = page.dataset.back; }
});
