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
        form.querySelectorAll("[name=minutes]").forEach(function (b) {
          b.setAttribute("aria-pressed", b === chip ? "true" : "false");
        });
        if (note) { note.textContent = res.note; note.classList.remove("bad-text"); }
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

  if (dlg) dlg.addEventListener("close", function () { if (dlg.dataset.changed) location.reload(); });

  // ссылка «/#day» (уведомление «места нет — что двигаем?») сразу открывает «Разобрать день»
  var openDay = function () {
    if (dlg && location.hash === "#day" && !dlg.open) {
      dlg.showModal();
      history.replaceState(null, "", location.pathname + location.search);
    }
  };
  openDay();
  window.addEventListener("hashchange", openDay);

  // перетаскивание задачи на сетке дня: блок едет за курсором с шагом 5 минут; отпустили — задача закреплена на этом
  // времени (решение директора: PULSE её больше не двигает). Щелчок без сдвига — как раньше, открыть задачу
  var hm = function (m) { return ("0" + Math.floor(m / 60)).slice(-2) + ":" + ("0" + m % 60).slice(-2); };
  var minuteAt = function (el) {
    return Number(el.dataset.from) + Math.round(parseFloat(el.style.top) * 60 / Number(el.dataset.px));
  };
  var drag = null;
  document.addEventListener("dragstart", function (e) {  // у ссылки — своё перетаскивание браузера, оно не нужно
    if (e.target.closest && e.target.closest("[data-drag]")) e.preventDefault();
  });
  document.addEventListener("pointerdown", function (e) {
    var el = e.target.closest("[data-drag]");
    if (!el || e.button !== 0) return;
    drag = { el: el, y: e.clientY, top: parseFloat(el.style.top), moved: false };
  });
  document.addEventListener("pointermove", function (e) {
    if (!drag) return;
    var dy = e.clientY - drag.y;
    if (!drag.moved && Math.abs(dy) < 5) return;
    drag.moved = true;
    drag.el.classList.add("dragging");
    var step = Number(drag.el.dataset.px) / 12;  // 5 минут
    var col = drag.el.parentNode;
    var top = Math.round((drag.top + dy) / step) * step;
    drag.el.style.top = Math.max(0, Math.min(top, col.offsetHeight - drag.el.offsetHeight)) + "px";
    var meta = drag.el.querySelector(".pb-t");
    if (meta) meta.dataset.at = hm(minuteAt(drag.el));
    e.preventDefault();
  });
  document.addEventListener("pointerup", function () {
    if (!drag) return;
    var d = drag;
    drag = null;
    if (!d.moved) return;
    d.el.addEventListener("click", function (ev) { ev.preventDefault(); }, { once: true });  // это был не щелчок
    var m = Math.round(minuteAt(d.el) / 5) * 5;
    var body = new FormData();
    body.set("cid", d.el.dataset.drag);
    body.set("act", "move");
    body.set("start", hm(m));
    fetch("/day/act", { method: "POST", body: body, headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (res) { if (!res.ok) throw new Error(res.err); location.reload(); })
      .catch(function () { d.el.style.top = d.top + "px"; d.el.classList.remove("dragging"); });
  });
})();
