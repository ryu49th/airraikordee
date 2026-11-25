from ai.feedback import Feedback, FeedbackLog
from common.protocol import TOPIC_GW_CAUSE
from conftest import FakeMqtt


class FakeFirebase(object):
    def __init__(self, value):
        self.value = value
        self.status = 200

    def get(self, path):
        assert path == "feedback"
        return self.status, self.value


def test_feedback_lands_in_the_accuracy_table(tmp_path):
    db = FeedbackLog(str(tmp_path / "cause_log.db"))
    fb = FakeFirebase({
        "-a": {"cause_ts": 1794383000, "correct": True},
        "-b": {"cause_ts": 1794383600, "correct": False, "true_cause": "smoking"},
        "-c": {"correct": True},                       # no cause_ts: skipped
    })
    f = Feedback({}, FakeMqtt(), fb, db, clock=lambda: 1794390000.0)
    f.on_cause(TOPIC_GW_CAUSE, {"ts": 1794383000, "id": "c1794383000", "cause": "cooking", "confidence": 0.9,
                                "source": "rules"})
    f.on_cause(TOPIC_GW_CAUSE, {"ts": 1794383600, "id": "c1794383600", "cause": "incense_candle",
                                "confidence": 0.6, "source": "cloud"})
    assert f.step() == 2
    assert f.step() == 0                                 # same keys again: nothing new
    assert db.accuracy() == {"rules": (1, 1), "cloud": (1, 0), "all": (2, 1)}
    assert db.mistakes() == [("incense_candle", "smoking", 1)]


def test_firebase_down_stores_nothing(tmp_path):
    fb = FakeFirebase(None)
    fb.status = None
    f = Feedback({}, FakeMqtt(), fb, FeedbackLog(str(tmp_path / "x.db")))
    assert f.step() == 0


def test_feedback_without_a_known_cause_counts_as_unknown(tmp_path):
    db = FeedbackLog(str(tmp_path / "x.db"))
    Feedback({}, FakeMqtt(), FakeFirebase({"-z": {"cause_ts": 5, "correct": False}}), db).step()
    assert db.accuracy() == {"unknown": (1, 0), "all": (1, 0)}
