// Карточка PULSE поверх всех окон (macOS, JavaScript for Automation: osascript -l JavaScript, есть в каждой macOS).
// Запуск: osascript -l JavaScript macos_alert.js <адрес страницы> <сколько секунд жить> <ширина> <высота>
//
// Плавающая панель без рамки в правом верхнем углу экрана: не забирает фокус у текущего окна, видна на всех
// рабочих столах и поверх полноэкранных приложений. Внутри страница PULSE (opsdash/meeting_alert.py, /alert/<key>):
// стиль, анимации и кнопки рисует она. Страница говорит с окном через свой заголовок:
//   pulse:r:<x>,<y>,<ширина>,<высота карточки>,<высота окна>:<n>  где карточка (в точках от левого верхнего угла)
//                                                                    и какой высоты окно (верхний край на месте);
//   pulse:close                                                      закрыть окно.
// Окно появляется, только когда страница прислала размеры: не загрузилась, и ничего не видно, через <секунд> выход.
//
// Окно шире карточки: прозрачные поля под тень. Щелчки там должны уходить в окна под карточкой, поэтому окно
// ловит мышь, только пока курсор над самой карточкой (проверка 20 раз в секунду).
ObjC.import('Cocoa');
ObjC.import('WebKit');
ObjC.import('stdlib');

function run(argv) {
  const url = argv[0];
  const ttl = parseFloat(argv[1] || '900');
  const W = parseInt(argv[2] || '408', 10);
  let H = parseInt(argv[3] || '230', 10);
  let rect = null;  // карточка в окне: {x, y, w, h}, y — от верхнего края
  const born = Date.now();

  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy(1); // Accessory: без значка в Dock и без меню

  // панель без рамки по умолчанию не может стать ключевой: тогда кнопки в странице не нажимаются с первого раза
  ObjC.registerSubclass({
    name: 'PulseAlertPanel', superclass: 'NSPanel',
    methods: {
      canBecomeKeyWindow: { types: ['bool', []], implementation: function () { return true; } },
    },
  });

  let shown = false;
  let last = '';
  ObjC.registerSubclass({
    name: 'PulseAlertHost',
    methods: {
      'tick:': { types: ['void', ['id']], implementation: function () {
        if ((Date.now() - born) / 1000 > ttl) $.exit(0);
        follow();
        const t = web.title.js || '';
        if (t === last) return;
        last = t;
        if (t === 'pulse:close') $.exit(0);
        const m = /^pulse:r:(\d+),(\d+),(\d+),(\d+),(\d+)/.exec(t);
        if (m) {
          rect = { x: +m[1], y: +m[2], w: +m[3], h: +m[4] };
          H = Math.max(80, Math.min(600, +m[5]));
          place();
          if (!shown) { panel.orderFrontRegardless; shown = true; }
        }
      } },
    },
  });

  // NonactivatingPanel (128): нажатие не выводит PULSE вперёд и не уводит фокус из Zoom или письма
  const panel = $.PulseAlertPanel.alloc.initWithContentRectStyleMaskBackingDefer(
    $.NSMakeRect(0, 0, W, H), 128, $.NSBackingStoreBuffered, false);
  panel.floatingPanel = true;
  panel.level = 25;                              // NSStatusWindowLevel: выше обычных и плавающих окон
  panel.collectionBehavior = 1 | 16 | 64 | 256;  // все рабочие столы, неподвижна, вне Cmd+`, поверх полноэкранных
  panel.hidesOnDeactivate = false;
  panel.opaque = false;
  panel.backgroundColor = $.NSColor.clearColor;
  panel.hasShadow = false;                       // тень рисует страница: окно шире карточки, края прозрачные
  panel.releasedWhenClosed = false;
  panel.ignoresMouseEvents = true;               // пока курсор не над карточкой (follow)

  const cfg = $.WKWebViewConfiguration.alloc.init;
  const web = $.WKWebView.alloc.initWithFrameConfiguration($.NSMakeRect(0, 0, W, H), cfg);
  web.setValueForKey($.NSNumber.numberWithBool(false), 'drawsBackground');
  web.autoresizingMask = 2 | 16;                 // ширина и высота по окну
  panel.contentView = web;

  function place() {
    const vf = $.NSScreen.mainScreen.visibleFrame;  // экран, где сейчас работают, без строки меню и Dock
    panel.setFrameDisplay($.NSMakeRect(vf.origin.x + vf.size.width - W, vf.origin.y + vf.size.height - H, W, H), true);
  }

  // мышь над карточкой: окно её ловит; над прозрачным полем: щелчок уходит в окно под ним
  function follow() {
    if (!rect) return;
    const p = $.NSEvent.mouseLocation;
    const f = panel.frame;
    const top = f.origin.y + f.size.height - rect.y;
    const inside = p.x >= f.origin.x + rect.x && p.x <= f.origin.x + rect.x + rect.w && p.y <= top && p.y >= top - rect.h;
    if (panel.ignoresMouseEvents === inside) panel.ignoresMouseEvents = !inside;
  }

  place();
  const host = $.PulseAlertHost.alloc.init;
  $.NSTimer.scheduledTimerWithTimeIntervalTargetSelectorUserInfoRepeats(0.05, host, 'tick:', $(), true);
  web.loadRequest($.NSURLRequest.requestWithURL($.NSURL.URLWithString(url)));
  app.run;
}
