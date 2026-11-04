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
})();
