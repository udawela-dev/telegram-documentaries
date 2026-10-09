"""Component tests: the Gateway's Phase 5 Scripter integration.

Transport, the ADK Bouncer and the Converter are doubled; the real Interviewer
state machine, the shared state driver and the real PortraitStore are exercised
for real. The ``FakeScripter`` extends the real :class:`Scripter` (so the real
persistence path onto the shared store is exercised) but overrides the LLM seam
— no network. Mirrors the GateClient/FakeBouncer house style from
``test_gateway_converter.py``.

The locked chat-visible completion order is asserted end to end:
**profile text → hybrid photo → script text**.
"""
import logging

from src.bouncer import (
    BouncerDecision,
)
from src.converter import CONVERTER_REPLY_UNAVAILABLE
from src.gateway import Gateway
from src.interview_state import InterviewStateStore
from src.interviewer import INTERVIEWER_REPLY_RESET, Interviewer
from src.portrait_store import PortraitStore
from src.scripter import SCRIPTER_REPLY_UNAVAILABLE, Scripter, ScripterError
from src.telegram_models import (
    Chat,
    Message,
    PhotoSize,
    TelegramAPIError,
    TelegramFile,
    Update,
)

PHOTO_BYTES = b"image-bytes"
HYBRID_BYTES = b"hybrid-bytes"
SCRIPT = " ".join(f"word{i}" for i in range(70))  # a valid 70-word paragraph


class GateClient:
    """Scripted Telegram client with photo download + photo send support."""

    def __init__(
        self, *, fail_send_photo: bool = False, fail_send_message_text: str | None = None
    ) -> None:
        self.sent: list[tuple[int, str]] = []
        self.sent_photos: list[tuple[int, bytes]] = []
        self.calls: list[tuple] = []
        self.get_file_calls: list[str] = []
        self._batches: list[list[Update]] = []
        self._fail_send_photo = fail_send_photo
        # Fail (only) a send_message whose text matches this exact string: lets a
        # test make the *script* delivery fail while the profile text succeeds.
        self._fail_send_message_text = fail_send_message_text

    def queue(self, updates: list[Update]) -> None:
        self._batches.append(updates)

    def get_updates(self, offset=None, timeout=30, limit=None):
        return self._batches.pop(0) if self._batches else []

    def send_message(self, chat_id: int, text: str) -> None:
        if self._fail_send_message_text is not None and text == self._fail_send_message_text:
            raise TelegramAPIError("Bad Request: sendMessage failed")
        self.sent.append((chat_id, text))
        self.calls.append(("message", chat_id, text))

    def send_photo(self, chat_id: int, image_bytes: bytes, *, filename="hybrid.jpg", mime_type="image/jpeg") -> None:
        if self._fail_send_photo:
            raise TelegramAPIError("Bad Request: sendPhoto failed")
        self.sent_photos.append((chat_id, image_bytes))
        self.calls.append(("photo", chat_id, image_bytes))

    def get_file(self, file_id: str) -> TelegramFile:
        self.get_file_calls.append(file_id)
        return TelegramFile(file_id=file_id, file_path="photos/sample.jpg")

    def download_file(self, file_path: str) -> bytes:
        return PHOTO_BYTES


class FakeBouncer:
    """Scripted bouncer: preset verdicts per call, recorded resets."""

    def __init__(self, verdicts: list[bool]) -> None:
        self._verdicts = list(verdicts)
        self.resets: list[int] = []

    def classify(self, image_bytes: bytes, chat_id: int) -> BouncerDecision:
        human_present = self._verdicts.pop(0)
        return BouncerDecision(
            human_present=human_present,
            reason="clear face" if human_present else "no humans here",
        )

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


class FakeConverter:
    """Scripted converter: preset image bytes, recorded calls, switchable failure."""

    def __init__(self, result: bytes = HYBRID_BYTES) -> None:
        self._result = result
        self.calls: list[tuple] = []
        self.fail = False

    def hybridize(self, chat_id: int, portrait: bytes, profile) -> bytes:
        self.calls.append((chat_id, portrait, profile))
        if self.fail:
            raise RuntimeError("blocked key")
        return self._result

    def reset_chat(self, chat_id: int) -> None:
        return None


class FakeScripter(Scripter):
    """Scripted Scripter: preset paragraph, recorded calls, switchable failure.

    Extends the real Scripter so the real ``write_script`` persistence path runs
    against the shared store; only the LLM seam is overridden (no network).
    """

    def __init__(self, store: InterviewStateStore, script: str = SCRIPT, fail: bool = False) -> None:
        super().__init__(api_key="x", store=store, local_writer=None)
        self._script = script
        self.fail = fail
        self.calls: list[tuple] = []  # (chat_id, profile) per write_script call
        self.llm_calls: list[str] = []

    def write_script(self, chat_id: int, profile) -> str:
        self.calls.append((chat_id, profile))
        return super().write_script(chat_id, profile)

    def _run_llm(self, content, session_id: str) -> str:
        self.llm_calls.append(session_id)
        if self.fail:
            raise ScripterError("blocked key")
        return self._script


def _text_update(update_id: int, chat_id: int, text: str = "hi") -> Update:
    return Update(update_id=update_id, message=Message(chat=Chat(id=chat_id), text=text))


def _photo_update(update_id: int, chat_id: int) -> Update:
    return Update(
        update_id=update_id,
        message=Message(chat=Chat(id=chat_id), photo=[PhotoSize(file_id="f", width=100, height=100)]),
    )


def _run_full_interview(gateway: Gateway, client: GateClient, chat_id: int, first_update: int) -> None:
    """Drive one approved photo through all seven answers."""
    client.queue([_photo_update(first_update, chat_id)])
    gateway.poll_once()
    for index in range(7):
        client.queue([_text_update(first_update + 1 + index, chat_id, text=f"answer {index}")])
        gateway.poll_once()


# --- locked ordering: profile text → hybrid photo → script text -----------------


def test_full_pipeline_sends_profile_then_photo_then_script_and_stores_it(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    store = InterviewStateStore()
    interviewer = Interviewer(store=store)
    scripter = FakeScripter(store=store)
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=FakeConverter(),
        portraits=PortraitStore(),
        scripter=scripter,
    )

    with caplog.at_level(logging.INFO):
        _run_full_interview(gateway, client, chat_id=222, first_update=10)

    profile = interviewer.state(222).profile
    assert profile is not None
    assert client.calls[-3] == ("message", 222, profile.summary)
    assert client.calls[-2] == ("photo", 222, HYBRID_BYTES)
    assert client.calls[-1] == ("message", 222, SCRIPT)
    assert interviewer.state(222).script == SCRIPT
    assert scripter.calls == [(222, profile)]

    messages = [r.message for r in caplog.records]
    assert any("event=scripter_started" in m for m in messages)
    assert any("event=script_generated" in m and "source=gemini" in m for m in messages)
    assert any("event=script_stored" in m for m in messages)


def test_the_stored_script_is_the_same_text_sent_to_the_chat():
    """The gateway sends the validated paragraph the Scripter stored for Phase 6."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    store = InterviewStateStore()
    interviewer = Interviewer(store=store)
    scripter = FakeScripter(store=store)
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=FakeConverter(),
        portraits=PortraitStore(),
        scripter=scripter,
    )

    _run_full_interview(gateway, client, chat_id=223, first_update=30)

    assert client.sent[-1] == (223, SCRIPT)
    assert interviewer.state(223).script == SCRIPT


# --- scripter failure -----------------------------------------------------------


def test_scripter_failure_sends_unavailable_and_leaves_state_untouched(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    store = InterviewStateStore()
    interviewer = Interviewer(store=store)
    scripter = FakeScripter(store=store, fail=True)
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=FakeConverter(),
        portraits=PortraitStore(),
        scripter=scripter,
    )

    with caplog.at_level(logging.ERROR):
        _run_full_interview(gateway, client, chat_id=333, first_update=50)

    assert client.sent_photos == [(333, HYBRID_BYTES)]  # photo still delivered
    assert client.sent[-1] == (333, SCRIPTER_REPLY_UNAVAILABLE)
    assert interviewer.state(333).script is None
    assert any("event=script_failed" in r.message for r in caplog.records)

    # The loop survives: a later update is still processed.
    client.queue([_text_update(60, chat_id=333, text="/restart")])
    replied = gateway.poll_once()
    assert replied == 1
    assert client.sent[-1] == (333, INTERVIEWER_REPLY_RESET)


# --- script produced + stored, only delivery failed -----------------------------


def test_script_send_failure_keeps_the_stored_script_and_does_not_apologise(caplog):
    """A send failure is a delivery failure only: the script WAS produced and
    stored, so the user must not get the misleading 'quill ran dry' copy."""
    client = GateClient(fail_send_message_text=SCRIPT)
    bouncer = FakeBouncer(verdicts=[True])
    store = InterviewStateStore()
    interviewer = Interviewer(store=store)
    scripter = FakeScripter(store=store)
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=FakeConverter(),
        portraits=PortraitStore(),
        scripter=scripter,
    )

    with caplog.at_level(logging.ERROR):
        _run_full_interview(gateway, client, chat_id=444, first_update=170)

    # Profile text + hybrid photo delivered; only the script send failed.
    assert client.sent_photos == [(444, HYBRID_BYTES)]
    assert client.sent[-1] == (444, interviewer.state(444).profile.summary)
    # No misleading apology — the script exists, only its delivery failed.
    assert (444, SCRIPTER_REPLY_UNAVAILABLE) not in client.sent
    # The stored script survives for Phase 6 (the Narrator).
    assert interviewer.state(444).script == SCRIPT
    assert any(
        "event=script_send_failed" in r.message and "error_type=TelegramAPIError" in r.message
        for r in caplog.records
    )

    # The loop survives: a later update is still processed.
    client.queue([_text_update(180, chat_id=444, text="/restart")])
    replied = gateway.poll_once()
    assert replied == 1
    assert client.sent[-1] == (444, INTERVIEWER_REPLY_RESET)


# --- scripter=None regression ---------------------------------------------------


def test_scripter_none_keeps_phases_3_4_behaviour(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    store = InterviewStateStore()
    interviewer = Interviewer(store=store)
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=FakeConverter(),
        portraits=PortraitStore(),
        scripter=None,
    )

    with caplog.at_level(logging.INFO):
        _run_full_interview(gateway, client, chat_id=888, first_update=140)

    profile = interviewer.state(888).profile
    assert profile is not None
    assert client.sent_photos == [(888, HYBRID_BYTES)]
    # Exactly Phases 3+4: the photo is the last thing sent.
    assert client.calls[-1] == ("photo", 888, HYBRID_BYTES)
    assert interviewer.state(888).script is None
    messages = [r.message for r in caplog.records]
    assert not any("event=script_" in m or "event=scripter_" in m for m in messages)


# --- no scripting when conversion is skipped or failed --------------------------


def test_converter_failure_does_not_invoke_the_scripter(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    store = InterviewStateStore()
    interviewer = Interviewer(store=store)
    converter = FakeConverter()
    converter.fail = True
    scripter = FakeScripter(store=store)
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=converter,
        portraits=PortraitStore(),
        scripter=scripter,
    )

    with caplog.at_level(logging.ERROR):
        _run_full_interview(gateway, client, chat_id=334, first_update=70)

    assert client.sent_photos == []
    assert client.sent[-1] == (334, CONVERTER_REPLY_UNAVAILABLE)
    assert scripter.calls == []
    assert interviewer.state(334).script is None


def test_send_photo_failure_does_not_invoke_the_scripter(caplog):
    client = GateClient(fail_send_photo=True)
    bouncer = FakeBouncer(verdicts=[True])
    store = InterviewStateStore()
    interviewer = Interviewer(store=store)
    scripter = FakeScripter(store=store)
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=FakeConverter(),
        portraits=PortraitStore(),
        scripter=scripter,
    )

    with caplog.at_level(logging.ERROR):
        _run_full_interview(gateway, client, chat_id=336, first_update=110)

    assert client.sent_photos == []
    assert client.sent[-1] == (336, CONVERTER_REPLY_UNAVAILABLE)
    assert scripter.calls == []
    assert interviewer.state(336).script is None


def test_converter_none_does_not_invoke_the_scripter():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    store = InterviewStateStore()
    interviewer = Interviewer(store=store)
    scripter = FakeScripter(store=store)
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=None,
        portraits=PortraitStore(),
        scripter=scripter,
    )

    _run_full_interview(gateway, client, chat_id=335, first_update=90)

    profile = interviewer.state(335).profile
    assert profile is not None
    assert scripter.calls == []
    assert client.sent_photos == []
    assert client.calls[-1] == ("message", 335, profile.summary)  # exactly Phase 3


# --- reset clears the stored script ---------------------------------------------


def test_restart_after_a_stored_script_purges_it_and_a_fresh_cycle_works():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True, True])
    store = InterviewStateStore()
    interviewer = Interviewer(store=store)
    scripter = FakeScripter(store=store)
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=FakeConverter(),
        portraits=PortraitStore(),
        scripter=scripter,
    )

    _run_full_interview(gateway, client, 885, first_update=134)
    assert interviewer.state(885).script == SCRIPT

    client.queue([_text_update(142, chat_id=885, text="/restart")])
    gateway.poll_once()

    assert interviewer.state(885).script is None  # reset purges the stored script

    # Cycle two: a fresh interview stores a fresh script.
    _run_full_interview(gateway, client, 885, first_update=143)
    assert interviewer.state(885).script == SCRIPT
    assert len(scripter.calls) == 2
    assert client.sent[-1] == (885, SCRIPT)
