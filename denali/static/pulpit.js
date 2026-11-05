// Tryb edycji pulpitu (/?edycja=1): przeciąganie, klawiatura, nazwy, usuwanie z cofnięciem, okno „Dodaj”.
// Każda zmiana zapisuje się od razu. Historię „Cofnij” trzyma sessionStorage, bo dodanie czegoś przeładowuje stronę.
(function () {
  "use strict";
  var desk = document.getElementById("desk");
  if (!desk) { return; }
  var KEY = "denali.pulpit.cofnij";

  function post(path, data) {
    return fetch("/pulpit/api/" + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": desk.dataset.csrf },
      body: JSON.stringify(data)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) { throw new Error(d.blad || "Nie udało się zapisać."); }
        return d;
      });
    });
  }

  // ───── toast ─────
  var toast = desk.querySelector("[data-toast]");
  var toastText = desk.querySelector("[data-toast-text]");
  var toastUndo = desk.querySelector("[data-toast-undo]");
  var toastTimer = null;

  function say(text, canUndo) {
    toastText.textContent = text;
    toastUndo.hidden = !canUndo;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, canUndo ? 10000 : 5000);
  }
  function fail(e) { say(e.message || "Nie udało się zapisać.", false); }

  // ───── historia ─────
  function history() {
    try { return JSON.parse(sessionStorage.getItem(KEY) || "[]"); } catch (e) { return []; }
  }
  function remember(list) {
    try { sessionStorage.setItem(KEY, JSON.stringify(list.slice(-30))); } catch (e) { /* prywatne okno */ }
    undoButton.disabled = !list.length;
  }
  var undoButton = desk.querySelector("[data-undo]");
  undoButton.disabled = !history().length;

  function push(step) { var h = history(); h.push(step); remember(h); }

  function undo() {
    var h = history();
    var step = h.pop();
    if (!step) { return; }
    remember(h);
    var done;
    if (step.type === "uklad") { done = post("uklad", step.before); }
    else if (step.type === "nazwa") { done = post("nazwa", { id: step.id, name: step.name }); }
    else if (step.type === "rozmiar") { done = post("widzet", { id: step.id, size: step.size }); }
    else { done = post("cofnij", { token: step.token }); }
    done.then(function () { location.reload(); }).catch(fail);
  }
  undoButton.addEventListener("click", undo);
  toastUndo.addEventListener("click", function () { toast.hidden = true; undo(); });

  // ───── układ ─────
  function ids(box, attr) {
    return [].map.call(box.querySelectorAll(":scope > [" + attr + "]"), function (el) { return Number(el.getAttribute(attr)); });
  }
  function layout() {
    var tiles = {};
    [].forEach.call(desk.querySelectorAll("[data-tiles]"), function (box) { tiles[box.dataset.tiles] = ids(box, "data-tile"); });
    var sections = [].map.call(desk.querySelectorAll("[data-sections] > [data-section]"), function (el) {
      return el.dataset.section === "statystyki" ? "statystyki" : Number(el.dataset.section);
    });
    return { sections: sections, tiles: tiles, widgets: ids(desk.querySelector("[data-widgets]"), "data-widget") };
  }
  var current = JSON.parse(desk.dataset.layout || "{}");

  function saveLayout() {
    var next = layout();
    if (JSON.stringify(next) === JSON.stringify(current)) { return; }
    var before = current;
    current = next;
    post("uklad", next).then(function () { push({ type: "uklad", before: before }); }).catch(function (e) { fail(e); current = before; });
  }

  function saveSide() {
    var side = [].map.call(desk.querySelectorAll("[data-side] > [data-key]"), function (el) {
      return { key: el.dataset.key, on: el.querySelector("[data-side-on]").checked };
    });
    post("wyglad", { side: side }).catch(fail);
  }

  function sortable(box, group, handle, done) {
    window.Sortable.create(box, {
      group: group, handle: handle, animation: 150, ghostClass: "drop-here", chosenClass: "dragging",
      fallbackOnBody: true, emptyInsertThreshold: 24, onEnd: done
    });
  }
  [].forEach.call(desk.querySelectorAll("[data-tiles]"), function (box) { sortable(box, "kafelki", ".grip", saveLayout); });
  sortable(desk.querySelector("[data-widgets]"), "widzety", ".grip", saveLayout);
  sortable(desk.querySelector("[data-sections]"), "sekcje", ".section-head > .grip", saveLayout);
  sortable(desk.querySelector("[data-side]"), "panel", ".grip", saveSide);

  // Klawiatura: strzałki na uchwycie robią to samo, co przeciąganie. W kategoriach ↑↓ działa też między nimi.
  desk.addEventListener("keydown", function (e) {
    var grip = e.target.closest && e.target.closest(".grip");
    if (!grip || ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].indexOf(e.key) < 0) { return; }
    var item = grip.closest("[data-tile], [data-widget], [data-section], [data-key]");
    var back = e.key === "ArrowUp" || e.key === "ArrowLeft";
    var sibling = back ? item.previousElementSibling : item.nextElementSibling;
    e.preventDefault();
    if (sibling) {
      item.parentNode.insertBefore(item, back ? sibling : sibling.nextElementSibling);
    } else if (item.hasAttribute("data-tile")) {
      var all = [].slice.call(desk.querySelectorAll("[data-tiles]"));
      var box = all[all.indexOf(item.parentNode) + (back ? -1 : 1)];
      if (!box) { return; }
      box.insertBefore(item, back ? null : box.firstElementChild);
    } else { return; }
    grip.focus();
    if (item.hasAttribute("data-key")) { saveSide(); } else { saveLayout(); }
  });

  // ───── nazwa kategorii ─────
  desk.addEventListener("focusin", function (e) {
    if (e.target.dataset && e.target.dataset.rename) { e.target.dataset.was = e.target.value; }
  });
  desk.addEventListener("focusout", function (e) {
    var input = e.target;
    if (!input.dataset || !input.dataset.rename) { return; }
    var name = input.value.trim(), was = input.dataset.was;
    if (name === was) { return; }
    if (!name) { input.value = was; return; }
    post("nazwa", { id: Number(input.dataset.rename), name: name })
      .then(function () { input.value = name; push({ type: "nazwa", id: Number(input.dataset.rename), name: was }); })
      .catch(function (e) { input.value = was; fail(e); });
  });
  desk.addEventListener("keydown", function (e) {
    if (!e.target.dataset || !e.target.dataset.rename) { return; }
    if (e.key === "Enter") { e.preventDefault(); e.target.blur(); }
    if (e.key === "Escape") { e.target.value = e.target.dataset.was; e.target.blur(); }
  });

  // ───── usuwanie i rozmiar ─────
  desk.addEventListener("click", function (e) {
    var remove = e.target.closest("[data-remove]");
    if (remove) {
      var kind = remove.dataset.remove;
      post("usun", { kind: kind, id: Number(remove.dataset.id) }).then(function (d) {
        var el = remove.closest(kind === "ikona" ? "[data-tile]" : kind === "widzet" ? "[data-widget]" : "[data-section]");
        el.parentNode.removeChild(el);
        current = layout();
        push({ type: "kosz", token: d.token });
        say("Usunięto: " + d.name, true);
      }).catch(fail);
      return;
    }
    var size = e.target.closest("[data-size]");
    if (size) {
      var card = size.closest("[data-widget]");
      var wanted = Number(size.dataset.size);
      var was = card.classList.contains("wide") ? 2 : 1;
      if (wanted === was) { return; }
      post("widzet", { id: Number(card.dataset.widget), size: wanted }).then(function () {
        card.classList.toggle("wide", wanted === 2);
        [].forEach.call(card.querySelectorAll("[data-size]"), function (b) {
          b.setAttribute("aria-pressed", String(Number(b.dataset.size) === wanted));
        });
        push({ type: "rozmiar", id: Number(card.dataset.widget), size: was });
      }).catch(fail);
    }
  });

  // ───── wygląd i panel boczny ─────
  desk.addEventListener("change", function (e) {
    if (e.target.dataset.look) {
      var data = {};
      data[e.target.dataset.look] = e.target.checked;
      post("wyglad", data).catch(fail);
    } else if ("sideOn" in e.target.dataset) { saveSide(); }
  });

  // ───── okno „Dodaj do pulpitu” ─────
  var dialog = document.getElementById("dodaj");
  var search = dialog.querySelector("[data-add-search]");
  var picked = "";

  function filter() {
    var q = search.value.trim().toLowerCase();
    var counts = {}, shown = 0;
    [].forEach.call(dialog.querySelectorAll("[data-item]"), function (el) {
      var hit = el.dataset.q.indexOf(q) >= 0;
      if (hit) { counts[el.dataset.itemTab] = (counts[el.dataset.itemTab] || 0) + 1; counts[""] = (counts[""] || 0) + 1; }
      var on = hit && (!picked || el.dataset.itemTab === picked);
      el.hidden = !on;
      if (on) { shown += 1; }
    });
    [].forEach.call(dialog.querySelectorAll("[data-group]"), function (h) {
      h.hidden = !dialog.querySelector('[data-item-tab="' + h.dataset.group + '"]:not([hidden])');
    });
    [].forEach.call(dialog.querySelectorAll("[data-add-tab]"), function (b) {
      b.setAttribute("aria-selected", String(b.dataset.addTab === picked));
      b.querySelector("[data-add-tabcount]").textContent = counts[b.dataset.addTab] || 0;
    });
    dialog.querySelector("[data-add-count]").textContent = q ? shown + " wyników" : "";
    dialog.querySelector("[data-add-none]").hidden = shown > 0;
  }

  function openAdd(category, tab) {
    var select = dialog.querySelector("[data-add-category]");
    if (category && select.querySelector('option[value="' + category + '"]')) { select.value = category; }
    picked = tab || "";
    search.value = "";
    filter();
    dialog.showModal();
    search.focus();
  }

  function syncOwn() {
    dialog.querySelector("[data-add-own]").href = "/pulpit/ikony/nowa?kategoria=" + dialog.querySelector("[data-add-category]").value;
  }
  dialog.querySelector("[data-add-category]").addEventListener("change", syncOwn);
  syncOwn();

  desk.addEventListener("click", function (e) {
    var add = e.target.closest("[data-add]");
    if (add) { openAdd(add.dataset.category, add.dataset.tab); }
    if (e.target.closest("[data-close]")) { dialog.close(); }
    var tab = e.target.closest("[data-add-tab]");
    if (tab) { picked = tab.dataset.addTab; filter(); }
    if (e.target === dialog) { dialog.close(); }
  });
  search.addEventListener("input", filter);
  // Enter w wyszukiwarce dodaje pierwszy wynik, a nie pusty formularz.
  search.addEventListener("keydown", function (e) {
    // Esc w polu „search” tylko by je czyścił: okno ma się zamknąć od razu, jak w podpowiedzi na dole.
    if (e.key === "Escape") { e.preventDefault(); dialog.close(); return; }
    if (e.key !== "Enter") { return; }
    e.preventDefault();
    var first = dialog.querySelector("[data-item]:not([hidden]) button[name=key]");
    if (first) { first.click(); }
  });

  if (desk.dataset.openAdd !== undefined) {
    var zero = desk.dataset.openAdd === "0";
    openAdd(zero ? "" : desk.dataset.openAdd, zero ? "statystyki" : "");
  }

  // Nowa kategoria: od razu w polu nazwy. Kotwica nie wskazuje elementu, bo przejście do takiej zdjęłoby fokus z pola.
  var fresh = location.hash.match(/^#nowa-(\d+)$/);
  if (fresh) {
    var name = desk.querySelector('[data-rename="' + fresh[1] + '"]');
    if (name) { name.focus(); name.select(); name.scrollIntoView({ block: "center" }); }
  }
})();
