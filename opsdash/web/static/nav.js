// Переходы без перезагрузки (новый дизайн). Ссылки и формы PULSE не перезагружают окно: страница приходит с
// сервера тем же HTML, что и раньше, а на экране меняется только содержимое — меню стоит, прошлый экран мягко
// уходит, новый проявляется. После действия на той же странице («Готово», «Сохранить») прокрутка остаётся на
// месте, исчезнувшие карточки гаснут, остальные плавно съезжают на новые места, новые проявляются.
// Сервер ничего об этом не знает: если что-то пошло не так, PULSE просто открывает страницу обычным переходом.
// Скрипты страницы (app.js и др.) после смены экрана запускаются заново; обработчики на document и window,
// таймеры и кадры анимации прежнего экрана nav.js перед этим снимает, чтобы ничего не работало дважды.
// Классический дизайн не затронут (там body без d3). Ссылка или форма внутри [data-nav-full] — обычный переход.
(function () {
  "use strict";
  if (window.pulseNav) return;
  var body = document.body;
  var hardGo = function (url) { location.href = url; };
  window.pulseGo = hardGo;
  window.pulseReload = function () { location.reload(); };
  if (!body.classList.contains("d3") || !window.fetch || !window.DOMParser || !history.pushState || !window.Promise
      || !window.URL || !window.URLSearchParams || !window.Map || !window.Set) return;

  // ---------- учёт того, что повесили скрипты экрана ----------
  var page = { on: [], timeouts: new Set(), intervals: new Set(), frames: new Set() };
  var own = 0;  // > 0 — регистрирует сам nav.js: не учитываем
  var ET = EventTarget.prototype, nAdd = ET.addEventListener, nRemove = ET.removeEventListener;
  var nTimeout = window.setTimeout, nInterval = window.setInterval, nFrame = window.requestAnimationFrame;
  var nClearT = window.clearTimeout, nClearI = window.clearInterval, nCancelF = window.cancelAnimationFrame;
  var mine = function (fn) { own++; try { return fn(); } finally { own--; } };

  ET.addEventListener = function (type, fn, opts) {
    if (!own && fn && (this === document || this === window || this === body)) {
      page.on.push([this, type, fn, opts]);
      // экран сменился без загрузки: «load» и «DOMContentLoaded» уже были — вызываем сразу после скрипта
      var late = (this === window && type === "load" && document.readyState === "complete")
        || (this === document && type === "DOMContentLoaded" && document.readyState !== "loading");
      if (late && swapped) {
        var target = this;
        window.setTimeout(function () {
          var ev = new Event(type);
          if (typeof fn === "function") fn.call(target, ev); else if (fn.handleEvent) fn.handleEvent(ev);
        }, 0);
      }
    }
    return nAdd.call(this, type, fn, opts);
  };
  window.setTimeout = function (fn, ms) {
    var args = Array.prototype.slice.call(arguments, 2), set = page.timeouts, id;
    id = nTimeout.call(window, function () { set.delete(id); if (typeof fn === "function") fn.apply(window, args); }, ms);
    if (!own) set.add(id);
    return id;
  };
  window.setInterval = function () {
    var id = nInterval.apply(window, arguments);
    if (!own) page.intervals.add(id);
    return id;
  };
  window.clearInterval = function (id) { page.intervals.delete(id); return nClearI.call(window, id); };
  if (nFrame) {
    window.requestAnimationFrame = function (fn) {
      var set = page.frames, id;
      id = nFrame.call(window, function (t) { set.delete(id); fn(t); });
      if (!own) set.add(id);
      return id;
    };
  }
  var swapped = false;

  function teardown() {
    page.on.forEach(function (l) { nRemove.call(l[0], l[1], l[2], l[3]); });
    page.timeouts.forEach(function (id) { nClearT.call(window, id); });
    page.intervals.forEach(function (id) { nClearI.call(window, id); });
    if (nCancelF) page.frames.forEach(function (id) { nCancelF.call(window, id); });
    page = { on: [], timeouts: new Set(), intervals: new Set(), frames: new Set() };
  }

  // ---------- анимации: как в настройках ----------
  var motionOn = function () {
    if (body.classList.contains("motion-off")) return false;
    if (body.classList.contains("motion-normal") || body.classList.contains("motion-fast")) return true;
    return !(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  };
  var dur = function (ms) { return body.classList.contains("motion-fast") ? ms / 2 : ms; };

  // полоска загрузки сверху — если сервер отвечает дольше 150 мс. Живёт в <html>, а не в body: body меняется
  var bar = document.createElement("div");
  bar.className = "nav-bar";
  bar.setAttribute("aria-hidden", "true");
  document.documentElement.appendChild(bar);
  var barTimer = null;
  function progress(on) {
    nClearT.call(window, barTimer);
    if (on) {
      barTimer = nTimeout.call(window, function () { bar.className = "nav-bar run"; }, 150);
    } else if (bar.classList.contains("run")) {
      bar.className = "nav-bar run done";
      barTimer = nTimeout.call(window, function () { bar.className = "nav-bar"; }, 260);
    }
  }

  // ---------- карточки: какие ушли, какие пришли, какие сдвинулись ----------
  var ITEMS = ".task[data-task], [data-item], .frow, .notice[data-notice]";
  function keyOf(el) {
    if (el.hasAttribute("data-task")) return "t" + el.getAttribute("data-task");
    if (el.hasAttribute("data-item")) return "i" + el.getAttribute("data-item");
    if (el.hasAttribute("data-notice")) return "n" + el.getAttribute("data-notice");
    var h = el.querySelector("h3, .h3-ttl");
    return h ? "f" + el.getAttribute("data-kind") + "|" + h.textContent.trim() : "";
  }
  function itemMap(root) {
    var m = new Map();
    Array.prototype.forEach.call(root.querySelectorAll(ITEMS), function (el) {
      if (el.closest("dialog, template")) return;
      var k = keyOf(el);
      if (k && !m.has(k)) m.set(k, el);
    });
    return m;
  }
  var onScreen = function (r) { return r.height > 0 && r.bottom > -40 && r.top < innerHeight + 40; };

  // ---------- заголовок и стили новой страницы ----------
  function headKey(el) { return el.tagName === "LINK" ? "l" + el.getAttribute("href") : "s" + el.textContent; }
  function syncHead(doc) {
    var sel = 'link[rel="stylesheet"], style';
    var have = Array.prototype.slice.call(document.head.querySelectorAll(sel));
    var byKey = new Map(have.map(function (el) { return [headKey(el), el]; }));
    var keep = new Set(), loads = [], prev = null;
    Array.prototype.forEach.call(doc.head.querySelectorAll(sel), function (w) {
      var el = byKey.get(headKey(w));
      if (!el) {
        el = document.importNode(w, true);
        if (el.tagName === "LINK") {
          loads.push(new Promise(function (ok) { el.onload = el.onerror = ok; nTimeout.call(window, ok, 1500); }));
        }
        if (prev) prev.after(el); else document.head.appendChild(el);
      }
      keep.add(el);
      prev = el;
    });
    return Promise.all(loads).then(function () {
      return function () { have.forEach(function (el) { if (!keep.has(el)) el.remove(); }); };
    });
  }

  // скрипты нового экрана: вставленные через DOM не выполняются — заменяем копиями, по порядку
  function runScripts() {
    Array.prototype.forEach.call(body.querySelectorAll("script"), function (old) {
      if (old.hasAttribute("data-once") || (old.type && !/javascript|^module$/i.test(old.type))) return;
      var s = document.createElement("script");
      Array.prototype.forEach.call(old.attributes, function (a) { s.setAttribute(a.name, a.value); });
      s.async = false;
      if (!old.src) s.textContent = old.textContent;
      old.replaceWith(s);
    });
  }

  // ---------- переход ----------
  var ctl = null, curUrl = location.pathname + location.search;
  var saveScroll = function () {
    try { history.replaceState(Object.assign({}, history.state, { pulse: 1, y: scrollY }), ""); } catch (e) { /* без возврата */ }
  };
  var navOnce = function (doc) {
    var a = document.querySelector("script[data-once]"), b = doc.querySelector("script[data-once]");
    return !!(a && b && a.getAttribute("src") === b.getAttribute("src"));
  };

  // opts: method, body, pop (назад/вперёд, y — куда прокрутить), keep (та же страница: прокрутку не трогать)
  function go(url, opts) {
    opts = opts || {};
    var method = (opts.method || "GET").toUpperCase();
    var want = new URL(url, location.href);
    if (ctl) ctl.abort();
    var my = ctl = window.AbortController ? new AbortController() : null;
    progress(true);
    if (!opts.pop) saveScroll();
    var from = { path: location.pathname, y: scrollY };
    return fetch(want.href, { method: method, body: opts.body, credentials: "same-origin", signal: my && my.signal,
                               headers: { "X-Pulse-Nav": "1" } })
      .then(function (res) {
        if (my !== ctl) return null;
        var type = res.headers.get("content-type") || "";
        if (!/text\/html/i.test(type)) {
          // файл или ответ не страницей — пусть браузер откроет его сам (POST уже выполнен: идём туда, куда он привёл)
          return hardGo(method === "GET" ? want.href : (res.url || location.href));
        }
        return res.text().then(function (html) {
          if (my !== ctl) return null;
          return show(html, res, want, method, opts, from);
        });
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        if (method === "GET") hardGo(want.href); else location.reload();
      })
      .then(function () { if (my === ctl) { ctl = null; progress(false); } });
  }

  function show(html, res, want, method, opts, from) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    var nb = doc.body;
    var final = new URL(res.url || want.href);
    if (!res.redirected && want.hash) final.hash = want.hash;
    // не новый дизайн (вернули классический), свой каркас без JS-переходов или обновился сам PULSE — обычный переход
    if (!nb || !nb.classList.contains("d3") || nb.hasAttribute("data-nav-full") || !navOnce(doc)) {
      if (method === "GET" || res.redirected) return hardGo(final.href);
      document.open(); document.write(html); document.close();
      return null;
    }
    var same = final.pathname === from.path;
    var inPlace = same && (method === "POST" || opts.keep);
    return syncHead(doc).then(function (dropOldStyles) {
      var anim = motionOn();
      var before = null, gone = [];
      if (inPlace && anim) {
        before = new Map();
        var next = itemMap(nb);
        itemMap(document).forEach(function (el, k) {
          var r = el.getBoundingClientRect();
          if (!onScreen(r)) return;
          if (next.has(k)) before.set(k, r); else gone.push(el);
        });
      }
      // открытое окно, которое после действия не откроется снова, закрывается своей анимацией, а не пропадает
      var dlg = document.querySelector("dialog[open]");
      var closing = Promise.resolve();
      if (dlg && anim && inPlace && !final.searchParams.has("task")) {
        dlg.close();
        closing = new Promise(function (ok) { nTimeout.call(window, ok, dur(170)); });
      }
      var fadeOut = gone.length && gone.length < 30 && gone[0].animate ? Promise.all(gone.map(function (el) {
        return el.animate([{ opacity: 1 }, { opacity: 0, transform: "scale(.98)" }],
                          { duration: dur(140), easing: "cubic-bezier(.4,0,1,1)", fill: "forwards" }).finished.catch(function () {});
      })) : Promise.resolve();

      return Promise.all([fadeOut, closing]).then(function () {
        var swap = function () {
          try { doSwap(); } catch (err) { hardGo(final.href); throw err; }
        };
        var doSwap = function () {
          // прокрутку при «назад» ведём сами: браузер вернул бы её раньше, чем пришла страница
          if ("scrollRestoration" in history) history.scrollRestoration = "manual";
          teardown();
          dropOldStyles();
          document.title = doc.title;
          Array.prototype.slice.call(body.attributes).forEach(function (a) { if (!nb.hasAttribute(a.name)) body.removeAttribute(a.name); });
          Array.prototype.forEach.call(nb.attributes, function (a) { body.setAttribute(a.name, a.value); });
          if (inPlace || same) body.classList.add("nav-same");  // без проявления всего экрана: меняется только то, что изменилось
          var sideY = (document.querySelector(".sidebar") || {}).scrollTop || 0;
          body.replaceChildren.apply(body, Array.prototype.slice.call(nb.childNodes).map(function (n) { return document.adoptNode(n); }));
          var side = document.querySelector(".sidebar");
          if (side) side.scrollTop = sideY;
          mine(function () { document.documentElement.appendChild(bar); });
          var state = { pulse: 1, y: 0 };
          var url = final.pathname + final.search + final.hash;
          if (opts.pop) { /* адрес уже сменил браузер */ }
          else if (url === location.pathname + location.search + location.hash) history.replaceState(state, "", url);
          else history.pushState(state, "", url);
          curUrl = final.pathname + final.search;
          // прокрутка: назад/вперёд — как была, на той же странице — на месте, якорь — к нему, иначе — наверх
          if (opts.pop) scrollTo(0, opts.y || 0);
          else if (same) scrollTo(0, from.y);
          else if (final.hash && document.getElementById(decodeURIComponent(final.hash.slice(1)))) {
            document.getElementById(decodeURIComponent(final.hash.slice(1))).scrollIntoView();
          } else scrollTo(0, 0);
          swapped = true;
          runScripts();
          if (!same) {
            var main = document.querySelector(".main");
            if (main) { main.setAttribute("tabindex", "-1"); main.focus({ preventScroll: true }); }
          }
          mine(function () { nTimeout.call(window, function () { body.classList.remove("nav-same"); }, 450); });
          document.dispatchEvent(new CustomEvent("pulse:page", { detail: { url: url, same: same } }));
        };

        if (anim && !same && document.startViewTransition) {
          return document.startViewTransition(swap).updateCallbackDone.catch(function () {});
        }
        swap();
        if (before && before.size) flip(before);
        return null;
      });
    });
  }

  // карточки, что остались, плавно переезжают со старого места на новое; новые проявляются
  function flip(before) {
    itemMap(document).forEach(function (el, k) {
      var was = before.get(k);
      if (!was) {
        if (onScreen(el.getBoundingClientRect())) el.classList.add("m-arrive");
        return;
      }
      var now = el.getBoundingClientRect();
      var dx = was.left - now.left, dy = was.top - now.top;
      if ((Math.abs(dx) < 1 && Math.abs(dy) < 1) || !el.animate) return;
      el.animate([{ transform: "translate(" + dx + "px," + dy + "px)" }, { transform: "none" }],
                 { duration: dur(220), easing: "cubic-bezier(.2,.8,.2,1)" });
    });
  }

  // ---------- что перехватываем ----------
  function eligible(u) {
    if (u.origin !== location.origin) return false;
    if (/^\/(static|api)\//.test(u.pathname) || /\.[a-z0-9]{2,5}$/i.test(u.pathname)) return false;
    return true;
  }

  function onClick(e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || (a.target && a.target !== "_self") || a.hasAttribute("download") || a.closest("[data-nav-full]")) return;
    if (a.getAttribute("href").charAt(0) === "#") return;
    var u = new URL(a.href, location.href);
    if (!eligible(u)) return;
    if (u.pathname === location.pathname && u.search === location.search && u.hash) return;  // якорь на этой странице
    e.preventDefault();
    go(u.href, { keep: u.pathname === location.pathname });
  }

  var busy = new WeakSet();
  function submitSoft(form, sub) {
    var attr = function (name, formName) { return (sub && sub.getAttribute(formName)) || form.getAttribute(name); };
    var method = (attr("method", "formmethod") || "get").toLowerCase();
    var target = attr("target", "formtarget");
    if (method === "dialog" || (target && target !== "_self") || form.closest("[data-nav-full]")
        || (sub && sub.hasAttribute("data-nav-full"))) return false;
    var action = new URL(attr("action", "formaction") || location.href, location.href);
    if (!eligible(action)) return false;
    if (busy.has(form)) return true;  // уже отправляется — второй щелчок не отправит дважды
    var fd;
    try { fd = new FormData(form, sub || undefined); } catch (err) {
      fd = new FormData(form);
      if (sub && sub.name) fd.append(sub.name, sub.value);
    }
    if (method === "get") {
      action.search = new URLSearchParams(fd).toString();
      go(action.href, { keep: action.pathname === location.pathname });
      return true;
    }
    var multipart = (attr("enctype", "formenctype") || "").toLowerCase() === "multipart/form-data";
    busy.add(form);
    form.setAttribute("aria-busy", "true");
    go(action.href, { method: "POST", body: multipart ? fd : new URLSearchParams(fd) })
      .then(function () { busy.delete(form); form.removeAttribute("aria-busy"); });
    return true;
  }

  function onSubmit(e) {
    if (e.defaultPrevented || !(e.target instanceof HTMLFormElement)) return;
    if (submitSoft(e.target, e.submitter || null)) e.preventDefault();
  }

  // form.submit() из скриптов (фильтры, загрузка файла) событие submit не вызывает — ведём его сюда же
  var nSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    if (this.isConnected && body.classList.contains("d3") && submitSoft(this, null)) return;
    nSubmit.call(this);
  };

  mine(function () {
    // на window и в фазе всплытия: сначала отрабатывают обработчики страницы, и если они отменили действие — не трогаем
    window.addEventListener("click", onClick);
    window.addEventListener("submit", onSubmit);
    window.addEventListener("popstate", function (e) {
      if (location.pathname + location.search === curUrl) return;  // сменился только якорь
      go(location.href, { pop: true, y: e.state && e.state.y });
    });
    window.addEventListener("pagehide", saveScroll);
  });
  try { history.replaceState(Object.assign({}, history.state, { pulse: 1 }), ""); } catch (e) { /* ничего */ }

  window.pulseNav = { go: go, teardown: teardown };
  window.pulseGo = function (url) { go(url); };
  window.pulseReload = function () { go(location.href, { keep: true }); };
})();
