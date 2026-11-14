// «Реальность дня» (opsdash.day): оценка задачи в один клик и решения в окне «Разобрать день» — без перезагрузки.
// Без скриптов те же формы отправляются обычным образом и возвращают на страницу
(function () {
  "use strict";

  var send = function (form, submitter) {
    var body = new FormData(form);
    if (submitter && submitter.name) body.set(submitter.name, submitter.value);
    return fetch(form.action, { method: "POST", body: body, headers: { Accept: "application/json" } })
      .then(function (r) { return r.json().catch(function () { return { ok: false, err: "" }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.err || "");
        return res;
      });
  };
  var problem = function (x) {  // ответ PULSE — его словами; обрыв связи — своими
    return (x && x.name === "Error" && x.message) || "Не получилось сохранить — попробуйте ещё раз";
  };

  // окно «Разобрать день»: каждое решение сохраняется сразу, тело окна приходит заново — итог и окна пересчитаны.
  // Закрыли окно после изменений — Сводка обновляется: вердикт, план на сетке, лента
  var dlg = document.querySelector("[data-day-dlg]");
  var dayBody = dlg && dlg.querySelector("[data-day-body]");

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.matches("[data-day-act]") && dayBody && dayBody.contains(form)) {
      e.preventDefault();
      var buttons = form.querySelectorAll("button, select");
      buttons.forEach(function (b) { b.disabled = true; });
      send(form, e.submitter).then(function (res) {
        var keep = dayBody.scrollTop;
        dayBody.innerHTML = res.html;
        dayBody.scrollTop = keep;
        dlg.dataset.changed = "1";
      }).catch(function (x) {
        var err = dayBody.querySelector("[data-day-err]");
        if (err) { err.textContent = problem(x); err.hidden = false; }
        buttons.forEach(function (b) { b.disabled = false; });
      });
    } else if (form.matches("[data-est]")) {
      // оценка в окне задачи: подсветить выбранный чип и подпись «ваша оценка»
      e.preventDefault();
      var chip = e.submitter;
      var note = form.parentNode.querySelector("[data-est-note]");
      send(form, chip).then(function (res) {
        // в окне задачи чипы есть и в строке «Займёт у вас», и в «Мяч снова у вас» — подсветить в обоих
        var scope = form.closest("dialog") || document;
        scope.querySelectorAll('form[data-est][action="' + form.getAttribute("action") + '"]').forEach(function (f) {
          f.querySelectorAll("[name=minutes]").forEach(function (b) {
            b.setAttribute("aria-pressed", b.value === chip.value ? "true" : "false");
          });
        });
        note = note || scope.querySelector("[data-est-note]");
        if (note) { note.textContent = res.note; note.classList.remove("bad-text"); }
        var ball = form.closest("[data-ball]");  // «что осталось вам?» — ответ получен
        if (ball) ball.textContent = "Записано: осталось ≈" + chip.title + " — PULSE поставит это в план.";
      }).catch(function (x) {
        if (note) { note.textContent = problem(x); note.classList.add("bad-text"); }
      });
    }
  });

  document.addEventListener("click", function (e) {
    // «Свой срок»: быстрая кнопка сразу сохраняет дату
    var quick = e.target.closest("[data-day-due]");
    if (quick) {
      var form = quick.closest("form");
      form.querySelector("input[name=due]").value = quick.dataset.dayDue;
      form.requestSubmit();
      return;
    }
    // блок «Ответить» на сетке дня: показать в ленте только ответы
    var jump = e.target.closest("[data-feed-jump]");
    if (jump) {
      e.preventDefault();
      var chip = document.querySelector('[data-feed-filter="' + jump.dataset.feedJump + '"]');
      if (chip) { chip.click(); chip.scrollIntoView({ block: "center", behavior: "smooth" }); }
    }
  });

  if (dlg) dlg.addEventListener("close", function () { if (dlg.dataset.changed) pulseReload(); });

  // ссылка «/#day» (уведомление «места нет — что двигаем?») сразу открывает «Разобрать день»
  var openDay = function () {
    if (dlg && location.hash === "#day" && !dlg.open) {
      dlg.showModal();
      history.replaceState(null, "", location.pathname + location.search);
    }
  };
  openDay();
  window.addEventListener("hashchange", openDay);

  // перетаскивание задачи на сетке дня и в Календаре: блок едет за курсором с шагом 5 минут, в неделе — и в другой
  // день; отпустили — задача закреплена на этом времени (решение директора: PULSE её больше не двигает). Пачка мелочей
  // едет целиком. Нижний край блока — «сколько займёт»: тянем с шагом 15 минут. Щелчок без сдвига — меню блока
  var hm = function (m) { return ("0" + Math.floor(m / 60)).slice(-2) + ":" + ("0" + m % 60).slice(-2); };
  var minuteAt = function (el) {
    return Number(el.dataset.from) + Math.round(parseFloat(el.style.top) * 60 / Number(el.dataset.px));
  };
  var post = function (fields) {
    var body = new FormData();
    Object.keys(fields).forEach(function (k) { body.set(k, fields[k]); });
    return fetch("/day/act", { method: "POST", body: body, headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (res) { if (!res.ok) throw new Error(res.err); pulseReload(); });
  };
  var columnAt = function (x, y) {  // колонка дня недели под курсором (кроме прошедших дней)
    var hit = document.elementsFromPoint(x, y).filter(function (n) { return n.classList && n.classList.contains("wk-col"); })[0];
    return hit && !hit.hasAttribute("data-past") ? hit : null;
  };
  var drag = null;
  document.addEventListener("dragstart", function (e) {  // у ссылки — своё перетаскивание браузера, оно не нужно
    if (e.target.closest && e.target.closest("[data-drag]")) e.preventDefault();
  });
  document.addEventListener("pointerdown", function (e) {
    var el = e.target.closest("[data-drag]");
    if (!el || e.button !== 0) return;
    drag = { el: el, x: e.clientX, y: e.clientY, top: parseFloat(el.style.top), height: el.offsetHeight,
             col: el.parentNode, resize: !!e.target.closest("[data-resize]"), moved: false };
  });
  document.addEventListener("pointermove", function (e) {
    if (!drag) return;
    var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.abs(dy) < 5 && Math.abs(dx) < 12) return;
    drag.moved = true;
    var el = drag.el, px = Number(el.dataset.px), meta = el.querySelector(".pb-t");
    el.classList.add("dragging");
    if (drag.resize) {  // нижний край: сколько займёт, шаг 15 минут, не меньше 15
      var q = px / 4, h = Math.max(q, Math.round((drag.height + 2 + dy) / q) * q);
      el.style.height = (h - 2) + "px";
      if (meta) meta.dataset.at = "≈" + Math.round(h / px * 60) + " мин";
    } else {
      var col = columnAt(e.clientX, e.clientY);
      if (col && col !== el.parentNode && el.dataset.day) { col.appendChild(el); el.dataset.day = col.dataset.day; }
      var step = px / 12;  // 5 минут
      var top = Math.round((drag.top + dy) / step) * step;
      el.style.top = Math.max(0, Math.min(top, el.parentNode.offsetHeight - el.offsetHeight)) + "px";
      if (meta) meta.dataset.at = hm(minuteAt(el));
    }
    e.preventDefault();
  });
  document.addEventListener("pointerup", function () {
    if (!drag) return;
    var d = drag, el = d.el;
    drag = null;
    if (!d.moved) return;
    el.dataset.dragged = "1";  // это был не щелчок — меню не открываем
    var undo = function () {
      if (el.parentNode !== d.col) d.col.appendChild(el);
      el.style.top = d.top + "px";
      el.style.height = (d.height) + "px";
      el.classList.remove("dragging");
    };
    var ids = el.dataset.drag;
    if (d.resize) {
      var minutes = Math.max(15, Math.round((el.offsetHeight + 2) / Number(el.dataset.px) * 4) * 15);
      post({ cid: ids, act: "estimate", minutes: minutes }).catch(undo);
      return;
    }
    var fields = { cid: ids.split(",")[0], act: "move", start: hm(Math.round(minuteAt(el) / 5) * 5) };
    if (ids.indexOf(",") > 0) fields.cids = ids;
    if (el.dataset.day) fields.day = el.dataset.day;
    post(fields).catch(undo);
  });

  // меню блока задачи по щелчку — в том же окошке, что и карточка встречи: готово, «моя часть готова — жду…»,
  // не в этот день, сколько займёт. С Ctrl/Cmd — как раньше, открыть задачу
  var pop = document.querySelector("[data-ev-pop]");
  document.querySelectorAll(".pb[data-pb]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      if (el.dataset.dragged) { delete el.dataset.dragged; e.preventDefault(); e.stopPropagation(); return; }
      var tpl = document.getElementById("pbm-" + el.dataset.pb);
      if (!pop || !tpl || e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      e.stopPropagation();  // иначе общий обработчик сразу закроет окошко
      pop.innerHTML = tpl.innerHTML;
      pop.hidden = false;
      var r = el.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
      var left = r.right + 8 + w > window.innerWidth ? r.left - w - 8 : r.right + 8;
      pop.style.left = Math.max(12, Math.min(left, window.innerWidth - w - 12)) + "px";
      pop.style.top = Math.max(12, Math.min(r.top, window.innerHeight - h - 12)) + "px";
    });
  });
})();
