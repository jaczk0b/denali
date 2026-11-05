// Odświeżanie na żywo (SSE) i zegar pulpitu. Bez bibliotek, bez kroku budowania.
//
// Strona z atrybutem data-live na <body> słucha /na-zywo. Gdy odcisk stanu się zmieni, pobiera samą siebie
// i podmienia treść — bez mrugnięcia i bez utraty przewinięcia. Nie podmienia, gdy coś piszesz albo masz
// otwarte menu: czeka, aż skończysz.
(function () {
  "use strict";
  var body = document.body;
  var HOUR = 3600 * 1000;

  function clock() {
    var el = document.querySelector(".clock");
    if (el) {
      el.textContent = new Date().toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
    }
  }
  if (document.querySelector(".clock")) {
    setInterval(clock, 10 * 1000);
  }

  // Panel nie odpowiada → nie przeładowujemy na ślepo (tablet zostałby na stronie błędu przeglądarki),
  // tylko sprawdzamy co 30 s i przeładowujemy, gdy wróci.
  function reloadWhenUp() {
    fetch("/health", { cache: "no-store" })
      .then(function (r) {
        if (r.ok) { location.reload(); } else { setTimeout(reloadWhenUp, 30 * 1000); }
      })
      .catch(function () { setTimeout(reloadWhenUp, 30 * 1000); });
  }

  // Style liczone na serwerze (szerokość paska, miejsce w szafie) są w data-style: CSP blokuje atrybut style="…",
  // a ustawienie przez CSSOM przepuszcza.
  function inlineStyles() {
    var els = document.querySelectorAll("[data-style]");
    for (var i = 0; i < els.length; i++) {
      els[i].style.cssText = els[i].getAttribute("data-style");
    }
  }
  inlineStyles();

  // Wyjście do Pocket ID: zwykła nawigacja, bo przekierowania formularza poza panel blokuje CSP (form-action).
  var leave = document.querySelector("a[data-autogo]");
  if (leave) { location.replace(leave.href); }

  // Po potwierdzeniu hasłem panel sam dokańcza działanie, które na nie czekało.
  var resume = document.querySelector("form[data-autosubmit]");
  if (resume) { resume.submit(); }

  // Terminal SSH: skrypt modułu nie ma jak zgłosić, że sam się nie wczytał, więc pilnujemy tego stąd.
  var sshPage = document.getElementById("ssh");
  if (sshPage) {
    setTimeout(function () {
      if (sshPage.dataset.ready) { return; }
      var bar = document.getElementById("console-status");
      bar.textContent = "Skrypt terminala się nie wczytał (/static/ssh.js). Odśwież stronę: Ctrl+F5.";
      bar.classList.add("console-bad");
    }, 5000);
  }

  // Uprawnienia integracji: pod polami widać polecenie dla tego, co jest teraz zaznaczone.
  function grantCommands(box) {
    function show() {
      var keys = [].map.call(box.querySelectorAll("input[name=grant]:checked"), function (i) { return i.value; }).join(",");
      [].forEach.call(box.querySelectorAll("[data-command]"), function (el) {
        el.hidden = el.dataset.command !== keys;
      });
    }
    box.addEventListener("change", show);
    show();
  }
  [].forEach.call(document.querySelectorAll("[data-grants]"), grantCommands);

  // Mapa: najechanie (albo fokus z klawiatury) na urządzenie podświetla jego łańcuch zależności, reszta przygasa.
  // Delegacja na document — po odświeżeniu na żywo treść jest podmieniana, a obsługa zostaje.
  function mapFocus(e) {
    var svg = document.querySelector(".map-wrap svg");
    if (!svg) { return; }
    var node = e.target.closest && e.target.closest(".map-wrap [data-rel]");
    [].forEach.call(svg.querySelectorAll(".lit"), function (el) { el.classList.remove("lit"); });
    if (!node || e.type === "mouseout" || e.type === "focusout") { svg.classList.remove("focus"); return; }
    var ids = (node.dataset.rel + " " + node.dataset.id).split(" ");
    var set = {};
    ids.forEach(function (i) { if (i) { set[i] = true; } });
    [].forEach.call(svg.querySelectorAll("[data-id]"), function (el) {
      if (set[el.dataset.id]) { el.classList.add("lit"); }
    });
    [].forEach.call(svg.querySelectorAll("[data-a]"), function (el) {
      if (set[el.dataset.a] && set[el.dataset.b]) { el.classList.add("lit"); }
    });
    svg.classList.add("focus");
  }
  ["mouseover", "mouseout", "focusin", "focusout"].forEach(function (t) { document.addEventListener(t, mapFocus); });

  // Tablet raz na godzinę ładuje stronę od nowa — po wdrożeniu dostaje nowy wygląd.
  if (body.classList.contains("kiosk")) {
    setTimeout(reloadWhenUp, HOUR);
  }

  if (!body.dataset.live) {
    return;
  }
  if (!window.EventSource) {
    // bardzo stara przeglądarka: zwykłe przeładowanie co minutę
    setTimeout(reloadWhenUp, 60 * 1000);
    return;
  }

  var current = body.dataset.live;
  var wanted = current;

  function busy() {
    var a = document.activeElement;
    if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) { return true; }
    if (document.querySelector("details[open]")) { return true; }
    var fields = document.querySelectorAll("input, textarea, select");
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      if (f.type === "checkbox" || f.type === "radio") {
        if (f.checked !== f.defaultChecked) { return true; }
      } else if (f.tagName === "SELECT") {
        for (var j = 0; j < f.options.length; j++) {
          if (f.options[j].selected !== f.options[j].defaultSelected) { return true; }
        }
      } else if (f.type !== "hidden" && f.value !== f.defaultValue) {
        return true;
      }
    }
    return false;
  }

  var loading = false;
  function refresh() {
    if (wanted === current || loading || busy()) { return; }
    loading = true;
    var target = wanted;
    fetch(location.href, { headers: { "X-Denali-Live": "1" }, redirect: "manual", cache: "no-store" })
      .then(function (r) {
        if (r.type === "opaqueredirect" || r.status !== 200) {
          // wylogowano albo strony już nie ma — niech przeglądarka pokaże, co się stało
          location.reload();
          return null;
        }
        return r.text();
      })
      .then(function (html) {
        if (html === null || busy()) { return; }
        var doc = new DOMParser().parseFromString(html, "text/html");
        var y = window.scrollY;
        body.className = doc.body.className;
        body.innerHTML = doc.body.innerHTML;
        current = doc.body.dataset.live || target;
        body.dataset.live = current;
        document.title = doc.title;
        inlineStyles();
        document.dispatchEvent(new Event("denali:odswiezone"));
        window.scrollTo(0, y);
        clock();
      })
      .catch(function () { /* spróbujemy przy następnej okazji */ })
      .then(function () { loading = false; });
  }

  // odłożona podmiana: po skończeniu pisania, zamknięciu menu albo co 10 s
  document.addEventListener("focusout", function () { setTimeout(refresh, 300); });
  document.addEventListener("toggle", function () { setTimeout(refresh, 300); }, true);
  setInterval(refresh, 10 * 1000);

  var es = new EventSource("/na-zywo");
  es.addEventListener("stan", function (e) {
    wanted = e.data;
    refresh();
  });
  es.addEventListener("koniec", function () {
    // sesja wygasła albo została odebrana — przeładowanie pokaże logowanie
    es.close();
    location.reload();
  });
  es.onerror = function () {
    // CONNECTING: przeglądarka sama ponawia. CLOSED: serwer odpowiedział czymś innym niż strumień.
    if (es.readyState === EventSource.CLOSED) { reloadWhenUp(); }
  };
})();

// Przycisk „Kopiuj”: tekst w data-copy trafia do schowka, a napis na chwilę potwierdza
document.addEventListener("click", async (e) => {
  const button = e.target.closest("[data-copy]");
  if (!button) return;
  const target = button.querySelector("[data-copy-label]") || button;   // ikona pulpitu: bez kasowania obrazka
  const label = target.textContent;
  try {
    await navigator.clipboard.writeText(button.dataset.copy);
    target.textContent = "Skopiowano";
  } catch {
    target.textContent = "Nie udało się skopiować";
  }
  setTimeout(() => { target.textContent = label; }, 1500);
});

// Ctrl+K (albo „/” poza polem tekstowym): szybkie przejście do strony, urządzenia, procedury albo incydentu.
(function () {
  "use strict";
  if (document.body.classList.contains("kiosk") || !document.querySelector(".menu")) { return; }
  var box, input, list, items = [], active = 0, timer = null, seq = 0;

  function build() {
    box = document.createElement("div");
    box.className = "palette";
    box.hidden = true;
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Szybkie przejście");
    box.innerHTML = '<div class="palette-panel"><input type="search" class="palette-input" ' +
      'placeholder="Dokąd? Urządzenie, IP, procedura, strona…" aria-label="Szukaj" autocomplete="off">' +
      '<ul class="palette-list" role="listbox"></ul><p class="palette-help">↑↓ wybierz · Enter otwórz · Esc zamknij</p></div>';
    document.body.appendChild(box);
    input = box.querySelector("input");
    list = box.querySelector("ul");
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(load, 120); });
    input.addEventListener("keydown", keys);
    box.addEventListener("click", function (e) {
      var li = e.target.closest("li[data-url]");
      if (li) { location.href = li.dataset.url; } else if (e.target === box) { close(); }
    });
  }

  function load() {
    var mine = ++seq;
    fetch("/szukaj/szybko?q=" + encodeURIComponent(input.value))
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (data) { if (mine === seq) { items = data; active = 0; draw(); } })
      .catch(function () {});
  }

  function draw() {
    list.textContent = "";
    items.forEach(function (it, i) {
      var li = document.createElement("li");
      li.dataset.url = it.url;
      li.setAttribute("role", "option");
      if (i === active) { li.className = "on"; li.setAttribute("aria-selected", "true"); }
      var b = document.createElement("b"); b.textContent = it.label;
      var s = document.createElement("span"); s.textContent = it.sub;
      li.appendChild(b); li.appendChild(s);
      list.appendChild(li);
    });
    var on = list.querySelector(".on");
    if (on) { on.scrollIntoView({ block: "nearest" }); }
  }

  function keys(e) {
    if (e.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); draw(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); draw(); e.preventDefault(); }
    else if (e.key === "Enter" && items[active]) { location.href = items[active].url; e.preventDefault(); }
    else if (e.key === "Escape") { close(); }
  }

  function open() {
    if (!box) { build(); }
    box.hidden = false;
    input.value = "";
    input.focus();
    load();
  }
  function close() { if (box) { box.hidden = true; } }

  document.addEventListener("keydown", function (e) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "");
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); open(); }
    else if (e.key === "/" && !typing) { e.preventDefault(); open(); }
  });
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-palette]")) { e.preventDefault(); open(); }
  });
})();

// Karta urządzenia: zakładki przełączają widok (bez JavaScriptu widać wszystko, a linki przewijają do sekcji).
// Ostatnio wybrana zakładka zostaje zapamiętana w tej przeglądarce.
(function () {
  "use strict";
  var KEY = "denali.zakladka";

  function saved() {
    try { return localStorage.getItem(KEY) || "przeglad"; } catch (e) { return "przeglad"; }
  }

  function show(tab) {
    var nav = document.querySelector(".device-tabs");
    if (!nav) { return; }
    if (!nav.querySelector('[data-tab-link="' + tab + '"]')) { tab = "przeglad"; }
    [].forEach.call(nav.querySelectorAll("[data-tab-link]"), function (a) {
      if (a.dataset.tabLink === tab) { a.setAttribute("aria-current", "page"); } else { a.removeAttribute("aria-current"); }
    });
    [].forEach.call(document.querySelectorAll("[data-tab]"), function (el) {
      el.classList.toggle("tab-off", el.dataset.tab !== tab);
    });
  }

  function fromHash() {
    var el = location.hash && document.getElementById(location.hash.slice(1));
    var sec = el && el.closest("[data-tab]");
    return sec ? sec.dataset.tab : null;
  }

  document.addEventListener("click", function (e) {
    var a = e.target.closest("[data-tab-link]");
    if (!a) { return; }
    e.preventDefault();
    try { localStorage.setItem(KEY, a.dataset.tabLink); } catch (err) { /* prywatne okno */ }
    show(a.dataset.tabLink);
  });
  document.addEventListener("denali:odswiezone", function () { show(saved()); });
  window.addEventListener("hashchange", function () { var t = fromHash(); if (t) { show(t); } });
  show(fromHash() || saved());
})();

// Tablet przy szafie (sciana.html) i odblokowany tablet na każdej stronie:
// - alarm: nowy incydent → wybudzenie ekranu (Fully Kiosk) i krótki dźwięk, poza godzinami ciszy,
// - przyciemnienie: dotknięcie rozświetla na minutę; w godzinach ciszy ekran przygasa,
// - klawiatura PIN,
// - odblokowanie: dotyk przedłuża je na serwerze, a po czasie tablet sam wraca na ścianę.
(function () {
  "use strict";
  var body = document.body;
  var SEEN = "denali.alarmy";
  var audio = null;

  function fully() { return window.fully && typeof window.fully.turnScreenOn === "function" ? window.fully : null; }

  function wall() { return document.querySelector(".wall"); }

  function quietNow(el) {
    var from = parseInt(el.dataset.quietFrom, 10), to = parseInt(el.dataset.quietTo, 10);
    if (isNaN(from) || isNaN(to) || from === to) { return false; }
    var h = new Date().getHours();
    return from < to ? (h >= from && h < to) : (h >= from || h < to);
  }

  // Dźwięk bez pliku: trzy krótkie tony z Web Audio. Przeglądarka pozwala grać dopiero po pierwszym dotknięciu
  // (w Fully Kiosk: Web Content Settings → Autoplay Audio, wtedy od razu).
  function beep() {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === "suspended") { audio.resume(); }
      [0, 0.28, 0.56].forEach(function (t, i) {
        var o = audio.createOscillator(), g = audio.createGain();
        o.type = "sine";
        o.frequency.value = i === 2 ? 660 : 880;
        g.gain.setValueAtTime(0.0001, audio.currentTime + t);
        g.gain.exponentialRampToValueAtTime(0.4, audio.currentTime + t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + t + 0.22);
        o.connect(g); g.connect(audio.destination);
        o.start(audio.currentTime + t); o.stop(audio.currentTime + t + 0.25);
      });
    } catch (e) { /* bez dźwięku */ }
  }

  function seen() {
    try { return (localStorage.getItem(SEEN) || "").split(" ").filter(Boolean); } catch (e) { return []; }
  }

  function checkAlarm() {
    var el = wall();
    if (!el) { return; }
    body.classList.toggle("night", quietNow(el));
    var now = (el.dataset.alarm || "").split(" ").filter(Boolean);
    var old = seen();
    var fresh = now.filter(function (id) { return old.indexOf(id) < 0; });
    try { localStorage.setItem(SEEN, now.join(" ")); } catch (e) { /* prywatne okno */ }
    if (!fresh.length) { return; }
    var f = fully();
    if (f) { f.turnScreenOn(); }
    if (!quietNow(el)) { beep(); }
  }

  var awakeTimer = null;
  function wake() {
    if (!wall()) { return; }
    body.classList.add("awake");
    clearTimeout(awakeTimer);
    awakeTimer = setTimeout(function () { body.classList.remove("awake"); }, 60 * 1000);
  }

  // Odblokowanie: dotyk co najwyżej raz na 30 s mówi serwerowi „ktoś tu jest”; gdy minie czas — z powrotem na ścianę.
  var lastPing = 0;
  function untilEl() { return document.querySelector("[data-unlock-until]"); }
  function activity() {
    if (!untilEl() || Date.now() - lastPing < 30 * 1000) { return; }
    lastPing = Date.now();
    fetch("/tablet/aktywny", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var el = untilEl();
        if (d && d.until && el) { el.dataset.unlockUntil = String(Math.floor(d.until)); }
        if (d && !d.until) { location.href = "/"; }
      })
      .catch(function () {});
  }
  setInterval(function () {
    var el = untilEl();
    if (el && Date.now() / 1000 > parseInt(el.dataset.unlockUntil, 10) + 2) { location.href = "/"; }
  }, 15 * 1000);

  ["pointerdown", "keydown"].forEach(function (t) {
    document.addEventListener(t, function () {
      wake();
      activity();
      if (audio && audio.state === "suspended") { audio.resume(); }
    }, { passive: true });
  });

  document.addEventListener("click", function (e) {
    var k = e.target.closest("[data-key]");
    if (k) {
      var input = document.getElementById("pin");
      if (!input) { return; }
      input.value = k.dataset.key === "⌫" ? input.value.slice(0, -1) : (input.value + k.dataset.key).slice(0, 12);
      return;
    }
    if (e.target.closest("[data-test-sound]")) { beep(); }
  });

  document.addEventListener("denali:odswiezone", checkAlarm);
  checkAlarm();
  setInterval(function () { var el = wall(); if (el) { body.classList.toggle("night", quietNow(el)); } }, 60 * 1000);
})();
