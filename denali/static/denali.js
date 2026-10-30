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
