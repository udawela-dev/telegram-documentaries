"""Component tests: the Gateway's Phase 3 interview integration.

Transport and the ADK Bouncer are doubled; the routing, the real Interviewer
state machine, and the shared state driver are exercised for real (the ADK agent
is wired but never invoked offline). Mirrors the GateClient/FakeBouncer house
style from ``test_gateway_bouncer.py``.
"""
import logging

from src.bouncer import (
    BOUNCER_REJECTION,
    HUMAN_VERDICT_REPLY,
    NON_HUMAN_VERDICT_REPLY,
    BouncerDecision,
)
from src.gateway import (
    GATEWAY_REPLY_NEED_PHOTO,
    GATEWAY_REPLY_PHOTO_DURING_INTERVIEW,
    GATEWAY_REPLY_RETRY_HINT,
    REPLY_TEXT,
    Gateway,
)
from src.interview_state import (
    InterviewPhase,
    InterviewState,
    InterviewStateError,
    InterviewStateStore,
)
from src.interviewer import (
    INTERVIEW_QUESTIONS,
    INTERVIEWER_REPLY_RESET,
    INTERVIEWER_REPLY_UNAVAILABLE,
    Interviewer,
)
from src.telegram_models import (
    Chat,
    Message,
    PhotoSize,
    TelegramAPIError,
    TelegramFile,
    Update,
)


class GateClient:
    """Scripted Telegram client with photo download support."""

    def __init__(self, *, fail_get_file: bool = False, fail_download: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.get_file_calls: list[str] = []
        self._batches: list[list[Update]] = []
        self._fail_get_file = fail_get_file
        self._fail_download = fail_download

    def queue(self, updates: list[Update]) -> None:
        self._batches.append(updates)

    def get_updates(self, offset=None, timeout=30, limit=None):
        return self._batches.pop(0) if self._batches else []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def get_file(self, file_id: str) -> TelegramFile:
        self.get_file_calls.append(file_id)
        if self._fail_get_file:
            raise TelegramAPIError("Bad Request: wrong file identifier")
        return TelegramFile(file_id=file_id, file_path="photos/sample.jpg")

    def download_file(self, file_path: str) -> bytes:
        if self._fail_download:
            raise TelegramAPIError("ConnectError: connection refused")
        return b"image-bytes"


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


def _text_update(update_id: int, chat_id: int, text: str = "hi") -> Update:
    return Update(update_id=update_id, message=Message(chat=Chat(id=chat_id), text=text))


def _photo_update(update_id: int, chat_id: int) -> Update:
    return Update(
        update_id=update_id,
        message=Message(chat=Chat(id=chat_id), photo=[PhotoSize(file_id="f", width=100, height=100)]),
    )


def _interviewer() -> Interviewer:
    return Interviewer(store=InterviewStateStore())


def _gateway(client, bouncer, interviewer) -> Gateway:
    return Gateway(client, bouncer=bouncer, interviewer=interviewer)


# --- IDLE regression -----------------------------------------------------------


def test_idle_text_prompts_for_a_photo():
    """Phase 7: at IDLE the loop tells the user to send a portrait; 'Hi Mate'
    alone would leave them guessing. (COMPLETE chats keep the confirmation.)"""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[])
    interviewer = _interviewer()
    client.queue([_text_update(1, chat_id=111)])

    replied = _gateway(client, bouncer, interviewer).poll_once()

    assert replied == 1
    assert client.sent == [(111, GATEWAY_REPLY_NEED_PHOTO)]
    assert interviewer.state(111).phase is InterviewPhase.IDLE


# --- approved photo starts the interview ---------------------------------------


def test_approved_photo_sends_two_bouncer_messages_then_first_question():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = _interviewer()
    client.queue([_photo_update(2, chat_id=222)])

    _gateway(client, bouncer, interviewer).poll_once()

    assert client.sent == [
        (222, REPLY_TEXT),
        (222, HUMAN_VERDICT_REPLY),
        (222, INTERVIEW_QUESTIONS[0]),
    ]
    assert interviewer.state(222).phase is InterviewPhase.INTERVIEWING


# --- sequential one-question-per-message ---------------------------------------


def test_answers_advance_one_question_per_message_and_complete():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = _interviewer()
    gateway = _gateway(client, bouncer, interviewer)

    client.queue([_photo_update(3, chat_id=333)])
    gateway.poll_once()
    assert client.sent[-1] == (333, INTERVIEW_QUESTIONS[0])

    for index, answer in enumerate([f"answer {i}" for i in range(1, 8)]):
        client.queue([_text_update(10 + index, chat_id=333, text=answer)])
        replied = gateway.poll_once()
        assert replied == 1
        # Exactly one message per answer — never two questions at once.
        start = 3 + index  # messages already sent before this answer
        assert len(client.sent) == start + 1

    # Q2..Q7 were each sent once, then the completion summary.
    asked = [text for chat, text in client.sent if chat == 333 and text in INTERVIEW_QUESTIONS]
    assert asked == list(INTERVIEW_QUESTIONS)

    state = interviewer.state(333)
    assert state.phase is InterviewPhase.COMPLETE
    assert state.profile is not None
    assert client.sent[-1] == (333, state.profile.summary)


def test_completion_reply_contains_profile_labels_and_suggested_animal():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = _interviewer()
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_photo_update(4, chat_id=444)])
    gateway.poll_once()
    for index in range(7):
        client.queue([_text_update(20 + index, chat_id=444, text=f"answer {index}")])
        gateway.poll_once()

    final = client.sent[-1][1]

    for label in ("Habits", "Quirks", "Routines", "Preferences"):
        assert f"{label}:" in final
    assert "Suggested animal:" in final
    assert interviewer.state(444).profile.suggested_animal in final


def test_complete_chat_text_resends_the_stored_profile(caplog):
    """Spec: after completion, a text re-sends the stored profile + animal."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = _interviewer()
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_photo_update(9, chat_id=911)])
    gateway.poll_once()
    for index in range(7):
        client.queue([_text_update(70 + index, chat_id=911, text=f"answer {index}")])
        gateway.poll_once()

    profile = interviewer.state(911).profile
    assert profile is not None
    client.queue([_text_update(77, chat_id=911, text="tell me more")])

    with caplog.at_level(logging.INFO):
        gateway.poll_once()

    assert client.sent[-1] == (911, profile.summary)
    assert "Suggested animal:" in profile.summary
    assert any("event=interview_profile_resent" in r.message for r in caplog.records)


def test_complete_missing_profile_never_quietly_replies_hi_mate(caplog):
    """An inconsistent COMPLETE-without-profile state fails loudly, never 'Hi Mate'."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[])
    # Programmer-error/foreign state: COMPLETE but no profile, pre-seeded in the
    # shared driver before the Interviewer is built.
    store = InterviewStateStore()
    store._states[991] = InterviewState(
        chat_id=991,
        phase=InterviewPhase.COMPLETE,
        updated_at="2026-10-08T00:00:00+00:00",
    )
    interviewer = Interviewer(store=store)
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_text_update(78, chat_id=991, text="hello again")])

    with caplog.at_level(logging.ERROR):
        gateway.poll_once()

    assert client.sent == [(991, REPLY_TEXT)]
    assert any("event=interview_complete_missing_profile" in r.message for r in caplog.records)


# --- chat isolation ------------------------------------------------------------


def test_two_chats_interleaved_never_cross_state():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True, True])
    interviewer = _interviewer()
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_photo_update(5, chat_id=501), _photo_update(6, chat_id=502)])
    gateway.poll_once()

    # Chat 501 answers twice, chat 502 once.
    client.queue([_text_update(30, chat_id=501, text="a1")])
    gateway.poll_once()
    client.queue([_text_update(31, chat_id=502, text="b1")])
    gateway.poll_once()
    client.queue([_text_update(32, chat_id=501, text="a2")])
    gateway.poll_once()

    assert [text for chat, text in client.sent if chat == 501][-1] == INTERVIEW_QUESTIONS[2]
    assert [text for chat, text in client.sent if chat == 502][-1] == INTERVIEW_QUESTIONS[1]
    assert len(interviewer.state(501).answers) == 2
    assert len(interviewer.state(502).answers) == 1
    assert interviewer.state(501).answers[0][1] == "a1"
    assert interviewer.state(502).answers[0][1] == "b1"


# --- reset commands ------------------------------------------------------------


def test_restart_mid_interview_resets_both_stages_and_confirms():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = _interviewer()
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_photo_update(7, chat_id=701)])
    gateway.poll_once()
    client.queue([_text_update(40, chat_id=701, text="halfway answer")])
    gateway.poll_once()

    client.queue([_text_update(41, chat_id=701, text="/restart")])
    replied = gateway.poll_once()

    assert replied == 1
    assert bouncer.resets == [701]
    assert interviewer.state(701).phase is InterviewPhase.IDLE
    assert client.sent[-1] == (701, INTERVIEWER_REPLY_RESET)

    # After restart, plain text prompts for a portrait again (Phase 7).
    client.queue([_text_update(42, chat_id=701, text="hello again")])
    gateway.poll_once()
    assert client.sent[-1] == (701, GATEWAY_REPLY_NEED_PHOTO)


def test_start_command_also_resets():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = _interviewer()
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_photo_update(8, chat_id=801)])
    gateway.poll_once()

    client.queue([_text_update(50, chat_id=801, text="/start")])
    gateway.poll_once()

    assert bouncer.resets == [801]
    assert interviewer.state(801).phase is InterviewPhase.IDLE


def test_reset_command_failure_is_logged_and_never_kills_the_loop(caplog):
    class StubbornBouncer(FakeBouncer):
        def reset_chat(self, chat_id: int) -> None:
            raise RuntimeError("session service down")

    client = GateClient()
    bouncer = StubbornBouncer(verdicts=[])
    interviewer = _interviewer()
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_text_update(60, chat_id=601, text="/restart")])

    with caplog.at_level(logging.ERROR):
        replied = gateway.poll_once()

    assert replied == 1
    assert client.sent == [(601, INTERVIEWER_REPLY_RESET)]
    assert any("event=bouncer_session_reset_failed" in r.message for r in caplog.records)


def test_interviewer_reset_failure_is_logged_and_bouncer_still_resets(caplog):
    """Half of the reset contract: Interviewer.reset failing must not stop the
    Bouncer reset or the confirmation reply (fail loudly, loop survives)."""

    class StubbornInterviewer(Interviewer):
        def reset(self, chat_id: int) -> None:
            raise RuntimeError("session service down")

    client = GateClient()
    bouncer = FakeBouncer(verdicts=[])
    interviewer = StubbornInterviewer(store=InterviewStateStore())
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_text_update(61, chat_id=611, text="/restart")])

    with caplog.at_level(logging.ERROR):
        replied = gateway.poll_once()

    assert replied == 1
    assert bouncer.resets == [611]  # the other stage was still attempted
    assert client.sent == [(611, INTERVIEWER_REPLY_RESET)]
    assert any("event=interview_reset_failed" in r.message for r in caplog.records)


def test_interview_state_lookup_failure_replies_confirmation_and_recovers(caplog):
    """The interview phase decision failing degrades to the confirmation reply
    and a later /restart brings the chat back (no poisoned gateway)."""

    class ExplodingStateInterviewer(Interviewer):
        def state(self, chat_id: int):
            raise InterviewStateError("stale session")

    client = GateClient()
    bouncer = FakeBouncer(verdicts=[])
    interviewer = ExplodingStateInterviewer(store=InterviewStateStore())
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_text_update(62, chat_id=621, text="hello")])

    with caplog.at_level(logging.ERROR):
        gateway.poll_once()

    assert client.sent == [(621, REPLY_TEXT)]
    assert any("event=interview_state_failed" in r.message for r in caplog.records)

    client.queue([_text_update(63, chat_id=621, text="/restart")])
    gateway.poll_once()
    assert client.sent[-1] == (621, INTERVIEWER_REPLY_RESET)


# --- rejection purges interviewer state ----------------------------------------


def test_rejected_photo_keeps_rejection_flow_and_purges_interview_state():
    """Phase 7 decision: photos during an interview are refused, never re-gated.
    So a rejection is only reachable from IDLE — it must still purge state."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True, False])
    interviewer = _interviewer()
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_photo_update(9, chat_id=901)])
    gateway.poll_once()
    client.queue([_text_update(70, chat_id=901, text="mid answer")])
    gateway.poll_once()
    assert interviewer.state(901).phase is InterviewPhase.INTERVIEWING

    # Back to IDLE, then a non-human photo gets the full rejection flow.
    client.queue([_text_update(71, chat_id=901, text="/restart")])
    gateway.poll_once()
    assert interviewer.state(901).phase is InterviewPhase.IDLE

    client.queue([_photo_update(72, chat_id=901)])
    gateway.poll_once()

    assert client.sent[-2:] == [(901, BOUNCER_REJECTION), (901, NON_HUMAN_VERDICT_REPLY)]
    assert bouncer.resets == [901, 901]  # /restart + rejection both reset
    assert interviewer.state(901).phase is InterviewPhase.IDLE

    # Next text is the portrait prompt, not a question continuation (Phase 7).
    client.queue([_text_update(73, chat_id=901, text="anything")])
    gateway.poll_once()
    assert client.sent[-1] == (901, GATEWAY_REPLY_NEED_PHOTO)


def test_rejection_resets_only_the_rejected_chats_interviewer_state():
    """A rejection only happens from IDLE, and only touches that one chat."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True, True, False])
    interviewer = _interviewer()
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_photo_update(11, chat_id=1101), _photo_update(12, chat_id=1102)])
    gateway.poll_once()
    # Chat 1101 returns to IDLE, then sends a non-human photo → rejected.
    client.queue([_text_update(13, chat_id=1101, text="/restart")])
    gateway.poll_once()
    client.queue([_photo_update(14, chat_id=1101)])  # rejected
    gateway.poll_once()

    assert interviewer.state(1101).phase is InterviewPhase.IDLE
    assert interviewer.state(1102).phase is InterviewPhase.INTERVIEWING
    assert 1102 not in bouncer.resets  # the other chat's session is untouched


# --- backward compatibility ----------------------------------------------------


def test_gateway_without_interviewer_keeps_phase1_text_behaviour():
    """`interviewer=None` means every text (commands included) says "Hi Mate"."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    client.queue([_text_update(80, chat_id=1201, text="/restart")])
    client.queue([_text_update(81, chat_id=1201, text="hello")])

    gateway = Gateway(client, bouncer=bouncer)
    gateway.poll_once()
    gateway.poll_once()

    assert client.sent == [(1201, REPLY_TEXT), (1201, REPLY_TEXT)]
    assert bouncer.resets == []  # no interviewer → no reset semantics


def test_gateway_without_interviewer_approved_photo_sends_only_two_messages():
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    client.queue([_photo_update(90, chat_id=1301)])

    Gateway(client, bouncer=bouncer).poll_once()

    assert client.sent == [(1301, REPLY_TEXT), (1301, HUMAN_VERDICT_REPLY)]


# --- resilience ----------------------------------------------------------------


class FlakyInterviewer(Interviewer):
    """Real interviewer with switchable failures injected at the boundary."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_start = False
        self.fail_answer = False

    def start(self, chat_id: int) -> str:
        if self.fail_start:
            raise RuntimeError("interviewer agent down")
        return super().start(chat_id)

    def answer(self, chat_id: int, text: str):
        if self.fail_answer:
            raise RuntimeError("interviewer agent down")
        return super().answer(chat_id, text)


def test_start_failure_is_logged_and_loop_survives(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = FlakyInterviewer(store=InterviewStateStore())
    interviewer.fail_start = True
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_photo_update(91, chat_id=1401)])

    with caplog.at_level(logging.ERROR):
        replied = gateway.poll_once()

    assert replied == 1
    assert client.sent == [
        (1401, REPLY_TEXT),
        (1401, HUMAN_VERDICT_REPLY),
        (1401, INTERVIEWER_REPLY_UNAVAILABLE),
        (1401, GATEWAY_REPLY_RETRY_HINT),
    ]
    assert any("event=interview_start_failed" in r.message for r in caplog.records)

    # The loop still processes later batches (IDLE → portrait prompt, Phase 7).
    client.queue([_text_update(92, chat_id=1401, text="still here")])
    gateway.poll_once()
    assert client.sent[-1] == (1401, GATEWAY_REPLY_NEED_PHOTO)


def test_answer_failure_is_logged_and_loop_survives(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = FlakyInterviewer(store=InterviewStateStore())
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_photo_update(93, chat_id=1501)])
    gateway.poll_once()
    interviewer.fail_answer = True

    client.queue([_text_update(94, chat_id=1501, text="my answer")])
    with caplog.at_level(logging.ERROR):
        replied = gateway.poll_once()

    assert replied == 1
    assert client.sent[-2:] == [
        (1501, INTERVIEWER_REPLY_UNAVAILABLE),
        (1501, GATEWAY_REPLY_RETRY_HINT),
    ]
    assert any("event=interview_answer_failed" in r.message for r in caplog.records)

    # State is intact: the next good answer continues the sequence.
    interviewer.fail_answer = False
    client.queue([_text_update(95, chat_id=1501, text="my answer again")])
    gateway.poll_once()
    assert client.sent[-1] == (1501, INTERVIEW_QUESTIONS[1])


def test_photo_during_interview_is_refused_without_disturbing_the_interview():
    """Phase 7: a photo mid-interview is refused before the gate (and before any
    download) is even attempted — the in-progress interview is left untouched."""
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True, True])  # the second photo never reaches the gate
    interviewer = _interviewer()
    gateway = _gateway(client, bouncer, interviewer)
    client.queue([_photo_update(96, chat_id=1601)])
    gateway.poll_once()
    client.queue([_text_update(97, chat_id=1601, text="answer one")])
    gateway.poll_once()
    assert interviewer.state(1601).phase is InterviewPhase.INTERVIEWING
    classify_calls_before = len(bouncer.classify_calls)

    client.queue([_photo_update(98, chat_id=1601)])  # a second photo mid-interview
    gateway.poll_once()

    assert client.sent[-1] == (1601, GATEWAY_REPLY_PHOTO_DURING_INTERVIEW)
    assert len(bouncer.classify_calls) == classify_calls_before  # gate not re-run
    assert bouncer.resets == []  # no reset, no purge
    assert interviewer.state(1601).phase is InterviewPhase.INTERVIEWING
    assert len(interviewer.state(1601).answers) == 1  # interview undisturbed

    # The interview carries on: the next answer advances normally.
    client.queue([_text_update(99, chat_id=1601, text="answer two")])
    gateway.poll_once()
    assert client.sent[-1] == (1601, INTERVIEW_QUESTIONS[2])
    assert len(interviewer.state(1601).answers) == 2


# --- event logging -------------------------------------------------------------


def test_interview_events_are_logged(caplog):
    client = GateClient()
    bouncer = FakeBouncer(verdicts=[True])
    interviewer = _interviewer()
    gateway = _gateway(client, bouncer, interviewer)
    with caplog.at_level(logging.INFO):
        client.queue([_photo_update(99, chat_id=1701)])
        gateway.poll_once()
        for index in range(7):
            client.queue([_text_update(100 + index, chat_id=1701, text=f"answer {index}")])
            gateway.poll_once()
        client.queue([_text_update(200, chat_id=1701, text="/restart")])
        gateway.poll_once()

    messages = [r.message for r in caplog.records]
    assert any("event=interview_started" in m for m in messages)
    assert any("event=interview_answer_stored" in m for m in messages)
    assert any("event=interview_completed" in m and "animal=" in m for m in messages)
    assert any("event=interview_reset" in m for m in messages)
