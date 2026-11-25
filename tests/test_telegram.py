from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import httpx

from tests.conftest import NOW
from tests.test_pipeline import Clock, make_signal, quote
from trader.approval.telegram import TelegramBot, format_pending
from trader.audit import AuditLog
from trader.broker.commissions import IbkrTieredUS
from trader.broker.simulated import SimulatedBroker
from trader.config import RiskLimits
from trader.db import Database
from trader.pipeline import TradingSystem

CHAT = 4242


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.sent.append((method, body))
        if method == "getUpdates":
            ups = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
            return httpx.Response(200, json={"ok": True, "result": ups})
        return httpx.Response(200, json={"ok": True, "result": {}})


def setup(tmp_path: Path) -> tuple[TradingSystem, TelegramBot, FakeTelegram]:
    clock = Clock(NOW)
    db = Database(f"sqlite:///{tmp_path / 'tg.sqlite'}")
    broker = SimulatedBroker(settled_cash=D("1000"), clock=clock)
    broker.set_quote(quote("ACME", "50", clock()))
    fake = FakeTelegram()
    bot = TelegramBot("123:SECRET", CHAT, db, http=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    system = TradingSystem(
        db=db,
        broker=broker,
        limits=RiskLimits(),
        commissions=IbkrTieredUS(),
        audit=AuditLog(db),
        notifier=bot,
        clock=clock,
    )
    return system, bot, fake


def callback(update_id: int, data: str, chat: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cq{update_id}",
            "data": data,
            "from": {"id": chat, "username": "me"},
            "message": {"message_id": 7, "chat": {"id": chat}},
        },
    }


def test_pending_notification_and_button_approval(tmp_path: Path) -> None:
    system, bot, fake = setup(tmp_path)
    sig = make_signal()
    system.submit_signal(sig)
    system.run_cycle()
    pending = [b for m, b in fake.sent if m == "sendMessage" and "reply_markup" in b]
    assert len(pending) == 1
    buttons = pending[0]["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"ap:{sig.id}"
    assert "ACME - BUY" in pending[0]["text"]

    fake.updates = [callback(1, f"ap:{sig.id}", chat=999), callback(2, f"ap:{sig.id}", chat=CHAT)]
    assert bot.process_updates(system) == 1  # the stranger's press is ignored
    s = system.get_signal(sig.id)
    assert s is not None and s["status"] == "APPROVED" and s["decided_by"].startswith(f"telegram:{CHAT}")
    assert bot.process_updates(system) == 0  # offset advanced: no reprocessing
    assert system.run_cycle().executed == 1


def test_kill_command_only_turns_on(tmp_path: Path) -> None:
    system, bot, fake = setup(tmp_path)
    fake.updates = [
        {"update_id": 5, "message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/kill"}},
        {"update_id": 6, "message": {"chat": {"id": CHAT}, "from": {"id": CHAT}, "text": "/kill"}},
    ]
    assert bot.process_updates(system) == 1
    assert system.kill_switch_on()


def test_token_never_in_error(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'e.sqlite'}")
    bad = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    bot = TelegramBot("123:SECRET", CHAT, db, http=httpx.Client(transport=bad))
    try:
        bot.notify("x")
    except Exception as exc:
        assert "SECRET" not in str(exc)
    else:
        raise AssertionError("expected failure")


def test_format_pending_truncates_long_thesis() -> None:
    sig = {
        "id": "x",
        "ticker": "ACME",
        "horizon_days": 5,
        "conviction": "0.7",
        "source": "s",
        "thesis": "a" * 5000,
        "evidence": [{"url": "https://www.sec.gov/a"}],
        "plan": {
            "entry": {
                "qty": "1",
                "limit_price": "10",
                "stop_price": "9",
                "stop_loss_pct": "0.1",
                "notional": "10",
            }
        },
    }
    text = format_pending(sig)
    assert len(text) < 2000 and "https://www.sec.gov/a" in text


def test_kill_processed_even_if_an_earlier_command_fails(tmp_path: Path) -> None:
    system, bot, fake = setup(tmp_path)

    def broken_status() -> dict[str, object]:
        raise RuntimeError("IB Gateway not connected")

    system.status = broken_status  # type: ignore[method-assign]
    fake.updates = [
        {"update_id": 10, "message": {"chat": {"id": CHAT}, "from": {"id": CHAT}, "text": "/status"}},
        {"update_id": 11, "message": {"chat": {"id": CHAT}, "from": {"id": CHAT}, "text": "/kill"}},
    ]
    bot.process_updates(system)
    assert system.kill_switch_on()
    assert bot.process_updates(system) == 0  # both acknowledged: nothing is reprocessed


def test_approval_survives_stale_callback_query(tmp_path: Path) -> None:
    system, bot, fake = setup(tmp_path)
    sig = make_signal()
    system.submit_signal(sig)
    system.run_cycle()
    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/answerCallbackQuery"):
            return httpx.Response(400, json={"ok": False, "description": "query is too old"})
        return original(request)

    bot.http = httpx.Client(transport=httpx.MockTransport(handler))
    fake.updates = [callback(20, f"ap:{sig.id}", chat=CHAT)]
    bot.process_updates(system)
    s = system.get_signal(sig.id)
    assert s is not None and s["status"] == "APPROVED"
    texts = [b.get("text", "") for m, b in fake.sent if m == "sendMessage"]
    assert any("APPROVATO" in t for t in texts) and not any("Comando non eseguito" in t for t in texts)


def test_updates_left_for_next_poll_after_deadline(tmp_path: Path) -> None:
    import time as _time

    system, bot, fake = setup(tmp_path)
    fake.updates = [
        {"update_id": 30, "message": {"chat": {"id": CHAT}, "from": {"id": CHAT}, "text": "/pending"}}
    ]
    assert bot.process_updates(system, deadline=_time.monotonic() - 1) == 0
    assert bot.process_updates(system) == 1  # left unacknowledged, handled on the next poll


def test_kill_applied_first_even_past_deadline_and_before_approvals(tmp_path: Path) -> None:
    import time as _time

    system, bot, fake = setup(tmp_path)
    sig = make_signal()
    system.submit_signal(sig)
    system.run_cycle()
    fake.updates = [
        callback(40, f"ap:{sig.id}", chat=CHAT),
        {"update_id": 41, "message": {"chat": {"id": CHAT}, "from": {"id": CHAT}, "text": "/kill"}},
    ]
    bot.process_updates(system, deadline=_time.monotonic() - 1)  # budget already spent
    assert system.kill_switch_on()
    assert system.run_cycle().executed == 0


def test_group_member_cannot_command(tmp_path: Path) -> None:
    system, bot, fake = setup(tmp_path)
    sig = make_signal()
    system.submit_signal(sig)
    system.run_cycle()
    stranger = 777
    fake.updates = [
        {"update_id": 50, "message": {"chat": {"id": CHAT}, "from": {"id": stranger}, "text": "/kill"}},
        {
            "update_id": 51,
            "callback_query": {
                "id": "c",
                "data": f"ap:{sig.id}",
                "from": {"id": stranger},
                "message": {"message_id": 1, "chat": {"id": CHAT}},
            },
        },
    ]
    bot.process_updates(system)
    assert not system.kill_switch_on()
    assert system.get_signal(sig.id)["status"] == "PENDING_APPROVAL"  # type: ignore[index]
