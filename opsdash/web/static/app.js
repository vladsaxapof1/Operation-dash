// Небольшие улучшения интерфейса. Без JS всё тоже работает (кроме выбора доски Kaiten).
(function () {
  "use strict";

  // выпадающие списки фильтров отправляют форму сразу (без inline-скриптов — их запрещает CSP).
  // Дату, которую набирают с клавиатуры, отправляем, только когда год уже введён целиком
  document.querySelectorAll("select[data-autosubmit], input[data-autosubmit]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      if (sel.type === "date" && sel.value && Number(sel.value.slice(0, 4)) < 2000) return;
      sel.form.submit();
    });
  });

  // боковое меню: свернуть в иконки / закрепить. Помним в cookie — сервер сразу отдаёт нужный вид
  document.querySelectorAll("[data-nav-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var collapsed = document.body.classList.toggle("nav-collapsed");
      btn.title = collapsed ? "Закрепить меню" : "Свернуть меню";
      document.cookie = "nav=" + (collapsed ? "collapsed" : "") + "; path=/; max-age=31536000; samesite=lax";
    });
  });

  // после действия (Готово, Сохранить…) страница перезагружается — возвращаем прокрутку туда, где была,
  // а не в начало страницы. Только для той же страницы и только сразу после отправки
  var kept = null;
  try { kept = JSON.parse(sessionStorage.getItem("keep_scroll") || "null"); sessionStorage.removeItem("keep_scroll"); } catch (e) { kept = null; }
  if (kept && (kept.path !== location.pathname || Date.now() - kept.t > 20000)) kept = null;
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || (form.method || "").toLowerCase() !== "post") return;
    var box = document.querySelector("[data-gantt]");
    try {
      sessionStorage.setItem("keep_scroll", JSON.stringify({ path: location.pathname, t: Date.now(), y: window.scrollY,
        gx: box ? box.scrollLeft : null }));
    } catch (err) { /* приватный режим — просто без возврата прокрутки */ }
  });
  if (kept) {
    window.scrollTo(0, kept.y);
    window.addEventListener("load", function () { window.scrollTo(0, kept.y); });
  }

  // таймлайн задач: сразу показать «сейчас» (на трети ширины, чтобы было видно и прошлое), а после сохранения —
  // то же место, что было. После загрузки ещё раз: иначе браузер восстанавливает прежнюю прокрутку поверх нашей
  var ganttBox = document.querySelector("[data-gantt]");
  if (ganttBox) {
    var labelW = function () { return ganttBox.querySelector(".gantt-corner").offsetWidth; };
    var toNow = function () {
      if (kept && kept.gx !== null) { ganttBox.scrollLeft = kept.gx; window.scrollTo(0, kept.y); return; }
      ganttBox.scrollLeft = Math.max(0, Number(ganttBox.dataset.nowX) - (ganttBox.clientWidth - labelW()) / 3);
    };
    toNow();
    window.addEventListener("load", function () { requestAnimationFrame(toNow); });

    // шкала дат едет вместе со страницей: таблица во всю высоту (без своей прокрутки), а шапку сдвигаем вниз,
    // пока таблица на экране (position: sticky тут не работает — таблица листается вбок)
    var head = ganttBox.querySelector(".gantt-head");
    var stick = function () {
      var r = ganttBox.getBoundingClientRect();
      var y = Math.min(Math.max(0, -r.top), Math.max(0, r.height - head.offsetHeight - 46));
      head.style.transform = y ? "translateY(" + y + "px)" : "";
      head.classList.toggle("stuck", y > 0);
    };
    window.addEventListener("scroll", stick, { passive: true });
    window.addEventListener("resize", stick);
    stick();

    // ширина колонки с задачами: тянем за край, двойной щелчок — по самому длинному названию. Помним в cookie
    var grip = ganttBox.querySelector("[data-gantt-resize]");
    var setW = function (w, save) {
      w = Math.round(Math.min(720, Math.max(200, w)));
      ganttBox.style.setProperty("--gl", w + "px");
      if (save) document.cookie = "gantt_w=" + w + "; path=/; max-age=31536000; samesite=lax";
    };
    grip.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      var x0 = e.clientX, w0 = labelW();
      grip.setPointerCapture(e.pointerId);
      document.body.classList.add("resizing");
      var move = function (ev) { setW(w0 + ev.clientX - x0, false); };
      var up = function () {
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", up);
        document.body.classList.remove("resizing");
        setW(labelW(), true);
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", up);
    });
    grip.addEventListener("dblclick", function () {
      var widest = 0;
      ganttBox.querySelectorAll(".gantt-what").forEach(function (el) { widest = Math.max(widest, el.scrollWidth); });
      setW(widest + 32, true);  // + поля ячейки
    });
  }

  // новые задачи (добавил Claude, вы ещё не видели): отметка «новая» снимается, когда курсор задержался на задаче
  // (не мгновенно — чтобы не снять все, просто проведя мышью по доске) или когда открыли её окно
  var markSeen = function (id) {
    var marked = document.querySelectorAll('[data-new="' + id + '"]');
    if (!id || !marked.length) return;
    marked.forEach(function (el) {
      el.removeAttribute("data-new");
      el.classList.remove("new");
      el.querySelectorAll(".chip.new").forEach(function (chip) { chip.remove(); });
    });
    fetch("/commitments/" + id + "/seen", { method: "POST" }).then(function (r) { return r.json(); }).then(function (res) {
      document.querySelectorAll('.nav a[href="/commitments"] .badge, .btn.toggle .badge').forEach(function (b) {
        if (res.unseen) b.textContent = res.unseen; else b.remove();
      });
    }).catch(function () { /* не получилось — отметка вернётся при следующем открытии страницы */ });
  };
  document.querySelectorAll("[data-new]").forEach(function (el) {
    var timer = null;
    el.addEventListener("mouseenter", function () {
      if (el.dataset.new) timer = setTimeout(function () { markSeen(el.dataset.new); }, 600);
    });
    el.addEventListener("mouseleave", function () { clearTimeout(timer); });
  });

  // карточка задачи — окно по центру экрана: клик по задаче (на доске, в списке, на таймлайне) — просмотр,
  // карандаш — сразу правка. Закрыть — ✕, Esc (сам браузер) или клик мимо окна
  var showTask = function (dlg, edit) {
    dlg.querySelector(".dlg-view").hidden = edit;
    dlg.querySelector(".dlg-edit").hidden = !edit;
    if (!dlg.open) dlg.showModal();
    // фокус — на поле заголовка, а при просмотре на само окно (иначе браузер обводит рамкой ✕)
    (edit ? dlg.querySelector(".dlg-edit [name=title]") : dlg.querySelector(".dlg-box")).focus();
    markSeen(dlg.id.slice(5));
  };
  var pressedOn = null;  // нажали внутри окна, а отпустили снаружи (выделяли текст) — не закрываем
  document.addEventListener("mousedown", function (e) { pressedOn = e.target; });
  document.addEventListener("click", function (e) {
    var t = e.target;
    var dlg = t.closest("dialog.dlg");
    if (dlg) {
      // ссылка на связанную задачу (часть задачи, подзадача): её окно есть на странице — открываем его,
      // нет — обычный переход в «Задачи»
      var rel = t.closest("[data-task-open]");
      if (rel) {
        var other = document.getElementById("task-" + rel.dataset.taskOpen);
        if (other) {
          e.preventDefault();
          if (other !== dlg) { dlg.close(); showTask(other, false); }
        }
        return;
      }
      if ((t === dlg && pressedOn === dlg) || t.closest("[data-dlg-close]")) dlg.close();
      else if (t.closest("[data-dlg-edit]")) showTask(dlg, true);
      else if (t.closest("[data-dlg-view]")) showTask(dlg, false);
      return;
    }
    var opener = t.closest("[data-task-open]");
    var card = t.closest("[data-task]");
    if (!opener && (!card || t.closest("a, button, form, input, select, textarea, label, summary"))) return;
    var target = document.getElementById("task-" + (opener ? opener.dataset.taskOpen : card.dataset.task));
    if (!target) return;
    e.preventDefault();
    showTask(target, !!(opener && opener.hasAttribute("data-edit")));
  });
  document.addEventListener("keydown", function (e) {  // полоса на таймлайне — с клавиатуры тоже
    if (e.key === "Enter" && e.target.matches("[data-task-open][role=button]")) e.target.click();
  });

  // поле «Часть задачи»: подсказки (открытые задачи) подгружаем при первом вводе — не держим их в каждой странице
  document.addEventListener("focusin", function (e) {
    var list = e.target.list;
    if (!list || !list.dataset.src || list.dataset.loaded) return;
    list.dataset.loaded = "1";
    fetch(list.dataset.src).then(function (r) { return r.json(); }).then(function (labels) {
      labels.forEach(function (label) {
        var opt = document.createElement("option");
        opt.value = label;
        list.appendChild(opt);
      });
    }).catch(function () { delete list.dataset.loaded; });
  });

  // «Задачи» по ссылке ?task=<id> (со встречи, со сводки): показать задачу на доске и открыть её окно.
  // Из адреса ссылку убираем — после «Готово» или «Сохранить» окно не должно открываться снова
  var focus = document.querySelector("[data-focus-task]");
  if (focus) {
    var fid = focus.dataset.focusTask;
    var spot = document.querySelector('.task[data-task="' + fid + '"], li[data-task="' + fid + '"], .gantt-bar[data-task-open="' + fid + '"]');
    if (spot) {
      spot.scrollIntoView({ block: "center" });
      spot.classList.add("flash");
    }
    var fdlg = document.getElementById("task-" + fid);
    if (fdlg) showTask(fdlg, false);
    var query = location.search.replace(/([?&])task=\d+(&|$)/, "$1").replace(/[?&]$/, "");
    history.replaceState(null, "", location.pathname + query + location.hash);
  }

  // связанные задачи (основная и подзадачи) подсвечиваются вместе: навели на одну — видно остальные на доске
  document.querySelectorAll(".task[data-rel]").forEach(function (card) {
    var light = function (on) {
      card.dataset.rel.split(" ").forEach(function (id) {
        document.querySelectorAll('.task[data-task="' + id + '"]').forEach(function (el) { el.classList.toggle("rel-hl", on); });
      });
    };
    card.addEventListener("mouseenter", function () { light(true); });
    card.addEventListener("mouseleave", function () { light(false); });
  });

  // доска задач: свернуть колонку. Помним в cookie — сервер сразу отдаёт её свёрнутой
  document.querySelectorAll("[data-kb-fold]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      btn.closest("[data-kb-col]").classList.toggle("folded");
      var folded = Array.prototype.map.call(document.querySelectorAll("[data-kb-col].folded"), function (col) { return col.dataset.kbCol; });
      document.cookie = "kb_folded=" + folded.join(".") + "; path=/; max-age=31536000; samesite=lax";
    });
  });

  // долгие действия: кнопка показывает, что работа идёт, и не нажимается второй раз
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var btn = form.querySelector("button");
      if (btn) { btn.disabled = true; btn.textContent = form.dataset.busy; }
    });
  });

  // подтверждение перед отправкой формы
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // «Копировать»: черновик ответа, текст напоминания, бриф, итоги встречи. Текст — в [data-copy] рядом с кнопкой
  // (в общем [data-copy-scope] или строке списка). Через document: кнопки бывают и в карточке встречи, собранной на лету
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy-btn]");
    if (!btn) return;
    var scope = btn.closest("[data-copy-scope]") || btn.closest("li");
    var src = scope && scope.querySelector("[data-copy]");
    if (!src) return;
    var text = src.tagName === "TEXTAREA" ? src.value : src.innerText;
    var label = btn.dataset.label || (btn.dataset.label = btn.textContent);
    var done = function () { btn.textContent = "Скопировано ✓"; setTimeout(function () { btn.textContent = label; }, 1800); };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done);
    else { var ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); done(); }
  });

  // бриф к встрече: «Открыть бриф» в карточке встречи календаря — окно с брифом целиком
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-brief-open]");
    if (!btn) return;
    var dlg = document.getElementById("brief-" + btn.dataset.briefOpen);
    if (!dlg) return;
    e.stopPropagation();
    var popup = document.querySelector("[data-ev-pop]");
    if (popup) popup.hidden = true;
    dlg.showModal();
    dlg.querySelector(".dlg-box").focus();
  });

  // проверка подключений
  document.querySelectorAll("[data-check]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var what = btn.dataset.check;
      var out = document.querySelector('[data-check-result="' + what + '"]');
      out.className = "check-result"; out.textContent = "проверяю…";
      btn.disabled = true;
      fetch("/settings/check/" + what, { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          out.className = "check-result " + (res.ok ? "ok" : "bad");
          out.textContent = (res.ok ? "✓ " : "✗ ") + res.message;
        })
        .catch(function (e) { out.className = "check-result bad"; out.textContent = "✗ " + e; })
        .finally(function () { btn.disabled = false; });
    });
  });

  // большие списки (чаты): отправляем только изменённые строки — тысяча полей упирается в лимит формы
  document.querySelectorAll("form[data-changed-only]").forEach(function (form) {
    var selects = form.querySelectorAll("select");
    selects.forEach(function (sel) { sel.dataset.initial = sel.value; });
    var radios = form.querySelectorAll("input[type=radio]");
    radios.forEach(function (r) { r.dataset.initial = r.checked ? "1" : ""; });
    form.addEventListener("submit", function () {
      selects.forEach(function (sel) { if (sel.value === sel.dataset.initial) sel.disabled = true; });
      radios.forEach(function (r) {
        var group = form.querySelectorAll('input[name="' + r.name + '"]');
        var same = Array.prototype.every.call(group, function (x) { return x.checked === !!x.dataset.initial; });
        if (same) r.disabled = true;
      });
    });
  });

  // --- настройки Telegram и Пачки: чат читается / нет / никогда — сохраняем сразу по нажатию;
  // фильтры по выбору и по типу (личные, группы, каналы, боты) работают вместе ---
  var chatForm = document.querySelector("form[data-chats]");
  if (chatForm) {
    var savedNote = chatForm.querySelector("[data-chat-saved]"), savedTimer = null;
    var chatRows = Array.prototype.map.call(chatForm.querySelectorAll("tbody tr"), function (row) {
      return { row: row, text: row.cells[0].textContent.toLowerCase() };
    });
    var chatShow = "", chatKind = "", chatQuery = "";
    var applyChatFilter = function () {
      chatRows.forEach(function (r) {
        var show = (!chatShow || r.row.dataset.state === chatShow) && (!chatKind || r.row.dataset.kind === chatKind)
          && (!chatQuery || r.text.indexOf(chatQuery) >= 0);
        r.row.style.display = show ? "" : "none";
      });
    };
    // счётчики считаем с учётом второго фильтра: «Читаю» среди личных, «Группы» среди нечитаемых
    var recount = function () {
      chatForm.querySelectorAll("[data-count]").forEach(function (n) {
        n.textContent = chatRows.filter(function (r) {
          return (!chatKind || r.row.dataset.kind === chatKind) && (!n.dataset.count || r.row.dataset.state === n.dataset.count);
        }).length;
      });
      chatForm.querySelectorAll("[data-kind-count]").forEach(function (n) {
        n.textContent = chatRows.filter(function (r) {
          return (!chatShow || r.row.dataset.state === chatShow) && r.row.dataset.kind === n.dataset.kindCount;
        }).length;
      });
    };
    chatForm.querySelector("[data-chats-submit]").hidden = true;  // со скриптом кнопка не нужна
    chatForm.addEventListener("change", function (e) {
      var input = e.target;
      if (input.type !== "radio") return;
      var row = input.closest("tr");
      row.dataset.state = input.value;
      row.classList.toggle("dim", input.value !== "1");
      recount();
      var body = new URLSearchParams({ source: chatForm.elements.source.value, ajax: "1" });
      body.append(input.name, input.value);
      fetch(chatForm.getAttribute("action"), { method: "POST", body: body })
        .then(function (r) { if (!r.ok) throw new Error(r.status); savedNote.textContent = "Сохранено ✓"; })
        .catch(function () { savedNote.textContent = "Не сохранилось — обновите страницу"; })
        .finally(function () { clearTimeout(savedTimer); savedTimer = setTimeout(function () { savedNote.textContent = ""; }, 2000); });
    });
    var searchTimer = null;
    chatForm.querySelector("[data-chat-search]").addEventListener("input", function (e) {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () { chatQuery = e.target.value.trim().toLowerCase(); applyChatFilter(); }, 80);
    });
    chatForm.querySelectorAll("[data-show]").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        chatShow = a.dataset.show;
        chatForm.querySelectorAll("[data-show]").forEach(function (x) { x.classList.toggle("active", x === a); });
        applyChatFilter();
        recount();
      });
    });
    chatForm.querySelectorAll("[data-chat-kinds] [data-kind]").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        chatKind = a.dataset.kind;
        chatForm.querySelectorAll("[data-chat-kinds] [data-kind]").forEach(function (x) { x.classList.toggle("active", x === a); });
        applyChatFilter();
        recount();
      });
    });
    chatForm.addEventListener("keydown", function (e) { if (e.key === "Enter" && e.target.matches("[data-chat-search]")) e.preventDefault(); });
  }

  // фильтр строк таблицы. Текст строк собираем один раз: innerText на каждую букву пересчитывал
  // раскладку всей таблицы, и с тысячей чатов ввод заметно тормозил
  function rowText(row) {
    var cells = row.cells ? Array.prototype.filter.call(row.cells, function (c) { return !c.querySelector("select, input, button"); }) : [row];
    return cells.map(function (c) { return c.textContent; }).join(" ").toLowerCase();
  }
  document.querySelectorAll("[data-filter]").forEach(function (input) {
    var rows = null, timer = null;
    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        rows = rows || Array.prototype.map.call(document.querySelectorAll(input.dataset.filter), function (row) {
          return { row: row, text: rowText(row), shown: true };
        });
        var q = input.value.trim().toLowerCase();
        rows.forEach(function (r) {
          var show = !q || r.text.indexOf(q) >= 0;
          if (show !== r.shown) { r.row.style.display = show ? "" : "none"; r.shown = show; }
        });
      }, 80);
    });
  });

  // --- документы: поиск на лету и типы одним нажатием — страницу не перезагружаем, поле ввода не теряет фокус ---
  var liveForm = document.querySelector("[data-live-search]");
  if (liveForm) {
    var liveBox = document.querySelector("[data-live-results]"), liveTimer = null, liveSeq = 0;
    var liveRun = function () {
      if (liveForm.elements.q.value.trim().length === 1) return;  // одна буква — ещё не запрос
      var url = liveForm.getAttribute("action") + "?" + new URLSearchParams(new FormData(liveForm)).toString();
      var my = ++liveSeq;
      fetch(url).then(function (r) { return r.text(); }).then(function (html) {
        if (my !== liveSeq) return;  // пока ждали ответа, запрос уже поменялся
        var fresh = new DOMParser().parseFromString(html, "text/html").querySelector("[data-live-results]");
        if (!fresh) return;
        liveBox.replaceWith(fresh);
        liveBox = fresh;
        history.replaceState(null, "", url);
      });
    };
    liveForm.elements.q.addEventListener("input", function () { clearTimeout(liveTimer); liveTimer = setTimeout(liveRun, 250); });
    liveForm.addEventListener("submit", function (e) { e.preventDefault(); clearTimeout(liveTimer); liveRun(); });
    document.addEventListener("click", function (e) {
      var chip = e.target.closest("[data-live-results] [data-type]");
      if (!chip || e.metaKey || e.ctrlKey) return;
      e.preventDefault();
      liveForm.elements.type.value = chip.dataset.type;
      liveRun();
    });
  }

  // --- Kaiten: пространства и направления ---
  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (text != null) n.textContent = text;
    return n;
  }
  function option(value, text) { return el("option", { value: value }, text); }

  var spacesBox = document.getElementById("kaiten-spaces");
  var spaceSel = document.querySelector("[data-kaiten-space]");
  var boardSel = document.querySelector("[data-kaiten-board]");
  var columnSel = document.querySelector("[data-kaiten-column]");
  var laneSel = document.querySelector("[data-kaiten-lane]");
  var pathInput = document.querySelector("[data-kaiten-path]");
  var boardsCache = [];

  if (spacesBox) {
    var tracked = {};
    try { JSON.parse(spacesBox.dataset.tracked || "[]").forEach(function (s) { if (s.track !== false) tracked[s.id] = true; }); } catch (e) {}
    fetch("/api/kaiten/spaces").then(function (r) { return r.json(); }).then(function (res) {
      spacesBox.innerHTML = "";
      if (!res.ok) { spacesBox.appendChild(el("p", { class: "muted small" }, "Не удалось загрузить: " + res.message + ". Сохраните адрес и токен выше.")); return; }
      var form = el("form", { method: "post", action: "/settings/kaiten/spaces" });
      res.spaces.forEach(function (s) {
        var label = el("label", { class: "check" });
        var cb = el("input", { type: "checkbox", name: "track", value: String(s.id) });
        if (tracked[s.id]) cb.checked = true;
        label.appendChild(cb);
        label.appendChild(document.createTextNode(" " + s.title));
        form.appendChild(label);
        form.appendChild(el("input", { type: "hidden", name: "title__" + s.id, value: s.title }));
        if (spaceSel) spaceSel.appendChild(option(s.id, s.title));
      });
      var actions = el("div", { class: "form-actions" });
      actions.appendChild(el("button", { class: "btn primary" }, "Сохранить пространства"));
      form.appendChild(actions);
      spacesBox.appendChild(form);
    });
  }

  function updatePath() {
    if (!pathInput) return;
    var parts = [spaceSel, boardSel, columnSel, laneSel].filter(function (s) { return s && s.value; })
      .map(function (s) { return s.options[s.selectedIndex].text; });
    pathInput.value = parts.join(" / ");
  }

  if (spaceSel) {
    spaceSel.addEventListener("change", function () {
      boardSel.innerHTML = ""; boardSel.appendChild(option("", "загрузка…"));
      columnSel.innerHTML = ""; columnSel.appendChild(option("", "первая"));
      laneSel.innerHTML = ""; laneSel.appendChild(option("", "по умолчанию"));
      if (!spaceSel.value) return;
      fetch("/api/kaiten/boards?space=" + encodeURIComponent(spaceSel.value)).then(function (r) { return r.json(); }).then(function (res) {
        boardSel.innerHTML = ""; boardSel.appendChild(option("", "—"));
        boardsCache = res.ok ? res.boards : [];
        boardsCache.forEach(function (b) { boardSel.appendChild(option(b.id, b.title)); });
        updatePath();
      });
    });
    boardSel.addEventListener("change", function () {
      var b = boardsCache.filter(function (x) { return String(x.id) === boardSel.value; })[0];
      columnSel.innerHTML = ""; columnSel.appendChild(option("", "первая"));
      laneSel.innerHTML = ""; laneSel.appendChild(option("", "по умолчанию"));
      if (b) {
        b.columns.forEach(function (c) { columnSel.appendChild(option(c.id, c.title)); });
        b.lanes.forEach(function (l) { laneSel.appendChild(option(l.id, l.title)); });
      }
      updatePath();
    });
    columnSel.addEventListener("change", updatePath);
    laneSel.addEventListener("change", updatePath);
  }

  // календарь: добавить участника из справочника одним нажатием
  document.querySelectorAll("[data-add-attendee]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var box = document.querySelector("[data-attendees]");
      var list = box.value.split(/[\s,;]+/).filter(Boolean);
      if (list.indexOf(btn.dataset.addAttendee) < 0) list.push(btn.dataset.addAttendee);
      box.value = list.join(", ");
    });
  });

  // --- календарь-неделя и расписание на сводке: карточка встречи, новая встреча по клику на свободное время, линия «сейчас» ---
  var pop = document.querySelector("[data-ev-pop]");
  if (pop) {
    var closePop = function () { pop.hidden = true; };
    document.querySelectorAll("[data-ev]").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        var detail = document.getElementById("ev-" + btn.dataset.ev);
        if (!detail) return;
        pop.innerHTML = detail.innerHTML;  // разметка уже экранирована на сервере
        pop.hidden = false;
        var r = btn.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
        var left = r.right + 8 + w > window.innerWidth ? r.left - w - 8 : r.right + 8;
        pop.style.left = Math.max(12, Math.min(left, window.innerWidth - w - 12)) + "px";
        pop.style.top = Math.max(12, Math.min(r.top, window.innerHeight - h - 12)) + "px";
      });
    });
    document.addEventListener("click", function (e) { if (!pop.contains(e.target)) closePop(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePop(); });
    window.addEventListener("scroll", closePop, { passive: true });
  }
  document.querySelectorAll(".wk-col").forEach(function (col) {
    col.addEventListener("click", function (e) {
      if (e.target !== col) return;  // нажали на встречу, а не на свободное время
      var hourPx = parseFloat(getComputedStyle(col).getPropertyValue("--hour")) || 48;
      var minutes = Number(col.dataset.hourFrom) * 60 + Math.floor((e.clientY - col.getBoundingClientRect().top) / hourPx * 2) * 30;
      document.querySelector("[data-new-day]").value = col.dataset.day;
      document.querySelector("[data-new-time]").value = ("0" + Math.floor(minutes / 60)).slice(-2) + ":" + ("0" + minutes % 60).slice(-2);
      window.location.hash = "new";
      setTimeout(function () { document.querySelector("[data-new-title]").focus(); }, 0);
    });
  });
  // время считаем от серверного (часовой пояс из настроек), а не от часов браузера
  var nowLine = document.querySelector("[data-now]");
  if (nowLine) {
    var t0 = Date.now(), top0 = Number(nowLine.dataset.now), min0 = Number(nowLine.dataset.nowMin);
    var nowHourPx = parseFloat(getComputedStyle(nowLine).getPropertyValue("--hour")) || 48;
    var nowLabel = nowLine.querySelector("[data-now-label]");
    setInterval(function () {
      var passed = (Date.now() - t0) / 60000;
      nowLine.style.top = top0 + passed / 60 * nowHourPx + "px";
      if (nowLabel) {
        var m = Math.floor(min0 + passed) % 1440;
        nowLabel.textContent = ("0" + Math.floor(m / 60)).slice(-2) + ":" + ("0" + m % 60).slice(-2);
      }
    }, 30000);
  }

  // «?» у поля: подсказка раскрывается вправо, а у правого края карточки или таблицы — влево,
  // иначе её обрезает (таблица листается вбок и прячет всё, что за её краем)
  document.querySelectorAll(".help").forEach(function (h) {
    var place = function () {
      var box = h.closest(".table-wrap, .card");
      var right = Math.min(box ? box.getBoundingClientRect().right : window.innerWidth, window.innerWidth) - 8;
      h.classList.toggle("flip", h.getBoundingClientRect().left + 290 > right);
    };
    h.addEventListener("mouseenter", place);
    h.addEventListener("focus", place);
  });

  // идёт обновление: ждём, пока PULSE перезапустится с другой версией, и открываем настройки заново
  var waitBuild = document.querySelector("[data-wait-build]");
  if (waitBuild) {
    var started = Date.now(), idle = 0;
    var poll = setInterval(function () {
      fetch("/health", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (res) {
        if (res.build && res.build !== waitBuild.dataset.waitBuild) {
          clearInterval(poll);
          window.location.href = "/settings?tab=general&msg=" + encodeURIComponent("PULSE обновлён: версия " + res.build) + "#updates";
          return;
        }
        // задание закончилось, а PULSE так и не перезапустился — значит, обновление не удалось
        return fetch("/api/status", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (st) {
          var busy = st.running.indexOf("update") >= 0 || st.queued.indexOf("update") >= 0;
          idle = busy ? 0 : idle + 1;
          if (idle >= 4) {
            clearInterval(poll);
            waitBuild.className = "alert bad";
            waitBuild.textContent = "Обновление не удалось — подробности в «Журнале» (задание «Обновление PULSE»).";
          }
        });
      }).catch(function () { idle = 0; /* перезапускается — подождём */ });
      if (Date.now() - started > 10 * 60000) {
        clearInterval(poll);
        waitBuild.textContent = "Обновление идёт дольше обычного — загляните в «Журнал».";
      }
    }, 3000);
  }

  // подсказка справа внизу: «Позже» прячет её на неделю
  document.querySelectorAll("[data-toast-close]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var toast = btn.closest("[data-toast]");
      toast.hidden = true;
      document.cookie = "voice_tip=" + encodeURIComponent(toast.dataset.toast) + "; path=/; max-age=604800; samesite=lax";
    });
  });

  // --- вход в Telegram по QR: меняем картинку при смене токена, после сканирования завершаем вход ---
  var qrBox = document.querySelector("[data-tg-qr]");
  if (qrBox) {
    var qrVersion = qrBox.dataset.version;
    var qrTimer = setInterval(function () {
      fetch("/settings/telegram/qr/status").then(function (r) { return r.json(); }).then(function (res) {
        if (res.state === "waiting") {
          if (String(res.version) !== qrVersion) {
            qrVersion = String(res.version);
            var img = qrBox.querySelector("img");
            if (img) img.src = "/settings/telegram/qr.svg?v=" + qrVersion;
          }
          return;
        }
        clearInterval(qrTimer);
        if (res.state === "ok") document.getElementById("tg-qr-done").submit();
        else if (res.state === "password") window.location.href = "/settings?tab=telegram&need=password";
        else if (res.state !== qrBox.dataset.tgQr) window.location.reload();
      });
    }, 2000);
  }

  // --- обновлять страницу, пока идёт расшифровка/разбор ---
  var watching = Array.prototype.map.call(document.querySelectorAll("[data-meeting-status]"), function (n) { return n.dataset.meetingStatus; });
  var busy = Array.prototype.some.call(document.querySelectorAll("[data-meeting-status]"), function (n) {
    return /очеред|расшифровка|разбор/.test(n.textContent);
  });
  if (watching.length && busy) {
    var last = null;
    setInterval(function () {
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (res) {
        var state = JSON.stringify(res.meetings.map(function (m) { return [m.id, m.status, Math.round((m.progress || 0) * 20)]; }));
        if (last !== null && state !== last) window.location.reload();
        last = state;
        res.meetings.forEach(function (m) {
          var bar = document.querySelector('[data-meeting-progress="' + m.id + '"]');
          if (bar) bar.style.width = Math.round((m.progress || 0) * 100) + "%";
        });
      });
    }, 5000);
  }
  // --- «Спросить Claude»: контекст к вопросу, микрофон, ответ по ходу работы ---
  var askForm = document.querySelector("[data-ask-form]");
  if (askForm) {
    var ctxList = askForm.querySelector("[data-ctx-list]");
    var askText = askForm.querySelector("[data-ask-text]");
    var addMenu = askForm.querySelector("[data-ask-add]");
    var fileInput = askForm.querySelector("[data-ctx-files]");
    var addCtx = function (value, label, files) {
      if (!files && Array.prototype.some.call(ctxList.querySelectorAll("input[name=ctx]"), function (i) { return i.value === value; })) return;
      var chip = document.createElement("span");
      chip.className = "chip acc ctx-chip";
      chip.textContent = label;
      if (files) chip.dataset.files = "1";
      else {
        var hidden = document.createElement("input");
        hidden.type = "hidden"; hidden.name = "ctx"; hidden.value = value;
        chip.appendChild(hidden);
      }
      var x = document.createElement("button");
      x.type = "button"; x.textContent = "✕"; x.setAttribute("data-ctx-remove", ""); x.setAttribute("aria-label", "Убрать");
      chip.appendChild(x);
      ctxList.appendChild(chip);
    };
    var picked = function () { addMenu.open = false; askText.focus(); };
    // меню «Контекст» закрывается кликом мимо него и Esc, а не только повторным нажатием
    document.addEventListener("click", function (e) { if (addMenu.open && !addMenu.contains(e.target)) addMenu.open = false; });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && addMenu.open) addMenu.open = false; });
    ctxList.addEventListener("click", function (e) {
      if (!e.target.matches("[data-ctx-remove]")) return;
      var chip = e.target.closest(".ctx-chip");
      if (chip.dataset.files) fileInput.value = "";
      chip.remove();
    });
    var meetingPick = askForm.querySelector("[data-ctx-meeting]");
    meetingPick.addEventListener("change", function () {
      if (!meetingPick.value) return;
      addCtx("meeting:" + meetingPick.value, "встреча «" + meetingPick.options[meetingPick.selectedIndex].text + "»");
      meetingPick.value = "";
      picked();
    });
    var taskPick = askForm.querySelector("[data-ctx-task]");
    taskPick.addEventListener("change", function () {
      var m = taskPick.value.match(/^#(\d+)\s*·\s*([^·]+)/);
      if (!m) return;
      addCtx("task:" + m[1], "задача «" + m[2].trim() + "»");
      taskPick.value = "";
      picked();
    });
    var docPick = askForm.querySelector("[data-ctx-doc]"), docList = document.getElementById("ask-docs"), docPaths = {}, docTimer;
    docPick.addEventListener("input", function () {
      if (docPaths[docPick.value]) {
        addCtx("doc:" + docPaths[docPick.value], "документ «" + docPick.value + "»");
        docPick.value = "";
        picked();
        return;
      }
      clearTimeout(docTimer);
      docTimer = setTimeout(function () {
        fetch("/api/ask/docs?q=" + encodeURIComponent(docPick.value)).then(function (r) { return r.json(); }).then(function (items) {
          docList.innerHTML = "";
          items.forEach(function (it) {
            docPaths[it.label] = it.path;
            var o = document.createElement("option");
            o.value = it.label;
            docList.appendChild(o);
          });
        });
      }, 250);
    });
    fileInput.addEventListener("change", function () {
      Array.prototype.forEach.call(ctxList.querySelectorAll("[data-files]"), function (c) { c.remove(); });
      var names = Array.prototype.map.call(fileInput.files, function (f) { return f.name; });
      if (names.length) { addCtx("", (names.length > 1 ? "файлы: " : "файл «") + names.join(", ") + (names.length > 1 ? "" : "»"), true); picked(); }
    });
    askText.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); askForm.requestSubmit(); }
    });
    askForm.addEventListener("submit", function (e) {
      if (e.defaultPrevented) return;
      if (!askText.value.trim() && !ctxList.children.length) { e.preventDefault(); askText.focus(); return; }
      var send = askForm.querySelector("[data-ask-send]");
      send.disabled = true; send.textContent = "Отправляю…";
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-ask-example]"), function (b) {
      b.addEventListener("click", function () { askText.value = b.dataset.askExample; askText.focus(); });
    });

    // диктовка, как в Handy: звук режется на фразы по паузам, каждая сразу уходит в Whisper на этом компьютере —
    // текст появляется, пока вы говорите. Нажать ещё раз (или Esc) — закончить
    var mic = askForm.querySelector("[data-mic]"), micStatus = askForm.querySelector("[data-mic-status]"), dict = null;
    var PRE_ROLL = 0.3, PAUSE = 0.7, MAX_PHRASE = 20, MIN_SPEECH = 0.25;  // секунды
    var wavBlob = function (chunks, rate) {
      var n = chunks.reduce(function (s, c) { return s + c.length; }, 0);
      var view = new DataView(new ArrayBuffer(44 + n * 2)), off = 44;
      var str = function (o, s) { for (var i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
      str(0, "RIFF"); view.setUint32(4, 36 + n * 2, true); str(8, "WAVEfmt "); view.setUint32(16, 16, true);
      view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, rate, true);
      view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
      str(36, "data"); view.setUint32(40, n * 2, true);
      chunks.forEach(function (c) {
        for (var i = 0; i < c.length; i++, off += 2) view.setInt16(off, Math.max(-1, Math.min(1, c[i])) * 0x7fff, true);
      });
      return new Blob([view], { type: "audio/wav" });
    };
    var showStatus = function () {
      if (!dict) return;
      micStatus.textContent = (dict.failed ? dict.failed + " · " : "")
        + (dict.stopping ? "Дописываю…" : "● Слушаю" + (dict.pending ? " · распознаю…" : " — говорите, текст появится сам"));
    };
    var insertText = function (text) {
      if (!text) return;
      var before = askText.value;
      if (!before.trim() || /[.!?…]\s*$/.test(before)) text = text.charAt(0).toUpperCase() + text.slice(1);  // начало предложения
      askText.value = before + (before && !/\s$/.test(before) ? " " : "") + text;
      askText.scrollTop = askText.scrollHeight;
    };
    var sendPhrase = function (chunks, rate) {
      var fd = new FormData();
      fd.append("audio", wavBlob(chunks, rate), "phrase.wav");
      dict.pending++;
      showStatus();
      var d = dict;
      d.chain = d.chain.then(function () {
        // что уже надиктовано — Whisper продолжит связно; в пустом поле подсказка «Вопрос:» даёт заглавные и «?»
        fd.append("prompt", askText.value.slice(-300) || "Вопрос:");
        return fetch("/api/ask/voice", { method: "POST", body: fd }).then(function (r) { return r.json(); })
          .then(function (res) { if (res.error) d.failed = res.error; else insertText(res.text); })
          .catch(function () { d.failed = "Не получилось распознать фразу"; });
      }).then(function () { d.pending--; showStatus(); });
    };
    var endPhrase = function () {
      var p = dict.phrase;
      dict.phrase = null;
      if (p && p.speech >= MIN_SPEECH) sendPhrase(p.chunks, dict.rate);
    };
    var onFrame = function (frame) {
      var d = dict, sec = frame.length / d.rate, sum = 0;
      for (var i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
      var level = Math.sqrt(sum / frame.length);
      var loud = level > Math.max(0.012, d.floor * 3);
      if (!loud) d.floor = d.floor * 0.995 + level * 0.005;  // фоновый шум комнаты
      mic.style.boxShadow = "0 0 0 " + Math.min(10, 2 + level * 120).toFixed(1) + "px rgba(220, 60, 60, .25)";
      d.preroll.push(frame);
      d.prerollSec += sec;
      while (d.prerollSec - d.preroll[0].length / d.rate > PRE_ROLL) d.prerollSec -= d.preroll.shift().length / d.rate;
      if (loud) {
        if (!d.phrase) d.phrase = { chunks: d.preroll.slice(0, -1), len: d.prerollSec - sec, speech: 0, quiet: 0 };
        d.phrase.speech += sec;
        d.phrase.quiet = 0;
      } else if (d.phrase) {
        d.phrase.quiet += sec;
      }
      if (d.phrase) {
        d.phrase.chunks.push(frame);
        d.phrase.len += sec;
        if (d.phrase.quiet >= PAUSE || d.phrase.len >= MAX_PHRASE) endPhrase();
      }
    };
    var stopDictation = function () {
      if (!dict || dict.stopping) return;
      var d = dict;
      d.stopping = true;
      endPhrase();
      showStatus();
      d.stream.getTracks().forEach(function (t) { t.stop(); });
      d.ctx.close();
      mic.style.boxShadow = "";
      d.chain.then(function () {
        mic.classList.remove("on");
        if (dict === d) dict = null;
        micStatus.textContent = d.failed || "";
        askText.focus();
      });
    };
    var startDictation = function (stream) {
      var ctx, source;
      try { ctx = new AudioContext({ sampleRate: 16000 }); source = ctx.createMediaStreamSource(stream); }
      catch (e) {  // Firefox не смешивает частоты — берём частоту устройства, сервер приведёт её к 16 кГц
        if (ctx) ctx.close();
        ctx = new AudioContext();
        source = ctx.createMediaStreamSource(stream);
      }
      if (ctx.state === "suspended") ctx.resume();  // браузер мог создать его «на паузе» — звук бы не шёл
      dict = { ctx: ctx, stream: stream, rate: ctx.sampleRate, floor: 0.005, preroll: [], prerollSec: 0, phrase: null,
               pending: 0, chain: Promise.resolve(), stopping: false };
      mic.classList.add("on");
      showStatus();
      var connect = function (node) { source.connect(node); node.connect(ctx.destination); };  // звук не выводит — только слушает
      var legacy = function () {  // браузеры без AudioWorklet
        var proc = ctx.createScriptProcessor(4096, 1, 1);
        proc.onaudioprocess = function (e) { if (dict && !dict.stopping) onFrame(e.inputBuffer.getChannelData(0).slice(0)); };
        connect(proc);
      };
      if (!ctx.audioWorklet) { legacy(); return; }
      ctx.audioWorklet.addModule("/static/mic-worklet.js").then(function () {
        var node = new AudioWorkletNode(ctx, "pulse-mic");
        node.port.onmessage = function (e) { if (dict && !dict.stopping) onFrame(e.data); };
        connect(node);
      }).catch(legacy);
    };
    mic.addEventListener("click", function () {
      if (dict) { stopDictation(); return; }
      if (!navigator.mediaDevices || !window.AudioContext) { micStatus.textContent = "Этот браузер не умеет записывать звук"; return; }
      fetch("/api/ask/voice/warm", { method: "POST" });  // модель загрузится, пока звучит первая фраза
      navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
        .then(startDictation)
        .catch(function () { micStatus.textContent = "Нет доступа к микрофону — разрешите его в браузере"; });
    });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && dict) stopDictation(); });
    askForm.addEventListener("submit", function (e) {  // отправили посреди диктовки — сначала дописываем последнюю фразу
      if (!dict) return;
      e.preventDefault();
      var d = dict;
      stopDictation();
      d.chain.then(function () { askForm.requestSubmit(); });
    }, true);
  }

  // ответ Claude пишется по ходу: шаги и текст обновляются, по готовности — страница целиком (Markdown, кнопки)
  var askLive = document.querySelector("[data-ask-live]");
  if (askLive) {
    var liveSteps = askLive.querySelector("[data-live-steps]"), liveText = askLive.querySelector("[data-live-text]"),
        liveState = askLive.querySelector("[data-live-state]");
    var askPoll = setInterval(function () {
      fetch("/api/ask/" + askLive.dataset.askLive + "/live", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (res) {
        if (!res.running) { clearInterval(askPoll); window.location.reload(); return; }
        var nearEnd = window.innerHeight + window.scrollY >= document.body.scrollHeight - 200;
        liveSteps.innerHTML = "";
        (res.steps || []).forEach(function (step) {
          var d = document.createElement("div");
          d.textContent = step;
          liveSteps.appendChild(d);
        });
        liveText.textContent = res.text || "";
        if (liveState) liveState.textContent = res.text ? "пишет ответ" : "думает";
        if (nearEnd) window.scrollTo(0, document.body.scrollHeight);
      });
    }, 1200);
  }
})();
