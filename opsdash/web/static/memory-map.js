/* Карта памяти PULSE на canvas, без сторонних библиотек.
   «Созвездие»: точки — факты, цвет точки — сколько у факта связей, линии — связи четырёх видов (общая сущность, по
   времени, по смыслу, причина) с переключателями в легенде. «Сущности»: точки — люди, клиенты, проекты; толщина
   линии — сколько раз упоминались вместе, цвет — как давно.
   Раскладка — силовая (Барнс — Хат для отталкивания, пружины для связей), остывает сама; «Пауза» её останавливает.
   Колесо — масштаб вокруг курсора, перетаскивание — сдвиг (точки — переносит), наведение — подпись, щелчок —
   карточка справа. Цвета берутся из токенов темы (style.css, dark.css, graphite*.css) и перечитываются при смене. */
(function () {
  'use strict';
  var wrap = document.querySelector('[data-mem-map]');
  if (!wrap) return;
  var stage = wrap.querySelector('.mem-map-stage');
  var canvas = stage.querySelector('canvas');
  var ctx = canvas.getContext('2d');
  var tip = stage.querySelector('[data-tip]');
  var legend = stage.querySelector('[data-legend]');
  var counter = stage.querySelector('[data-counter]');
  var card = wrap.querySelector('[data-card]');
  var elist = stage.querySelector('[data-elist]');
  var tools = document.querySelector('[data-map-tools]');

  var LINK_KINDS = [['entity', 'Общая сущность'], ['temporal', 'По времени'], ['semantic', 'По смыслу'], ['causal', 'Причина']];
  var EKINDS = { person: 'человек', company: 'компания', product: 'продукт', project: 'проект', metric: 'метрика',
    system: 'система', team: 'команда', other: 'другое' };
  var state = { view: 'facts', emode: 'graph', show: { entity: true, temporal: true, semantic: true, causal: true },
    scale: 1, tx: 0, ty: 0, running: true, alpha: 1, hover: null, sel: null, nodes: [], links: [], byId: {},
    neighbours: {}, total: 0, maxDeg: 1, dpr: 1, w: 0, h: 0, drag: null, t0: performance.now(), lastData: null };
  var C = {};

  // --- цвета из темы ---------------------------------------------------------------------------------------
  function token(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  function rgb(c) {
    var d = document.createElement('div');
    d.style.color = c; document.body.appendChild(d);
    var m = getComputedStyle(d).color.match(/\d+(\.\d+)?/g) || [0, 0, 0];
    d.remove();
    return [+m[0], +m[1], +m[2]];
  }
  function mix(a, b, t) { return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }
  function css(c, a) { return 'rgba(' + Math.round(c[0]) + ',' + Math.round(c[1]) + ',' + Math.round(c[2]) + ',' + (a == null ? 1 : a) + ')'; }
  function readColors() {
    C = {
      bg: rgb(token('--sunken', '#eee')), panel: rgb(token('--panel', '#fff')), text: rgb(token('--text', '#222')),
      muted: rgb(token('--muted', '#666')), faint: rgb(token('--faint', '#aaa')), accent: rgb(token('--accent', '#3b5bdb')),
      warn: rgb(token('--warn', '#e67700')), ok: rgb(token('--ok', '#2f9e44')), bad: rgb(token('--bad', '#e03131')),
      violet: rgb(token('--evg-line', '#7048e8'))
    };
    C.link = { entity: C.accent, temporal: C.ok, semantic: C.violet, causal: C.bad };
    C.ekind = { person: C.accent, company: C.warn, product: C.ok, project: C.violet, metric: C.bad, system: C.muted,
      team: mix(C.ok, C.accent, 0.5), other: C.faint };
  }
  function degColor(n) {  // мало связей — бледная, средне — акцент, много — тёплый
    var t = n.rank == null ? 0.5 : n.rank;
    return t < 0.5 ? mix(C.faint, C.accent, t * 2) : mix(C.accent, C.warn, (t - 0.5) * 2);
  }

  // --- данные ---------------------------------------------------------------------------------------------
  function filters() {
    var q = {};
    tools.querySelectorAll('[data-f]').forEach(function (el) { q[el.dataset.f] = el.value; });
    return q;
  }
  function load() {
    var f = filters(), url;
    if (state.view === 'facts') {
      var narrowed = f.kind || f.entity || f.source;
      url = '/api/memory/graph?view=facts&days=' + encodeURIComponent(f.days || '30') + '&kind=' + encodeURIComponent(f.kind || '') +
        '&entity=' + encodeURIComponent(f.entity || '') + '&source=' + encodeURIComponent(f.source || '') + '&limit=' + (narrowed ? 2000 : 300);
    } else {
      url = '/api/memory/graph?view=entities&kind=' + encodeURIComponent(f.ekind || '') + '&limit=300';
    }
    counter.textContent = 'Загружаю…';
    fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(build)
      .catch(function () { counter.textContent = 'Не получилось загрузить карту'; });
  }
  function build(data) {
    state.lastData = data;
    var old = state.byId, nodes = [], byId = {}, n = data.nodes.length, R = Math.sqrt(n) * 18 + 40;
    data.nodes.forEach(function (d, i) {
      var p = old[d.id], a = i * 2.39996, r = R * Math.sqrt((i + 0.5) / n);
      var node = { d: d, id: d.id, x: p ? p.x : Math.cos(a) * r, y: p ? p.y : Math.sin(a) * r, vx: 0, vy: 0, deg: 0 };
      nodes.push(node); byId[d.id] = node;
    });
    var links = [], nb = {};
    data.links.forEach(function (l) {
      var a = byId[l[0]], b = byId[l[1]];
      if (!a || !b) return;
      var link = state.view === 'facts' ? { a: a, b: b, kind: l[2], w: l[3] || 1 } : { a: a, b: b, kind: 'co', n: l[2], day: l[3], last: l[4] };
      links.push(link); a.deg++; b.deg++;
      (nb[a.id] = nb[a.id] || []).push(b.id); (nb[b.id] = nb[b.id] || []).push(a.id);
    });
    var maxDeg = 1;
    nodes.forEach(function (o) {
      maxDeg = Math.max(maxDeg, o.deg);
      var size = state.view === 'facts' ? o.deg : (o.d.facts * 2 + o.d.mentions / 3);
      o.r = state.view === 'facts' ? 1.8 + Math.min(4.5, Math.sqrt(o.deg) * 0.55) : 3.5 + Math.min(12, Math.sqrt(size) * 1.2);
    });
    if (state.view === 'entities') {
      var maxN = 1, today = Date.now();
      links.forEach(function (l) { maxN = Math.max(maxN, l.n); });
      links.forEach(function (l) {
        var age = l.last ? (today - Date.parse(l.last)) / 864e5 : 90;
        l.fresh = Math.max(0, Math.min(1, 1 - age / 60));
        l.width = 0.6 + 3 * Math.log(1 + l.n) / Math.log(1 + maxN);
      });
    }
    // цвет — по месту среди остальных (а не по числу): в плотной памяти почти у всех много связей
    nodes.slice().sort(function (a, b) { return a.deg - b.deg; }).forEach(function (o, i, arr) { o.rank = arr.length > 1 ? i / (arr.length - 1) : 0.5; });
    state.nodes = nodes; state.links = links; state.byId = byId; state.neighbours = nb; state.maxDeg = maxDeg;
    state.total = data.total || nodes.length; state.alpha = 1; state.sel = null; state.hover = null;
    // раскладку прогреваем сразу, без рисования: карта появляется уже собранной, а не разлетается на глазах
    var warm = Math.min(220, Math.round(60000 / Math.max(1, nodes.length + links.length / 4)));
    for (var i = 0; i < warm && state.alpha > 0.08; i++) tick();
    if (!state.touched) state.needFit = !fit();
    drawLegend(data);
    if (state.view === 'entities' && state.emode === 'list') renderList();
    draw(performance.now());  // сразу, не дожидаясь кадра: в фоновой вкладке кадров нет
    kick();
  }

  // --- раскладка ------------------------------------------------------------------------------------------
  function quad(nodes) {  // дерево Барнса — Хата: масса и центр тяжести каждой клетки
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach(function (n) { minX = Math.min(minX, n.x); minY = Math.min(minY, n.y); maxX = Math.max(maxX, n.x); maxY = Math.max(maxY, n.y); });
    var size = Math.max(maxX - minX, maxY - minY) + 1;
    var root = { x0: minX, y0: minY, s: size, m: 0, cx: 0, cy: 0, kids: null, n: null };
    function insert(c, n, depth) {
      c.cx = (c.cx * c.m + n.x) / (c.m + 1); c.cy = (c.cy * c.m + n.y) / (c.m + 1); c.m++;
      if (!c.kids && !c.n) { c.n = n; return; }
      if (depth > 24) return;
      if (!c.kids) {
        var h = c.s / 2;
        c.kids = [0, 1, 2, 3].map(function (i) { return { x0: c.x0 + (i & 1) * h, y0: c.y0 + (i >> 1) * h, s: h, m: 0, cx: 0, cy: 0, kids: null, n: null }; });
        var old = c.n; c.n = null; put(c, old, depth);
      }
      put(c, n, depth);
    }
    function put(c, n, depth) {
      var h = c.s / 2, i = (n.x >= c.x0 + h ? 1 : 0) + (n.y >= c.y0 + h ? 2 : 0);
      insert(c.kids[i], n, depth + 1);
    }
    nodes.forEach(function (n) { insert(root, n, 0); });
    return root;
  }
  function repel(c, n, k) {
    if (!c || !c.m) return;
    var dx = c.cx - n.x, dy = c.cy - n.y, d2 = dx * dx + dy * dy + 0.01;
    if (c.n === n && !c.kids) return;
    if (!c.kids || (c.s * c.s) / d2 < 0.64) {  // θ = 0.8
      var f = k * c.m / d2;
      n.vx -= dx * f; n.vy -= dy * f;
      return;
    }
    for (var i = 0; i < 4; i++) repel(c.kids[i], n, k);
  }
  var LEN = { entity: 70, temporal: 55, semantic: 65, causal: 45, co: 170 };
  function tick() {
    var nodes = state.nodes, a = state.alpha;
    if (!nodes.length) return;
    var root = quad(nodes), k = (state.view === 'facts' ? 70 : 160) * a;
    nodes.forEach(function (n) { repel(root, n, k); });
    state.links.forEach(function (l) {
      var dx = l.b.x - l.a.x, dy = l.b.y - l.a.y, d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      // сила пружины — как в d3: 1 / меньшая степень концов, иначе узлы со многими связями слипаются в клубок
      var w = (state.view === 'facts' ? (l.kind === 'causal' ? 1 : 0.5) * (l.w || 1) : 0.4 + 0.12 * Math.min(5, l.n)) /
        Math.max(1, Math.min(l.a.deg, l.b.deg));
      var f = (d - LEN[l.kind]) / d * w * a;
      var ba = l.a.deg / (l.a.deg + l.b.deg);
      l.b.vx -= dx * f * ba; l.b.vy -= dy * f * ba; l.a.vx += dx * f * (1 - ba); l.a.vy += dy * f * (1 - ba);
    });
    nodes.forEach(function (n) {
      n.vx -= n.x * 0.012 * a; n.vy -= n.y * 0.012 * a;  // к центру
      if (state.drag && state.drag.node === n) { n.vx = n.vy = 0; return; }
      n.vx *= 0.58; n.vy *= 0.58;
      n.x += Math.max(-30, Math.min(30, n.vx)); n.y += Math.max(-30, Math.min(30, n.vy));
    });
    state.alpha = Math.max(0, a - 0.012);
  }

  // --- рисование ------------------------------------------------------------------------------------------
  function resize() {
    var r = stage.getBoundingClientRect();
    state.dpr = window.devicePixelRatio || 1; state.w = r.width; state.h = r.height;
    canvas.width = Math.round(r.width * state.dpr); canvas.height = Math.round(r.height * state.dpr);
    if (!state.touched) state.needFit = !fit();
    if (state.nodes.length) draw(performance.now());
    kick(true);
  }
  function fit() {
    if (!state.nodes.length || state.w < 80 || state.h < 80) return false;
    // вписываем 96% точек: одиночки на краю не должны сжимать всю карту в точку
    var xs = state.nodes.map(function (n) { return n.x; }).sort(function (a, b) { return a - b; });
    var ys = state.nodes.map(function (n) { return n.y; }).sort(function (a, b) { return a - b; });
    var lo = Math.floor(xs.length * 0.02), hi = Math.max(lo, Math.ceil(xs.length * 0.98) - 1);
    var minX = xs[lo], maxX = xs[hi], minY = ys[lo], maxY = ys[hi];
    var s = Math.min((state.w - 80) / (maxX - minX + 1), (state.h - 100) / (maxY - minY + 1));
    state.scale = Math.max(0.15, Math.min(3, s));
    state.tx = state.w / 2 - (minX + maxX) / 2 * state.scale; state.ty = state.h / 2 - (minY + maxY) / 2 * state.scale - 10;
    return true;
  }
  function toScreen(n) { return [n.x * state.scale + state.tx, n.y * state.scale + state.ty]; }
  function visibleLink(l) { return state.view !== 'facts' || state.show[l.kind]; }
  function focusSet() {
    var id = state.sel != null ? state.sel : null;
    if (id == null) return null;
    var s = {}; s[id] = true;
    (state.neighbours[id] || []).forEach(function (x) { s[x] = true; });
    return s;
  }
  function draw(now) {
    var c = ctx, s = state.scale, focus = focusSet();
    c.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
    c.clearRect(0, 0, state.w, state.h);
    c.save(); c.translate(state.tx, state.ty); c.scale(s, s);
    var shown = 0;
    if (state.view === 'facts') {
      LINK_KINDS.forEach(function (lk) {
        var kind = lk[0];
        if (!state.show[kind]) return;
        c.beginPath();
        var col = C.link[kind];
        state.links.forEach(function (l) {
          if (l.kind !== kind) return;
          if (focus && !(focus[l.a.id] && focus[l.b.id] && (l.a.id === state.sel || l.b.id === state.sel))) return;
          c.moveTo(l.a.x, l.a.y); c.lineTo(l.b.x, l.b.y); shown++;
        });
        c.strokeStyle = css(col, focus ? 0.75 : kind === 'causal' ? 0.7 : 0.28);
        c.lineWidth = (kind === 'causal' ? 1.6 : 1) / s;
        c.stroke();
        if (focus) {  // остальные связи — еле видно
          c.beginPath();
          state.links.forEach(function (l) {
            if (l.kind !== kind || (l.a.id === state.sel || l.b.id === state.sel)) return;
            c.moveTo(l.a.x, l.a.y); c.lineTo(l.b.x, l.b.y);
          });
          c.strokeStyle = css(col, 0.06); c.stroke();
        }
      });
    } else {
      state.links.forEach(function (l) {
        var on = !focus || (l.a.id === state.sel || l.b.id === state.sel);
        c.beginPath(); c.moveTo(l.a.x, l.a.y); c.lineTo(l.b.x, l.b.y);
        c.strokeStyle = css(mix(C.faint, C.accent, l.fresh), on ? (focus ? 0.85 : 0.45) : 0.06);
        c.lineWidth = l.width / s; c.stroke(); if (on) shown++;
      });
    }
    var t = (now - state.t0) / 1000, visible = 0;
    state.nodes.forEach(function (n) {
      var p = toScreen(n);
      if (p[0] < -20 || p[1] < -20 || p[0] > state.w + 20 || p[1] > state.h + 20) return;
      visible++;
      var dim = focus && !focus[n.id];
      var col = state.view === 'facts' ? degColor(n) : (C.ekind[n.d.kind] || C.faint);
      c.beginPath(); c.arc(n.x, n.y, n.r / Math.sqrt(s) + 0.3, 0, Math.PI * 2);
      c.fillStyle = css(col, dim ? 0.15 : 0.92); c.fill();
      if (n.d.me && !dim) { c.lineWidth = 1.2 / s; c.strokeStyle = css(C.text, 0.7); c.stroke(); }
      if (n.d.new && t < 12) {  // новый факт вспыхивает
        var k = (t % 1.5) / 1.5;
        c.beginPath(); c.arc(n.x, n.y, (n.r + 10 * k) / Math.sqrt(s), 0, Math.PI * 2);
        c.strokeStyle = css(C.warn, 0.7 * (1 - k)); c.lineWidth = 2 / s; c.stroke();
      }
      if (state.sel === n.id || state.hover === n) {
        c.beginPath(); c.arc(n.x, n.y, n.r / Math.sqrt(s) + 4 / s, 0, Math.PI * 2);
        c.strokeStyle = css(C.text, 0.9); c.lineWidth = 1.5 / s; c.stroke();
      }
    });
    c.restore();
    // подписи — у самых связанных (больше при приближении), у наведённой и выбранной
    var labels = 0, budget = Math.round(10 * Math.max(1, s * s)), order = state.nodes.slice().sort(function (a, b) { return b.deg - a.deg; });
    c.font = '12px ' + token('--font', 'system-ui');
    c.textBaseline = 'middle';
    var placed = [];
    function label(n, strong) {
      var p = toScreen(n), text = state.view === 'facts' ? n.d.label : n.d.name;
      if (p[0] < 0 || p[1] < 0 || p[0] > state.w || p[1] > state.h) return false;
      var w = c.measureText(text).width, x = p[0] + n.r + 5, y = p[1];
      for (var i = 0; i < placed.length; i++) { var q = placed[i]; if (x < q[0] + q[2] && x + w > q[0] && Math.abs(y - q[1]) < 14) return false; }
      placed.push([x, y, w]);
      c.lineWidth = 3; c.strokeStyle = css(C.bg, 0.92); c.strokeText(text, x, y);
      c.fillStyle = css(strong ? C.text : C.muted, 1); c.fillText(text, x, y);
      return true;
    }
    if (state.sel != null && state.byId[state.sel]) { label(state.byId[state.sel], true); labels++; }
    if (state.hover) { label(state.hover, true); labels++; }
    var focusIds = focus;
    for (var i = 0; i < order.length && labels < budget; i++) {
      var n = order[i];
      if (n === state.hover || n.id === state.sel) continue;
      if (focusIds && !focusIds[n.id]) continue;
      if (state.view === 'facts' && n.deg < 2 && !focusIds) break;
      if (label(n, false)) labels++;
    }
    counter.innerHTML = (state.view === 'facts'
      ? 'Фактов: <b>' + state.nodes.length + '</b>' + (state.total > state.nodes.length ? ' из ' + state.total : '')
      : 'Сущностей: <b>' + state.nodes.length + '</b>') +
      ' · видно: <b>' + visible + '</b> · подписей: <b>' + labels + '</b> · связей: <b>' + shown + '</b> · масштаб: <b>' +
      s.toFixed(1).replace('.', ',') + '×</b>' + (state.running ? '' : ' · пауза');
  }
  var raf = 0;
  function frame(now) {
    raf = 0;
    if (state.running && state.alpha > 0) { tick(); tick(); }
    if (state.needFit && (state.alpha < 0.7 || !state.running) && fit()) state.needFit = !!state.running && state.alpha > 0.05;
    draw(now);
    var anim = state.nodes.some(function (n) { return n.d.new; }) && (now - state.t0) < 12000;
    if ((state.running && state.alpha > 0) || anim || state.drag) raf = requestAnimationFrame(frame);
  }
  function kick(redrawOnly) {
    if (!redrawOnly && state.alpha < 0.3) state.alpha = 0.3;
    if (!raf) raf = requestAnimationFrame(frame);
  }

  // --- легенда, список, карточка ---------------------------------------------------------------------------
  function drawLegend(data) {
    if (state.view === 'facts') {
      var counts = {};
      state.links.forEach(function (l) { counts[l.kind] = (counts[l.kind] || 0) + 1; });
      var grad = 'linear-gradient(90deg,' + css(C.faint) + ',' + css(C.accent) + ',' + css(C.warn) + ')';
      legend.innerHTML = '<b>Связей у факта</b><div class="grad" style="background:' + grad + '"></div>' +
        '<div class="scale"><span>мало</span><span>много</span></div><b style="margin-top:6px">Линии</b>' +
        LINK_KINDS.map(function (lk) {
          return '<label><input type="checkbox" data-kind="' + lk[0] + '"' + (state.show[lk[0]] ? ' checked' : '') + '>' +
            '<span class="sw" style="background:' + css(C.link[lk[0]]) + '"></span>' + lk[1] + ' <span class="muted">' + (counts[lk[0]] || 0) + '</span></label>';
        }).join('');
    } else {
      var used = {};
      state.nodes.forEach(function (n) { used[n.d.kind] = true; });
      legend.innerHTML = '<b>Сущности</b>' + Object.keys(EKINDS).filter(function (k) { return used[k]; }).map(function (k) {
        return '<label><span class="sw" style="height:9px;width:9px;border-radius:50%;background:' + css(C.ekind[k]) + '"></span>' + EKINDS[k] + '</label>';
      }).join('') + '<b style="margin-top:6px">Линия</b><span>толщина — сколько раз вместе</span>' +
        '<div class="grad" style="background:linear-gradient(90deg,' + css(C.faint) + ',' + css(C.accent) + ')"></div><div class="scale"><span>давно</span><span>недавно</span></div>';
    }
  }
  legend.addEventListener('change', function (e) {
    var k = e.target.dataset && e.target.dataset.kind;
    if (!k) return;
    state.show[k] = e.target.checked; kick(true);
  });
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function renderList() {
    var rows = state.nodes.slice().sort(function (a, b) { return (b.d.facts * 3 + b.d.mentions) - (a.d.facts * 3 + a.d.mentions); });
    elist.innerHTML = '<div class="mem-thead"><span>Сущность</span><span>Вид</span><span class="num">Фактов</span><span class="num">Упоминаний</span><span class="num">Связей</span></div>' +
      rows.map(function (n) {
        return '<a class="mem-trow" href="/memory/page/' + n.id + '"><span class="mem-tname">' + esc(n.d.name) + '</span><span class="muted">' + esc(n.d.kind_title) +
          '</span><span class="num">' + n.d.facts + '</span><span class="num">' + n.d.mentions + '</span><span class="num">' + n.deg + '</span></a>';
      }).join('');
  }
  function showCard(n) {
    if (!n) { return; }
    card.innerHTML = '<p class="mem-card-empty">Загружаю…</p>';
    var url = state.view === 'facts' ? '/api/memory/fact/' + n.id : '/api/memory/entity/' + n.id;
    fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { card.innerHTML = '<p class="mem-card-empty">' + esc(d.error) + '</p>'; return; }
      if (state.view === 'facts') {
        card.innerHTML = '<h3>' + esc(d.text) + '</h3><div class="muted small">' + esc(d.kind) + ' · на ' + esc(d.day) + ' · ' + esc(d.status) + '</div>' +
          (d.prev ? '<p class="small">было: ' + esc(d.prev) + '</p>' : '') +
          (d.because ? '<p class="small">потому что: ' + esc(d.because) + '</p>' : '') +
          '<p class="small">' + (d.source_link ? '<a href="' + esc(d.source_link) + '">' + esc(d.source) + '</a>' : esc(d.source)) + '</p>' +
          (d.quote ? '<blockquote class="small mem-quote">«' + esc(d.quote) + '»</blockquote>' : '') +
          (d.entities.length ? '<div class="small">О ком и о чём: ' + d.entities.map(function (e) { return '<a href="/memory/page/' + e.id + '">' + esc(e.name) + '</a>'; }).join(', ') + '</div>' : '') +
          (d.neighbours.length ? '<div class="small muted" style="margin-top:10px">Соседи (' + d.neighbours.length + ')</div><ul>' + d.neighbours.slice(0, 15).map(function (x) {
            return '<li><a class="nb" data-nb="' + x.id + '">' + esc(x.text) + '</a> <span class="muted">· ' + esc(x.kind_title) + '</span></li>';
          }).join('') + '</ul>' : '') +
          '<p class="small"><a href="/memory?tab=feed&days=0&q=' + encodeURIComponent(d.text.slice(0, 40)) + '#f' + d.id + '">в ленте →</a></p>';
      } else {
        card.innerHTML = '<h3><a href="/memory/page/' + d.id + '">' + esc(d.name) + '</a></h3><div class="muted small">' + esc(d.kind) +
          (d.aliases && d.aliases.length ? ' · также: ' + esc(d.aliases.slice(0, 4).join(', ')) : '') + '</div>' +
          (d.lead ? '<p class="small">' + esc(d.lead) + '</p>' : '') +
          (d.facts.length ? '<ul>' + d.facts.map(function (f) { return '<li>' + esc(f.text) + ' <span class="muted">· ' + esc(f.day) + '</span></li>'; }).join('') + '</ul>' : '<p class="small muted">Фактов пока нет.</p>') +
          (d.tasks.length ? '<div class="small muted">Задачи</div><ul>' + d.tasks.map(function (c) { return '<li><a href="/commitments?task=' + c.id + '">' + esc(c.title) + '</a></li>'; }).join('') + '</ul>' : '') +
          '<p><a class="btn sm" href="/memory/page/' + d.id + '">Страница целиком</a></p>';
      }
    }).catch(function () { card.innerHTML = '<p class="mem-card-empty">Не получилось загрузить.</p>'; });
  }
  card.addEventListener('click', function (e) {
    var a = e.target.closest('[data-nb]');
    if (!a) return;
    var n = state.byId[+a.dataset.nb];
    if (n) { select(n); center(n); }
  });
  function select(n) { state.sel = n ? n.id : null; showCard(n); kick(true); }
  function center(n) { state.tx = state.w / 2 - n.x * state.scale; state.ty = state.h / 2 - n.y * state.scale; kick(true); }

  // --- мышь ----------------------------------------------------------------------------------------------
  function pick(mx, my) {
    var best = null, bd = 14 * 14;
    state.nodes.forEach(function (n) {
      var p = toScreen(n), dx = p[0] - mx, dy = p[1] - my, d = dx * dx + dy * dy, rr = Math.max(n.r + 4, 8);
      if (d < bd && d < rr * rr * 2.5) { bd = d; best = n; }
    });
    return best;
  }
  function pos(e) { var r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
  canvas.addEventListener('wheel', function (e) {
    e.preventDefault();
    var p = pos(e), k = Math.exp(-e.deltaY * 0.0015), s = Math.max(0.12, Math.min(8, state.scale * k));
    state.tx = p[0] - (p[0] - state.tx) * (s / state.scale); state.ty = p[1] - (p[1] - state.ty) * (s / state.scale);
    state.scale = s; state.touched = true; state.needFit = false; kick(true);
  }, { passive: false });
  canvas.addEventListener('pointerdown', function (e) {
    var p = pos(e), n = pick(p[0], p[1]);
    state.drag = { x: p[0], y: p[1], tx: state.tx, ty: state.ty, node: n, moved: false };
    canvas.setPointerCapture(e.pointerId); canvas.classList.add('dragging');
  });
  canvas.addEventListener('pointermove', function (e) {
    var p = pos(e), d = state.drag;
    if (d) {
      if (Math.abs(p[0] - d.x) + Math.abs(p[1] - d.y) > 3) d.moved = true;
      if (d.node) { d.node.x = (p[0] - state.tx) / state.scale; d.node.y = (p[1] - state.ty) / state.scale; if (state.running) state.alpha = Math.max(state.alpha, 0.15); }
      else { state.tx = d.tx + p[0] - d.x; state.ty = d.ty + p[1] - d.y; state.touched = true; state.needFit = false; }
      kick(true); return;
    }
    var n = pick(p[0], p[1]);
    if (n !== state.hover) { state.hover = n; kick(true); }
    if (n) {
      tip.hidden = false;
      tip.innerHTML = state.view === 'facts'
        ? esc(n.d.text) + '<div class="muted">' + esc(n.d.day) + (n.d.about.length ? ' · ' + esc(n.d.about.join(', ')) : '') + ' · связей ' + n.deg + '</div>'
        : '<b>' + esc(n.d.name) + '</b><div class="muted">' + esc(n.d.kind_title) + ' · фактов ' + n.d.facts + ' · упоминаний ' + n.d.mentions + '</div>';
      var x = Math.min(p[0] + 14, state.w - 330), y = Math.min(p[1] + 14, state.h - 60);
      tip.style.left = Math.max(4, x) + 'px'; tip.style.top = Math.max(4, y) + 'px';
      canvas.style.cursor = 'pointer';
    } else { tip.hidden = true; canvas.style.cursor = ''; }
  });
  canvas.addEventListener('pointerup', function (e) {
    var d = state.drag; state.drag = null; canvas.classList.remove('dragging');
    if (d && !d.moved) { var p = pos(e), n = pick(p[0], p[1]); select(n); if (!n) card.innerHTML = '<p class="mem-card-empty">Щёлкните по точке, чтобы открыть карточку.</p>'; }
    kick(true);
  });
  canvas.addEventListener('pointerleave', function () { tip.hidden = true; if (state.hover) { state.hover = null; kick(true); } });

  // --- кнопки и фильтры ----------------------------------------------------------------------------------
  wrap.querySelectorAll('[data-act]').forEach(function (b) {
    b.addEventListener('click', function () {
      var act = b.dataset.act;
      if (act === 'pause') { state.running = !state.running; b.textContent = state.running ? 'Пауза' : 'Продолжить'; b.setAttribute('aria-pressed', String(!state.running)); if (state.running) state.alpha = Math.max(state.alpha, 0.2); kick(true); }
      if (act === 'fit') { state.touched = false; fit(); kick(true); }
      if (act === 'full') { setFull(!wrap.classList.contains('full')); }
    });
  });
  function setFull(on) {
    wrap.classList.toggle('full', on);
    var b = wrap.querySelector('[data-act="full"]');
    b.textContent = on ? 'Свернуть' : 'Во весь экран'; b.setAttribute('aria-pressed', String(on));
    setTimeout(function () { resize(); fit(); kick(true); }, 30);
  }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && wrap.classList.contains('full')) setFull(false); });
  tools.querySelectorAll('[data-view]').forEach(function (b) {
    b.addEventListener('click', function () {
      if (state.view === b.dataset.view) return;
      state.view = b.dataset.view; state.byId = {}; state.needFit = true;
      tools.querySelectorAll('[data-view]').forEach(function (x) { var on = x === b; x.classList.toggle('active', on); x.setAttribute('aria-pressed', String(on)); });
      tools.querySelectorAll('[data-for]').forEach(function (x) { x.hidden = x.dataset.for !== state.view; });
      elist.hidden = !(state.view === 'entities' && state.emode === 'list'); canvas.hidden = !elist.hidden;
      card.innerHTML = '<p class="mem-card-empty">Щёлкните по точке, чтобы открыть карточку.</p>';
      load();
    });
  });
  tools.querySelectorAll('[data-emode]').forEach(function (b) {
    b.addEventListener('click', function () {
      state.emode = b.dataset.emode;
      tools.querySelectorAll('[data-emode]').forEach(function (x) { var on = x === b; x.classList.toggle('active', on); x.setAttribute('aria-pressed', String(on)); });
      elist.hidden = state.emode !== 'list'; canvas.hidden = !elist.hidden;
      if (state.emode === 'list') renderList(); else kick(true);
    });
  });
  tools.querySelectorAll('[data-f]').forEach(function (el) { el.addEventListener('change', function () { state.needFit = true; state.touched = false; load(); }); });

  // --- тема и размер -------------------------------------------------------------------------------------
  function retheme() { readColors(); if (state.lastData) drawLegend(state.lastData); kick(true); }
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    (mq.addEventListener ? mq.addEventListener.bind(mq, 'change') : mq.addListener.bind(mq))(retheme);
  }
  if (window.ResizeObserver) new ResizeObserver(function () { resize(); }).observe(stage);
  window.addEventListener('resize', resize);
  readColors(); resize(); state.needFit = true; load();
  window.__memMap = state;  // для проверки в браузере: сколько точек, связей, масштаб
  // замер без сервера: n точек и m связей — сколько мс уходит на шаг раскладки и на кадр (60 кадров/с — это 16 мс)
  window.__memMapBench = function (n, m) {
    var kinds = ['entity', 'temporal', 'semantic', 'causal'], nodes = [], links = [];
    for (var i = 0; i < n; i++) nodes.push({ id: i + 1, kind: 'state', text: 'факт ' + i, label: 'факт ' + i, deg: 0, day: '', about: [] });
    for (var j = 0; j < m; j++) { var a = 1 + Math.floor(Math.random() * n), b = 1 + Math.floor(Math.random() * n); if (a !== b) links.push([a, b, kinds[j % 4], 1]); }
    state.view = 'facts'; state.byId = {};
    var t0 = performance.now(); build({ nodes: nodes, links: links, total: n }); var tb = performance.now() - t0;
    state.alpha = 1; t0 = performance.now(); for (var k = 0; k < 20; k++) tick(); var tt = (performance.now() - t0) / 20;
    t0 = performance.now(); for (k = 0; k < 20; k++) draw(performance.now()); var td = (performance.now() - t0) / 20;
    return { nodes: n, links: links.length, build_ms: Math.round(tb), tick_ms: +tt.toFixed(1), draw_ms: +td.toFixed(1) };
  };
})();
