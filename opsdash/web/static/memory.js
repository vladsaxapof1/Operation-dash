/* «Память»: блок «Что известно по теме» в окне задачи грузится, когда его раскрыли (без запуска Claude).
   Карта памяти — memory-map.js, подключается только на вкладке «Карта». */
(function () {
  'use strict';
  document.addEventListener('toggle', function (e) {
    var d = e.target;
    if (!d.matches || !d.matches('[data-mem-known]') || !d.open || d.dataset.loaded) return;
    d.dataset.loaded = '1';
    var box = d.querySelector('.dlg-known-b');
    fetch('/api/memory/task/' + encodeURIComponent(d.dataset.memKnown), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.text() : ''; })
      .then(function (html) { box.classList.remove('muted'); box.innerHTML = html || 'Не получилось загрузить.'; })
      .catch(function () { box.textContent = 'Не получилось загрузить.'; delete d.dataset.loaded; });
  }, true);
})();
