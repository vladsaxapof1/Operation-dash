// «Офис» картинкой: один большой офис из кубиков — у PULSE и у каждой папки с Claude Code свой open space,
// спереди общий лаунж с кухней, диванами и переговорным столом. Всё рисуется кодом, без картинок и чужих наборов.
// Что рисовать — из списков под картинкой (<script type="application/json" data-office-json>, его обновляет
// app.js; собирает opsdash.office.scene). Офис двигают мышью (зажать и тянуть), подведя курсор к краю или
// стрелками; «+»/«−» и щипок на трекпаде — ближе и дальше. Сотрудники живут: кто взялся за работу — идёт к своему
// столу, свободные отдыхают в лаунже или сидят у себя, по коридору ходит кот. Нажатие на сотрудника или его стол —
// карточка справа. Кадры рисуются, только пока страница на экране и что-то движется (не чаще 24 в секунду).
(function () {
  "use strict";
  var scene = document.querySelector("[data-office-scene]"), body = document.querySelector("[data-office]");
  var canvas = scene && scene.querySelector("canvas");
  if (!canvas || !body || !canvas.getContext) return;
  var ctx = canvas.getContext("2d"), wrap = scene.querySelector("[data-scene-wrap]"),
      panel = scene.querySelector("[data-office-panel]"), panelBody = scene.querySelector("[data-panel-body]");
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var CELL_W = 28, CELL_D = 26, Y0 = 8, WALL_H = 30, SPEED = 13, FPS = 24, EDGE = 28;
  var ZOOMS = [2.6, 3.4, 4.3, 5.4, 6.8, 8.6];
  var ZONES = ["#5b7fd6", "#d0776a", "#5fae86", "#c9a24a", "#9c6fd1", "#4fb3c8", "#d46aa0", "#8a9a5b"];
  var AREA = ["#7c6cf0", "#e0864a", "#3fa08a", "#c95a8a"];  // цвет вывески и ковролина каждого open space
  var SKIN = ["#f1c7a5", "#d9a37e", "#b97d57"], HAIR = ["#2b1d16", "#6b3e1f", "#c9a24a", "#1c1c22", "#8a3324"];
  var ROLES = {  // кто сидит за рабочими местами PULSE: цвет одежды, причёска, что лежит на столе
    triage: { shirt: "#7c6cf0", hair: 0, skin: 0, prop: "mail" },
    meetings: { shirt: "#2bb3a3", hair: 1, skin: 1, prop: "notes" },
    transcribe: { shirt: "#e86a92", hair: 2, skin: 0, prop: "tape", phones: true },
    briefs: { shirt: "#3c64c8", hair: 3, skin: 2, prop: "folders" },
    digests: { shirt: "#e8903a", hair: 4, skin: 0, prop: "paper" },
    kb: { shirt: "#9a6a3c", hair: 3, skin: 1, prop: "books" },
    bot: { shirt: "#4caf50", hair: 1, skin: 2, prop: "phone" },
    chat: { shirt: "#4fb3e8", hair: 0, skin: 1, prop: "mugs" },
    courier: { shirt: "#d9534f", hair: 4, skin: 2, prop: "parcels", cap: true }
  };
  var DAY = { night: false, bg0: "#f1e9de", bg1: "#ddcfbd", wood: ["#cfac85", "#c7a47c", "#d4b38f", "#c29e76"], seam: "#a8845f",
    slab: "#7d6650", wall: "#ebe0d2", wallTop: "#f7efe6", base: "#bba38a" };
  var NIGHT = { night: true, bg0: "#231a28", bg1: "#100c14", wood: ["#4a3631", "#45322d", "#503b35", "#43302b"], seam: "#30221d",
    slab: "#24191c", wall: "#523739", wallTop: "#6e4d4e", base: "#2f2124" };
  var BUBBLE = { working: ["#f1e3ff", "#3a2a4f"], error: ["#ffd5d5", "#7a1f1f"], waiting: ["#ffe0b8", "#6b3d06"],
    blocked: ["#fff0a8", "#5e4b00"], queued: ["#e9ecef", "#3a3f47"], chat: ["#ffffff", "#333"], hover: ["#ffffff", "#222"] };
  var DOT = { working: "#51cf66", error: "#ff6b6b", waiting: "#ffa94d", blocked: "#ffd43b", queued: "#ffd43b" };
  var CHATTER = { coffee: ["☕", "Кофе?", "…"], sofa: ["…", "ха-ха", "Как дела?", "👍"], sit: ["…", "Обсудим?"], stand: ["…", "Уф"] };

  var data = null, world = null, pal = NIGHT;
  var U = 5.4, OX = 0, OY = 0, dpr = 1, cw = 0, ch = 0, camWorld = null;
  var chars = {}, cat = null, hover = null, selected = null, hits = [];
  var pending = false, onScreen = true, clockTimer = 0, wakeTimer = 0, lastT = 0;
  var drag = null, mouse = null, keyPan = null;

  // --- геометрия: мир в «кубиках», x — вправо-вниз, y — влево-вниз, z — вверх --------------------------------
  function P(x, y, z) { return [OX + (x - y) * U, OY + (x + y) * U * 0.5 - z * U]; }
  function flat() { ctx.setTransform(dpr, 0, 0, dpr, 0, 0); }
  // плоскость в мире как лист бумаги: пол (x, y), стена напротив (вдоль x), стена слева (вдоль −y — так текст не зеркальный)
  function plane(kind, x, y, z, k) {
    var o = P(x, y, z), s = dpr * U / (k || 1), ox = dpr * o[0], oy = dpr * o[1];
    if (kind === "floor") ctx.setTransform(s, s / 2, -s, s / 2, ox, oy);
    else if (kind === "wallY") ctx.setTransform(s, s / 2, 0, s, ox, oy);
    else ctx.setTransform(s, -s / 2, 0, s, ox, oy);
  }
  var shades = {};
  function shade(hex, k) {
    var key = hex + k;
    if (shades[key]) return shades[key];
    var n = parseInt(hex.slice(1), 16), c = [n >> 16, (n >> 8) & 255, n & 255].map(function (v) {
      return Math.round(k > 0 ? v + (255 - v) * k : v * (1 + k));
    });
    return (shades[key] = "rgb(" + c.join(",") + ")");
  }
  function hash(s) { var h = 7; for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h); }
  function rnd(a, b) { return a + Math.random() * (b - a); }

  function box(list, x, y, z, w, d, h, c, extra) {
    var b = { x0: x, y0: y, z0: z, x1: x + w, y1: y + d, z1: z + h, c: c };
    if (extra) for (var k in extra) b[k] = extra[k];
    list.push(b);
    return b;
  }
  function quad(a, b, c, d, fill) {
    ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.lineTo(c[0], c[1]); ctx.lineTo(d[0], d[1]); ctx.closePath();
    ctx.fillStyle = fill; ctx.fill(); ctx.strokeStyle = fill; ctx.stroke();  // обводка тем же цветом — без щелей между гранями
  }
  function drawBox(b) {
    if (b.draw) { b.draw(b); flat(); return; }
    var was = ctx.globalAlpha;
    if (b.alpha) ctx.globalAlpha = was * b.alpha;
    var top = [P(b.x0, b.y0, b.z1), P(b.x1, b.y0, b.z1), P(b.x1, b.y1, b.z1), P(b.x0, b.y1, b.z1)];
    var e = P(b.x0, b.y1, b.z0), f = P(b.x1, b.y1, b.z0), g = P(b.x1, b.y0, b.z0);
    quad(e, f, top[2], top[3], b.left || b.c);
    quad(f, g, top[1], top[2], b.right || shade(b.c, -0.26));
    quad(top[0], top[1], top[2], top[3], b.top || shade(b.c, 0.16));
    ctx.globalAlpha = was;
  }
  // кто кого заслоняет: a рисуем раньше b, если между ними есть разделяющая плоскость и a — по дальнюю сторону
  function behind(a, b) {
    if (a.x1 <= b.x0) return true; if (b.x1 <= a.x0) return false;
    if (a.y1 <= b.y0) return true; if (b.y1 <= a.y0) return false;
    if (a.z1 <= b.z0) return true; if (b.z1 <= a.z0) return false;
    return a.x0 + a.x1 + a.y0 + a.y1 < b.x0 + b.x1 + b.y0 + b.y1;
  }
  function rect(b) {  // прямоугольник на экране при U = 1 — пересекаются ли на экране, от масштаба не зависит
    return [b.x0 - b.y1, (b.x0 + b.y0) / 2 - b.z1, b.x1 - b.y0, (b.x1 + b.y1) / 2 - b.z0];
  }
  function depthSort(items) {
    var n = items.length, r = items.map(rect), next = items.map(function () { return []; }), indeg = new Array(n).fill(0);
    for (var i = 0; i < n; i++) for (var j = i + 1; j < n; j++) {
      if (r[i][0] >= r[j][2] || r[j][0] >= r[i][2] || r[i][1] >= r[j][3] || r[j][1] >= r[i][3]) continue;
      if (behind(items[i], items[j])) { next[i].push(j); indeg[j]++; } else { next[j].push(i); indeg[i]++; }
    }
    var out = [], queue = [];
    for (i = 0; i < n; i++) if (!indeg[i]) queue.push(i);
    while (queue.length) {
      var k = queue.shift();
      out.push(items[k]);
      next[k].forEach(function (m) { if (!--indeg[m]) queue.push(m); });
    }
    if (out.length < n) items.forEach(function (it) { if (out.indexOf(it) < 0) out.push(it); });  // круг — как получится
    return out;
  }
  function bounds(list) {
    var b = { x0: Infinity, y0: Infinity, z0: Infinity, x1: -Infinity, y1: -Infinity, z1: -Infinity };
    list.forEach(function (it) {
      b.x0 = Math.min(b.x0, it.x0); b.y0 = Math.min(b.y0, it.y0); b.z0 = Math.min(b.z0, it.z0);
      b.x1 = Math.max(b.x1, it.x1); b.y1 = Math.max(b.y1, it.y1); b.z1 = Math.max(b.z1, it.z1);
    });
    return b;
  }
  function group(key, base, extra) { var g = bounds(base); g.key = key; g.base = base; g.sorted = null; if (extra) for (var k in extra) g[k] = extra[k]; return g; }

  // --- офис: open space на каждый офис, коридор, лаунж ------------------------------------------------------
  function build(areas) {
    var x = 6, maxRows = 1, out = [], stations = [], groups = [], spots = [];
    areas.forEach(function (a, ai) {
      var cells = [], zones = [], cols = 3, rows = 1;
      if (a.kind === "pulse") {
        a.desks.forEach(function (d, i) { cells.push({ col: i % 3, row: Math.floor(i / 3), d: d }); });
        rows = Math.max(1, Math.ceil(a.desks.length / 3));
      } else {  // проекты подряд, по 4 стола в ряд; проект не разрываем между рядами
        var col = 0, row = 0, used = 0;
        a.zones.forEach(function (z) {
          var n = z.desks.length, color = ZONES[z.color % ZONES.length];
          if (col && col + n > 4) { col = 0; row++; }
          var zone = { col: col, row: row, n: n, name: z.name, color: color, cells: [] };
          zones.push(zone);
          z.desks.forEach(function (d, i) { var c = { col: col + i, row: row, d: d, color: color }; cells.push(c); zone.cells.push(c); });
          col += n; used = Math.max(used, col);
        });
        cols = Math.max(2, used); rows = row + 1;
      }
      var area = { key: a.key, kind: a.kind, name: a.name, board: a.board || [], x0: x, x1: x + 8 + cols * CELL_W,
        rows: rows, color: AREA[ai % AREA.length], spine: x + 3.5, cells: cells, zones: zones };
      cells.forEach(function (c) { c.cx = x + 6 + c.col * CELL_W; c.cy = Y0 + c.row * CELL_D; c.area = area; });
      zones.forEach(function (z) { z.x = x + 6 + z.col * CELL_W + 1; z.y = Y0 + z.row * CELL_D + 1; z.w = z.n * CELL_W - 2; z.d = CELL_D - 2; });
      out.push(area);
      maxRows = Math.max(maxRows, rows);
      x = area.x1 + 10;
    });
    var W = Math.max(x - 2, 132), corrY = Y0 + maxRows * CELL_D + 6, ly = corrY + 8, D = ly + 40;
    var w = { W: W, D: D, corrY: corrY, ly: ly, areas: out, stations: stations, groups: groups, spots: spots, lights: [] };
    out.forEach(function (area, i) {
      area.y1 = corrY - 4;
      area.cells.forEach(function (c) { stations.push(station(w, c)); });
      if (i < out.length - 1) {  // перегородка до коридора: низ — светлая панель, верх — стекло
        var px = area.x1 + 5, part = [];
        box(part, px - 0.6, 0, 0, 1.2, corrY - 9, 7, pal.night ? "#5b4a52" : "#d9d2c8");
        box(part, px - 0.3, 0, 7, 0.6, corrY - 9, 7, "#9fd0e8", { alpha: 0.28 });
        groups.push(group("part" + i, part));
      }
      decorArea(w, area);
    });
    lounge(w);
    return w;
  }
  function decorArea(w, area) {
    var p = [];  // растения по углам open space
    plant(p, area.x1 - 3, area.y1 - 6, 1);
    w.groups.push(group("plant-a" + area.key, p));
    if (area === w.areas[0]) {  // книжный шкаф у левой стены
      var s = [];
      shelfX(s, 0.4, 40);
      w.groups.push(group("shelf", s));
    }
  }
  function lounge(w) {
    var ly = w.ly, corrY = w.corrY, W = w.W, g;
    // кухня у левой стены: стойка, кофемашина, холодильник
    var k = [];
    box(k, 0.4, ly + 2, 0, 5, 24, 7, "#8a6248");
    box(k, 0.2, ly + 1.8, 7, 5.6, 24.4, 0.7, "#d8d2c8");
    box(k, 1, ly + 4, 7.7, 3.6, 4, 5, "#2b2b33", { top: "#3a3a44" });
    box(k, 4.6, ly + 5, 10.5, 0.1, 1, 0.6, "#ff5a5a");
    box(k, 1.6, ly + 12, 7.7, 2.4, 2.4, 2.6, "#c94f3d");
    [[1.4, ly + 17], [3, ly + 18.2], [1.6, ly + 20]].forEach(function (m) { box(k, m[0], m[1], 7.7, 1.3, 1.3, 1.6, "#f4f1ea", { top: "#5a3a22" }); });
    box(k, 0.4, ly + 28, 0, 5.6, 7, 19, "#e9eef2", { top: "#f8fbfd" });
    box(k, 5.95, ly + 29, 9, 0.1, 0.6, 5, "#9aa3ad");
    w.groups.push(group("kitchen", k));
    [["k1", ly + 6, "coffee"], ["k2", ly + 13.5, "coffee"], ["k3", ly + 21, "stand"]].forEach(function (s) {
      spot(w, { id: s[0], x: 9.5, y: s[1], dir: 3, pose: s[2], route: [[13, s[1], "v:kitchen"], [13, corrY, "corr"]] });
    });
    // диваны с журнальными столиками на ковре
    var rug = { x: 20, y: ly + 1, w: 50, d: 23 };
    w.rugs = [rug];
    [24, 48].forEach(function (sx, n) {
      var list = [];
      sofa(list, sx, ly + 4, 18, n ? "#5d7fb8" : "#b0566b");
      g = group("sofa" + n, list);
      w.groups.push(g);
      for (var i = 0; i < 3; i++) {
        var x = sx + 3 + i * 6;
        spot(w, { id: "s" + n + i, x: x, y: ly + 7.7, dir: 2, pose: "sofa", seatZ: 4, group: g,
          route: [[x, ly + 13, "l:sofas"], [20, ly + 13, "v:sofas"], [20, corrY, "corr"]] });
      }
    });
    var t = [];
    box(t, 27, ly + 15, 0, 38, 4, 3.4, "#7a5238", { top: "#8c6244" });
    box(t, 31, ly + 15.8, 3.4, 1.4, 1.4, 1.8, "#f4f1ea", { top: "#5a3a22" });
    box(t, 52, ly + 16.2, 3.4, 3, 2.2, 0.4, "#c0563f"); box(t, 52.3, ly + 16.4, 3.8, 2.6, 2, 0.4, "#4f8fd1");
    w.groups.push(group("tables", t));
    var p1 = []; plant(p1, 16.5, ly + 2, 1.2); w.groups.push(group("plant-l1", p1));
    var l1 = []; lamp(l1, 72, ly + 3); w.groups.push(group("lamp-l1", l1)); w.lights.push([73.2, ly + 4.2, 15.5]);
    // переговорный стол
    var mx = 92, my = ly + 17;
    if (mx + 16 < W) {
      var m = [];
      box(m, mx - 1, my - 1, 0, 2, 2, 6.4, "#3a3a40"); box(m, mx - 3, my - 3, 0, 6, 6, 0.5, "#3a3a40");
      box(m, mx - 6, my - 4, 6.4, 12, 8, 0.8, "#c98a55", { top: "#d99a63" });
      box(m, mx - 4, my - 6, 6.4, 8, 12, 0.8, "#c98a55", { top: "#d99a63" });
      [[0, -9.5, 2], [0, 9.5, 0], [-9.5, 0, 1], [9.5, 0, 3]].forEach(function (c, i) { stool(m, mx + c[0], my + c[1], c[2]); });
      g = group("meet", m);
      w.groups.push(g);
      spot(w, { id: "m0", x: mx, y: my - 9.5, dir: 2, pose: "sit", seatZ: 4.5, group: g, route: [[mx, corrY, "corr"]] });
      spot(w, { id: "m1", x: mx, y: my + 9.5, dir: 0, pose: "sit", seatZ: 4.5, group: g, route: [[mx + 15, my + 9.5, "l:meetS"], [mx + 15, corrY, "corr"]] });
      spot(w, { id: "m2", x: mx - 9.5, y: my, dir: 1, pose: "sit", seatZ: 4.5, group: g, route: [[mx - 9.5, corrY, "corr"]] });
      spot(w, { id: "m3", x: mx + 9.5, y: my, dir: 3, pose: "sit", seatZ: 4.5, group: g, route: [[mx + 9.5, corrY, "corr"]] });
    }
    // кулер с водой
    var wx = 118;
    if (wx + 8 < W) {
      var c = [];
      box(c, wx, ly + 2, 0, 3.2, 3.2, 9, "#e9eef2", { top: "#f8fbfd" });
      box(c, wx + 0.4, ly + 2.4, 9, 2.4, 2.4, 3.6, "#7cc6ee", { alpha: 0.7 });
      box(c, wx + 3.2, ly + 3.2, 6, 0.3, 0.8, 0.8, "#4f8fd1");
      w.groups.push(group("cooler", c));
      spot(w, { id: "w0", x: wx + 1.6, y: ly + 8.5, dir: 0, pose: "coffee", route: [[wx - 3, ly + 8.5, "l:cool"], [wx - 3, corrY, "corr"]] });
    }
    // перед каждым следующим open space — свой уголок отдыха: кресла-мешки, диван, растения, торшер
    w.areas.slice(1).forEach(function (a, n) {
      var x0 = Math.max(a.x0 + 4, 130), colors = ["#e8903a", "#5fae86", "#9c6fd1"];
      for (var b = 0; b < 3; b++) {
        var bx = x0 + 6 + b * 12;
        if (bx + 8 > W) return;
        var bag = [];
        box(bag, bx - 3, ly + 9, 0, 6, 6, 2.6, colors[b]);
        box(bag, bx - 3, ly + 8, 2.6, 6, 2, 3.4, shade(colors[b], -0.1));
        g = group("bag" + n + b, bag);
        w.groups.push(g);
        spot(w, { id: "b" + n + b, x: bx, y: ly + 11.2, dir: 2, pose: "sofa", seatZ: 2.6, group: g,
          route: [[bx, ly + 22, "l:bags" + n], [x0, ly + 22, "v:bags" + n], [x0, corrY, "corr"]] });
      }
      var sx = x0 + 46;
      if (sx + 26 < W) {
        var list = [];
        sofa(list, sx, ly + 4, 18, ["#3fa08a", "#c95a8a", "#5d7fb8"][n % 3]);
        g = group("sofa-a" + n, list);
        w.groups.push(g);
        for (var i = 0; i < 3; i++) {
          var x = sx + 3 + i * 6;
          spot(w, { id: "sa" + n + i, x: x, y: ly + 7.7, dir: 2, pose: "sofa", seatZ: 4, group: g,
            route: [[x, ly + 13, "l:sofa-a" + n], [sx - 4, ly + 13, "v:sofa-a" + n], [sx - 4, corrY, "corr"]] });
        }
        var tb = [];
        box(tb, sx + 3, ly + 15, 0, 12, 4, 3.4, "#7a5238", { top: "#8c6244" });
        box(tb, sx + 6, ly + 15.8, 3.4, 1.4, 1.4, 1.8, "#f4f1ea", { top: "#5a3a22" });
        w.groups.push(group("table-a" + n, tb));
        w.rugs.push({ x: sx - 4, y: ly + 1, w: 26, d: 22 });
        var lp = []; lamp(lp, sx + 22, ly + 3); w.groups.push(group("lamp-a" + n, lp)); w.lights.push([sx + 23.2, ly + 4.2, 15.5]);
      }
      var pp = []; plant(pp, Math.min(a.x1, W - 6), ly + 2, 1.3); w.groups.push(group("plant-a2" + n, pp));
    });
    var p3 = []; plant(p3, 2, w.D - 6, 1); w.groups.push(group("plant-l3", p3));
  }
  function spot(w, s) { s.owner = null; s.lounge = true; w.spots.push(s); return s; }

  function sofa(list, x, y, wd, color) {
    box(list, x, y, 0, wd, 2.2, 9, shade(color, -0.08));
    box(list, x, y + 2.2, 0, wd, 4.8, 4, color);
    box(list, x - 1.6, y, 0, 1.6, 7, 6.4, shade(color, -0.05)); box(list, x + wd, y, 0, 1.6, 7, 6.4, shade(color, -0.05));
  }
  function stool(list, x, y, dir) {
    box(list, x - 0.4, y - 0.4, 0, 0.8, 0.8, 3.6, "#3a3a40");
    box(list, x - 2.4, y - 2.4, 3.6, 4.8, 4.8, 0.9, "#4a3a5c");
    var bx = dir === 2 ? [x - 2.4, y - 3.1, 4.8, 0.7] : dir === 0 ? [x - 2.4, y + 2.4, 4.8, 0.7] : dir === 1 ? [x - 3.1, y - 2.4, 0.7, 4.8] : [x + 2.4, y - 2.4, 0.7, 4.8];
    box(list, bx[0], bx[1], 4.5, bx[2], bx[3], 4.2, "#4a3a5c");
  }
  function shelfX(list, x, y) {  // шкаф вдоль левой стены
    var wood = "#6e4a33", cols = ["#c0563f", "#e0b13c", "#4f8fd1", "#5fae86", "#9c6fd1", "#d07a3a"];
    box(list, x, y, 0, 3, 0.8, 17, wood); box(list, x, y + 9.2, 0, 3, 0.8, 17, wood);
    box(list, x, y, 0, 0.5, 10, 17, shade(wood, -0.2));
    [0, 5.5, 11, 16.4].forEach(function (z) { box(list, x + 0.5, y + 0.8, z, 2.5, 8.4, 0.6, wood); });
    [0.6, 6.1, 11.6].forEach(function (z, r) {
      for (var k = 0; k < 7; k++) box(list, x + 0.9, y + 1 + k * 1.15, z, 1.9, 1, 3.4 + ((k + r) % 3) * 0.5, cols[(k * 2 + r) % cols.length]);
    });
  }
  function plant(list, x, y, size) {
    box(list, x, y, 0, 2.6 * size, 2.6 * size, 2.4 * size, "#a5573a");
    box(list, x + 0.3, y + 0.3, 2.4 * size, 2 * size, 2 * size, 0.3, "#3b2a1f");
    var leaf = "#3f8f4e";
    box(list, x + 0.9 * size, y + 0.9 * size, 2.4 * size, 0.8, 0.8, 3 * size, "#2f6b3a");
    box(list, x - 0.4, y + 0.6, 4 * size, 1.8 * size, 1.4, 1, leaf);
    box(list, x + 1.5 * size, y + 0.2, 4.6 * size, 1.8 * size, 1.4, 1, shade(leaf, 0.1));
    box(list, x + 0.5, y + 1.4 * size, 5.2 * size, 1.6, 1.6 * size, 1, shade(leaf, 0.2));
  }
  function lamp(list, x, y) {
    box(list, x, y, 0, 2.4, 2.4, 0.5, "#2b2b30");
    box(list, x + 0.9, y + 0.9, 0.5, 0.6, 0.6, 14, "#3a3a40");
    box(list, x - 0.3, y - 0.3, 14.5, 3, 3, 2.6, "#f3d9a4", { top: "#fff3d6" });
  }

  // --- рабочее место: стол, монитор, стул и что лежит на столе; сотрудник садится сюда, когда работает ---------
  function station(w, cell) {
    var d = cell.d, cx = cell.cx, cy = cell.cy, items = [], role = ROLES[d.role] || null, seed = hash(d.id);
    var wood = "#8a5a3c", legs = "#5e3d2a", chair = "#4a3a5c", dark = "#24242c";
    box(items, cx + 4, cy + 4, 7, 16, 7, 1, wood);
    [[4.3, 4.3], [18.7, 4.3], [4.3, 9.7], [18.7, 9.7]].forEach(function (p) { box(items, cx + p[0], cy + p[1], 0, 1, 1, 7, legs); });
    box(items, cx + 7.4, cy + 4.6, 8, 3.2, 2.2, 0.4, dark);
    box(items, cx + 8.6, cy + 5.4, 8.4, 0.8, 0.8, 1.8, dark);
    var st = { cell: cell, id: d.id, cx: cx, cy: cy, seed: seed, role: role };
    var frame = box(items, cx + 5, cy + 5, 10.2, 8, 1, 5.4, "#1d1d24");
    items.push({ x0: frame.x0, x1: frame.x1, y0: frame.y1, y1: frame.y1 + 0.05, z0: frame.z0, z1: frame.z1,
      draw: function () { screen(frame, st.cell.d, seed); } });
    box(items, cx + 9.6, cy + 8.3, 8, 5, 1.8, 0.4, "#34343e");
    props(items, role ? role.prop : ["mugs", "paper", "books", "notes"][seed % 4], cx, cy);
    box(items, cx + 12, cy + 14.8, 0, 1, 1, 4, dark);
    box(items, cx + 10.2, cy + 14.3, 0, 4.6, 2, 0.4, dark);
    box(items, cx + 9.5, cy + 12.4, 4, 6, 5.6, 1, chair);
    box(items, cx + 9.5, cy + 17.2, 5, 6, 1, 4.4, chair);
    st.group = group("st:" + d.id, items, { station: st });
    var aisle = "a:" + cell.area.key + ":" + cell.row, spine = cell.area.spine, ay = cy + 23;
    st.chair = { id: "chair:" + d.id, x: cx + 12.5, y: cy + 14.5, dir: 0, seatZ: 5, group: st.group,
      route: [[cx + 17.5, cy + 14.5, "c:" + d.id], [cx + 17.5, ay, aisle], [spine, ay, "s:" + cell.area.key], [spine, w.corrY, "corr"]] };
    st.beside = { id: "beside:" + d.id, x: cx + 21.5, y: cy + 19.5, dir: 2,
      route: [[cx + 21.5, ay, aisle], [spine, ay, "s:" + cell.area.key], [spine, w.corrY, "corr"]] };
    w.groups.push(st.group);
    return st;
  }
  function props(list, kind, cx, cy) {
    var x = cx + 15.4, y = cy + 5.2;
    if (kind === "mail") {
      [0, 0.35, 0.7].forEach(function (z, k) { box(list, x + k * 0.2, y + 0.6 - k * 0.15, 8 + z, 3.4, 2.4, 0.35, "#f2efe6"); });
      box(list, x + 1.4, y + 1.2, 9.05, 0.6, 0.6, 0.1, "#d64545");
    } else if (kind === "notes") {
      box(list, x, y + 1.2, 8, 3, 3.4, 0.3, "#f3d36b");
      box(list, x + 3.2, y + 0.4, 8, 0.9, 0.9, 2.6, "#2a2a30"); box(list, x + 3, y + 0.2, 10.6, 1.3, 1.3, 1.1, "#5a5a66");
    } else if (kind === "tape") {
      box(list, x, y + 0.6, 8, 3.6, 2.6, 1.2, "#3a3a44");
      box(list, x + 0.5, y + 1.1, 9.2, 1, 1, 0.1, "#ddd"); box(list, x + 2.1, y + 1.1, 9.2, 1, 1, 0.1, "#ddd");
    } else if (kind === "folders") {
      box(list, x, y, 8, 3.6, 4, 0.5, "#3c9a5c"); box(list, x + 0.2, y + 0.2, 8.5, 3.4, 3.8, 0.5, "#3c64c8");
      box(list, x + 0.1, y + 0.1, 9, 3.5, 3.9, 0.5, "#e0b13c");
    } else if (kind === "paper") {
      box(list, x - 0.2, y + 0.8, 8, 4.2, 3, 0.3, "#dcd8cc");
      [0, 1, 2].forEach(function (k) { box(list, x + 0.3, y + 1.2 + k * 0.8, 8.3, 3.2, 0.3, 0.02, "#8a8a8a"); });
    } else if (kind === "books") {
      ["#c0563f", "#4f8fd1", "#e0b13c", "#5fae86"].forEach(function (c, k) { box(list, x + k * 0.95, y, 8, 0.85, 3, 3 + (k % 2) * 0.6, c); });
    } else if (kind === "phone") {
      box(list, x + 0.4, y + 1, 8, 2.6, 1.8, 0.9, "#26262c"); box(list, x + 0.5, y + 0.8, 8.9, 2.4, 0.8, 0.5, "#3a3a42");
    } else if (kind === "mugs") {
      box(list, x + 0.4, y + 0.6, 8, 1.4, 1.4, 1.8, "#f4f1ea", { top: "#5a3a22" });
      box(list, x + 2.4, y + 2, 8, 1.4, 1.4, 1.8, "#e86a92", { top: "#5a3a22" });
    } else if (kind === "parcels") {
      box(list, cx + 20.6, cy + 4.6, 0, 4, 4, 3, "#c49a62"); box(list, cx + 21.1, cy + 5.1, 3, 3, 3, 2.4, "#b88a52");
      box(list, cx + 22.4, cy + 5.1, 5.4, 0.4, 3, 0.05, "#e8d4a8");
    }
  }
  function screen(frame, d, seed) {  // экран монитора как лист на его передней грани
    var w = frame.x1 - frame.x0 - 0.8, h = frame.z1 - frame.z0 - 0.8, st = d.state, t = now();
    plane("wallY", frame.x0 + 0.4, frame.y1 + 0.01, frame.z1 - 0.4);
    var bgc = { working: "#0e2236", queued: "#2e2610", error: "#3a1214", idle: "#101722", waiting: "#2c1d0e", blocked: "#2c260e" }[st] || "#08080b";
    ctx.fillStyle = bgc; ctx.fillRect(0, 0, w, h);
    if (st === "off" || st === "rest") return;
    ctx.save(); ctx.beginPath(); ctx.rect(0, 0, w, h); ctx.clip();
    var cols = st === "error" ? ["#ff6b6b", "#ffa8a8"] : st === "queued" || st === "waiting" || st === "blocked" ? ["#ffd166", "#f4a261"] : ["#7ee787", "#79c0ff", "#d2a8ff", "#ffa657"];
    var go = st === "working" && !reduce, scroll = go ? (t * 1.4) % 0.8 : 0, first = go ? Math.floor(t * 1.4 / 0.8) : 0;
    ctx.globalAlpha = st === "idle" ? 0.35 : 0.95;
    for (var i = 0; i < 7; i++) {
      var n = seed + first + i, len = 1.2 + ((n * 7919) % 50) / 10, ind = ((n * 31) % 4) * 0.6;
      ctx.fillStyle = cols[n % cols.length];
      ctx.fillRect(0.4 + ind, 0.4 + i * 0.8 - scroll, Math.min(len, w - 0.8 - ind), 0.38);
    }
    if (st === "error") { ctx.globalAlpha = 1; ctx.fillStyle = "#ff6b6b"; ctx.fillRect(w / 2 - 0.3, 0.8, 0.6, 2); ctx.fillRect(w / 2 - 0.3, 3.2, 0.6, 0.6); }
    ctx.restore();
  }
  function robot(list, x, y, t) {  // субагент: робот-помощник за ноутбуком сбоку от стола, огонёк антенны мигает
    var metal = "#9aa3b5";
    box(list, x - 1.6, y - 3.2, 0, 3.2, 2.2, 5.6, "#5e5e6e");
    box(list, x - 1.4, y - 3, 5.6, 2.8, 1.8, 0.25, "#2c2c34");
    box(list, x - 1.3, y - 3.1, 5.85, 2.6, 0.2, 1.7, "#2c2c34", { left: "#3d8bfd" });
    box(list, x - 1.2, y, 0, 1, 1, 4.2, metal); box(list, x + 0.2, y, 0, 1, 1, 4.2, metal);
    box(list, x - 1.6, y - 0.3, 4.2, 3.2, 2.2, 3.4, metal);
    var bob = reduce ? 0 : 0.25 * Math.sin(t * 15);
    box(list, x - 2.4, y - 1.8, 6.2 + bob, 0.8, 2.4, 0.8, shade(metal, -0.15));
    box(list, x + 1.6, y - 1.8, 6.2 - bob, 0.8, 2.4, 0.8, shade(metal, -0.15));
    box(list, x - 1.4, y - 0.4, 7.6, 2.8, 2.6, 2.4, shade(metal, 0.1));
    box(list, x - 0.15, y + 0.8, 10, 0.3, 0.3, 1.2, "#555");
    box(list, x - 0.3, y + 0.65, 11.2, 0.6, 0.6, 0.6, reduce || Math.sin(t * 5) > 0 ? "#4fe3ff" : "#1f6f80");
  }

  // --- сотрудник: строим лицом к −y и поворачиваем; поза — сидит, печатает, стоит, идёт, машет, пьёт кофе -------
  function rot(x, y, dir) { return dir === 0 ? [x, y] : dir === 1 ? [-y, x] : dir === 2 ? [-x, -y] : [y, -x]; }
  function person(list, wx, wy, dir, pose, look, t, seed, s) {
    var L = [], pants = "#2e3550", skin = look.skin, shirt = look.shirt, hair = look.hair, eye = "#1d1d24";
    function lb(x, y, z, w, d, h, c, extra) { L.push([x, y, z, w, d, h, c, extra]); }
    function head(z) {
      lb(-2, -2, z, 4, 4, 3.8, skin);
      lb(-2.15, -2.15, z + 3.8, 4.3, 4.3, 0.8, hair);
      lb(-2.15, 2, z + 0.6, 4.3, 0.5, 3.2, hair);
      lb(-1.2, -2.12, z + 1.8, 0.7, 0.12, 0.8, eye); lb(0.5, -2.12, z + 1.8, 0.7, 0.12, 0.8, eye);
      lb(-0.5, -2.12, z + 0.7, 1, 0.12, 0.3, "#9b4a4a");
      if (look.cap) lb(-2.3, -3.7, z + 4.6, 4.6, 5.8, 0.9, shirt);
      if (look.phones) { lb(-2.6, -0.7, z + 1, 0.6, 1.8, 2, "#2a2a30"); lb(2, -0.7, z + 1, 0.6, 1.8, 2, "#2a2a30"); lb(-2.3, -0.3, z + 4.6, 4.6, 0.8, 0.5, "#2a2a30"); }
    }
    if (pose === "type" || pose === "sit" || pose === "sofa") {  // сидит; (0, 0) — середина туловища
      lb(-1.9, -4.5, 0, 1.6, 1.6, s, pants); lb(0.3, -4.5, 0, 1.6, 1.6, s, pants);
      lb(-1.9, -4.5, s, 1.6, 4.4, 1.4, pants); lb(0.3, -4.5, s, 1.6, 4.4, 1.4, pants);
      lb(-2.2, -1.5, s + 1.4, 4.4, 3, 4.6, shirt);
      var lift = pose === "type" && !reduce ? 0.35 : 0, ph = t * 13 + seed;
      [[-3.4, lift * (0.5 + 0.5 * Math.sin(ph))], [2.2, lift * (0.5 + 0.5 * Math.sin(ph + Math.PI))]].forEach(function (a) {
        if (pose === "sofa") {
          lb(a[0], -0.9, s + 2.6, 1.2, 1.4, 3.4, shirt); lb(a[0], -3.6, s + 1.5, 1.2, 2.7, 1.1, shirt); lb(a[0], -4.2, s + 1.5, 1.2, 0.6, 1, skin);
        } else {
          lb(a[0], -0.9, s + 3.4, 1.2, 1.4, 2.6, shirt); lb(a[0], -5.5, s + 3.4 + a[1], 1.2, 4.6, 1.1, shirt); lb(a[0], -6.1, s + 3.4 + a[1], 1.2, 0.6, 1, skin);
        }
      });
      head(s + 6 + (pose === "type" && !reduce ? 0.12 * Math.sin(t * 2.2 + seed) : 0));
    } else {  // стоит; (0, 0) — между ступнями
      var walk = pose === "walk" && !reduce, sw = walk ? Math.sin(t * 9 + seed) : 0, bob = walk ? 0.25 * Math.abs(sw) : 0;
      lb(-1.9, -0.8 + sw * 0.9, 0, 1.6, 1.6, 6, pants); lb(0.3, -0.8 - sw * 0.9, 0, 1.6, 1.6, 6, pants);
      lb(-2.2, -1.5, 6 + bob, 4.4, 3, 4.6, shirt);
      lb(-3.4, -0.7 - sw * 0.8, 6.6 + bob, 1.2, 1.4, 4, shirt); lb(-3.4, -0.7 - sw * 0.8, 6 + bob, 1.2, 1.4, 0.6, skin);
      if (pose === "wave") {  // ждёт ответа — машет вам
        var wob = reduce ? 0 : 0.6 * Math.sin(t * 7 + seed);
        lb(2.2, -0.7, 9.4, 1.2, 1.4, 3.4, shirt); lb(2.2 + wob, -0.7, 12.8, 1.2, 1.4, 1.1, skin);
      } else if (pose === "coffee") {  // с чашкой, иногда отпивает
        var sip = reduce ? 0 : Math.max(0, Math.sin(t * 0.9 + seed)) > 0.92 ? 1.6 : 0;
        lb(2.2, -0.7, 8.6, 1.2, 1.4, 2, shirt); lb(2.2, -3, 8.4 + sip, 1.2, 2.3, 1, shirt);
        lb(2, -4.4, 8.6 + sip, 1.6, 1.4, 1.6, "#f4f1ea", { top: "#5a3a22" });
      } else {
        lb(2.2, -0.7 + sw * 0.8, 6.6 + bob, 1.2, 1.4, 4, shirt); lb(2.2, -0.7 + sw * 0.8, 6 + bob, 1.2, 1.4, 0.6, skin);
      }
      head(10.6 + bob);
    }
    L.forEach(function (b) {
      var a = rot(b[0], b[1], dir), c = rot(b[0] + b[3], b[1] + b[4], dir);
      box(list, wx + Math.min(a[0], c[0]), wy + Math.min(a[1], c[1]), b[2], Math.abs(c[0] - a[0]), Math.abs(c[1] - a[1]), b[5], b[6], b[7]);
    });
  }
  function catModel(list, x, y, dir, sitting, t) {  // кот: ходит по коридору, иногда садится
    var L = [], fur = "#2a2a2e", step = sitting || reduce ? 0 : 0.5 * Math.sin(t * 12);
    function lb(a, b, z, w, d, h, c, e) { L.push([a, b, z, w, d, h, c, e]); }
    if (sitting) { lb(-1.2, -1, 0, 2.4, 3, 2.6, fur); lb(-1, -2.6, 2, 2, 2, 2, fur); }
    else {
      lb(-1, -2.4, 1.4, 2, 5, 1.8, fur);
      [[-1, -2.2], [0.4, -2.2], [-1, 1.8], [0.4, 1.8]].forEach(function (l, i) { lb(l[0], l[1] + (i % 2 ? step : -step) * 0.4, 0, 0.6, 0.6, 1.4, fur); });
      lb(-1.1, -4.2, 2.4, 2.2, 2, 2, fur);
    }
    var hz = sitting ? 4 : 4.4, hy = sitting ? -2.6 : -4.2;
    lb(-1, hy - 0.1, hz, 0.6, 0.6, 0.8, fur); lb(0.4, hy - 0.1, hz, 0.6, 0.6, 0.8, fur);
    lb(-0.7, hy - 0.12, hz - 1.2, 0.4, 0.1, 0.4, "#ffd43b"); lb(0.3, hy - 0.12, hz - 1.2, 0.4, 0.1, 0.4, "#ffd43b");
    lb(-0.3, sitting ? 1.8 : 2.6, sitting ? 0.2 : 2.6, 0.6, sitting ? 2.4 : 2.6, 0.6, fur);
    L.forEach(function (b) {
      var a = rot(b[0], b[1], dir), c = rot(b[0] + b[3], b[1] + b[4], dir);
      box(list, x + Math.min(a[0], c[0]), y + Math.min(a[1], c[1]), b[2], Math.abs(c[0] - a[0]), Math.abs(c[1] - a[1]), b[5], b[6], b[7]);
    });
  }

  // --- кто где: желаемое место, отдых, дорога ---------------------------------------------------------------
  function now() { return performance.now() / 1000; }
  function looks(st) {
    var r = st.role, seed = st.seed;
    return r ? { shirt: r.shirt, hair: HAIR[r.hair], skin: SKIN[r.skin], cap: r.cap, phones: r.phones }
      : { shirt: st.cell.color || ZONES[seed % ZONES.length], hair: HAIR[seed % HAIR.length], skin: SKIN[(seed >> 3) % SKIN.length] };
  }
  // свободный сотрудник PULSE отдыхает и ходит по офису; неактивная сессия папки сидит у себя полупрозрачной
  function idle(c) { return c.st.cell.d.state === "idle"; }
  function sleepy(st) { return st.cell.area.kind === "folder" && st.cell.d.state === "rest"; }
  function busy(state) { return state === "working" || state === "waiting" || state === "blocked" || state === "error" || state === "queued"; }
  function quiet(z) { return !z.cells.some(function (c) { return busy(c.d.state); }); }
  function want(c) {
    var s = c.st.cell.d.state;
    if (s === "working" || s === "queued" || s === "error") return c.st.chair;
    if (s === "waiting" || s === "blocked") return c.st.beside;
    return c.rest || c.st.chair;
  }
  function pickRest(c, t) {  // свободен: посидеть у себя или пойти в лаунж на свободное место
    var free = world.spots.filter(function (s) { return !s.owner || s.owner === c.id; });
    c.rest = Math.random() < 0.45 || !free.length ? c.st.chair : free[Math.floor(Math.random() * free.length)];
    c.nextAt = t + rnd(25, 70);
  }
  function sync(first) {
    var t = now(), seen = {};
    world.stations.forEach(function (st) {
      var d = st.cell.d;
      if (d.state === "off") return;
      seen[st.id] = true;
      var c = chars[st.id];
      if (!c) {
        c = chars[st.id] = { id: st.id, st: st, look: looks(st), seed: st.seed, path: null, at: null, x: 0, y: 0, dir: 0, chatAt: t + rnd(8, 30) };
        if (idle(c)) pickRest(c, t);
        if (first || !c.rest || c.rest === st.chair) place(c, want(c));  // новый сотрудник появляется сразу на месте
        else { place(c, st.chair); go(c, want(c)); }
      }
      c.st = st;
      if (!idle(c)) c.rest = null; else if (!c.rest) pickRest(c, t);
      var target = want(c);
      if (target !== c.at && target !== c.goal) go(c, target);
    });
    Object.keys(chars).forEach(function (id) { if (!seen[id]) { release(chars[id]); delete chars[id]; } });
    if (!cat) cat = { x: world.W * 0.3, y: world.corrY + 1, dir: 1, sit: true, until: t + rnd(4, 12), tx: 0 };
  }
  function reserve(c, s) { if (s && s.lounge) s.owner = c.id; }
  function release(c) { world.spots.forEach(function (s) { if (s.owner === c.id) s.owner = null; }); }
  function place(c, s) { release(c); reserve(c, s); c.at = s; c.goal = null; c.path = null; c.x = s.x; c.y = s.y; c.dir = s.dir; }
  function go(c, s) {
    if (c.path) { c.next = s; return; }  // идёт — дойдёт и пойдёт дальше
    if (reduce || !c.at) { place(c, s); return; }
    release(c); reserve(c, s);
    c.path = route(c.at, s); c.goal = s; c.at = null;
  }
  function route(a, b) {  // по проходам: от места к линии, общей с маршрутом цели (ряд, коридор), по ней — к цели
    var ra = a.route, rb = b.route;
    for (var i = 0; i < ra.length; i++) for (var j = 0; j < rb.length; j++) {
      if (ra[i][2] === rb[j][2]) return ra.slice(0, i + 1).concat(rb.slice(0, j + 1).reverse()).concat([[b.x, b.y]]);
    }
    return ra.concat(rb.slice().reverse()).concat([[b.x, b.y]]);
  }
  function update(dt, t) {
    Object.keys(chars).forEach(function (id) {
      var c = chars[id];
      if (c.path) {
        var left = SPEED * dt;
        while (left > 0 && c.path.length) {
          var p = c.path[0], dx = p[0] - c.x, dy = p[1] - c.y, dist = Math.hypot(dx, dy);
          if (dist > 0.01) c.dir = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 1 : 3) : (dy > 0 ? 2 : 0);
          if (dist <= left) { c.x = p[0]; c.y = p[1]; c.path.shift(); left -= dist; } else { c.x += dx / dist * left; c.y += dy / dist * left; left = 0; }
        }
        if (!c.path.length) {
          c.path = null; c.at = c.goal; c.goal = null; c.dir = c.at.dir;
          if (c.next) { var n = c.next; c.next = null; if (n !== c.at) go(c, n); }
        }
      } else if (idle(c) && t >= c.nextAt) {
        pickRest(c, t);
        if (c.rest !== c.at) go(c, c.rest);
      }
      if (!reduce && c.at && c.at.lounge && t >= c.chatAt) { c.chatEnd = t + 3; c.chatAt = t + rnd(15, 40); c.chatText = null; }
    });
    if (cat && !reduce) {
      if (cat.sit && t >= cat.until) { cat.sit = false; cat.tx = rnd(8, world.W - 8); }
      if (!cat.sit) {
        var dx = cat.tx - cat.x, stepx = 4 * dt;
        cat.dir = dx > 0 ? 1 : 3;
        if (Math.abs(dx) <= stepx) { cat.x = cat.tx; cat.sit = true; cat.until = t + rnd(6, 18); } else cat.x += Math.sign(dx) * stepx;
      }
    }
    if (mouse && !drag && mouse.edge && t - mouse.edgeSince > 0.3) {  // курсор у края — офис едет (как в стратегиях)
      OX -= mouse.edge[0] * 700 * dt; OY -= mouse.edge[1] * 700 * dt;
      clampCam();
    }
    if (keyPan) { OX -= keyPan[0] * 600 * dt; OY -= keyPan[1] * 600 * dt; clampCam(); }
  }
  function moving() {  // что сейчас движется — тогда нужны кадры
    if (mouse && mouse.edge && !drag) return true;
    if (keyPan) return true;
    if (reduce) return false;
    if (cat && !cat.sit) return true;
    return Object.keys(chars).some(function (id) {
      var c = chars[id], s = c.st.cell.d.state;
      return c.path || ((s === "working" || s === "waiting" || s === "blocked") && c.at && !c.at.lounge) || (c.st.cell.d.agents || []).length;
    });
  }
  function nextWake(t) {  // когда кто-нибудь из отдыхающих встанет, кот проснётся или кто-то заговорит
    var at = Infinity;
    Object.keys(chars).forEach(function (id) {
      var c = chars[id];
      if (idle(c)) at = Math.min(at, c.nextAt);
      if (c.at && c.at.lounge) at = Math.min(at, c.chatEnd > t ? c.chatEnd : c.chatAt);
    });
    if (cat && cat.sit) at = Math.min(at, cat.until);
    return at;
  }

  // --- камера: перетаскивание, края, зум ------------------------------------------------------------------
  function centerOn(wx, wy) { OX = cw / 2 - (wx - wy) * U; OY = ch / 2 - (wx + wy) * U / 2 + 4 * U; }
  function viewCenter() {
    var sx = (cw / 2 - OX) / U, sy = (ch / 2 - OY - 4 * U) / U;
    return [(sx + 2 * sy) / 2, (2 * sy - sx) / 2];
  }
  function clampCam() {  // середина вида не уходит за пределы пола
    if (!world) return;
    var c = viewCenter(), x = Math.max(0, Math.min(world.W, c[0])), y = Math.max(0, Math.min(world.D, c[1]));
    if (x !== c[0] || y !== c[1]) centerOn(x, y);
    camWorld = viewCenter();
  }
  function fitU() {
    var W = world.W, D = world.D;
    return Math.max(1.2, Math.min((cw - 30) / (W + D), (ch - 40) / ((W + D) / 2 + WALL_H + 4)));
  }
  function zoomTo(u, sx, sy) {
    if (!world) return;
    u = Math.max(fitU(), Math.min(ZOOMS[ZOOMS.length - 1], u));
    if (sx == null) { sx = cw / 2; sy = ch / 2; }
    var bx = (sx - OX) / U, by = (sy - OY) / U;  // точка под курсором остаётся под курсором
    U = u; OX = sx - bx * U; OY = sy - by * U;
    clampCam(); save(); redraw();
  }
  function zoomStep(k, sx, sy) {
    var next = k > 0 ? ZOOMS.filter(function (z) { return z > U + 0.01; })[0] : ZOOMS.filter(function (z) { return z < U - 0.01; }).pop();
    zoomTo(next == null ? (k > 0 ? U : fitU()) : next, sx, sy);
  }
  function fitAll() {
    U = fitU();
    var W = world.W, D = world.D, minX = -D * U, maxX = W * U, minY = -WALL_H * U, maxY = (W + D) / 2 * U;
    OX = (cw - minX - maxX) / 2; OY = (ch - minY - maxY) / 2 + 10;
    camWorld = viewCenter(); save(); redraw();
  }
  function save() { try { localStorage.setItem("office-cam", JSON.stringify({ u: U, c: camWorld })); } catch (e) { /* не запоминаем */ } }
  function restore() {
    var s = null;
    try { s = JSON.parse(localStorage.getItem("office-cam") || "null"); } catch (e) { s = null; }
    U = s && s.u ? s.u : ZOOMS[2];
    U = Math.max(fitU(), Math.min(ZOOMS[ZOOMS.length - 1], U));
    var c = s && s.c ? s.c : [world.W * 0.42, world.corrY * 0.75];
    centerOn(c[0], c[1]); clampCam();
  }

  // --- кадр -----------------------------------------------------------------------------------------------
  var patterns = {};
  function woodFloor() {  // доски вдоль x, стыки вразбежку (1 кубик = 4 точки узора)
    var key = "wood" + pal.night;
    if (patterns[key]) return patterns[key];
    var c = document.createElement("canvas"), g = c.getContext("2d");
    c.width = 64; c.height = 32;
    for (var r = 0; r < 4; r++) {
      g.fillStyle = pal.wood[r]; g.fillRect(0, r * 8, 64, 8);
      g.fillStyle = pal.seam; g.fillRect(0, r * 8 + 7, 64, 1); g.fillRect((r * 23 + 9) % 64, r * 8, 1, 8);
    }
    return (patterns[key] = ctx.createPattern(c, "repeat"));
  }
  function carpet(area) { return pal.night ? shade(area.color, -0.62) : shade(area.color, 0.62); }
  function drawRoom() {
    var W = world.W, D = world.D;
    flat();
    var g = ctx.createLinearGradient(0, 0, 0, ch);
    g.addColorStop(0, pal.bg0); g.addColorStop(1, pal.bg1);
    ctx.fillStyle = g; ctx.fillRect(0, 0, cw, ch);
    drawBox({ x0: 0, y0: 0, z0: -2.5, x1: W, y1: D, z1: 0, c: pal.slab, top: pal.wood[0] });
    plane("floor", 0, 0, 0, 4);
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = woodFloor(); ctx.fillRect(0, 0, W * 4, D * 4);
    ctx.imageSmoothingEnabled = true;
    plane("floor", 0, 0, 0);
    world.areas.forEach(function (a) {  // ковролин open space: ровный цвет и едва заметные стыки плиток
      ctx.fillStyle = carpet(a);
      ctx.fillRect(a.x0, 2, a.x1 - a.x0, a.y1 - 2);
      ctx.strokeStyle = pal.night ? "rgba(255,255,255,.035)" : "rgba(60,40,90,.06)"; ctx.lineWidth = 0.12;
      ctx.beginPath();
      for (var gx = a.x0 + 6; gx < a.x1; gx += 6) { ctx.moveTo(gx, 2); ctx.lineTo(gx, a.y1); }
      for (var gy = 8; gy < a.y1; gy += 6) { ctx.moveTo(a.x0, gy); ctx.lineTo(a.x1, gy); }
      ctx.stroke();
    });
    world.areas.forEach(function (a) {
      a.zones.forEach(function (z) {  // проект, где никто не работает и никто не ждёт, — коврик бледнее
        ctx.globalAlpha = quiet(z) ? 0.4 : 1;
        rug(z.x, z.y, z.w, z.d, shade(z.color, pal.night ? -0.5 : 0.5), 1.5);
        ctx.globalAlpha = 1;
      });
    });
    (world.rugs || []).forEach(function (r) { rug(r.x, r.y, r.w, r.d, pal.night ? "#3c4a40" : "#a9bba4", 3); });
    flat();
    drawBox({ x0: -1.5, y0: -1.5, z0: -2.5, x1: W, y1: 0, z1: WALL_H, c: pal.wall, top: pal.wallTop });
    drawBox({ x0: -1.5, y0: 0, z0: -2.5, x1: 0, y1: D, z1: WALL_H, c: pal.wall, top: pal.wallTop, right: shade(pal.wall, -0.14) });
    drawBox({ x0: 0, y0: 0, z0: 0, x1: W, y1: 0.4, z1: 1.2, c: pal.base });
    drawBox({ x0: 0, y0: 0.4, z0: 0, x1: 0.4, y1: D, z1: 1.2, c: pal.base });
    world.areas.forEach(function (a) {  // у каждого open space на стене: окно, вывеска, экран со сводкой
      var wdt = a.x1 - a.x0;
      windowOnWall(a.x0 + 7, 9, Math.min(18, wdt * 0.3), 12);
      plane("wallY", a.x0 + wdt / 2, 0.02, 25.6, 10);
      ctx.font = "900 64px system-ui, -apple-system, sans-serif"; ctx.textAlign = "center";
      ctx.fillStyle = a.color; ctx.fillText(a.name.toUpperCase(), 0, 0); ctx.textAlign = "left";
      flat();
      var tx = a.x1 - 25;
      drawBox({ x0: tx, y0: 0, z0: 9, x1: tx + 21, y1: 0.8, z1: 21, c: "#15151b" });
      plane("wallY", tx + 1, 0.81, 20, 10);
      ctx.fillStyle = "#6fb7ff"; ctx.font = "700 19px ui-monospace, Menlo, Consolas, monospace";
      a.board.slice(0, 5).forEach(function (line, i) { ctx.fillText(clip(line, 17), 3, 20 + i * 21); });
      flat();
    });
    drawBox({ x0: 0, y0: 26, z0: 9, x1: 0.5, y1: 38, z1: 19, c: "#b98d5e" });  // доска со стикерами и часы — на левой стене
    plane("wallX", 0.5, 38, 19);
    ["#f6d365", "#a0e7a0", "#ffb3c1", "#9ad0f5", "#f6d365", "#ffb3c1"].forEach(function (c, k) { ctx.fillStyle = c; ctx.fillRect(1 + (k % 3) * 3.7, 1.3 + Math.floor(k / 3) * 4.4, 2.7, 2.9); });
    flat();
    drawBox({ x0: 0, y0: 10, z0: 15, x1: 0.8, y1: 20, z1: 19.5, c: "#1b1418" });
    var d = new Date();
    plane("wallX", 0.81, 19.6, 18.6, 10);
    ctx.fillStyle = "#ff8a3d"; ctx.font = "700 30px ui-monospace, Menlo, Consolas, monospace";
    ctx.fillText(("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2), 6, 34);
    flat();
  }
  function rug(x, y, w, h, fill, r) {
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x, y, w, h, r); else ctx.rect(x, y, w, h);
    ctx.fillStyle = fill; ctx.fill();
  }
  function windowOnWall(x, z, w, h) {
    drawBox({ x0: x - 0.6, y0: 0, z0: z - 0.6, x1: x + w + 0.6, y1: 0.5, z1: z + h + 0.6, c: "#6b4a32" });
    plane("wallY", x, 0.52, z + h);
    var sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, pal.night ? "#0f1636" : "#7cc4f2"); sky.addColorStop(1, pal.night ? "#26305e" : "#cfeafa");
    ctx.fillStyle = sky; ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = "#ffffff";
    if (pal.night) { ctx.fillStyle = "#fff6c8"; [[0.2, 0.2], [0.55, 0.35], [0.8, 0.15], [0.3, 0.7], [0.7, 0.75]].forEach(function (s) { ctx.fillRect(s[0] * w, s[1] * h, 0.45, 0.45); }); }
    else { ctx.globalAlpha = 0.85; [[0.1, 0.25, 0.3, 0.13], [0.6, 0.5, 0.3, 0.13]].forEach(function (c) { ctx.fillRect(c[0] * w, c[1] * h, c[2] * w, c[3] * h); }); ctx.globalAlpha = 1; }
    ctx.fillStyle = "#6b4a32"; ctx.fillRect(w / 2 - 0.35, 0, 0.7, h); ctx.fillRect(0, h / 2 - 0.35, w, 0.7);
    flat();
    var curtain = pal.night ? "#8a4f2c" : "#c96f3f";
    drawBox({ x0: x - 3, y0: 0.5, z0: z - 1.5, x1: x - 0.9, y1: 1.3, z1: z + h + 1.2, c: curtain });
    drawBox({ x0: x + w + 0.9, y0: 0.5, z0: z - 1.5, x1: x + w + 3, y1: 1.3, z1: z + h + 1.2, c: curtain });
  }
  function visible(b) {
    var a = P(b.x0, b.y1, b.z1)[0], c = P(b.x1, b.y0, 0)[0], top = P(b.x0, b.y0, b.z1)[1], bot = P(b.x1, b.y1, b.z0)[1];
    return a < cw + 40 && c > -40 && top < ch + 40 && bot > -60;
  }
  function draw() {
    if (!world || !cw) return;
    var t = now();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    drawRoom();
    ctx.lineWidth = 0.6; ctx.lineJoin = "round";
    // кто где сейчас: сидящих пристёгиваем к их мебели (так правильно заслоняются стулом и столом), остальные — сами по себе
    var extra = {}, solo = [], placed = [];
    Object.keys(chars).forEach(function (id) {
      var c = chars[id], s = c.at, d = c.st.cell.d, list = [];
      var pose = c.path ? "walk" : !s ? "stand" : s === c.st.chair ? (d.state === "working" ? "type" : "sit")
        : s === c.st.beside ? "wave" : s.pose;
      person(list, c.x, c.y, c.dir, pose, c.look, t, c.seed, (s && s.seatZ) || 5);
      var top = pose === "type" || pose === "sit" || pose === "sofa" ? ((s && s.seatZ) || 5) + 12 : 17;
      c.head = [c.x, c.y, top];
      if (s && s.group && !c.path) { var more = extra[s.group.key] || (extra[s.group.key] = []); more.push.apply(more, list); }
      else { var g = group("ch:" + id, list); g.char = c; solo.push(g); }
      c.box = bounds(list);
    });
    world.stations.forEach(function (st) {
      var agents = (st.cell.d.agents || []).slice(0, 2);
      if (!agents.length) return;
      var list = extra[st.group.key] || (extra[st.group.key] = []);
      agents.forEach(function (a, k) { robot(list, st.cx + 22.2, st.cy + 8 + k * 6.5, t + k * 0.37); });
    });
    if (cat) { var cl = []; catModel(cl, cat.x, cat.y, cat.dir, cat.sit, t); solo.push(group("cat", cl)); }
    // тени под ногами
    plane("floor", 0, 0, 0.01);
    ctx.fillStyle = pal.night ? "rgba(0,0,0,.35)" : "rgba(60,40,20,.18)";
    Object.keys(chars).forEach(function (id) { var c = chars[id]; ctx.beginPath(); ctx.ellipse(c.x, c.y, 2.8, 2.8, 0, 0, Math.PI * 2); ctx.fill(); });
    if (cat) { ctx.beginPath(); ctx.ellipse(cat.x, cat.y, 1.8, 1.8, 0, 0, Math.PI * 2); ctx.fill(); }
    [hover, selected].forEach(function (id, k) {  // подсветка на полу под выбранным
      var c = id && chars[id], st = id && world.stations.filter(function (s) { return s.id === id; })[0];
      if (!st) return;
      ctx.strokeStyle = k ? "#9d8cff" : "rgba(255,255,255,.6)"; ctx.lineWidth = k ? 0.5 : 0.35;
      ctx.fillStyle = k ? "rgba(157,140,255,.16)" : "rgba(255,255,255,.08)";
      ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(st.cx + 2.5, st.cy + 2.5, 21, 19, 2); else ctx.rect(st.cx + 2.5, st.cy + 2.5, 21, 19); ctx.fill(); ctx.stroke();
      if (c && (c.path || (c.at && c.at.lounge))) { ctx.beginPath(); ctx.ellipse(c.x, c.y, 3.6, 3.6, 0, 0, Math.PI * 2); ctx.stroke(); }
    });
    flat(); ctx.lineWidth = 0.6;
    var groups = world.groups.concat(solo).filter(visible);
    hits = [];
    depthSort(groups).forEach(function (g) {
      var more = extra[g.key];
      if (g.station && sleepy(g.station)) ctx.globalAlpha = 0.38;
      if (more) depthSort(g.base.concat(more)).forEach(drawBox);
      else (g.sorted || (g.sorted = depthSort(g.base))).forEach(drawBox);
      ctx.globalAlpha = 1;
      if (g.station) hits.push({ id: g.station.id, poly: hull(more ? bounds(g.base.concat(more)) : g) });
      if (g.char) hits.push({ id: g.char.id, poly: hull(g) });
    });
    Object.keys(chars).forEach(function (id) {  // сидящих тоже можно нажать
      var c = chars[id];
      if (c.at && c.at.group && !c.path && c.box && c.at.lounge) hits.push({ id: id, poly: hull(c.box) });
    });
    lights();
    zoneTags(placed);
    nameTags(t, placed);
  }
  function lights() {  // свет мониторов и ламп — «светлым» наложением; ночью края темнее
    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    world.stations.forEach(function (st) {
      var col = { working: "90,150,255", error: "255,80,80", queued: "255,190,80", waiting: "255,170,80", blocked: "255,210,80" }[st.cell.d.state];
      if (!col) return;
      var c = P(st.cx + 9, st.cy + 6.5, 12.5), r = U * 11;
      if (c[0] < -r || c[0] > cw + r || c[1] < -r || c[1] > ch + r) return;
      var grd = ctx.createRadialGradient(c[0], c[1], 0, c[0], c[1], r);
      grd.addColorStop(0, "rgba(" + col + "," + (pal.night ? 0.22 : 0.1) + ")"); grd.addColorStop(1, "rgba(" + col + ",0)");
      ctx.fillStyle = grd; ctx.fillRect(c[0] - r, c[1] - r, r * 2, r * 2);
    });
    world.lights.forEach(function (p) {
      var l = P(p[0], p[1], p[2]), lr = U * 30, lg = ctx.createRadialGradient(l[0], l[1], 0, l[0], l[1], lr);
      lg.addColorStop(0, "rgba(255,200,120," + (pal.night ? 0.2 : 0.06) + ")"); lg.addColorStop(1, "rgba(255,200,120,0)");
      ctx.fillStyle = lg; ctx.fillRect(l[0] - lr, l[1] - lr, lr * 2, lr * 2);
    });
    ctx.restore();
    if (pal.night) {
      var v = ctx.createRadialGradient(cw / 2, ch / 2, Math.min(cw, ch) * 0.35, cw / 2, ch / 2, Math.max(cw, ch) * 0.75);
      v.addColorStop(0, "rgba(0,0,0,0)"); v.addColorStop(1, "rgba(0,0,0,.4)");
      ctx.fillStyle = v; ctx.fillRect(0, 0, cw, ch);
    }
  }
  function overlaps(r, list) { return list.some(function (q) { return r[0] < q[2] && q[0] < r[2] && r[1] < q[3] && q[1] < r[3]; }); }
  function zoneTags(placed) {  // табличка проекта у переднего угла его коврика; наезжает на соседнюю — ниже
    ctx.font = "700 11px system-ui, -apple-system, sans-serif";
    world.areas.forEach(function (a) {
      a.zones.forEach(function (z) {
        var p = P(z.x + 0.5, z.y + z.d - 0.5, 0), name = clip(z.name, 30), w = ctx.measureText(name).width + 22;
        if (p[0] < -w || p[0] > cw || p[1] < -20 || p[1] > ch + 20) return;
        for (var k = 0; k < 6 && overlaps([p[0] - 6, p[1] - 9, p[0] - 6 + w, p[1] + 9], placed); k++) p = [p[0], p[1] + 20];
        placed.push([p[0] - 6, p[1] - 9, p[0] - 6 + w, p[1] + 9]);
        ctx.globalAlpha = quiet(z) ? 0.5 : 1;
        ctx.fillStyle = pal.night ? "rgba(20,16,24,.82)" : "rgba(255,255,255,.9)";
        pill(p[0] - 6, p[1] - 9, w, 18, 9);
        ctx.fillStyle = z.color; ctx.beginPath(); ctx.arc(p[0] + 4, p[1], 4, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = pal.night ? "#f3ece6" : "#2b2320"; ctx.textBaseline = "middle"; ctx.fillText(name, p[0] + 12, p[1] + 0.5);
        ctx.globalAlpha = 1;
        ctx.textBaseline = "alphabetic";
      });
    });
  }
  // бейдж с именем над головой (точка — состояние); реплика — у работающих, у выбранного и в болтовне в лаунже.
  // Сначала важные (выбранный, работающие, ждущие), подписи остальных пропускаем, если наезжают
  function nameTags(t, placed) {
    var list = Object.keys(chars).map(function (id) { return chars[id]; });
    world.stations.forEach(function (st) { if (st.cell.d.state === "off") list.push({ id: st.id, st: st, head: [st.cx + 9, st.cy + 5.5, 17], empty: true }); });
    var rank = function (c) { var s = c.st.cell.d.state; return c.id === selected || c.id === hover ? 0 : s === "working" || s === "error" || s === "waiting" || s === "blocked" ? 1 : sleepy(c.st) ? 3 : 2; };
    list.sort(function (a, b) { return rank(a) - rank(b); });
    list.forEach(function (c) {
      var d = c.st.cell.d, p = P(c.head[0], c.head[1], c.head[2]), picked = c.id === selected || c.id === hover;
      if (p[0] < -150 || p[0] > cw + 150 || p[1] < -40 || p[1] > ch + 40) return;
      ctx.font = "600 11px system-ui, -apple-system, sans-serif";
      var name = clip(d.label, 22), w = ctx.measureText(name).width + 22, r = [p[0] - w / 2, p[1] - 9, p[0] + w / 2, p[1] + 9];
      if (rank(c) >= 2 && overlaps(r, placed)) return;
      var faint = c.empty || (sleepy(c.st) && !picked);
      placed.push(r);
      ctx.fillStyle = faint ? "rgba(20,16,24,.45)" : "rgba(20,16,24,.82)";
      pill(r[0], r[1], w, 18, 9);
      ctx.fillStyle = DOT[d.state] || "#9aa0a8"; ctx.beginPath(); ctx.arc(r[0] + 9, p[1], 3.5, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = faint ? "rgba(255,255,255,.55)" : "#fff"; ctx.textBaseline = "middle"; ctx.fillText(name, r[0] + 16, p[1] + 0.5);
      ctx.textBaseline = "alphabetic";
      var text = "", style = "hover";
      if (d.state === "working") { text = d.say; style = "working"; }
      else if (picked) { text = d.say || d.hint; style = d.say && BUBBLE[d.state] ? d.state : "hover"; }
      else if (c.chatEnd > t && c.at && c.at.lounge) {
        var opts = CHATTER[c.at.pose] || CHATTER.stand;
        c.chatText = c.chatText || opts[Math.floor(Math.random() * opts.length)]; text = c.chatText; style = "chat";
      }
      if (text) placed.push(bubble(r[2] + 4, p[1], r[0] - 4, text, BUBBLE[style]));
    });
  }
  function pill(x, y, w, h, r) { ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(x, y, w, h, r); else ctx.rect(x, y, w, h); ctx.fill(); }
  // реплика справа от подписи (у правого края — слева), хвостиком к подписи
  function bubble(right, y, left, text, colors) {
    ctx.font = "500 11.5px system-ui, -apple-system, sans-serif";
    var lines = wrapText(text, 160, 2), w = 0;
    lines.forEach(function (l) { w = Math.max(w, ctx.measureText(l).width); });
    w += 14;
    var h = lines.length * 15 + 9, toLeft = right + 6 + w > cw - 4, bx = toLeft ? Math.max(4, left - 6 - w) : right + 6;
    var by = Math.max(4, y - h + 6), tip = toLeft ? bx + w + 6 : bx - 6;
    ctx.fillStyle = colors[0];
    pill(bx, by, w, h, 7);
    ctx.beginPath(); ctx.moveTo(tip, y); ctx.lineTo(toLeft ? bx + w - 1 : bx + 1, by + h - 12); ctx.lineTo(toLeft ? bx + w - 1 : bx + 1, by + h - 4); ctx.fill();
    ctx.fillStyle = colors[1];
    lines.forEach(function (l, i) { ctx.fillText(l, bx + 7, by + 16 + i * 15); });
    return [bx, by, bx + w, by + h];
  }
  function wrapText(text, max, maxLines) {
    var words = String(text).split(/\s+/), lines = [], cur = "";
    words.forEach(function (wd) {
      var next = cur ? cur + " " + wd : wd;
      if (ctx.measureText(next).width <= max || !cur) cur = next; else { lines.push(cur); cur = wd; }
    });
    if (cur) lines.push(cur);
    if (lines.length > maxLines) { lines = lines.slice(0, maxLines); lines[maxLines - 1] = lines[maxLines - 1].replace(/\s*\S*$/, "") + "…"; }
    return lines.map(function (l) { while (ctx.measureText(l).width > max && l.length > 2) l = l.slice(0, -2) + "…"; return l; });
  }
  function clip(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  function hull(g) {  // контур на экране — для нажатий
    var pts = [];
    [g.x0, g.x1].forEach(function (x) { [g.y0, g.y1].forEach(function (y) { [g.z0, g.z1].forEach(function (z) { pts.push(P(x, y, z)); }); }); });
    pts.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    var cross = function (o, a, b) { return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]); }, lo = [], up = [];
    pts.forEach(function (p) { while (lo.length > 1 && cross(lo[lo.length - 2], lo[lo.length - 1], p) <= 0) lo.pop(); lo.push(p); });
    pts.slice().reverse().forEach(function (p) { while (up.length > 1 && cross(up[up.length - 2], up[up.length - 1], p) <= 0) up.pop(); up.push(p); });
    return lo.slice(0, -1).concat(up.slice(0, -1));
  }
  function inside(poly, x, y) {
    var c = false;
    for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      if ((poly[i][1] > y) !== (poly[j][1] > y) && x < (poly[j][0] - poly[i][0]) * (y - poly[i][1]) / (poly[j][1] - poly[i][1]) + poly[i][0]) c = !c;
    }
    return c;
  }

  // --- когда рисовать: кадр — по таймеру (24 в секунду, пока что-то движется), иначе — к следующему событию -----
  function canRun() { return onScreen && document.visibilityState === "visible" && !scene.classList.contains("off"); }
  function schedule(delay) {
    if (pending || !canRun()) return;
    pending = true;
    clearTimeout(wakeTimer);
    setTimeout(function () {
      requestAnimationFrame(function () {
        pending = false;
        if (!canRun()) return;
        var t = now(), dt = Math.min(0.1, lastT ? t - lastT : 0);
        lastT = t;
        if (world) update(dt, t);
        draw();
        if (moving()) schedule(Math.max(0, 1000 / FPS - (now() - t) * 1000));
        else {
          lastT = 0;
          var wake = world ? nextWake(t) : Infinity;
          if (wake < Infinity) wakeTimer = setTimeout(redraw, Math.max(50, (wake - t) * 1000));
        }
      });
    }, delay);
  }
  function redraw() { schedule(0); }
  function tickClock() {  // часы на стене и день/ночь — раз в минуту
    clearTimeout(clockTimer);
    var hour = new Date().getHours(), next = hour >= 8 && hour < 20 ? DAY : NIGHT;
    if (next !== pal && world) { var sig = world.sig; pal = next; world = build(data.areas); world.sig = sig; relink(); } else pal = next;
    redraw();
    clockTimer = setTimeout(tickClock, 60000 - Date.now() % 60000 + 50);
  }
  function relink() {  // офис пересобран — сотрудники возвращаются на свои (новые) места без анимации
    Object.keys(chars).forEach(function (id) {
      var st = world.stations.filter(function (s) { return s.id === id; })[0];
      if (!st) { delete chars[id]; return; }
      var c = chars[id];
      c.st = st; c.rest = null; c.path = null; c.goal = null; c.next = null;
      if (idle(c)) pickRest(c, now());
      place(c, want(c));
    });
    world.spots.forEach(function (s) { s.owner = null; });
    Object.keys(chars).forEach(function (id) { reserve(chars[id], chars[id].at); });
  }
  function resize() {
    var w = wrap.clientWidth;
    if (!w) return;
    var keep = world && cw ? viewCenter() : null;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    cw = w; ch = Math.round(Math.max(360, Math.min(720, w * 0.58, window.innerHeight * 0.75)));
    canvas.style.height = ch + "px";
    canvas.width = Math.round(cw * dpr); canvas.height = Math.round(ch * dpr);
    patterns = {};
    if (world) { if (keep) { centerOn(keep[0], keep[1]); clampCam(); } else restore(); }
    redraw();
  }
  function load() {
    var el = body.querySelector("[data-office-json]"), next = null;
    try { next = el ? JSON.parse(el.textContent) : null; } catch (e) { next = null; }
    if (!next || !next.areas) return;
    var sig = JSON.stringify(next.areas.map(function (a) { return [a.key, a.desks ? a.desks.map(function (d) { return d.id; }) : a.zones.map(function (z) { return [z.name, z.desks.map(function (d) { return d.id; })]; })]; }));
    var first = !world;
    data = next;
    if (!world || sig !== world.sig) {  // другие столы — пересобрать офис
      var keep = world && cw ? viewCenter() : null;
      world = build(data.areas); world.sig = sig;
      if (first) { if (cw) restore(); } else { relink(); if (keep) { centerOn(keep[0], keep[1]); clampCam(); } }
    } else {  // те же столы — обновить, кто чем занят
      var byId = {};
      data.areas.forEach(function (a) { (a.desks || []).forEach(function (d) { byId[d.id] = d; }); (a.zones || []).forEach(function (z) { z.desks.forEach(function (d) { byId[d.id] = d; }); }); });
      world.stations.forEach(function (st) { if (byId[st.id]) st.cell.d = byId[st.id]; });
    }
    sync(first);
    if (selected && !panel.hidden) openPanel(selected, true);
    redraw();
  }

  // --- мышь и клавиши ---------------------------------------------------------------------------------------
  function local(e) { var r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
  function at(x, y) {
    for (var i = hits.length - 1; i >= 0; i--) if (inside(hits[i].poly, x, y)) return hits[i].id;
    return null;
  }
  function source(id) {
    return Array.prototype.filter.call(body.querySelectorAll("[data-obj]"), function (el) { return el.dataset.obj === id; })[0];
  }
  function openPanel(id, quiet) {
    var el = source(id);
    if (!el) { closePanel(); return; }
    var copy = el.cloneNode(true);
    copy.removeAttribute("data-obj");
    var keep = Array.prototype.map.call(panelBody.querySelectorAll("details[open]"), function (d) { return d.dataset.key; });
    panelBody.replaceChildren(copy);
    if (quiet) Array.prototype.forEach.call(panelBody.querySelectorAll("details[data-key]"), function (d) { if (keep.indexOf(d.dataset.key) >= 0) d.open = true; });
    panel.hidden = false;
    selected = id;
  }
  function closePanel() { panel.hidden = true; selected = null; redraw(); }
  canvas.addEventListener("pointerdown", function (e) {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, ox: OX, oy: OY, moved: false };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", function (e) {
    var p = local(e);
    if (drag) {
      var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) > 5) { drag.moved = true; canvas.classList.add("dragging"); }
      if (drag.moved) { OX = drag.ox + dx; OY = drag.oy + dy; clampCam(); redraw(); }
      return;
    }
    var ex = p[0] < EDGE ? -(1 - p[0] / EDGE) : p[0] > cw - EDGE ? 1 - (cw - p[0]) / EDGE : 0;
    var ey = p[1] < EDGE ? -(1 - p[1] / EDGE) : p[1] > ch - EDGE ? 1 - (ch - p[1]) / EDGE : 0;
    var edge = e.pointerType === "mouse" && (ex || ey) ? [ex, ey] : null;
    mouse = { x: p[0], y: p[1], edge: edge, edgeSince: edge ? (mouse && mouse.edge ? mouse.edgeSince : now()) : 0 };
    var id = at(p[0], p[1]);
    canvas.style.cursor = id ? "pointer" : "";
    if (id !== hover) { hover = id; redraw(); }
    if (edge) redraw();
  });
  canvas.addEventListener("pointerup", function (e) {
    var d = drag;
    drag = null; canvas.classList.remove("dragging");
    if (!d) return;
    if (d.moved) { save(); return; }
    var p = local(e), id = at(p[0], p[1]);
    if (id) openPanel(id); else closePanel();
    redraw();
  });
  canvas.addEventListener("pointercancel", function () { drag = null; canvas.classList.remove("dragging"); });
  canvas.addEventListener("pointerleave", function () { mouse = null; if (hover) { hover = null; redraw(); } });
  canvas.addEventListener("wheel", function (e) {  // щипок на трекпаде и Ctrl/⌘ + колесо — зум; просто колесо листает страницу
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    var p = local(e);
    zoomTo(U * Math.exp(-e.deltaY * 0.01), p[0], p[1]);
  }, { passive: false });
  var KEYS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  canvas.addEventListener("keydown", function (e) {
    if (KEYS[e.key]) { e.preventDefault(); keyPan = KEYS[e.key]; redraw(); }
    else if (e.key === "+" || e.key === "=") zoomStep(1);
    else if (e.key === "-") zoomStep(-1);
  });
  canvas.addEventListener("keyup", function (e) { if (KEYS[e.key]) { keyPan = null; save(); } });
  canvas.addEventListener("blur", function () { keyPan = null; });
  Array.prototype.forEach.call(scene.querySelectorAll("[data-zoom]"), function (b) {
    b.addEventListener("click", function () { var k = +b.dataset.zoom; if (k) zoomStep(k); else fitAll(); });
  });
  scene.querySelector("[data-panel-close]").addEventListener("click", closePanel);
  scene.querySelector("[data-panel-list]").addEventListener("click", function (e) {
    e.preventDefault();
    var el = selected && source(selected);
    if (!el) return;
    for (var d = el.closest("details"); d; d = d.parentElement && d.parentElement.closest("details")) d.open = true;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("office-flash");
    setTimeout(function () { el.classList.remove("office-flash"); }, 1600);
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !panel.hidden) closePanel(); });

  // скрыть картинку — запоминаем в браузере (только удобство: без хранилища просто показываем)
  var toggle = scene.querySelector("[data-scene-toggle]");
  var setOff = function (off) {
    scene.classList.toggle("off", off);
    toggle.textContent = off ? "Показать картинку" : "Скрыть картинку";
    try { localStorage.setItem("office-scene", off ? "off" : "on"); } catch (e) { /* без хранилища — не запоминаем */ }
    if (!off) resize();
  };
  toggle.addEventListener("click", function () { setOff(!scene.classList.contains("off")); });
  try { if (localStorage.getItem("office-scene") === "off") setOff(true); } catch (e) { /* показываем */ }

  new MutationObserver(load).observe(body, { childList: true });
  if (window.ResizeObserver) new ResizeObserver(resize).observe(wrap); else window.addEventListener("resize", resize);
  if (window.IntersectionObserver) new IntersectionObserver(function (es) { onScreen = es[0].isIntersecting; redraw(); }).observe(canvas);
  document.addEventListener("visibilitychange", redraw);
  var hour = new Date().getHours();
  pal = hour >= 8 && hour < 20 ? DAY : NIGHT;
  load();
  resize();
  tickClock();
})();
