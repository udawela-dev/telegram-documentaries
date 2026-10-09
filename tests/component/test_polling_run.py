"""Component tests: the polling loop's run() with scripted doubles (no I/O).

Honestly labelled per the repo philosophy: multiple dependencies are mocked
(both client operations), the behaviour under test is real.
"""
import logging
import threading

from src.gateway import Gateway
from src.telegram_models import Chat, Message, TelegramAPIError, Update


class RunClient:
    """Scripted client: steps may be update batches, callables, or exceptions."""

    def __init__(self) -> None:
        self.steps: list = []
        self.get_updates_calls = 0
        self.sent: list[tuple[int, str]] = []

    def get_updates(self, offset=None, timeout=30, limit=None):
        self.get_updates_calls += 1
        if not self.steps:
            return []
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(offset) if callable(step) else step

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def _update(update_id: int, chat_id: int) -> Update:
    return Update(update_id=update_id, message=Message(chat=Chat(id=chat_id), text="hi"))


def test_run_survives_poll_failure_then_processes_and_stops():
    client = RunClient()
    stop = threading.Event()
    client.steps = [
        TelegramAPIError("ConnectError: connection refused"),
        [_update(1, chat_id=111)],
        lambda _offset: (stop.set(), []),
    ]
    gateway = Gateway(client, backoff_seconds=0.0)

    gateway.run(stop)

    assert client.sent == [(111, "Hi Mate")]
    assert client.get_updates_calls == 3


def test_run_stops_immediately_when_event_pre_set():
    client = RunClient()
    stop = threading.Event()
    stop.set()

    Gateway(client).run(stop)

    assert client.get_updates_calls == 0
    assert client.sent == []


def test_run_processes_whole_batch_then_exits_on_stop():
    client = RunClient()
    stop = threading.Event()
    client.steps = [
        [_update(1, chat_id=111), _update(2, chat_id=222)],
        lambda _offset: (stop.set(), []),
    ]

    Gateway(client, backoff_seconds=0.0).run(stop)

    assert client.sent == [(111, "Hi Mate"), (222, "Hi Mate")]


def test_run_logs_polling_started_and_stopped(caplog):
    client = RunClient()
    stop = threading.Event()
    stop.set()

    with caplog.at_level(logging.INFO):
        Gateway(client).run(stop)

    messages = [r.message for r in caplog.records]
    assert any("event=polling_started" in m for m in messages)
    assert any("event=polling_stopped" in m for m in messages)


def test_run_unexpected_poll_error_is_logged_and_loop_survives(caplog):
    client = RunClient()
    stop = threading.Event()
    client.steps = [
        ValueError("something bizarre"),
        lambda _offset: (stop.set(), []),
    ]

    with caplog.at_level(logging.ERROR):
        Gateway(client, backoff_seconds=0.0).run(stop)

    assert any("event=poll_unexpected_failure" in r.message for r in caplog.records)
    assert client.get_updates_calls == 2