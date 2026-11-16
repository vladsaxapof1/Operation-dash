// Календарь для полей даты в новом дизайне. Окошко браузера у <input type="date"> стилями не меняется, поэтому
// по щелчку на поле открываем своё: месяц сеткой, неделя с понедельника, внизу «Сегодня», «Завтра» и «Очистить»
// (у необязательного поля). Само поле остаётся обычным <input type="date">: формы отправляют то же YYYY-MM-DD,
// дату можно и впечатать. При переходе без перезагрузки nav.js снимает все обработчики на document и window и
// запускает скрипт заново — поэтому подключаемся при каждом запуске, без флага «уже подключён» (с флагом календарь
// переставал открываться после первой же смены экрана или сохранения формы).
(function () {
  "use strict";

  var MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
    "ноябрь", "декабрь"];
  var MONTHS_OF = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
    "ноября", "декабря"];
  var DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

  var pop = null, input = null, view = null, focusIso = null;

  var pad = function (n) { return (n < 10 ? "0" : "") + n; };
  var iso = function (d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); };
  var parse = function (s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
    return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
  };
  var addDays = function (d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); };
  var today = function () { var d = new Date(); return new Date(d.getFullYear(), d.getMonth(), d.getDate()); };
  var on = function () { return document.body && document.body.classList.contains("d3"); };
  var usable = function (el) {
    return el && el.matches && el.matches('input[type="date"]') && !el.disabled && !el.readOnly && on();
  };
  var allowed = function (s) {
    return !!s && !(input.min && s < input.min) && !(input.max && s > input.max);
  };

  function build() {
    pop = document.createElement("div");
    pop.className = "dp";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "Выбор даты");
    if ("popover" in pop) pop.setAttribute("popover", "manual");
    pop.addEventListener("mousedown", function (e) { e.preventDefault(); }); // фокус остаётся в поле
    pop.addEventListener("click", onClick);
    pop.addEventListener("keydown", onKey);
  }

  function render() {
    var t = iso(today()), tomorrow = iso(addDays(today(), 1)), val = input.value;
    var first = new Date(view.getFullYear(), view.getMonth(), 1);
    var start = addDays(first, -((first.getDay() + 6) % 7));
    var html = '<div class="dp-head"><button type="button" class="dp-nav" data-dp-month="-1" aria-label="Прошлый месяц">' +
      '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M10 3 5 8l5 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></button>' +
      '<b class="dp-title" aria-live="polite">' + MONTHS[view.getMonth()] + " " + view.getFullYear() + "</b>" +
      '<button type="button" class="dp-nav" data-dp-month="1" aria-label="Следующий месяц">' +
      '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 3 5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></button></div>' +
      '<div class="dp-grid" role="grid">';
    DAYS.forEach(function (d, i) { html += '<span class="dp-wd' + (i > 4 ? " we" : "") + '">' + d + "</span>"; });
    if (!focusIso || parse(focusIso).getMonth() !== view.getMonth()) {
      focusIso = val && parse(val) && parse(val).getMonth() === view.getMonth() && parse(val).getFullYear() === view.getFullYear()
        ? val : (t.slice(0, 7) === iso(first).slice(0, 7) ? t : iso(first));
    }
    for (var i = 0; i < 42; i++) {
      var d = addDays(start, i), s = iso(d), cls = "dp-d";
      if (d.getMonth() !== view.getMonth()) cls += " out";
      if (s === t) cls += " today";
      if (s === val) cls += " sel";
      if (i % 7 > 4) cls += " we";
      html += '<button type="button" class="' + cls + '" data-dp-day="' + s + '"' + (allowed(s) ? "" : " disabled") +
        ' tabindex="' + (s === focusIso ? 0 : -1) + '" aria-label="' + d.getDate() + " " + MONTHS_OF[d.getMonth()] + " " +
        d.getFullYear() + '"' + (s === val ? ' aria-selected="true"' : "") + ">" + d.getDate() + "</button>";
      if (i === 34 && addDays(start, 35).getMonth() !== view.getMonth()) break; // пятая неделя — последняя
    }
    html += '</div><div class="dp-foot">' +
      '<button type="button" class="dp-q' + (val === t ? " on" : "") + '" data-dp-day="' + t + '"' + (allowed(t) ? "" : " disabled") + ">Сегодня</button>" +
      '<button type="button" class="dp-q' + (val === tomorrow ? " on" : "") + '" data-dp-day="' + tomorrow + '"' + (allowed(tomorrow) ? "" : " disabled") + ">Завтра</button>" +
      (input.required ? "" : '<button type="button" class="dp-q dp-clear" data-dp-clear' + (val ? "" : " disabled") + ">Очистить</button>") +
      "</div>";
    pop.innerHTML = html;
  }

  function place() {
    if (!pop || !input) return;
    var r = input.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight, gap = 6;
    var top = r.bottom + gap;
    if (top + h > innerHeight - 8 && r.top - gap - h > 8) top = r.top - gap - h;
    var left = Math.min(Math.max(8, r.left), innerWidth - w - 8);
    pop.style.top = Math.round(Math.max(8, top)) + "px";
    pop.style.left = Math.round(left) + "px";
  }

  function open(el) {
    if (!pop) build();
    if (input === el && pop.isConnected) return;
    close(false);
    input = el;
    view = parse(el.value) || today();
    view = new Date(view.getFullYear(), view.getMonth(), 1);
    focusIso = null;
    // внутри открытого окна (<dialog>) всё снаружи недоступно — календарь кладём в то же окно
    (el.closest("dialog[open]") || document.body).appendChild(pop);
    render();
    if (pop.showPopover) { try { pop.showPopover(); } catch (e) { /* уже открыт */ } }
    pop.classList.add("open");
    place();
    el.setAttribute("aria-expanded", "true");
  }

  function close(refocus) {
    if (!pop || !pop.isConnected) return;
    if (pop.hidePopover) { try { pop.hidePopover(); } catch (e) { /* уже закрыт */ } }
    pop.classList.remove("open");
    pop.remove();
    if (input) {
      input.removeAttribute("aria-expanded");
      if (refocus) input.focus();
    }
    input = null;
  }

  function pick(value) {
    var el = input;
    el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    close(true);
  }

  function month(step) {
    view = new Date(view.getFullYear(), view.getMonth() + step, 1);
    focusIso = null;
    render();
    place();
  }

  function onClick(e) {
    var b = e.target.closest("button");
    if (!b || b.disabled) return;
    if (b.hasAttribute("data-dp-month")) month(+b.getAttribute("data-dp-month"));
    else if (b.hasAttribute("data-dp-day")) pick(b.getAttribute("data-dp-day"));
    else if (b.hasAttribute("data-dp-clear")) pick("");
  }

  function moveFocus(days) {
    var d = addDays(parse(focusIso), days);
    focusIso = iso(d);
    if (d.getMonth() !== view.getMonth() || d.getFullYear() !== view.getFullYear()) {
      view = new Date(d.getFullYear(), d.getMonth(), 1);
    }
    render();
    place();
    var b = pop.querySelector('[data-dp-day="' + focusIso + '"].dp-d');
    if (b) b.focus();
  }

  function onKey(e) {
    var k = e.key;
    if (k === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); return; }
    if (!e.target.classList.contains("dp-d")) return;
    var step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[k];
    if (step) { e.preventDefault(); moveFocus(step); return; }
    if (k === "PageUp" || k === "PageDown") { e.preventDefault(); month(k === "PageUp" ? -1 : 1); }
  }

  // щелчок по полю — открыть; щелчок мимо — закрыть
  document.addEventListener("click", function (e) {
    if (usable(e.target)) { open(e.target); return; }
    if (pop && pop.isConnected && !pop.contains(e.target)) close(false);
  }, true);
  document.addEventListener("keydown", function (e) {
    var el = e.target;
    if (pop && pop.isConnected && el === input) {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(false); return; }
      if (e.key === "ArrowDown" && e.altKey) { // с клавиатуры — в сетку дней
        e.preventDefault();
        var b = pop.querySelector('.dp-d[tabindex="0"]');
        if (b) b.focus();
        return;
      }
      if (e.key === "Tab") close(false);
    } else if (usable(el) && e.key === "ArrowDown" && e.altKey) {
      e.preventDefault();
      open(el);
      var f = pop.querySelector('.dp-d[tabindex="0"]');
      if (f) f.focus();
    }
  }, true);
  // впечатали дату руками — календарь показывает её
  document.addEventListener("input", function (e) {
    if (pop && pop.isConnected && e.target === input && parse(input.value)) {
      var d = parse(input.value);
      view = new Date(d.getFullYear(), d.getMonth(), 1);
      focusIso = input.value;
      render();
      place();
    }
  });
  addEventListener("resize", function () { close(false); });
  addEventListener("scroll", function (e) {
    if (pop && pop.isConnected && !pop.contains(e.target)) place();
  }, true);
  // переход на другой экран без перезагрузки — календарь закрываем
  addEventListener("popstate", function () { close(false); });
  document.addEventListener("pulse:page", function () { close(false); });
})();
