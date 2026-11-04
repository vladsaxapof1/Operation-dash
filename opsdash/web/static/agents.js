// «Поручить агенту» (opsdash.agents): окно поверх окна задачи — панель подгружается кусочком страницы
// (/agents?task=<id>&part=1), формы уходят фоном и возвращают её же. Пока Claude собирает промт — шаги по ходу.
// Галочка у файла убирает его строку из промта и возвращает обратно; правка промта сохраняется сама.
// Без скриптов кнопка ведёт на страницу /agents?task=<id> с обычными формами.
(function () {
  "use strict";

  var HEADERS = { "X-Agent-Panel": "1" };
  var poll = null;

  function dialog() {
    var dlg = document.getElementById("agent-dlg");
    if (!dlg) {
      dlg = document.createElement("dialog");
      dlg.className = "dlg dlg-agent";
      dlg.id = "agent-dlg";
      dlg.setAttribute("aria-label", "Поручить агенту");
      dlg.innerHTML = '<div class="dlg-box" tabindex="-1"></div>';
      dlg.addEventListener("close", function () { stopPoll(); });
      document.body.appendChild(dlg);
    }
    return dlg;
  }

  function stopPoll() { if (poll) { clearInterval(poll); poll = null; } }

  // кусочек панели — на место прежнего (в окне или на странице); идёт сборка — следим за шагами
  function show(box, html) {
    stopPoll();
    box.innerHTML = html;
    var panel = box.querySelector("[data-agent-panel]");
    if (panel && panel.dataset.agentLive) watch(box, panel);
  }

  function load(box, task) {
    return fetch("/agents?task=" + encodeURIComponent(task) + "&part=1", { cache: "no-store" })
      .then(function (r) { return r.text(); })
      .then(function (html) { show(box, html); });
  }

  function watch(box, panel) {
    var steps = panel.querySelector("[data-agent-steps]");
    poll = setInterval(function () {
      fetch("/api/agents/" + panel.dataset.agentLive + "/live", { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.status !== "running") { load(box, panel.dataset.task); return; }
          if (!steps) return;
          steps.innerHTML = "";
          (res.steps || []).forEach(function (s) {
            var d = document.createElement("div");
            d.textContent = s;
            steps.appendChild(d);
          });
        })
        .catch(function () { /* PULSE перезапускается — спросим ещё раз */ });
    }, 1500);
  }

  function boxOf(el) {
    var dlg = el.closest("#agent-dlg");
    return dlg ? dlg.querySelector(".dlg-box") : el.closest(".agent-page");
  }

  // «Поручить агенту» в окне задачи
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-agent-open]");
    if (!btn || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    var dlg = dialog();
    var box = dlg.querySelector(".dlg-box");
    box.innerHTML = '<p class="muted">Загружаю…</p>';
    if (!dlg.open) dlg.showModal();
    load(box, btn.dataset.agentOpen).then(function () { box.focus(); });
  });

  // формы панели — фоном: собрать, пересобрать, остановить. «Пересобрать» с «Что поправить» берёт папку и «Кто»
  // из верхней строки; несохранённая правка промта сначала сохраняется — Claude начнёт с неё
  document.addEventListener("submit", function (e) {
    var form = e.target.closest("[data-agent-form]");
    var box = form && boxOf(form);
    if (!box) return;
    e.preventDefault();
    var panel = form.closest("[data-agent-panel]");
    var data = new FormData(form);
    var where = panel.querySelector(".agent-where");
    if (where && where !== form) {
      var w = new FormData(where);
      ["folder", "engine"].forEach(function (k) { if (w.has(k)) data.set(k, w.get(k)); });
    }
    var btn = form.querySelector("button");
    if (btn) btn.disabled = true;
    save(panel, true).then(function () {
      return fetch(form.action, { method: "POST", body: data, headers: HEADERS });
    }).then(function (r) { return r.text(); })
      .then(function (html) { show(box, html); })
      .catch(function () { if (btn) btn.disabled = false; });
  });

  // «Кто»: Codex — вместо «Открыть в Claude» подсказка скопировать промт
  document.addEventListener("change", function (e) {
    if (!e.target.matches("[data-agent-panel] input[name=engine]")) return;
    var panel = e.target.closest("[data-agent-panel]");
    var codex = e.target.value === "codex";
    panel.querySelectorAll("[data-agent-launch]").forEach(function (b) { b.hidden = (b.dataset.agentLaunch === "codex") !== codex; });
  });

  function said(panel, text, bad) {
    var el = panel.querySelector("[data-agent-said]");
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("bad-text", !!bad);
  }

  var saveTimer = null;
  function save(panel, quiet) {
    var form = panel.querySelector("[data-agent-prompt]");
    if (!form) return Promise.resolve(true);
    clearTimeout(saveTimer);
    var body = new FormData();
    body.append("prompt", form.querySelector("[data-agent-text]").value);
    return fetch("/agents/" + form.dataset.agentPrompt + "/save", { method: "POST", body: body, headers: HEADERS })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) said(panel, res.error || "Не сохранилось", true);
        else if (!quiet) said(panel, "Сохранено");
        return res.ok;
      })
      .catch(function () { said(panel, "Не сохранилось — PULSE не отвечает", true); return false; });
  }

  // правка промта сохраняется сама, через секунду после последней буквы
  document.addEventListener("input", function (e) {
    if (!e.target.matches("[data-agent-text]")) return;
    var panel = e.target.closest("[data-agent-panel]");
    clearTimeout(saveTimer);
    said(panel, "");
    saveTimer = setTimeout(function () { save(panel); }, 1000);
  });

  // галочка у файла: снята — строки с этим файлом в промте нет; поставлена — строка возвращается в «## Файлы»
  document.addEventListener("change", function (e) {
    var box = e.target.closest("[data-agent-file]");
    if (!box) return;
    var panel = box.closest("[data-agent-panel]");
    var area = panel.querySelector("[data-agent-text]");
    var lines = area.value.split("\n");
    var path = box.dataset.path;
    if (!box.checked) {
      lines = lines.filter(function (l) { return l.indexOf(path) === -1 || !/^\s*[-*]\s/.test(l); });
    } else if (area.value.indexOf(path) === -1) {
      var head = -1, end = lines.length;
      lines.forEach(function (l, i) {
        if (/^#+\s*Файлы/i.test(l)) head = i;
        else if (head >= 0 && end === lines.length && i > head && /^#/.test(l)) end = i;
      });
      if (head < 0) lines.push("", "## Файлы", box.dataset.line);
      else {
        while (end > head + 1 && !lines[end - 1].trim()) end--;
        lines.splice(end, 0, box.dataset.line);
      }
    }
    area.value = lines.join("\n");
    save(panel);
  });

  // «Копировать промт», «Открыть в Claude» и «Открыть в Codex»: сначала сохраняем правку — агенту уходит то, что на экране
  document.addEventListener("click", function (e) {
    var copy = e.target.closest("[data-agent-copy]");
    var launch = e.target.closest("[data-agent-launch]");
    var store = e.target.closest("[data-agent-save-btn]");
    var panel = (copy || launch || store) && (copy || launch || store).closest("[data-agent-panel]");
    if (!panel) return;
    e.preventDefault();
    var area = panel.querySelector("[data-agent-text]");
    if (copy) {
      var done = function () { said(panel, "Скопировано — вставьте агенту"); };
      if (navigator.clipboard) navigator.clipboard.writeText(area.value).then(done, function () { area.select(); });
      else { area.select(); document.execCommand("copy"); done(); }
      save(panel, true);
      return;
    }
    if (store) { save(panel); return; }
    launch.disabled = true;
    said(panel, launch.dataset.agentLaunch === "codex" ? "Открываю Codex…" : "Открываю Claude…");
    var form = panel.querySelector("[data-agent-prompt]");
    var body = new FormData();
    body.append("prompt", area.value);
    body.append("engine", launch.dataset.agentLaunch || "claude");
    fetch(form.action, { method: "POST", body: body, headers: HEADERS })
      .then(function (r) { return r.json(); })
      .then(function (res) { said(panel, res.ok ? res.said : res.error, !res.ok); })
      .catch(function () { said(panel, "Не открылось — PULSE не отвечает", true); })
      .then(function () { launch.disabled = false; });
  });

  // страница /agents?task=<id>: та же панель — следим за сборкой, если она идёт
  var page = document.querySelector(".agent-page");
  var live = page && page.querySelector("[data-agent-live]");
  if (live) watch(page, live);

  // открыть панель с любой страницы: кнопка или ссылка с data-agent-open="<id задачи>" (её щелчок ловится выше)
  // или ?agent=<id задачи> в адресе — например, /commitments?task=18&agent=18 или /?agent=18
  var asked = new URLSearchParams(location.search).get("agent");
  if (asked && /^\d+$/.test(asked) && !page) {
    var dlg = dialog();
    var box = dlg.querySelector(".dlg-box");
    dlg.showModal();
    load(box, asked).then(function () { box.focus(); });
  }
})();
