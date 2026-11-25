"""Telegram bot: approval requests with inline buttons, fill/exit notifications, kill switch.

Security model:
* Only updates from the configured private chat are honoured; everything else is ignored.
* From Telegram you can approve/reject pending signals and turn the kill switch ON.
  Turning it OFF is deliberately only possible from the CLI (asymmetric: stopping is easy,
  resuming requires being at the machine).
* The bot token is a secret: it is never logged or echoed.
"""

from __future__ import annotations

import contextlib
import logging
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from trader.db import Database
from trader.domain import SignalStatus

if TYPE_CHECKING:
    from trader.pipeline import TradingSystem

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram_update_offset"
KILL_COMMANDS = frozenset({"/kill", "/stop"})
CALL_TIMEOUT = 5.0  # seconds per Telegram HTTP phase (connect/read/write)
API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


def format_pending(signal: dict[str, Any]) -> str:
    entry = signal.get("plan", {}).get("entry", {})
    qty = entry.get("qty", "?")
    limit = entry.get("limit_price", "?")
    stop = entry.get("stop_price", "?")
    stop_pct = Decimal(entry.get("stop_loss_pct", "0")) * 100
    notional = Decimal(entry.get("notional", "0")).quantize(Decimal("0.01"))
    thesis = str(signal.get("thesis", ""))
    if len(thesis) > 1200:
        thesis = thesis[:1200] + " [...]"
    links: list[str] = [
        str(e["url"]) for e in signal.get("evidence", []) if isinstance(e, dict) and e.get("url")
    ]
    lines = [
        "NUOVO SEGNALE DA APPROVARE",
        f"{signal['ticker']} - BUY {qty} az. limite {limit} USD (~{notional} USD)",
        f"Stop {stop} (-{stop_pct:.1f}%) | orizzonte {signal['horizon_days']} gg | "
        f"convinzione {signal['conviction']}",
        f"Fonte: {signal['source']}",
        f"Tesi: {thesis}",
    ]
    if links:
        lines.append("Fonti: " + " ".join(dict.fromkeys(links[:3])))
    lines += [f"Scade: {signal.get('expires_at')}", f"ID: {signal['id']}"]
    return "\n".join(lines)


class TelegramBot:
    def __init__(self, token: str, chat_id: int, db: Database, http: httpx.Client | None = None) -> None:
        if not token:
            raise TelegramError("missing bot token")
        self._token = token
        self.chat_id = chat_id
        self.db = db
        self.http = http or httpx.Client(timeout=CALL_TIMEOUT)

    def _api(self, method: str, **params: Any) -> Any:
        try:
            resp = self.http.post(f"{API}/bot{self._token}/{method}", json=params)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method}: network error {type(exc).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {resp.status_code}") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'error')}")
        return data.get("result")

    # ------------------------------------------------------------ Notifier
    def notify(self, text: str) -> None:
        self._api("sendMessage", chat_id=self.chat_id, text=text[:4000])

    def notify_pending(self, signal: dict[str, Any]) -> None:
        sid = signal["id"]
        self._api(
            "sendMessage",
            chat_id=self.chat_id,
            text=format_pending(signal)[:4000],
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Approva", "callback_data": f"ap:{sid}"},
                        {"text": "Rifiuta", "callback_data": f"rj:{sid}"},
                    ]
                ]
            },
        )

    # -------------------------------------------------------------- inbound
    def process_updates(self, system: TradingSystem, deadline: float | None = None) -> int:
        """Poll once (non-blocking) and handle button presses and commands. Returns handled count.
        With ``deadline`` (time.monotonic) remaining updates are left for the next poll."""
        offset = int(self.db.kv_get(OFFSET_KEY, 0))
        updates = (
            self._api("getUpdates", offset=offset, timeout=0, allowed_updates=["callback_query", "message"])
            or []
        )
        # /kill first, for every fetched update and regardless of any deadline: it is a local write,
        # idempotent, and must never wait behind slower commands or the time budget.
        for upd in updates:
            msg = upd.get("message") or {}
            if self._authorized(msg) and str(msg.get("text", "")).strip().lower() in KILL_COMMANDS:
                self._kill(system, msg)
        handled = 0
        for upd in updates:
            if deadline is not None and time.monotonic() >= deadline:
                break  # not acknowledged: handled on the next poll
            # Acknowledge each update before handling it: a failing command (e.g. /status while the
            # broker is disconnected) must never block the ones after it, such as /kill.
            offset = max(offset, int(upd["update_id"]) + 1)
            self.db.kv_set(OFFSET_KEY, offset)
            try:
                if "callback_query" in upd:
                    handled += self._on_callback(upd["callback_query"], system, deadline)
                elif "message" in upd:
                    handled += self._on_message(upd["message"], system, deadline)
            except Exception as exc:
                log.warning("telegram update %s failed: %s", upd.get("update_id"), exc)
                with contextlib.suppress(TelegramError):
                    self.notify(f"Comando non eseguito: {type(exc).__name__}: {str(exc)[:200]}")
        return handled

    def _authorized(self, item: dict[str, Any]) -> bool:
        """The configured private chat AND its owner (a group member is never authorised)."""
        chat = (item.get("message") or {}).get("chat") if "data" in item else item.get("chat")
        sender = item.get("from") or {}
        return (
            chat is not None
            and int(chat.get("id", 0)) == self.chat_id
            and int(sender.get("id", 0)) == self.chat_id
        )

    def _kill(self, system: TradingSystem, msg: dict[str, Any]) -> None:
        if not system.kill_switch_on():
            system.set_kill_switch(True, reason="telegram command", actor=f"telegram:{msg['chat']['id']}")

    def _on_callback(self, cq: dict[str, Any], system: TradingSystem, deadline: float | None = None) -> int:
        message = cq.get("message") or {}
        if not self._authorized(cq):
            self._api("answerCallbackQuery", callback_query_id=cq["id"], text="Non autorizzato")
            return 0
        data = str(cq.get("data", ""))
        action, _, sid = data.partition(":")
        if action not in {"ap", "rj"} or not sid:
            return 0
        user = cq.get("from", {})
        actor = f"telegram:{user.get('id')}:{user.get('username', '')}"
        ok, msg = system.decide(sid, approve=(action == "ap"), actor=actor)
        verdict = ("APPROVATO" if action == "ap" else "RIFIUTATO") if ok else f"NON APPLICATO ({msg})"

        # The decision is recorded: cosmetic Telegram calls may fail (e.g. "query is too old")
        # without ever turning a real approval into a misleading error message, and are skipped
        # once the time budget is over (only the verdict message is always sent).
        def in_time() -> bool:
            return deadline is None or time.monotonic() < deadline

        if in_time():
            with contextlib.suppress(TelegramError):
                self._api("answerCallbackQuery", callback_query_id=cq["id"], text=verdict[:190])
        if in_time() and "message_id" in message:
            with contextlib.suppress(TelegramError):
                self._api(
                    "editMessageReplyMarkup",
                    chat_id=self.chat_id,
                    message_id=message["message_id"],
                    reply_markup={"inline_keyboard": []},
                )
        try:
            self.notify(f"Segnale {sid[:8]}: {verdict}")
        except TelegramError as exc:  # the decision stands; never report it as a failed command
            log.warning("verdict message not delivered: %s", exc)
        return 1

    def _on_message(self, msg: dict[str, Any], system: TradingSystem, deadline: float | None = None) -> int:
        if not self._authorized(msg):
            return 0
        text = str(msg.get("text", "")).strip().lower()
        if text in KILL_COMMANDS:
            self._kill(system, msg)
            return 1
        if text == "/status":
            st = system.status()
            pos = ", ".join(f"{t} {p['qty']}" for t, p in st["positions"].items()) or "nessuna"
            self.notify(
                f"Equity {st['equity']} USD | cash {st['settled_cash']} | posizioni: {pos} | "
                f"kill switch: {'ON' if st['kill_switch'].get('on') else 'off'}"
            )
            return 1
        if text == "/pending":
            pending = system.list_signals(SignalStatus.PENDING_APPROVAL)
            if not pending:
                self.notify("Nessun segnale in attesa.")
            for s in pending:
                if deadline is not None and time.monotonic() >= deadline:
                    break  # the rest is shown at the next /pending
                self.notify_pending(s)
            return 1
        if text in {"/help", "/start"}:
            self.notify(
                "Comandi: /status, /pending, /kill (attiva il kill switch). "
                "La disattivazione del kill switch si fa solo da CLI."
            )
            return 1
        return 0
