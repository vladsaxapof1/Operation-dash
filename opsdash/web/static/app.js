// Небольшие улучшения интерфейса. Без JS всё тоже работает (кроме выбора доски Kaiten).
(function () {
  "use strict";

  // новый дизайн: окна закрываются коротким затуханием (v3-motion.css, .m-closing), а не пропадают разом. Работает для
  // любого dialog.close() и для Esc. Анимации выключены (настройка или «уменьшить движение» в системе) — у .m-closing
  // нет анимации, и окно закрывается сразу. Классический дизайн не затронут: там body без d3
  // nav.js запускает app.js заново после смены экрана: окна подменяем один раз, а Esc (обработчик на document,
  // nav.js его снимает перед сменой) вешаем каждый раз
  if (document.body.classList.contains("d3") && window.HTMLDialogElement && HTMLDialogElement.prototype.close
      && !HTMLDialogElement.prototype._m3) {
    var dlgProto = HTMLDialogElement.prototype, nativeClose = dlgProto.close, nativeModal = dlgProto.showModal;
    dlgProto._m3 = true;
    var stopClosing = function (dlg) {
      clearTimeout(dlg._mClosing);
      dlg.removeEventListener("animationend", dlg._mEnd);
      dlg._mClosing = dlg._mEnd = null;
      dlg.classList.remove("m-closing");
    };
    dlgProto.close = function () {
      var dlg = this, args = arguments;
      if (!dlg.open) return nativeClose.apply(dlg, args);
      if (dlg._mClosing) return undefined;  // уже закрывается
      dlg.classList.add("m-closing");
      var cs = getComputedStyle(dlg);
      if (cs.animationName === "none" || !parseFloat(cs.animationDuration)) {
        dlg.classList.remove("m-closing");
        return nativeClose.apply(dlg, args);
      }
      var finish = function () { stopClosing(dlg); nativeClose.apply(dlg, args); };
      dlg._mEnd = function (e) { if (e.target === dlg && !e.pseudoElement) finish(); };
      dlg.addEventListener("animationend", dlg._mEnd);
      dlg._mClosing = setTimeout(finish, 400);  // анимация не дошла до конца (вкладка в фоне) — всё равно закрываем
      return undefined;
    };
    dlgProto.showModal = function () {
      if (this._mClosing) { stopClosing(this); return undefined; }  // открыли снова, пока закрывалось, — остаётся открытым
      return nativeModal.apply(this, arguments);
    };
  }
  if (document.body.classList.contains("d3") && window.HTMLDialogElement && HTMLDialogElement.prototype._m3) {
    document.addEventListener("cancel", function (e) {  // Esc: браузер закрыл бы окно сразу — закрываем так же, как ✕
      var dlg = e.target;
      if (!(dlg instanceof HTMLDialogElement) || !e.cancelable || !dlg.open) return;
      e.preventDefault();
      dlg.close();
    }, true);
  }
  // скелетон на время загрузки (v3-motion.css: .d3-skel): серые полоски по форме будущего содержимого.
  // shape: "lines" — несколько строк текста, "panel" — заголовок, строки и поле, "card" — карточка
  window.pulseSkel = function (shape) {
    var i = function (cls) { return '<i class="sk ' + cls + '"></i>'; };
    var inner = shape === "panel" ? i("h w-40") + i("w-80") + i("w-60") + i("blk") + '<span class="sk-row">' + i("btn") + i("btn") + "</span>"
      : shape === "card" ? i("h w-70") + i("w-40") + i("w-90") + i("w-80") + i("w-50")
      : i("w-80") + i("w-60") + i("w-70");
    return '<div class="d3-skel" role="status" aria-label="Загружаю">' + inner + "</div>";
  };

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
    btn.addEventListener("click", function (e) {
      var collapsed = document.body.classList.toggle("nav-collapsed");
      // свёрнутое меню раскрыто, пока в нём фокус: после щелчка мышью отпускаем кнопку, чтобы меню сразу сложилось
      if (e.detail) btn.blur();
      btn.title = collapsed ? "Развернуть меню" : "Свернуть меню";
      btn.setAttribute("aria-label", btn.title);
      document.cookie = "nav=" + (collapsed ? "collapsed" : "") + "; path=/; max-age=31536000; samesite=lax";
    });
  });

  // Переписка: лента открывается на последнем сообщении (или на сообщении из адреса #m123), открытый чат виден
  // в списке; прокрутка ленты к верху подгружает более ранние сообщения и оставляет взгляд на том же месте
  var feed = document.querySelector("[data-chat-feed]");
  if (feed) {
    var mark = location.hash && /^#m\d+$/.test(location.hash) ? document.getElementById(location.hash.slice(1)) : null;
    if (mark && feed.contains(mark)) feed.scrollTop += mark.getBoundingClientRect().top - feed.getBoundingClientRect().top - 40;
    else feed.scrollTop = feed.scrollHeight;
    var activeChat = document.querySelector("[data-chat-list] .chat-i.active");
    if (activeChat) activeChat.scrollIntoView({ block: "nearest" });
    var loadingEarlier = false;
    feed.addEventListener("scroll", function () {
      var link = feed.querySelector("[data-feed-more]");
      if (!link || loadingEarlier || feed.scrollTop > 150) return;
      loadingEarlier = true;
      fetch(link.href, { credentials: "same-origin" }).then(function (r) { return r.text(); }).then(function (html) {
        var fresh = new DOMParser().parseFromString(html, "text/html").querySelector("[data-chat-feed]");
        if (!fresh) return;
        var fromBottom = feed.scrollHeight - feed.scrollTop;
        feed.innerHTML = fresh.innerHTML;
        feed.scrollTop = feed.scrollHeight - fromBottom;
      }).catch(function () {}).then(function () { loadingEarlier = false; });
    }, { passive: true });
  }

  // телефон: лист «Ещё» закрывается кликом мимо него
  var more = document.querySelector("[data-tabbar-more]");
  if (more) document.addEventListener("click", function (e) { if (more.open && !more.contains(e.target)) more.open = false; });

  // «Задачи»: пунктирная плашка «Ещё N задач со сроком после …» внизу колонки доски, группы списка или Ганта
  // раскрывает дальние задачи под собой, второй щелчок — прячет
  var showLater = function (btn, open) {
    var box = btn && document.getElementById(btn.getAttribute("aria-controls"));
    if (!box) return;
    box.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  };
  var laterBtn = function (id) { return document.querySelector('[data-later][aria-controls="' + id + '"]'); };
  document.querySelectorAll("[data-later]").forEach(function (btn) {
    btn.addEventListener("click", function () { showLater(btn, btn.getAttribute("aria-expanded") !== "true"); });
  });

  // после действия (Готово, Сохранить…) страница перезагружается — возвращаем прокрутку туда, где была,
  // а не в начало страницы, и раскрытые дальние задачи. Только для той же страницы и только сразу после отправки
  var kept = null;
  try { kept = JSON.parse(sessionStorage.getItem("keep_scroll") || "null"); sessionStorage.removeItem("keep_scroll"); } catch (e) { kept = null; }
  if (kept && (kept.path !== location.pathname || Date.now() - kept.t > 20000)) kept = null;
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || (form.method || "").toLowerCase() !== "post") return;
    var box = document.querySelector("[data-gantt]");
    try {
      sessionStorage.setItem("keep_scroll", JSON.stringify({ path: location.pathname, t: Date.now(), y: window.scrollY,
        gx: box ? box.scrollLeft : null,
        later: Array.prototype.map.call(document.querySelectorAll('[data-later][aria-expanded="true"]'),
                                        function (b) { return b.getAttribute("aria-controls"); }) }));
    } catch (err) { /* приватный режим — просто без возврата прокрутки */ }
  });
  if (kept) {
    (kept.later || []).forEach(function (id) { showLater(laterBtn(id), true); });
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
    document.querySelectorAll("[data-gantt-today]").forEach(function (b) {  // «Сегодня» в шапке Ганта
      b.addEventListener("click", function () { kept = null; ganttBox.scrollTo({ left: Math.max(0, Number(ganttBox.dataset.nowX) - (ganttBox.clientWidth - labelW()) / 3), behavior: "smooth" }); });
    });

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

    // стрелки «ждёт»: от конца полосы задачи, которую ждут, к началу полосы той, что ждёт (data-waits — id через
    // пробел). Рисуем поверх таймлайна заново, когда меняется раскладка: свернули группу, потянули ширину, окно
    var body = ganttBox.querySelector(".gantt-body");
    var NS = "http://www.w3.org/2000/svg";
    var links = document.createElementNS(NS, "svg");
    links.setAttribute("class", "gantt-links");
    links.setAttribute("aria-hidden", "true");
    body.appendChild(links);
    var drawLinks = function () {
      var box = body.getBoundingClientRect();
      links.setAttribute("width", body.scrollWidth);
      links.setAttribute("height", body.scrollHeight);
      var out = ['<defs><marker id="gl-a" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">' +
                 '<path d="M0 0L8 4L0 8z" class="gl-head"/></marker>' +
                 '<marker id="gl-ah" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">' +
                 '<path d="M0 0L8 4L0 8z" class="gl-head hl"/></marker></defs>'];
      body.querySelectorAll(".gantt-bar[data-waits]").forEach(function (bar) {
        if (!bar.offsetParent) return;  // группа свёрнута
        var b = bar.getBoundingClientRect();
        bar.dataset.waits.split(" ").forEach(function (id) {
          var from = body.querySelector('.gantt-bar[data-task-open="' + id + '"]');
          if (!from || !from.offsetParent) return;  // ту задачу фильтр спрятал или её группа свёрнута
          var a = from.getBoundingClientRect();
          var x1 = Math.round(a.right - box.left), y1 = Math.round(a.top + a.height / 2 - box.top);
          var x2 = Math.round(b.left - box.left), y2 = Math.round(b.top + b.height / 2 - box.top);
          var d = x2 - x1 >= 24
            ? "M" + x1 + " " + y1 + "H" + (x1 + 10) + "V" + y2 + "H" + (x2 - 1)
            : "M" + x1 + " " + y1 + "h8V" + Math.round(b.top - box.top - 6) + "H" + (x2 - 12) + "V" + y2 + "H" + (x2 - 1);
          out.push('<path d="' + d + '" data-from="' + id + '" data-to="' + bar.dataset.taskOpen + '" marker-end="url(#gl-a)"/>');
        });
      });
      links.innerHTML = out.join("");
    };
    var hl = function (id, on) {
      links.querySelectorAll('[data-from="' + id + '"], [data-to="' + id + '"]').forEach(function (p) {
        p.classList.toggle("hl", on);
        p.setAttribute("marker-end", on ? "url(#gl-ah)" : "url(#gl-a)");
      });
    };
    body.querySelectorAll(".gantt-bar").forEach(function (bar) {
      bar.addEventListener("mouseenter", function () { hl(bar.dataset.taskOpen, true); });
      bar.addEventListener("mouseleave", function () { hl(bar.dataset.taskOpen, false); });
    });
    var redraw = function () { requestAnimationFrame(drawLinks); };
    if (window.ResizeObserver) new ResizeObserver(redraw).observe(body);  // свернули группу, потянули ширину
    window.addEventListener("resize", redraw);
    window.addEventListener("load", redraw);
    drawLinks();
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
    fetch("/commitments/" + id + "/seen", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (res) {  // число на «Новые» в «Задачах» — сразу, без перезагрузки
        document.querySelectorAll("[data-unseen]").forEach(function (b) {
          if (b.textContent.trim() !== String(res.unseen)) {  // число поменялось — короткая подсветка (v3-motion.css)
            b.classList.remove("m-bump");
            void b.offsetWidth;
            b.classList.add("m-bump");
          }
          b.textContent = res.unseen; b.hidden = !res.unseen;
        });
      })
      .catch(function () { /* не получилось — отметка вернётся при следующем открытии страницы */ });
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
  var showTask = function (dlg, edit, remind) {
    dlg.querySelector(".dlg-view").hidden = edit;
    dlg.querySelector(".dlg-edit").hidden = !edit;
    var rem = dlg.querySelector(".dlg-remind");  // колокольчик на карточке, «напомнить» у связи — окно с напоминанием
    if (rem && remind) rem.open = true;
    // карточка задачи скрыта (свёрнутые «готовые», фильтр) — окно внутри неё не отрисуется, но модальное
    // окно всё равно заблокирует страницу. Такое окно переносим в body
    if (!dlg.open && !dlg.parentElement.checkVisibility()) document.body.appendChild(dlg);
    if (!dlg.open) dlg.showModal();
    logScroll(dlg);
    logResume(dlg);
    // фокус — на поле заголовка, а при просмотре на само окно (иначе браузер обводит рамкой ✕)
    var first = edit ? (dlg.querySelector(".dlg-edit [name=title]") || dlg.querySelector(".dlg-edit input[type=text]"))
      : (remind && rem ? rem.querySelector("textarea") : dlg.querySelector(".dlg-box"));
    if (first) first.focus();
    markSeen(dlg.id.slice(5));
  };
  var closeMenus = function (except) {
    document.querySelectorAll("details.menu[open]").forEach(function (m) { if (m !== except) m.open = false; });
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
          if (other !== dlg) { dlg.close(); showTask(other, false, rel.hasAttribute("data-remind")); }
        }
        return;
      }
      if ((t === dlg && pressedOn === dlg) || t.closest("[data-dlg-close]")) dlg.close();
      else if (t.closest("[data-dlg-edit]")) { closeMenus(); showTask(dlg, true); }
      else if (t.closest("[data-dlg-view]")) showTask(dlg, false);
      return;
    }
    var opener = t.closest("[data-task-open]");
    var card = t.closest("[data-task]");
    if (!opener && (!card || t.closest("a, button, form, input, select, textarea, label, summary"))) return;
    // карточка на доске раскрывается на месте (зависимости, кнопки, «открыть»); второй клик — сворачивает
    if (!opener && card.hasAttribute("data-expand")) {
      var open = card.classList.toggle("open");
      card.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) markSeen(card.dataset.task);
      return;
    }
    var target = document.getElementById("task-" + (opener ? opener.dataset.taskOpen : card.dataset.task));
    if (!target) return;
    e.preventDefault();
    showTask(target, !!(opener && opener.hasAttribute("data-edit")), !!(opener && opener.hasAttribute("data-remind")));
  });
  document.addEventListener("keydown", function (e) {  // полоса на таймлайне и карточка доски — с клавиатуры тоже
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-task-open][role=button], [data-expand]")) {
      e.preventDefault();
      e.target.click();
    }
    if (e.key === "Escape") closeMenus();
  });

  // ход задачи в окне задачи: комментарий («жду ответа» — вопрос), правка и удаление записи — без перезагрузки:
  // лента и строка «Сейчас» приходят готовыми; ⌘/Ctrl+Enter — записать. «Собрать с Claude» — тоже без перезагрузки:
  // в ленте точки и что Claude делает сейчас (опрос раз в 2,5 с), закончил — новые записи и итог
  function logScroll(dlg) {
    var list = dlg && dlg.querySelector("[data-log-list]");
    if (list) list.scrollTop = list.scrollHeight;
  }
  var logApply = function (dlg, res) {
    var list = dlg.querySelector("[data-log-list]");
    if (list && res.list !== undefined) list.innerHTML = res.list;
    var slot = dlg.querySelector("[data-now-slot]");
    if (slot && res.now !== undefined) {
      var box = document.createElement("div");
      box.innerHTML = res.now;
      if (box.firstElementChild) slot.replaceWith(box.firstElementChild);
    }
    var empty = dlg.querySelector("[data-log-empty]");
    if (empty && res.count !== undefined) empty.hidden = !!res.count;
    logScroll(dlg);
  };
  var logPost = function (form, body) {
    return fetch(form.action, { method: "POST", body: body, headers: { Accept: "application/json" } })
      .then(function (r) { return r.json().catch(function () { return { ok: false, err: "" }; }); })
      .then(function (res) { if (!res.ok) throw new Error(res.err || ""); return res; });
  };
  var logError = function (dlg, x) {  // ответ PULSE — его словами; обрыв связи — своими
    var err = dlg.querySelector("[data-log-err]");
    if (!err) return;
    err.textContent = (x && x.name === "Error" && x.message) || "Не получилось сохранить — попробуйте ещё раз";
    err.hidden = false;
  };
  // «Собрать с Claude»: точки и шаг, пока идёт; итог — когда закончил
  var logBusy = function (dlg, res) {
    var aside = dlg.querySelector("[data-log-task]");
    var busy = dlg.querySelector("[data-log-busy]");
    var btn = dlg.querySelector("[data-log-collect]");
    var on = res && (res.state === "running" || res.state === "queued");
    if (aside) { if (on) aside.dataset.logState = res.state; else delete aside.dataset.logState; }
    if (btn) btn.hidden = !!on;
    if (!busy) return;
    busy.hidden = !on;
    if (on) {
      var title = busy.querySelector("[data-log-busy-title]");
      var step = busy.querySelector("[data-log-busy-step]");
      if (res.state === "queued") {
        title.textContent = "Ждёт очереди";
        step.textContent = "начнёт, как освободится";
      } else {
        title.textContent = ((aside && aside.dataset.ai) || "Claude") + " собирает ход задачи";
        step.textContent = res.step || "ищет в переписке, встречах и Kaiten";
      }
    }
  };
  var logDone = function (dlg, res) {
    var done = dlg.querySelector("[data-log-done]");
    if (!done) return;
    var m = /\+(\d+)/.exec(res.result || "");
    done.className = "log-done small " + (res.ok === false ? "bad-text" : m ? "ok-text" : "muted");
    done.textContent = res.ok === false ? "Не получилось собрать: " + (res.result || "ошибка — подробности в «Журнале»")
      : m ? "Готово — добавлено записей: " + m[1] : "Готово — нового по задаче не нашлось";
    done.hidden = false;
    clearTimeout(done._t);
    done._t = setTimeout(function () { done.hidden = true; }, 15000);
  };
  var logPoll = function (dlg) {
    var aside = dlg && dlg.querySelector("[data-log-task]");
    if (!aside || dlg._logTimer) return;
    var tick = function () {
      if (!dlg.open) { clearInterval(dlg._logTimer); dlg._logTimer = null; return; }  // откроют снова — продолжим
      fetch("/api/commitments/" + aside.dataset.logTask + "/log", { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (!res.ok) return;
          logBusy(dlg, res);
          if (res.state === "idle") {
            clearInterval(dlg._logTimer);
            dlg._logTimer = null;
            logApply(dlg, res);
            logDone(dlg, res);
          }
        })
        .catch(function () { /* связь пропала — следующая попытка через 2,5 с */ });
    };
    dlg._logTimer = setInterval(tick, 2500);
    tick();
  };
  var logResume = function (dlg) {  // окно открыли, а сбор уже идёт (нажали раньше или в другой вкладке)
    var aside = dlg && dlg.querySelector("[data-log-task]");
    if (aside && aside.dataset.logState) logPoll(dlg);
  };
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.matches("[data-log-form], [data-log-remove], [data-log-collect], [data-log-edit-form]")) return;
    var dlg = form.closest("dialog");
    if (!dlg || !window.fetch) return;
    e.preventDefault();
    if (form.dataset.logConfirm && !window.confirm(form.dataset.logConfirm)) return;
    var buttons = form.querySelectorAll("button");
    buttons.forEach(function (b) { b.disabled = true; });
    var err = dlg.querySelector("[data-log-err]");
    if (err) err.hidden = true;
    var collect = form.matches("[data-log-collect]");
    if (collect) {
      var done = dlg.querySelector("[data-log-done]");
      if (done) done.hidden = true;
      logBusy(dlg, { state: "queued" });
    }
    var body = form.matches("[data-log-remove], [data-log-collect]") ? null : new FormData(form);
    logPost(form, body)
      .then(function (res) {
        if (collect) { logBusy(dlg, res); logPoll(dlg); return; }
        if (form.matches("[data-log-form]")) form.reset();
        logApply(dlg, res);
      })
      .catch(function (x) {
        if (collect) logBusy(dlg, { state: "idle" });
        logError(dlg, x);
      })
      .finally(function () { buttons.forEach(function (b) { b.disabled = false; }); });
  });
  // «Изменить» у записи: текст становится полем, «Сохранить» / «Отмена» (Esc), ⌘/Ctrl+Enter — сохранить
  document.addEventListener("click", function (e) {
    var pen = e.target.closest("[data-log-edit]");
    var cancel = e.target.closest("[data-log-edit-cancel]");
    if (cancel) {
      var li0 = cancel.closest(".log-i");
      li0.querySelector("[data-log-edit-form]").remove();
      li0.classList.remove("editing");
      return;
    }
    if (!pen) return;
    var li = pen.closest(".log-i");
    var dlg = pen.closest("dialog");
    if (!li || li.querySelector("[data-log-edit-form]")) return;
    var aside = dlg.querySelector("[data-log-task]");
    var form = document.createElement("form");
    form.className = "log-edit";
    form.method = "post";
    form.action = "/commitments/" + aside.dataset.logTask + "/log/" + li.dataset.logId + "/edit";
    form.setAttribute("data-log-edit-form", "");
    var ta = document.createElement("textarea");
    ta.name = "text";
    ta.rows = 3;
    ta.required = true;
    var logT = li.querySelector(".log-t");  // исходный текст: пути в нём показаны кнопками
    ta.value = logT ? (logT.dataset.text != null ? logT.dataset.text : logT.textContent) : "";
    ta.setAttribute("aria-label", "Текст записи");
    var row = document.createElement("div");
    row.className = "log-form-row";
    row.innerHTML = '<span class="grow"></span><button type="button" class="btn sm ghost" data-log-edit-cancel>Отмена</button>'
      + '<button class="btn sm primary">Сохранить</button>';
    form.appendChild(ta);
    form.appendChild(row);
    li.classList.add("editing");
    li.appendChild(form);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  });
  document.addEventListener("keydown", function (e) {
    var form = e.target.closest && e.target.closest("[data-log-form], [data-log-edit-form]");
    if (!form) return;
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      form.requestSubmit();
    } else if (e.key === "Escape" && form.matches("[data-log-edit-form]")) {
      e.preventDefault();  // Esc — отменить правку, а не закрыть окно задачи
      e.stopPropagation();
      form.querySelector("[data-log-edit-cancel]").click();
    }
  });

  // раскрывашки [data-reveal]: «+ связь», «+ пункты» в окне задачи, черновик ответа и напоминание в ленте Сводки
  document.addEventListener("click", function (e) {
    var reveal = e.target.closest("[data-reveal]");
    if (!reveal) return;
    var pane = document.getElementById(reveal.dataset.reveal);
    if (!pane) return;
    e.preventDefault();
    pane.classList.add("m-reveal");  // новый дизайн: панель плавно проявляется (v3-motion.css)
    pane.hidden = !pane.hidden;
    var field = !pane.hidden && pane.querySelector("input[type=text], textarea");
    if (field) field.focus();
  });

  // «Ответить из Сводки» в Переписке ведёт на /#att-<id>: раскрываем черновик этого ответа
  if (/^#att-\d+$/.test(location.hash)) {
    var pane = document.getElementById(location.hash.slice(1));
    if (pane) { pane.hidden = false; pane.closest(".frow").scrollIntoView({ block: "center" }); }
  }

  // плитки срока в форме ([data-date-pick] [data-date]): «Сегодня», «Завтра»… ставят дату в поле due той же формы,
  // повторный щелчок — снять; своя дата снимает подсветку плиток («Сделать задачей» на Сводке)
  var datePickMark = function (form) {
    var input = form.querySelector("input[name=due]");
    var quick = false;
    form.querySelectorAll("[data-date-pick] [data-date]").forEach(function (b) {
      var on = !!input.value && b.dataset.date === input.value;
      quick = quick || on;
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    var own = input.closest(".due-own");
    if (own) own.classList.toggle("on", !!input.value && !quick);
  };
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-date-pick] [data-date]");
    var input = b && b.form && b.form.querySelector("input[name=due]");
    if (!input) return;
    input.value = b.getAttribute("aria-pressed") === "true" ? "" : b.dataset.date;
    datePickMark(b.form);
  });
  document.addEventListener("input", function (e) {
    if (e.target.name === "due" && e.target.form && e.target.form.querySelector("[data-date-pick]")) datePickMark(e.target.form);
  });

  // «Сделать задачей»: «Мне должны» — делает отправитель (он в «Кто делает», «Кому» — пусто), «Моя задача» — обратно
  document.addEventListener("change", function (e) {
    var form = e.target.name === "direction" && e.target.closest("form[data-att-task]");
    if (!form) return;
    var sender = form.dataset.attTask, who = form.elements.who, whom = form.elements.whom;
    if (!sender) return;
    if (e.target.value === "to_me" && !who.value && whom.value === sender) { who.value = sender; whom.value = ""; }
    else if (e.target.value === "mine" && !whom.value && who.value === sender) { whom.value = sender; who.value = ""; }
  });

  // свой MCP-сервер: «Программа на этом компьютере» или «Адрес в интернете» — показываем только нужные поля
  var mcpTr = document.querySelectorAll("[data-mcp-tr]");
  function mcpShow() {
    var on = document.querySelector("[data-mcp-tr]:checked");
    document.querySelectorAll("[data-mcp-show]").forEach(function (box) { box.hidden = !on || box.dataset.mcpShow !== on.value; });
  }
  if (mcpTr.length) { mcpTr.forEach(function (r) { r.addEventListener("change", mcpShow); }); mcpShow(); }

  // «Решения»: ссылка на решение (#d<id>) или вопрос (#q<id>) — раскрываем вопрос и показываем это решение
  if (/^#[dq]\d+$/.test(location.hash)) {
    var dmark = document.getElementById(location.hash.slice(1));
    if (dmark) {
      for (var dp = dmark; dp; dp = dp.parentElement) if (dp.tagName === "DETAILS" && dp.classList.contains("dq")) dp.open = true;
      dmark.scrollIntoView({ block: "center" });
      dmark.classList.add("is-target");
    }
  }

  // «Спросить Claude»: поиск по разговорам — прячем непохожие, пустые дни тоже
  var askFind = document.querySelector("[data-ask-find]");
  if (askFind) askFind.addEventListener("input", function () {
    var q = askFind.value.trim().toLowerCase();
    document.querySelectorAll("[data-ask-item]").forEach(function (li) { li.hidden = !!q && li.dataset.askItem.indexOf(q) < 0; });
    document.querySelectorAll("[data-ask-day]").forEach(function (h) {
      var list = h.nextElementSibling;
      h.hidden = list && !list.querySelector("[data-ask-item]:not([hidden])");
    });
  });

  // справка: у каждого блока кода — «Копировать»
  document.querySelectorAll("[data-help-doc] pre").forEach(function (pre) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn sm copy-code";
    b.textContent = "Копировать";
    b.addEventListener("click", function () {
      var code = pre.querySelector("code") || pre;
      if (navigator.clipboard) navigator.clipboard.writeText(code.innerText).then(function () {
        b.textContent = "Скопировано ✓";
        setTimeout(function () { b.textContent = "Копировать"; }, 1800);
      });
    });
    pre.appendChild(b);
  });

  // лента «Нужно от вас»: фильтр по типу — подтвердить, ответить, напомнить
  document.querySelectorAll("[data-feed-filter]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var kind = chip.dataset.feedFilter;
      document.querySelectorAll("[data-feed-filter]").forEach(function (c) { c.classList.toggle("active", c === chip); });
      document.querySelectorAll(".frow[data-kind]").forEach(function (row) { row.hidden = !!kind && row.dataset.kind !== kind; });
    });
  });

  // «Уточнить срок»: задачи без срока по одной. Быстрая кнопка ставит дату в поле, «Сохранить срок» сохраняет и
  // показывает следующую; «Оставить без срока» — PULSE больше не спросит; «Не сейчас» — дальше без изменений.
  // Закрыли окно после изменений — страница обновляется: счётчики, лента и доска
  var dueDlg = document.querySelector("dialog[data-due]");
  if (dueDlg) {
    var dueCards = Array.prototype.slice.call(dueDlg.querySelectorAll("[data-due-card]"));
    var dueAt = 0, dueDone = { set: 0, skip: 0, later: 0 };
    var dueShow = function (i) {
      dueAt = i;
      dueCards.forEach(function (f, n) { f.hidden = n !== i; });
      var end = i >= dueCards.length;
      dueDlg.querySelector("[data-due-step]").textContent = end ? "" : (i + 1) + " из " + dueCards.length;
      dueDlg.querySelector("[data-due-end]").hidden = !end;
      if (end) {
        var parts = [];
        if (dueDone.set) parts.push("срок поставлен: " + dueDone.set);
        if (dueDone.skip) parts.push("оставлены без срока: " + dueDone.skip);
        if (dueDone.later) parts.push("на потом: " + dueDone.later + " — они останутся в разборе");
        dueDlg.querySelector("[data-due-sum]").textContent = "Готово" + (parts.length ? " · " + parts.join(" · ") : "") + ".";
        dueDlg.querySelector("[data-due-end] [data-dlg-close]").focus();
      } else {
        dueCards[i].querySelector("[data-due-pick]").focus();
      }
    };
    dueCards.forEach(function (form) {
      var input = form.querySelector("input[name=due]");
      var save = form.querySelector("[data-due-save]");
      var err = form.querySelector("[data-due-err]");
      var mark = function () {  // подсветить выбранное и показать дату на кнопке: «Сохранить срок 06.11»
        var quick = false;
        form.querySelectorAll("[data-due-pick]").forEach(function (b) {
          var on = b.dataset.duePick === input.value;
          quick = quick || on;
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
        input.closest(".due-own").classList.toggle("on", !!input.value && !quick);
        save.textContent = input.value ? "Сохранить срок " + input.value.slice(8, 10) + "." + input.value.slice(5, 7) : "Сохранить срок";
      };
      form.querySelectorAll("[data-due-pick]").forEach(function (b) {
        b.addEventListener("click", function () { input.value = b.dataset.duePick; mark(); save.focus(); });
      });
      input.addEventListener("input", mark);
      form.querySelector("[data-due-next]").addEventListener("click", function () { dueDone.later++; dueShow(dueAt + 1); });
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var act = e.submitter && e.submitter.value === "skip" ? "skip" : "save";
        var body = new FormData(form);
        body.set("act", act);
        var buttons = form.querySelectorAll("button");
        buttons.forEach(function (b) { b.disabled = true; });
        err.hidden = true;
        fetch(form.action, { method: "POST", body: body, headers: { Accept: "application/json" } })
          .then(function (r) { return r.json().catch(function () { return { ok: false, err: "" }; }); })
          .then(function (res) {
            if (!res.ok) throw new Error(res.err || "");
            dueDone[act === "skip" ? "skip" : "set"]++;
            dueDlg.dataset.changed = "1";
            dueShow(dueAt + 1);
          })
          .catch(function (x) {  // ответ PULSE — его словами; обрыв связи — своими
            err.textContent = (x.name === "Error" && x.message) || "Не получилось сохранить — попробуйте ещё раз";
            err.hidden = false;
          })
          .finally(function () { buttons.forEach(function (b) { b.disabled = false; }); });
      });
    });
    dueDlg.addEventListener("close", function () { if (dueDlg.dataset.changed) pulseReload(); });
  }

  // «Без движения»: задачи, по которым давно ничего не происходит, по одной (opsdash.quiet). Каждое действие —
  // своя форма: записать в ход, собрать ход с Claude, новый срок, не трогать неделю, уже сделано. Сохраняем без
  // перезагрузки и показываем следующую; «Напомнить» — обычная форма напоминания (уйдёт со страницы, как на Сводке).
  // Закрыли окно после изменений — страница обновляется
  var qDlg = document.querySelector("dialog[data-quiet]");
  if (qDlg) {
    var qCards = Array.prototype.slice.call(qDlg.querySelectorAll("[data-quiet-card]"));
    var qAt = 0, qDone = { note: 0, collect: 0, due: 0, snooze: 0, done: 0, later: 0 };
    var qShow = function (i) {
      qAt = i;
      qCards.forEach(function (c, n) { c.hidden = n !== i; });
      var end = i >= qCards.length;
      qDlg.querySelector("[data-quiet-step]").textContent = end ? "" : (i + 1) + " из " + qCards.length;
      qDlg.querySelector("[data-quiet-end]").hidden = !end;
      if (end) {
        var parts = [];
        if (qDone.note) parts.push("записано в ход: " + qDone.note);
        if (qDone.collect) parts.push("Claude собирает ход: " + qDone.collect + " — появится уведомление");
        if (qDone.due) parts.push("новый срок: " + qDone.due);
        if (qDone.snooze) parts.push("отложено на неделю: " + qDone.snooze);
        if (qDone.done) parts.push("закрыто: " + qDone.done);
        if (qDone.later) parts.push("на потом: " + qDone.later);
        qDlg.querySelector("[data-quiet-sum]").textContent = "Готово" + (parts.length ? " · " + parts.join(" · ") : "") + ".";
        qDlg.querySelector("[data-quiet-end] [data-dlg-close]").focus();
      } else {
        var first = qCards[i].querySelector("input[name=text]");
        if (first) first.focus();
      }
    };
    qCards.forEach(function (card) {
      var err = card.querySelector("[data-quiet-err]");
      card.querySelectorAll("form[data-quiet-form]").forEach(function (form) {
        form.addEventListener("submit", function (e) {
          e.preventDefault();
          var act = form.dataset.quietForm, buttons = card.querySelectorAll("button");
          buttons.forEach(function (b) { b.disabled = true; });
          err.hidden = true;
          fetch(form.action, { method: "POST", body: new FormData(form), headers: { Accept: "application/json" } })
            .then(function (r) { return r.json().catch(function () { return { ok: r.ok, err: "" }; }); })
            .then(function (res) {
              if (!res.ok) throw new Error(res.err || "");
              qDone[act]++;
              qDlg.dataset.changed = "1";
              qShow(qAt + 1);
            })
            .catch(function (x) {
              err.textContent = (x.name === "Error" && x.message) || "Не получилось сохранить — попробуйте ещё раз";
              err.hidden = false;
            })
            .finally(function () { buttons.forEach(function (b) { b.disabled = false; }); });
        });
      });
      card.querySelector("[data-quiet-next]").addEventListener("click", function () { qDone.later++; qShow(qAt + 1); });
    });
    qDlg.addEventListener("close", function () { if (qDlg.dataset.changed) pulseReload(); });
  }

  // закрываемые подсказки: «×» прячет и запоминает (cookie на год)
  document.querySelectorAll("[data-dismiss-btn]").forEach(function (b) {
    b.addEventListener("click", function () {
      var box = b.closest("[data-dismiss]");
      box.hidden = true;
      document.cookie = box.dataset.dismiss + "=1; path=/; max-age=31536000; samesite=lax";
    });
  });

  // меню «…» и панель «Фильтр»: открыто одно, закрываются кликом мимо
  document.addEventListener("click", function (e) {
    var m = e.target.closest("details.menu");
    closeMenus(m);
    if (m && e.target.closest(".menu-i")) setTimeout(function () { m.open = false; }, 0);
  });

  // статус в окне задачи: Новая / В работе / Ждёт / Готово — сразу сохраняется. «Ждёт» у задачи без связи —
  // открываем «+ связь»: нужно сказать, чего она ждёт
  document.querySelectorAll("[data-task-state]").forEach(function (sel) {
    var was = sel.value;
    sel.addEventListener("change", function () {
      if (sel.value === "wait" && sel.dataset.blocked !== "1") {
        var pane = sel.closest("dialog").querySelector("[id^=link-]");
        if (pane) { pane.hidden = false; pane.querySelector("input[type=text]").focus(); }
        sel.value = was;
        return;
      }
      sel.form.submit();
    });
  });

  // окна по кнопке ([data-dlg-open]): «+ Задача»; «+» в колонке доски подставляет тип задачи
  var openDlg = function (dlg, dir) {
    if (!dlg) return;
    var sel = dir && dlg.querySelector("select[name=direction]");
    if (sel) sel.value = dir;
    if (!dlg.open) dlg.showModal();
    var first = dlg.querySelector("input[name=title], input[type=text]");
    if (first) first.focus();
  };
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-dlg-open]");
    if (!b) return;
    e.preventDefault();
    openDlg(document.getElementById(b.dataset.dlgOpen), b.dataset.addDir);
    var pn = b.dataset.personName;  // «Claude заметил новых людей»: имя — сразу в форму
    var pi = pn && document.querySelector("#person-new [data-person-name]");
    if (pi) pi.value = pn;
    var t = b.dataset.uploadTitle;  // «Загрузить запись» у встречи календаря — с её названием и связью с ней
    var ti = t !== undefined && document.querySelector("[data-upload-title-input]");
    if (ti) ti.value = t;
    var ui = document.querySelector("[data-upload-uid-input]");
    if (ui) ui.value = b.dataset.uploadUid || "";
  });

  // зона «Перетащите запись сюда»: файл бросили или выбрали — сразу загружаем
  document.querySelectorAll("[data-drop]").forEach(function (form) {
    var input = form.querySelector("[data-drop-input]");
    input.addEventListener("change", function () { if (input.files.length) form.submit(); });
    ["dragenter", "dragover"].forEach(function (ev) {
      form.addEventListener(ev, function (e) { e.preventDefault(); form.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      form.addEventListener(ev, function (e) { e.preventDefault(); form.classList.remove("over"); });
    });
    form.addEventListener("drop", function (e) {
      if (!e.dataTransfer.files.length) return;
      input.files = e.dataTransfer.files;
      form.submit();
    });
  });
  document.querySelectorAll("dialog[data-open-on-load]:not(#add-dlg)").forEach(function (dlg) {
    if (dlg.querySelector(".dlg-view") && dlg.querySelector(".dlg-edit")) showTask(dlg, dlg.hasAttribute("data-edit-on-load"));
    else openDlg(dlg);
  });
  var addDlg = document.getElementById("add-dlg");
  if (addDlg && (location.hash === "#add" || addDlg.hasAttribute("data-open-on-load"))) {
    openDlg(addDlg);
    history.replaceState(null, "", location.pathname + location.search);
  }

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

  // доска задач: стопка — задачи одного проекта в колонке. Щелчок (Enter, пробел) по ней раскрывает задачи под ней,
  // второй — сворачивает. Раскрытые помним в cookie («mine-3» — колонка и проект): сервер сразу отдаёт их раскрытыми
  var openStack = function (stack, open) {
    var head = stack.querySelector("[data-stack-toggle]");
    var box = document.getElementById(head.getAttribute("aria-controls"));
    stack.classList.toggle("open", open);
    head.setAttribute("aria-expanded", open ? "true" : "false");
    if (box) box.hidden = !open;
  };
  var saveStacks = function () {
    var open = Array.prototype.map.call(document.querySelectorAll(".stack.open[data-stack]"), function (s) { return s.dataset.stack; });
    document.cookie = "kb_stacks=" + open.join(".") + "; path=/; max-age=31536000; samesite=lax";
  };
  document.querySelectorAll("[data-stack-toggle]").forEach(function (head) {
    var flip = function () {
      var stack = head.closest("[data-stack]");
      openStack(stack, !stack.classList.contains("open"));
      saveStacks();
    };
    head.addEventListener("click", flip);
    head.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); }
    });
  });

  // «Задачи» по ссылке ?task=<id> (со встречи, со сводки): показать задачу на доске и открыть её окно.
  // Из адреса ссылку убираем — после «Готово» или «Сохранить» окно не должно открываться снова
  var focus = document.querySelector("[data-focus-task]");
  if (focus) {
    var fid = focus.dataset.focusTask;
    var spot = document.querySelector('.task[data-task="' + fid + '"], li[data-task="' + fid + '"], .gantt-bar[data-task-open="' + fid + '"]');
    var far = spot && spot.closest(".later-items[hidden]");  // задача с дальним сроком — раскрыть её плашку
    if (far) showLater(laterBtn(far.id), true);
    var pile = spot && spot.closest(".stack:not(.open)");  // задача в свёрнутой стопке — раскрыть стопку (не запоминая)
    if (pile) openStack(pile, true);
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

  // подтверждение перед отправкой формы; у кнопки свой data-confirm («Уже напомнили» в форме «Отправить») —
  // спрашиваем её текст, пустой — не спрашиваем
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      var ask = e.submitter && e.submitter.hasAttribute("data-confirm") ? e.submitter.dataset.confirm : form.dataset.confirm;
      if (ask && !window.confirm(ask)) e.preventDefault();
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

  // пути к файлам в текстах (filepaths.py): щелчок — показать в Finder / Проводнике, значок — скопировать полный путь.
  // В фазе захвата: путь бывает внутри строки задачи или карточки, которые сами открываются по щелчку
  function fpathTip(box, text, bad) {
    var old = box.querySelector(".fpath-tip");
    if (old) old.remove();
    var tip = document.createElement("span");
    tip.className = "fpath-tip" + (bad ? " bad" : "");
    tip.setAttribute("role", "status");
    tip.textContent = text;
    box.appendChild(tip);
    var over = tip.getBoundingClientRect().right - (window.innerWidth - 8);  // у правого края окна — сдвигаем влево
    if (over > 0) tip.style.left = -over + "px";
    setTimeout(function () { tip.remove(); }, bad ? 3500 : 1800);
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".fpath-go, .fpath-copy");
    if (!btn) return;
    var box = btn.closest("[data-fpath]");
    if (!box) return;
    e.preventDefault();
    e.stopPropagation();
    var path = box.dataset.fpath;
    if (btn.classList.contains("fpath-copy")) {
      var done = function () { fpathTip(box, "Путь скопирован"); };
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(path).then(done, function () { fpathTip(box, path, true); });
      else { var ta = document.createElement("textarea"); ta.value = path; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); done(); }
      return;
    }
    var body = new FormData();
    body.append("path", path);
    fetch("/files/reveal", { method: "POST", body: body }).then(function (r) { return r.json(); }).then(function (res) {
      fpathTip(box, res.ok ? res.msg : res.err, !res.ok);
    }, function () { fpathTip(box, "PULSE не ответил — попробуйте ещё раз", true); });
  }, true);

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
      liveBox.setAttribute("aria-busy", "true");  // прежние результаты бледнеют, пока ищем (v3-motion.css)
      fetch(url).then(function (r) { return r.text(); }).then(function (html) {
        if (my !== liveSeq) return;  // пока ждали ответа, запрос уже поменялся
        var fresh = new DOMParser().parseFromString(html, "text/html").querySelector("[data-live-results]");
        if (!fresh) return;
        liveBox.replaceWith(fresh);
        liveBox = fresh;
        history.replaceState(null, "", url);
      }).catch(function () { /* не ответил — оставляем прежние результаты */ }).then(function () {
        if (my === liveSeq) liveBox.removeAttribute("aria-busy");
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
      // окно «Новая встреча» (#new-dlg) — модальное: открываем его; старая разметка без окна — по якорю #new
      var newDlg = document.getElementById("new-dlg");
      if (newDlg) openDlg(newDlg);
      else window.location.hash = "new";
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
          var job = waitBuild.dataset.waitJob || "update";
          var busy = st.running.indexOf(job) >= 0 || st.queued.indexOf(job) >= 0;
          idle = busy ? 0 : idle + 1;
          if (idle >= 4) {
            clearInterval(poll);
            waitBuild.className = "alert bad";
            waitBuild.textContent = job === "restart" ? "PULSE не перезапустился — закройте его и откройте снова (или START)."
              : "Обновление не удалось — подробности в «Журнале» (задание «Обновление PULSE»).";
          }
        });
      }).catch(function () { idle = 0; /* перезапускается — подождём */ });
      if (Date.now() - started > 10 * 60000) {
        clearInterval(poll);
        waitBuild.textContent = "Обновление идёт дольше обычного — загляните в «Журнал».";
      }
    }, 3000);
  }

  // окно «У PULSE новый дизайн» — один раз: как закрыли («Круто», Esc или клик мимо окна), сервер его больше не отдаёт
  var hello = document.querySelector("[data-design-intro]");
  if (hello && hello.showModal) {
    hello.addEventListener("close", function () {
      fetch(hello.getAttribute("data-seen-url") || "/api/design-intro/seen", { method: "POST" });
    }, { once: true });
    hello.showModal();
  }

  // подсказка справа внизу: «Позже» прячет её на неделю
  document.querySelectorAll("[data-toast-close]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var toast = btn.closest("[data-toast]");
      toast.hidden = true;
      document.cookie = "voice_tip=" + encodeURIComponent(toast.dataset.toast) + "; path=/; max-age=604800; samesite=lax";
    });
  });

  // --- Подключения: окно источника поверх каталога (CON-3). Старые ссылки «Почта#gmail» и «Календарь#google» —
  // в окна Gmail и Google Календаря; закрыли окно — адрес снова у каталога, чтобы «Обновить» не открывало его опять
  var connDlg = document.getElementById("conn-dlg");
  if (connDlg) {
    var oldHash = { mail: { "#gmail": "gmail" }, calendar: { "#google": "google_calendar" },
                    claude: { "#chatgpt": "chatgpt" } }[connDlg.dataset.conn];
    if (oldHash && oldHash[location.hash]) location.replace("/settings?tab=" + oldHash[location.hash]);
    connDlg.addEventListener("close", function () { history.replaceState(null, "", "/settings?tab=connections"); });
    // окно открывается сверху — с описанием источника и шагами, а не прокрученным к первому полю формы
    if (connDlg.contains(document.activeElement)) document.activeElement.blur();
    connDlg.scrollTop = 0;
    var anchor = location.hash.length > 1 && connDlg.querySelector(location.hash.replace(/[^#\w-]/g, ""));
    if (anchor) anchor.scrollIntoView({ block: "start" });  // «Расход Claude» (#usage) — сразу к нему
  }

  // --- «Нет» — почему? (MOD-3): причина кнопкой; «другое» — словами (Enter); ✕ — не отвечать ---
  var whyBar = document.querySelector("[data-why]");
  if (whyBar) {
    var whySend = function (reason, note) {
      var body = new FormData();
      body.append("reason", reason);
      body.append("note", note || "");
      fetch("/why/" + whyBar.dataset.why, { method: "POST", body: body }).then(function () {
        whyBar.innerHTML = "<span class=\"why-t\">Спасибо — учту. Повторяющиеся причины станут правилом после вашего «Да».</span>";
        setTimeout(function () { whyBar.remove(); }, 2500);
      });
    };
    var whyNote = whyBar.querySelector("[data-why-note]");
    whyBar.addEventListener("click", function (e) {
      var b = e.target.closest("[data-why-reason]");
      if (b) {
        if (b.dataset.whyReason === "other" && whyNote.hidden) { whyNote.hidden = false; whyNote.focus(); return; }
        whySend(b.dataset.whyReason, whyNote.value);
      }
      if (e.target.closest("[data-why-close]")) whyBar.remove();
    });
    whyNote.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); whySend("other", whyNote.value); } });
    history.replaceState(null, "", location.href.replace(/([?&])why=\d+&?/, "$1").replace(/[?&]$/, ""));
  }

  // --- Навыки: поиск по названию и описанию, фильтр по разделам ---
  var skSearch = document.querySelector("[data-sk-search]");
  if (skSearch) {
    var skArea = "";
    var skApply = function () {
      var q = skSearch.value.trim().toLowerCase(), shown = 0;
      document.querySelectorAll("[data-sk-area]").forEach(function (sec) {
        var n = 0;
        sec.querySelectorAll("[data-sk-text]").forEach(function (card) {
          var ok = (!skArea || sec.dataset.skArea === skArea) && (!q || card.dataset.skText.indexOf(q) >= 0);
          card.hidden = !ok;
          if (ok) n++;
        });
        sec.hidden = !n;
        shown += n;
      });
      document.querySelector("[data-sk-empty]").hidden = shown > 0;
    };
    skSearch.addEventListener("input", skApply);
    document.querySelectorAll("[data-sk-areas] [data-area]").forEach(function (b) {
      b.addEventListener("click", function () {
        skArea = b.dataset.area;
        document.querySelectorAll("[data-sk-areas] [data-area]").forEach(function (x) { x.classList.toggle("active", x === b); });
        skApply();
      });
    });
  }

  // --- мастер первого запуска: на узком экране главы — полосой; текущая — в зоне видимости ---
  var wizActive = document.querySelector(".wiz-nav .wiz-step.active");
  if (wizActive && wizActive.parentElement.scrollWidth > wizActive.parentElement.clientWidth) {
    wizActive.parentElement.scrollLeft = wizActive.offsetLeft - 16;
  }

  // --- мастер первого запуска: часовой пояс — из браузера, пока имя не указано (свежая установка) ---
  var tzInput = document.querySelector("[data-tz-guess]");
  if (tzInput) {
    var guess = "";
    try { guess = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { guess = ""; }
    var nameInput = tzInput.form.querySelector("[name=name]");
    if (guess && guess !== tzInput.value) {
      if (nameInput && !nameInput.value) tzInput.value = guess;
      else {
        var hint = tzInput.form.querySelector("[data-tz-hint]");
        if (hint) hint.textContent = "На этом компьютере: " + guess;
      }
    }
  }

  // идёт первый разбор после мастера — раз в 5 секунд: готово (или не получилось) — обновить Сводку
  if (document.querySelector("[data-first-run]")) {
    var firstTimer = setInterval(function () {
      fetch("/welcome/status").then(function (r) { return r.json(); }).then(function (res) {
        if (res.status !== "queued" && res.status !== "running") { clearInterval(firstTimer); pulseReload(); }
      }).catch(function () { /* PULSE перезапускается — спросим в следующий раз */ });
    }, 5000);
  }

  // «Что PULSE уже нашёл» на Сводке: закрыли — больше не показываем (ONB-3)
  var finds = document.querySelector("dialog[data-first-finds]");
  if (finds) finds.addEventListener("close", function () { fetch("/welcome/seen", { method: "POST" }); }, { once: true });

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
        else if (res.state === "password") window.location.href = location.pathname === "/welcome"
          ? "/welcome?step=messages&open=telegram&need=password" : "/settings?tab=telegram&need=password";
        else if (res.state !== qrBox.dataset.tgQr) pulseReload();
      });
    }, 2000);
  }

  // --- обновлять страницу, пока идёт расшифровка/разбор ---
  var watching = Array.prototype.map.call(document.querySelectorAll("[data-meeting-status]"), function (n) { return n.dataset.meetingStatus; });
  var busy = Array.prototype.some.call(document.querySelectorAll("[data-meeting-status]"), function (n) {
    return /очеред|расшифровк|разбор/.test(n.textContent);
  });
  // запись нашлась в папке, но встречи ещё нет — ждём, когда PULSE её возьмёт
  var waitingRec = !!document.querySelector("[data-waiting]");
  if ((watching.length && busy) || waitingRec) {
    var last = null;
    setInterval(function () {
      fetch(waitingRec ? "/api/status?waiting=1" : "/api/status").then(function (r) { return r.json(); }).then(function (res) {
        var state = JSON.stringify([res.meetings.map(function (m) { return [m.id, m.status, Math.round((m.progress || 0) * 20)]; }), res.waiting || []]);
        if (last !== null && state !== last) pulseReload();
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
    // поле растёт вместе с текстом (до ~10 строк), как в Claude
    var grow = function () {
      askText.style.height = "auto";
      askText.style.height = Math.min(askText.scrollHeight, 260) + "px";
      askText.style.overflowY = askText.scrollHeight > 260 ? "auto" : "hidden";
    };
    askText.addEventListener("input", grow);
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
      send.disabled = true;
      send.title = "Отправляю…";
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-ask-example]"), function (b) {
      b.addEventListener("click", function () { askText.value = b.dataset.askExample; grow(); askText.focus(); });
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
      grow();
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
        if (!res.running) { clearInterval(askPoll); pulseReload(); return; }
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

  // --- «Офис»: тело вкладки перезапрашиваем, пока страница на экране: кто-то работает — каждые 3 с, иначе раз в 15 с.
  // На скрытой вкладке не спрашиваем вовсе; открытые «История» и «Шаги» после обновления остаются открытыми
  var officeBox = document.querySelector("[data-office]");
  if (officeBox) {
    // какие спойлеры офисов раскрыты — помним в браузере (только удобство: без хранилища все свёрнуты)
    var remembered = {};
    try { remembered = JSON.parse(localStorage.getItem("office-open") || "{}") || {}; } catch (e) { remembered = {}; }
    Array.prototype.forEach.call(officeBox.querySelectorAll("details[data-remember]"), function (d) { if (remembered[d.dataset.key]) d.open = true; });
    officeBox.addEventListener("toggle", function (e) {
      var d = e.target;
      if (!d.matches || !d.matches("details[data-remember]")) return;
      remembered[d.dataset.key] = d.open;
      try { localStorage.setItem("office-open", JSON.stringify(remembered)); } catch (err) { /* не запоминаем */ }
    }, true);
    // плашки офисов: показываем список выбранного офиса на месте — без перезагрузки, картинка остаётся как была.
    // Адрес и запрос обновления (data-live) — уже с новым офисом, чтобы обновление не вернуло прежний
    Array.prototype.forEach.call(document.querySelectorAll("[data-office-tab]"), function (tab) {
      tab.addEventListener("click", function (e) {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;  // в новой вкладке — как обычная ссылка
        e.preventDefault();
        var key = tab.dataset.officeTab;
        Array.prototype.forEach.call(document.querySelectorAll("[data-office-tab]"), function (t) {
          var on = t === tab;
          t.classList.toggle("active", on);
          if (on) t.setAttribute("aria-current", "true"); else t.removeAttribute("aria-current");
        });
        Array.prototype.forEach.call(officeBox.querySelectorAll(".office-group[data-key]"), function (g) { g.hidden = g.dataset.key !== "g-" + key; });
        officeBox.dataset.live = "/office/live?o=" + encodeURIComponent(key);
        try { history.replaceState(null, "", "?o=" + encodeURIComponent(key)); } catch (err) { /* адрес не меняем */ }
      });
    });
    var officeTimer = null;
    var officeActive = function () {
      var mark = officeBox.querySelector("[data-office-active]");
      return mark && mark.dataset.officeActive === "1";
    };
    var officeNext = function () {
      clearTimeout(officeTimer);
      officeTimer = setTimeout(officeRefresh, officeActive() ? 3000 : 15000);
    };
    var officeRefresh = function () {
      if (document.visibilityState !== "visible") return;  // вернёмся — обновим сразу (visibilitychange)
      fetch(officeBox.dataset.live, { cache: "no-store" }).then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.text();
      }).then(function (html) {
        var open = Array.prototype.map.call(officeBox.querySelectorAll("details[open][data-key]"), function (d) { return d.dataset.key; });
        officeBox.innerHTML = html;
        open.forEach(function (key) {
          Array.prototype.forEach.call(officeBox.querySelectorAll("details[data-key]"), function (d) { if (d.dataset.key === key) d.open = true; });
        });
        officeNext();
      }).catch(function () { clearTimeout(officeTimer); officeTimer = setTimeout(officeRefresh, 30000); });
    };
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") officeRefresh(); });
    officeNext();
  }

  // --- угол экрана: что PULSE делает сейчас и что сделал. Видно три последних, остальные — «Ещё N».
  // Обновляется сам: пока что-то идёт — каждые 5 с, иначе раз в 15 с
  var nbox = document.querySelector("[data-notices]");
  if (nbox) {
    var nlist = nbox.querySelector("[data-notice-list]"), nfoot = nbox.querySelector("[data-notice-foot]"),
        nhead = nbox.querySelector("[data-notice-head]"), nmore = nbox.querySelector("[data-notice-more]"), nnow = nbox.querySelector("[data-notice-now]"),
        nnowText = nbox.querySelector("[data-notice-now-text]"), nnowAct = nbox.querySelector("[data-notice-now-act]"),
        nnowLink = nbox.querySelector("[data-notice-now-link]");
    nnowAct.dataset.html = nnowAct.innerHTML.trim().replace(/\s+/g, " ");
    // «Остановить» расшифровку в углу: кнопки приходят с сервером на лету — подтверждение ловим здесь, а не data-confirm
    nbox.addEventListener("submit", function (e) {
      var ask = e.target.dataset && e.target.dataset.trConfirm;
      if (ask && !window.confirm(ask)) e.preventDefault();
    });
    // на телефоне и в «Спросить Claude» (там внизу строка вопроса) — одно последнее, на остальных страницах — три
    var SHOWN = document.body.classList.contains("page-ask") || window.innerWidth <= 760 ? 1 : 3;
    var ago = function (iso) {
      var s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
      if (s < 60) return "только что";
      if (s < 3600) return Math.floor(s / 60) + " мин назад";
      if (s < 86400) return Math.floor(s / 3600) + " ч назад";
      return Math.floor(s / 86400) + " дн назад";
    };
    var layout = function () {
      var items = nlist.querySelectorAll("[data-notice]:not(.leaving)");  // уезжающее уже не считаем — его место займёт следующее
      Array.prototype.forEach.call(items, function (el, i) { el.classList.toggle("extra", i >= SHOWN); });
      var more = Math.max(0, items.length - SHOWN);
      if (!more) nbox.classList.remove("open");
      nmore.hidden = !more;
      nmore.textContent = nbox.classList.contains("open") ? "Свернуть" : "Ещё " + more;
      nfoot.hidden = !more;
      nhead.hidden = items.length < 2;
      nlist.hidden = !nlist.querySelector("[data-notice]");
    };
    // скрыть: уезжает вправо и тает, затем схлопывается (style.css: .leaving, .gone)
    var leave = function (el) {
      if (el.classList.contains("leaving")) return;
      el.style.height = el.offsetHeight + "px";
      el.classList.add("leaving");
      layout();
      setTimeout(function () { el.classList.add("gone"); }, 200);
      setTimeout(function () { el.remove(); layout(); }, 380);
    };
    var dismiss = function (el) {
      leave(el);
      return fetch("/api/notices/" + el.dataset.notice + "/dismiss", { method: "POST", keepalive: true });
    };
    var make = function (n) {
      var el = document.createElement("div");
      el.className = "notice m-arrive " + (n.level || "info");  // m-arrive — въезжает сбоку (v3-motion.css)
      el.dataset.notice = n.id;
      el.dataset.key = n.kind + "|" + n.title;
      var body = document.createElement("div");
      body.className = "notice-b";
      var t = document.createElement(n.link ? "a" : "span");
      t.className = "notice-t";
      t.textContent = n.title;
      if (n.link) t.href = n.link;
      body.appendChild(t);
      if (n.text) {
        var x = document.createElement("div");
        x.className = "notice-x";
        x.textContent = n.text;
        body.appendChild(x);
      }
      var tm = document.createElement("div");
      tm.className = "notice-time";
      tm.dataset.ts = n.created;
      tm.textContent = ago(n.created);
      body.appendChild(tm);
      var close = document.createElement("button");
      close.type = "button"; close.className = "notice-close"; close.title = "Скрыть"; close.textContent = "✕";
      close.setAttribute("data-notice-close", ""); close.setAttribute("aria-label", "Скрыть");
      el.appendChild(body);
      el.appendChild(close);
      return el;
    };
    nbox.addEventListener("click", function (e) {
      var el = e.target.closest("[data-notice]"), link = el && el.querySelector("a.notice-t");
      if (e.target.closest("[data-notice-close]")) {
        dismiss(el);
      } else if (link && !el.classList.contains("leaving")) {
        // клик по уведомлению — открываем, куда оно ведёт, а само скрываем: прочитано
        var href = link.href, newTab = e.ctrlKey || e.metaKey || e.shiftKey;
        var sent = dismiss(el);
        if (newTab) {
          if (!e.target.closest("a")) window.open(href, "_blank");  // по ссылке браузер откроет вкладку сам
          return;
        }
        e.preventDefault();
        // уходим, когда уведомление уехало и сервер его скрыл — иначе на новой странице оно покажется снова
        var went = false, go = function () { if (!went) { went = true; pulseGo(href); } };
        Promise.all([sent.catch(function () {}), new Promise(function (r) { setTimeout(r, 220); })]).then(go);
        setTimeout(go, 1500);  // сервер не ответил — всё равно переходим
      } else if (e.target.closest("[data-notice-clear]")) {
        fetch("/api/notices/dismiss-all", { method: "POST" });
        Array.prototype.forEach.call(nlist.querySelectorAll("[data-notice]"), function (n, i) {
          if (n.offsetHeight) setTimeout(function () { leave(n); }, i * 50);  // видимые уезжают по очереди
          else n.remove();
        });
        layout();
      } else if (e.target.closest("[data-notice-more]")) {
        nbox.classList.toggle("open");
        layout();
      }
    });
    var refreshNotices = function () {
      fetch("/api/notices?after=" + (nbox.dataset.last || 0), { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (res) {
        res.items.slice().reverse().forEach(function (n) {  // свежие — наверх
          Array.prototype.forEach.call(nlist.querySelectorAll("[data-key]"), function (old) {
            if (old.dataset.key === n.kind + "|" + n.title) old.remove();  // та же ошибка снова — обновилась, не копия
          });
          nlist.insertBefore(make(n), nlist.firstChild);
        });
        if (res.items.length) nbox.dataset.last = res.items[0].id;
        var now = res.running || [];
        nnow.hidden = !now.length;
        nnowText.textContent = now.map(function (r) { return r.title + (r.detail ? " — " + r.detail : ""); }).join("; ");
        // идёт расшифровка — рядом кнопки паузы и остановки (разметку присылает сервер), ссылка — во «Встречи»
        var trHtml = (res.tr_html || "").replace(/\s+/g, " ");  // те же кнопки — не пересоздаём (курсор, фокус)
        if (nnowAct.dataset.html !== trHtml) { nnowAct.innerHTML = res.tr_html || ""; nnowAct.dataset.html = trHtml; }
        nnow.classList.toggle("tr-paused", !!(res.tr && res.tr.paused));
        nnowLink.href = res.tr_only ? "/meetings" : "/office";
        nnowLink.title = res.tr_only ? "Открыть «Встречи»: вся очередь расшифровки" : "Открыть «Офис»: что делает Claude по шагам";
        Array.prototype.forEach.call(nlist.querySelectorAll("[data-ts]"), function (el) { el.textContent = ago(el.dataset.ts); });
        layout();
        setTimeout(refreshNotices, now.length ? 5000 : 15000);
      }).catch(function () { setTimeout(refreshNotices, 30000); });
    };
    // в «Спросить Claude» внизу поле вопроса: встаём над ним, только если угол на него заходит. На широком экране
    // поле по центру и до угла не достаёт — тогда угол внизу, как везде. На телефоне угол наверху (style.css)
    var dock = document.querySelector(".ask-dock");
    if (dock) {
      var place = function () {
        nbox.style.bottom = "";
        if (window.innerWidth <= 760) return;
        var d = dock.getBoundingClientRect(), b = nbox.getBoundingClientRect();
        nbox.style.bottom = (b.left < d.right && b.right > d.left ? window.innerHeight - d.top + 10 : 20) + "px";
      };
      place();
      window.addEventListener("resize", place);
      if (window.ResizeObserver) new ResizeObserver(place).observe(dock);  // поле растёт вместе с текстом
    }
    layout();
    setTimeout(refreshNotices, nnow.hidden ? 15000 : 5000);
  }
})();
