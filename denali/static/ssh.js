// Terminal SSH: xterm.js na WebSocket panelu. Klawisze idą binarnie, rozmiar okna tekstem (JSON).
// Etapy są widoczne, bo każdy może paść osobno: wczytanie skryptów → WebSocket do panelu → panel do maszyny → powłoka.
var page = document.getElementById("ssh");
var statusEl = document.getElementById("console-status");
var end = document.getElementById("console-end");
var CONNECT_LIMIT = 15000;      // tyle WebSocket może zwlekać, zanim uznamy, że proxy go nie przepuszcza

function say(text, bad) {
  statusEl.textContent = text;
  statusEl.classList.toggle("console-bad", Boolean(bad));
}

async function main() {
  page.dataset.ready = "1";     // denali.js pilnuje, czy ten skrypt w ogóle ruszył
  var xterm, addon;
  try {
    [xterm, addon] = await Promise.all([import("./xterm/xterm.mjs"), import("./xterm/addon-fit.mjs")]);
  } catch (e) {
    say("Nie wczytał się xterm.js (" + e.message + "). Odśwież stronę: Ctrl+F5.", true);
    return;
  }
  var encoder = new TextEncoder();
  var term = new xterm.Terminal({
    cursorBlink: true,
    fontFamily: "Consolas, 'Cascadia Mono', 'DejaVu Sans Mono', Menlo, monospace",
    fontSize: 14,
    scrollback: 10000,
    theme: { background: "#07090b", foreground: "#e6e9ed", cursor: "#6ea8ff", selectionBackground: "#3a5a8a" }
  });
  var fit = new addon.FitAddon();
  term.loadAddon(fit);
  term.open(document.getElementById("ssh-screen"));
  fit.fit();
  term.focus();

  say("Łączę z panelem…");
  var scheme = location.protocol === "https:" ? "wss:" : "ws:";
  var ws = new WebSocket(scheme + "//" + location.host + page.dataset.ws);
  ws.binaryType = "arraybuffer";
  var reachedPanel = false, reachedMachine = false, tooLong = false, finished = false;

  // Przy wiszącym połączeniu przeglądarka może długo nie wywołać onclose, więc limit sam kończy sesję.
  var limit = setTimeout(function () {
    tooLong = true;
    ws.close();
    finish(1006);
  }, CONNECT_LIMIT);

  function open() {
    return ws.readyState === WebSocket.OPEN;
  }

  function sendSize() {
    if (open()) { ws.send(JSON.stringify({ cols: term.cols, rows: term.rows })); }
  }

  ws.onopen = function () {
    clearTimeout(limit);
    reachedPanel = true;
    say("Panel łączy się z maszyną…");
    term.writeln("\x1b[2mPanel łączy się z maszyną, może to potrwać do 10 sekund…\x1b[0m");
    sendSize();
  };
  ws.onmessage = function (e) {
    if (!reachedMachine) {
      reachedMachine = true;
      term.reset();
      say("Połączono");
    }
    term.write(new Uint8Array(e.data));
  };
  async function finish(code) {
    if (finished) { return; }
    finished = true;
    clearTimeout(limit);
    end.textContent = "Wróć";
    say("Rozłączono");
    await new Promise(function (ok) { setTimeout(ok, 500); });
    var res = await fetch(page.dataset.end).catch(function () { return null; });
    var body = res && res.ok ? await res.json().catch(function () { return {}; }) : {};
    var reason = body.reason || "";
    if (!reason && tooLong) { reason = "WebSocket nie odpowiada: włącz Websockets Support w Nginx Proxy Manager."; }
    if (!reason && !reachedPanel) { reason = "Przeglądarka nie dostała połączenia WebSocket z panelem (kod " + code + ")."; }
    if (!reason && !reachedMachine) { reason = "Maszyna nie odpowiedziała (kod " + code + ")."; }
    if (reason) {
      say(reason, !reachedMachine);
      term.writeln("\r\n\x1b[1;31m" + reason + "\x1b[0m");
    }
  }
  ws.onclose = function (e) { finish(e.code); };

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
    if (finished) { location.href = page.dataset.back; } else { ws.close(); }
  });
}

main().catch(function (e) {
  say("Terminal się nie uruchomił: " + e.message, true);
});
