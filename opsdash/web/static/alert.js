/* Карточка «скоро встреча» поверх всех окон (templates/meeting_alert.html). Окно macOS (opsdash/macos_alert.js)
   читает заголовок страницы: «pulse:h:<высота>» — подогнать окно, «pulse:close» — закрыть. Кнопки шлют действие
   в PULSE (POST /alert/<key>/<действие>): там открывается звонок и запоминается ответ. */
(function () {
  "use strict";
  var body = document.body;
  var card = document.querySelector("[data-card]");
  if (!card) return;
  var key = body.dataset.key;
  var start = Number(body.dataset.start);
  var until = Number(body.dataset.until);
  var step = "";
  var leaving = false;
  var n = 0;

  // где карточка и какой высоты окно: окно ловит мышь только над карточкой, поля под тень пропускают щелчки
  function report() {
    // offset*, а не getBoundingClientRect: пока карточка въезжает, сдвиг анимации не должен попасть в размеры
    var x = card.offsetLeft, y = card.offsetTop, w = card.offsetWidth, ch = card.offsetHeight;
    var h = Math.ceil(y + ch + parseFloat(getComputedStyle(body).paddingBottom));
    document.title = "pulse:r:" + [x, y, w, ch, h].join(",") + ":" + (++n);  // счётчик: окно ловит только новый заголовок
  }

  // анимации: как в «Настройки → Основное → Анимации»; не выбирали — решает «уменьшить движение» в системе
  function animated() {
    if (body.classList.contains("motion-off")) return false;
    if (body.classList.contains("motion-normal") || body.classList.contains("motion-fast")) return true;
    return !matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function minutes(ms) { return Math.max(1, Math.round(ms / 60000)); }

  function countdown() {
    var left = start - Date.now();
    var text;
    if (left > 60000) text = "через " + minutes(left) + " мин";
    else if (left > 0) text = "меньше чем через минуту";
    else if (-left < 60000) text = "началась";
    else text = "идёт " + Math.floor(-left / 60000) + " мин";
    document.querySelectorAll("[data-countdown]").forEach(function (el) { el.textContent = text; });
    if (left <= 0 && step === "soon") show("record");
    if (Date.now() > until && step !== "done") leave();
  }

  function show(next) {
    if (next === step) return;
    var h0 = card.offsetHeight;
    document.querySelectorAll("[data-step-box]").forEach(function (box) {
      var on = box.dataset.stepBox === next;
      box.hidden = !on;
      box.classList.toggle("enter", on && step !== "");
    });
    var first = step === "";
    step = next;
    if (!first && animated()) {  // высота карточки — плавно от прежней к новой
      var h1 = card.offsetHeight;
      card.style.height = h0 + "px";
      card.offsetHeight;  // eslint-disable-line no-unused-expressions -- применить высоту до перехода
      card.classList.add("resizing");
      card.style.height = h1 + "px";
      setTimeout(function () { card.classList.remove("resizing"); card.style.height = ""; report(); }, 320);
    }
    report();
    var primary = card.querySelector('[data-step-box="' + next + '"] .btn.primary');
    if (primary && !first) primary.focus({ preventScroll: true });
  }

  function leave() {
    if (leaving) return;
    leaving = true;
    card.classList.add("out");
    setTimeout(function () { document.title = "pulse:close"; }, animated() ? 220 : 0);
  }

  function send(action, btn) {
    if (btn) btn.setAttribute("aria-busy", "true");
    return fetch("/alert/" + encodeURIComponent(key) + "/" + action, { method: "POST" })
      .then(function (r) { return r.ok ? r.json() : { step: "done" }; })
      .catch(function () { return { step: "done" }; })
      .finally(function () { if (btn) btn.removeAttribute("aria-busy"); });
  }

  card.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-act]");
    if (!btn || leaving) return;
    var action = btn.dataset.act;
    if (action === "brief") { send("brief", btn); return; }
    if (action === "join") { send("join", btn).then(function () { show("record"); }); return; }
    if (action === "recording") {
      send("recording", btn);
      show("done");
      setTimeout(leave, 1800);
      return;
    }
    send(action, btn);  // no-record, closed
    leave();
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !leaving) { send("closed"); leave(); }
  });

  show(body.dataset.step || "soon");
  countdown();
  setInterval(countdown, 1000);
  if (window.ResizeObserver) new ResizeObserver(function () { if (!card.classList.contains("resizing")) report(); }).observe(card);
})();
