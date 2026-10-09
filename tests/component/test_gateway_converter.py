"""Component tests: the Gateway's Phase 4 Converter integration.

Transport, the ADK Bouncer and the Converter are doubled; the real Interviewer
state machine, the shared state driver and the real PortraitStore are exercised
for real (the ADK agents are wired but never invoked offline). Mirrors the
GateClient/FakeBouncer house style from ``test_gateway_interviewer.py``.
"""
import logging

from src.bouncer import (
    BOUNCER_REJECTION,
    HUMAN_VERDICT_REPLY,
    NON_HUMAN_VERDICT_REPLY,
    BouncerDecision,
)
from src.converter import CONVERTER_REPLY_UNAVAILABLE, ConverterError
from src.gateway import GATEWAY_REPLY_RETRY_HINT, REPLY_TEXT, Gateway
from src.interview_state import (
    InterviewPhase,
    InterviewState,
    InterviewStateStore,
    UserProfile,
)
from src.interviewer import (
    INTERVIEW_QUESTIONS,
    INTERVIEWER_REPLY_RESET,
    Interviewer,
    InterviewReply,
)
from src.portrait_store import PortraitStore
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


class GateClient:
    """Scripted Telegram client with photo download + photo send support."""

    def __init__(
        self,
        *,
        fail_get_file: bool = False,
        fail_download: bool = False,
        fail_send_photo: bool = False,
    ) -> None:
        self.sent: list[tuple[int, str]] = []
        self.sent_photos: list[tuple[int, bytes]] = []
        self.calls: list[tuple] = []
        self.get_file_calls: list[str] = []
        self._batches: list[list[Update]] = []
        self._fail_get_file = fail_get_file
        self._fail_download = fail_download
        self._fail_send_photo = fail_send_photo

    def queue(self, updates: list[Update]) -> None:
        self._batches.append(updates)

    def get_updates(self, offset=None, timeout=30, limit=None):
        return self._batches.pop(0) if self._batches else []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))
        self.calls.append(("message", chat_id, text))

    def send_photo(self, chat_id: int, image_bytes: bytes, *, filename="hybrid.jpg", mime_type="image/jpeg") -> None:
        if self._fail_send_photo:
            raise TelegramAPIError("Bad Request: sendPhoto failed")
        self.sent_photos.append((chat_id, image_bytes))
        self.calls.append(("photo", chat_id, image_bytes))

    def get_file(self, file_id: str) -> TelegramFile:
        self.get_file_calls.append(file_id)
        if self._fail_get_file:
            raise TelegramAPIError("Bad Request: wrong file identifier")
        return TelegramFile(file_id=file_id, file_path="photos/sample.jpg")

    def download_file(self, file_path: str) -> bytes:
        if self._fail_download:
            raise TelegramAPIError("ConnectError: connection refused")
        return PHOTO_BYTES


class FakeBouncer:
    """Scripted bouncer: preset verdicts per call, recorded resets."""

    def __init__(self, verdicts: list[bool]) -> None:
        self._verdicts = list(verdicts)
        self.classify_calls: list[tuple[int, int]] = []
        self.resets: list[int] = []

    def classify(self, image_bytes: bytes, chat_id: int) -> BouncerDecision:
        self.classify_calls.append((len(image_bytes), chat_id))
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
        self.calls: list[tuple[int, bytes, UserProfile]] = []
        self.resets: list[int] = []  # converter_session_reset evidence
        self.fail = False
        self.fail_reset = False

    def hybridize(self, chat_id: int, portrait: bytes, profile: UserProfile) -> bytes:
        self.calls.append((chat_id, portrait, profile))
        if self.fail:
            raise ConverterError("blocked key")
        return self._result

    def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)
        if self.fail_reset:
            raise ConverterError("session service down")


class ProfilelessInterviewer:
    """Stub that reports an in-progress interview but completes with no profile."""

    def state(self, chat_id: int) -> InterviewState:
        return InterviewState(
            chat_id=chat_id,
            phase=InterviewPhase.INTERVIEWING,
            updated_at="2026-10-08T00:00:00+00:00",
        )

    def answer(self, chat_id: int, text: str) -> InterviewReply:
        return InterviewReply(messages=["Here is your profile"], completed=True, profile=None)

    def start(self, chat_id: int) -> str:
        return "Q1"

    def reset(self, chat_id: int) -> None:
        return None


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


# --- portrait capture on approval ----------------------------------------------


def test_approved_photo_saves_the_downloaded_portrait_for_that_chat():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = Interviewer(store=InterviewStateStore())
    portraits = PortraitStore()
    gateway = Gateway(client, bouncer=bouncer, interviewer=interviewer, portraits=portraits)

    client.queue([_photo_update(1, chat_id=111)])
    gateway.poll_once()

    assert portraits.get(111) == PHOTO_BYTES
    assert client.sent == [
        (111, REPLY_TEXT),
        (111, HUMAN_VERDICT_REPLY),
        (111, INTERVIEW_QUESTIONS[0]),
    ]


def test_rejected_photo_does_not_save_a_portrait():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[False])
    interviewer = Interviewer(store=InterviewStateStore())
    portraits = PortraitStore()
    gateway = Gateway(client, bouncer=bouncer, interviewer=interviewer, portraits=portraits)

    client.queue([_photo_update(2, chat_id=112)])
    gateway.poll_once()

    assert portraits.get(112) is None


class ExplodingSaveStore(PortraitStore):
    def save(self, chat_id: int, image_bytes: bytes) -> None:
        raise RuntimeError("store down")


class ExplodingDeleteStore(PortraitStore):
    def delete(self, chat_id: int) -> None:
        raise RuntimeError("store down")


def test_portrait_save_failure_is_logged_and_approval_continues(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = Interviewer(store=InterviewStateStore())
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        portraits=ExplodingSaveStore(),
    )

    client.queue([_photo_update(3, chat_id=113)])
    with caplog.at_level(logging.ERROR):
        gateway.poll_once()

    assert client.sent == [
        (113, REPLY_TEXT),
        (113, HUMAN_VERDICT_REPLY),
        (113, INTERVIEW_QUESTIONS[0]),
    ]
    assert any("event=portrait_save_failed" in r.message for r in caplog.records)


def test_portrait_purge_failure_is_logged_and_reset_still_confirms(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[])
    interviewer = Interviewer(store=InterviewStateStore())
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        portraits=ExplodingDeleteStore(),
    )

    client.queue([_text_update(4, chat_id=114, text="/restart")])
    with caplog.at_level(logging.ERROR):
        replied = gateway.poll_once()

    assert replied == 1
    assert client.sent[-1] == (114, INTERVIEWER_REPLY_RESET)
    assert any("event=portrait_delete_failed" in r.message for r in caplog.records)


# --- completion: profile text then exactly one photo ---------------------------


def test_completion_sends_profile_text_then_exactly_one_photo_with_hybrid_bytes(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = Interviewer(store=InterviewStateStore())
    portraits = PortraitStore()
    converter = FakeConverter()
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=converter,
        portraits=portraits,
    )

    with caplog.at_level(logging.INFO):
        _run_full_interview(gateway, client, chat_id=222, first_update=10)

    profile = interviewer.state(222).profile
    assert profile is not None
    assert client.sent_photos == [(222, HYBRID_BYTES)]
    # The profile text is sent, then the photo — same chat, no stray message.
    assert client.calls[-2] == ("message", 222, profile.summary)
    assert client.calls[-1] == ("photo", 222, HYBRID_BYTES)
    assert converter.calls == [(222, PHOTO_BYTES, profile)]
    assert any("event=hybrid_sent" in r.message for r in caplog.records)


def test_conversion_events_are_logged(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = Interviewer(store=InterviewStateStore())
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=FakeConverter(),
        portraits=PortraitStore(),
    )

    with caplog.at_level(logging.INFO):
        _run_full_interview(gateway, client, chat_id=223, first_update=30)

    messages = [r.message for r in caplog.records]
    assert any("event=portrait_saved" in m for m in messages)
    assert any("event=converter_started" in m for m in messages)
    assert any("event=hybrid_sent" in m for m in messages)


# --- converter failure ----------------------------------------------------------


def test_converter_failure_degrades_gracefully_and_loop_survives(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = Interviewer(store=InterviewStateStore())
    converter = FakeConverter()
    converter.fail = True
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=converter,
        portraits=PortraitStore(),
    )

    with caplog.at_level(logging.ERROR):
        _run_full_interview(gateway, client, chat_id=333, first_update=50)

    assert client.sent_photos == []
    assert client.sent[-2:] == [
        (333, CONVERTER_REPLY_UNAVAILABLE),
        (333, GATEWAY_REPLY_RETRY_HINT),
    ]
    assert any("event=converter_failed" in r.message for r in caplog.records)

    # The loop survives: a later update is still processed.
    client.queue([_text_update(60, chat_id=333, text="/restart")])
    replied = gateway.poll_once()
    assert replied == 1
    assert client.sent[-1] == (333, INTERVIEWER_REPLY_RESET)


def test_send_photo_failure_is_logged_and_loop_survives(caplog):
    client = GateClient(fail_send_photo=True)
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = Interviewer(store=InterviewStateStore())
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=FakeConverter(),
        portraits=PortraitStore(),
    )

    with caplog.at_level(logging.ERROR):
        _run_full_interview(gateway, client, chat_id=334, first_update=70)

    assert client.sent_photos == []
    assert client.sent[-2:] == [
        (334, CONVERTER_REPLY_UNAVAILABLE),
        (334, GATEWAY_REPLY_RETRY_HINT),
    ]
    assert any(
        "event=converter_failed" in r.message and "stage=send_photo" in r.message
        for r in caplog.records
    )

    client.queue([_text_update(80, chat_id=334, text="/restart")])
    assert gateway.poll_once() == 1


# --- missing portrait / profile -------------------------------------------------


def test_completion_with_no_saved_portrait_is_unavailable_and_logged(caplog):
    """A chat that completes without an approved photo cannot be hybridised."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[])
    interviewer = Interviewer(store=InterviewStateStore())
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=FakeConverter(),
        portraits=PortraitStore(),
    )
    # Start the interview directly — no portrait was ever saved.
    interviewer.start(chat_id=444)
    for index in range(7):
        client.queue([_text_update(90 + index, chat_id=444, text=f"answer {index}")])
        with caplog.at_level(logging.ERROR):
            gateway.poll_once()

    assert client.sent_photos == []
    assert client.sent[-2:] == [
        (444, CONVERTER_REPLY_UNAVAILABLE),
        (444, GATEWAY_REPLY_RETRY_HINT),
    ]
    assert any("event=converter_portrait_missing" in r.message for r in caplog.records)


def test_complete_without_profile_is_unavailable_and_logged(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[])
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=ProfilelessInterviewer(),
        converter=FakeConverter(),
        portraits=PortraitStore(),
    )

    client.queue([_text_update(100, chat_id=555, text="my answer")])
    with caplog.at_level(logging.ERROR):
        gateway.poll_once()

    assert client.sent_photos == []
    assert client.sent[-2:] == [
        (555, CONVERTER_REPLY_UNAVAILABLE),
        (555, GATEWAY_REPLY_RETRY_HINT),
    ]
    assert any("event=converter_profile_missing" in r.message for r in caplog.records)


# --- purge semantics ------------------------------------------------------------


def test_restart_purges_the_stored_portrait_and_converter_session():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = Interviewer(store=InterviewStateStore())
    portraits = PortraitStore()
    converter = FakeConverter()
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=converter,
        portraits=portraits,
    )
    client.queue([_photo_update(110, chat_id=666)])
    gateway.poll_once()
    assert portraits.get(666) == PHOTO_BYTES

    client.queue([_text_update(111, chat_id=666, text="/restart")])
    gateway.poll_once()

    assert portraits.get(666) is None
    assert converter.resets == [666]  # the ADK session is purged too


def test_rejected_photo_purges_the_stored_portrait_and_converter_session():
    """Rejection is only reachable when no interview is in progress (Phase 7
    decision: photos during an interview are refused, always). A rejection must
    still purge any stored portrait and the converter session."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[False])
    interviewer = Interviewer(store=InterviewStateStore())
    portraits = PortraitStore()
    portraits.save(777, PHOTO_BYTES)  # a portrait stored by an earlier approval
    converter = FakeConverter()
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=converter,
        portraits=portraits,
    )

    client.queue([_photo_update(120, chat_id=777)])  # non-human -> rejected
    gateway.poll_once()

    assert client.sent[-2:] == [(777, BOUNCER_REJECTION), (777, NON_HUMAN_VERDICT_REPLY)]
    assert portraits.get(777) is None
    assert converter.resets == [777]


def test_reset_purges_only_the_reset_chats_portrait():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True, True])
    interviewer = Interviewer(store=InterviewStateStore())
    portraits = PortraitStore()
    converter = FakeConverter()
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=converter,
        portraits=portraits,
    )
    client.queue([_photo_update(130, chat_id=881), _photo_update(131, chat_id=882)])
    gateway.poll_once()

    client.queue([_text_update(132, chat_id=881, text="/restart")])
    gateway.poll_once()

    assert portraits.get(881) is None
    assert portraits.get(882) == PHOTO_BYTES
    assert converter.resets == [881]  # chat-scoped session reset


def test_converter_session_reset_failure_is_logged_and_loop_survives(caplog):
    """Half of the reset contract: a broken converter reset must never stop the
    confirmation or the loop (fail loudly, keep going)."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[])
    converter = FakeConverter()
    converter.fail_reset = True
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=Interviewer(store=InterviewStateStore()),
        converter=converter,
        portraits=PortraitStore(),
    )
    client.queue([_text_update(133, chat_id=883, text="/restart")])

    with caplog.at_level(logging.ERROR):
        replied = gateway.poll_once()

    assert replied == 1
    assert client.sent[-1] == (883, INTERVIEWER_REPLY_RESET)
    assert any("event=converter_session_reset_failed" in r.message for r in caplog.records)


def test_second_interview_cycle_produces_a_fresh_hybrid_after_restart():
    """The single-call contract holds on repeat cycles: /restart → fresh photo
    → fresh interview → a second hybrid image with no stale state."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True, True])
    interviewer = Interviewer(store=InterviewStateStore())
    portraits = PortraitStore()
    converter = FakeConverter()
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=converter,
        portraits=portraits,
    )

    _run_full_interview(gateway, client, 885, first_update=134)
    assert client.sent_photos == [(885, HYBRID_BYTES)]
    assert len(converter.calls) == 1

    client.queue([_text_update(142, chat_id=885, text="/restart")])
    gateway.poll_once()

    # Cycle two: same chat, brand-new portrait + interview + hybrid.
    _run_full_interview(gateway, client, 885, first_update=143)
    assert client.sent_photos == [(885, HYBRID_BYTES), (885, HYBRID_BYTES)]
    assert len(converter.calls) == 2
    assert all(portrait == PHOTO_BYTES for _chat, portrait, _profile in converter.calls)


# --- Phase 3 regression ---------------------------------------------------------


def test_converter_none_keeps_phase3_completion_behaviour(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = Interviewer(store=InterviewStateStore())
    gateway = Gateway(client, bouncer=bouncer, interviewer=interviewer, converter=None)

    with caplog.at_level(logging.INFO):
        _run_full_interview(gateway, client, chat_id=888, first_update=140)

    profile = interviewer.state(888).profile
    assert profile is not None
    assert client.sent_photos == []
    assert client.sent[-1] == (888, profile.summary)  # exactly Phase 3
    messages = [r.message for r in caplog.records]
    assert not any("event=converter_" in m or "event=hybrid_" in m for m in messages)


def test_portraits_none_keeps_phase3_completion_behaviour():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = Interviewer(store=InterviewStateStore())
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=FakeConverter(),
        portraits=None,
    )

    _run_full_interview(gateway, client, chat_id=889, first_update=160)

    profile = interviewer.state(889).profile
    assert profile is not None
    assert client.sent_photos == []
    assert client.sent[-1] == (889, profile.summary)


def test_converter_metadata_is_not_required_after_completion():
    """A completed chat's later text re-sends the profile only (no re-hybridise)."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = Interviewer(store=InterviewStateStore())
    converter = FakeConverter()
    gateway = Gateway(
        client,
        bouncer=bouncer,
        interviewer=interviewer,
        converter=converter,
        portraits=PortraitStore(),
    )
    _run_full_interview(gateway, client, chat_id=890, first_update=180)
    photo_count = len(client.sent_photos)

    client.queue([_text_update(190, chat_id=890, text="tell me more")])
    gateway.poll_once()

    assert len(client.sent_photos) == photo_count  # no second hybrid
    assert client.sent[-1] == (890, interviewer.state(890).profile.summary)
