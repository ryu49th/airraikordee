import pytest

from common.protocol import UINT32, ProtocolError, SeqCounter, SeqTracker, clean_json, decode, encode


def test_encode_decode_round_trip_keeps_thai():
    msg = {"ts": 1.5, "advice_th": "เปิดหน้าต่าง", "pm25": 12}
    text = encode(msg)
    assert "เปิดหน้าต่าง" in text
    assert decode(text.encode("utf-8")) == msg


@pytest.mark.parametrize("bad", [b"not json", b"[1, 2]", b"42", b""])
def test_decode_rejects_non_objects(bad):
    with pytest.raises(ProtocolError):
        decode(bad)


def test_clean_json_replaces_nan_and_inf():
    msg = {"pm25": float("nan"), "list": [float("inf"), 1.0], "ok": 3}
    assert clean_json(msg) == {"pm25": None, "list": [None, 1.0], "ok": 3}
    encode(clean_json(msg))  # must not raise


def test_seq_counter_wraps_uint32():
    c = SeqCounter(start=UINT32 - 2)
    assert [c.next(), c.next(), c.next()] == [UINT32 - 1, 0, 1]


def test_seq_tracker_counts_gaps_ignores_reboot_and_duplicates():
    t = SeqTracker()
    assert t.update("node1", 10) == 0
    assert t.update("node1", 11) == 0
    assert t.update("node1", 14) == 2   # 12, 13 lost
    assert t.update("node1", 14) == 0   # duplicate
    assert t.update("node1", 0) == 0    # node rebooted
    assert t.update("node1", 1) == 0
    assert t.update("node1", UINT32 - 1) == 0
    assert t.lost["node1"] == 2


def test_seq_tracker_handles_wrap_around():
    t = SeqTracker()
    t.update("n", UINT32 - 1)
    assert t.update("n", 1) == 1        # seq 0 lost across the wrap
