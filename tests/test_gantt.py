from datetime import datetime

from opsdash import config, gantt


def test_layout_bars_weekends_and_now():
    now = datetime(2026, 10, 27, 12, 0, tzinfo=config.tz())  # вторник, полдень
    late = {"what": "Отчёт", "created": "2026-10-20T09:00:00+00:00", "due": "2026-10-26", "status": "open", "overdue": True}
    open_ = {"what": "Без срока", "created": "2026-10-25T09:00:00+00:00", "due": None, "status": "open"}
    g = gantt.layout([("mine", "Мои задачи", [late, open_])], "day", now=now)
    assert g["first"].isoformat() == "2026-10-19" and g["px"] == 64  # на день раньше первой задачи
    bars = [c["bar"] for c in g["groups"][0]["items"]]
    assert (bars[0]["left"], bars[0]["width"], bars[0]["overdue"]) == (64, 7 * 64, True)  # 20.10–26.10 включительно
    assert bars[1]["end"].isoformat() == "2026-10-27" and not bars[1]["due"]  # без срока — до сегодня
    assert 5 * 64 in g["weekends"] and 6 * 64 in g["weekends"]  # 24–25.10 — суббота и воскресенье
    assert g["now_x"] == 8 * 64 + 32  # 27.10, полдень — середина дня
    week = gantt.layout([("mine", "Мои задачи", [late])], "week", now=now)
    assert week["first"].weekday() == 0 and week["top"][0]["text"].endswith("– 25.10")  # неделя — с понедельника
