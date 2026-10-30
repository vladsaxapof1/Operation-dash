"""Поддельный Kaiten API для тестов (httpx.MockTransport)."""

import json
import re
from datetime import datetime, timedelta, timezone

import httpx


def iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class FakeKaiten:
    def __init__(self):
        self.me = {"id": 1, "full_name": "Анна Смирнова", "username": "anna"}
        self.users = [self.me, {"id": 2, "full_name": "Пётр Иванов", "username": "petr"},
                      {"id": 3, "full_name": "Мария Кузнецова", "username": "maria"}]
        self.spaces = [{"id": 10, "title": "Операционка"}, {"id": 20, "title": "Продажи"}]
        self.boards = {10: [{"id": 100, "title": "Проекты", "columns": [
            {"id": 1001, "title": "Очередь", "sort_order": 1}, {"id": 1002, "title": "В работе", "sort_order": 2}],
            "lanes": [{"id": 501, "title": "Основная", "condition": 1}]}], 20: []}
        self.cards = {}
        self.next_id = 5000
        self.comments = []
        self.members = []
        self.requests = []
        self.blockers = {}  # card_id → [blocker]
        self.blockers_tariff = True  # False — тариф без блокировок (402)
        for i in range(105):  # больше одной страницы
            self.add_card(10, f"Карточка {i}", state=2 if i % 2 else 1, moved_days=i % 10, responsible=2)

    def add_card(self, space, title, state=2, moved_days=0.0, responsible=None):
        cid = self.next_id
        self.next_id += 1
        members = []
        if responsible:
            u = next(u for u in self.users if u["id"] == responsible)
            members.append({"id": u["id"], "full_name": u["full_name"], "username": u["username"], "type": 2})
        self.cards[cid] = {
            "id": cid, "title": title, "board_id": 100, "column_id": 1002, "lane_id": 501, "state": state,
            "condition": 1, "board": {"title": "Проекты", "spaces": [{"space_id": space, "primary_path": True}]},
            "column": {"title": "В работе"}, "lane": {"title": "Основная"}, "members": members,
            "created": iso(30), "updated": iso(moved_days), "last_moved_at": iso(moved_days),
            "column_changed_at": iso(moved_days), "due_date": None, "_space": space,
        }
        return cid

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        path = request.url.path.replace("/api/latest", "")
        q = request.url.params
        body = json.loads(request.content) if request.content else {}
        if request.headers.get("Authorization") != "Bearer good-token":
            return httpx.Response(401, json={"message": "unauthorized"})
        if path == "/users/current":
            return httpx.Response(200, json=self.me)
        if path == "/users":
            off = int(q.get("offset", 0))
            return httpx.Response(200, json=self.users[off:off + int(q.get("limit", 100))])
        if path == "/spaces":
            return httpx.Response(200, json=self.spaces)
        m = re.fullmatch(r"/spaces/(\d+)/boards", path)
        if m:
            return httpx.Response(200, json=self.boards.get(int(m[1]), []))
        if path == "/cards" and request.method == "GET":
            space = int(q["space_id"])
            items = [c for c in self.cards.values() if c["_space"] == space and c["condition"] == 1]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 100))
            return httpx.Response(200, json=items[off:off + lim])
        if path == "/cards" and request.method == "POST":
            cid = self.add_card(10, body["title"], state=1)
            self.cards[cid].update({k: v for k, v in body.items() if k in ("due_date", "description")})
            return httpx.Response(200, json=self.cards[cid])
        m = re.fullmatch(r"/cards/(\d+)", path)
        if m and request.method == "GET":
            return httpx.Response(200, json=self.cards[int(m[1])])
        if m and request.method == "PATCH":
            self.cards[int(m[1])].update(body)
            return httpx.Response(200, json=self.cards[int(m[1])])
        m = re.fullmatch(r"/cards/(\d+)/members", path)
        if m:
            self.members.append((int(m[1]), body["user_id"]))
            return httpx.Response(200, json={"id": body["user_id"], "type": 1})
        m = re.fullmatch(r"/cards/(\d+)/members/(\d+)", path)
        if m:
            self.members.append((int(m[1]), int(m[2]), body["type"]))
            return httpx.Response(200, json={"type": body["type"]})
        m = re.fullmatch(r"/cards/(\d+)/blockers", path)
        if m and not self.blockers_tariff:
            return httpx.Response(402, json={"message": "Feature is not supported by tariff"})
        if m and request.method == "GET":
            return httpx.Response(200, json=self.blockers.get(int(m[1]), []))
        if m and request.method == "POST":
            self.next_id += 1
            other = self.cards.get(body.get("blocker_card_id"))
            item = {"id": self.next_id, "card_id": int(m[1]), "reason": body.get("reason"), "released": False,
                    "blocker_card_id": body.get("blocker_card_id"), "blocker_card_title": other and other["title"]}
            self.blockers.setdefault(int(m[1]), []).append(item)
            return httpx.Response(200, json=item)
        m = re.fullmatch(r"/cards/(\d+)/comments", path)
        if m and int(m[1]) not in self.cards:
            return httpx.Response(404, json={"message": "card not found"})
        if m:
            self.comments.append((int(m[1]), body["text"]))
            return httpx.Response(200, json={"id": 1, "text": body["text"]})
        return httpx.Response(404, json={"message": f"no route {request.method} {path}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
