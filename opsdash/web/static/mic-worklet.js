// Диктовка в «Спросить Claude»: отдаёт странице звук с микрофона кусочками — она режет его на фразы по паузам.
// Отдельным файлом, а не из строки: политика безопасности страницы разрешает только свои скрипты.
registerProcessor("pulse-mic", class extends AudioWorkletProcessor {
  process(inputs) {
    var ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
});
