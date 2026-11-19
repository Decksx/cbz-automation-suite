"""Wire DTOs preserve byte identities without serializing private dictionaries."""
from copy import deepcopy
import json

import pytest

from comic_automation.web.jobs import stream_frames as frames


@pytest.fixture
def payload():
    row = dict(job_id="0" + "A" * 25, nonce="b" * 32, admission_seq=2**53 + 1,
               state_version=2**53 + 2, tool_id="fixture", kind="task", readonly=1,
               dry_run=1, state="running", admitted_ms=1000, updated_ms=1001,
               exit_code=None, outcome=None, command="PRIVATE-COMMAND")
    page = dict(cursor="0", next_cursor="5", proven_offset="5", eof=False, log_status="live",
                records=[dict(type="line", **{"from": "0", "to": "5"}, text="a\nid:",
                              continued=False, fragment=False, secret="PRIVATE-RECORD")],
                secret="PRIVATE-PAGE")
    obs = dict(generation=2**53 + 3, cursor="5", progress=dict(done=1, total=2, percent=50,
               label="half", cursor="5", secret="PRIVATE-PROGRESS"), gap_count=0,
               last_gap=None, secret="PRIVATE-OBSERVATION")
    return page, obs, row


def decoded(packet):
    """Read the same strict JSON framing expected by the future browser parser."""
    return [json.loads(frame.split(b"data: ", 1)[1]) for frame in packet.frames]


def test_projection_is_immutable_encoded_once_and_newlines_cannot_inject_frames(payload):
    before = deepcopy(payload)
    packet = frames.encode_page(*payload)
    wire = b"".join(packet.frames)
    assert b"PRIVATE" not in wire and payload == before
    assert isinstance(packet.frames, tuple) and all(type(frame) is bytes for frame in packet.frames)
    assert wire.count(b"\nid: ") == 1 and b"\nid: 5\n" in wire
    assert wire.count(b"\n\n") == 4 and decoded(packet)[0]["text"] == "a\nid:"
    assert decoded(packet)[1]["state_version"] == str(2**53 + 2)
    assert decoded(packet)[1]["admission_seq"] == str(2**53 + 1)
    assert decoded(packet)[2]["generation"] == str(2**53 + 3)
    payload[0]["records"][0]["text"] = "changed"
    payload[1]["progress"]["label"] = "changed"
    assert b"changed" not in wire
    with pytest.raises(AttributeError):
        packet.frames = ()


def test_gap_precedes_bytes_and_only_source_frames_have_ids(payload):
    page, obs, row = payload
    gap = dict(type="gap", **{"from": "0", "to": "3"}, reason="retention", secret="PRIVATE")
    page["records"].insert(0, gap)
    page["records"][1]["from"] = "3"
    obs.update(last_gap=gap, gap_count=1)
    packet = frames.encode_page(page, obs, row)
    assert packet.frames[0].startswith(b"event: gap\nid: 3\n")
    assert packet.frames[1].startswith(b"event: log\nid: 5\n")
    assert all(b"\nid:" not in frame for frame in packet.frames[2:])
    assert b"PRIVATE" not in b"".join(packet.frames)
    assert b"id:" not in frames.HEARTBEAT and frames.HEARTBEAT.endswith(b"\n\n")


@pytest.mark.parametrize("change", [
    lambda p, o, r: p.update(eof=True),
    lambda p, o, r: p.update(log_status="PRIVATE"),
    lambda p, o, r: p.update(next_cursor="6"),
    lambda p, o, r: p["records"][0].update(**{"from": "1"}),
    lambda p, o, r: p["records"][0].update(continued=1),
    lambda p, o, r: p["records"][0].update(text="x" * (65536 + 1)),
    lambda p, o, r: o.update(generation=True),
    lambda p, o, r: o["progress"].update(percent=float("nan")),
    lambda p, o, r: o["progress"].update(cursor="05"),
    lambda p, o, r: o["progress"].update(label="x" * 4097),
    lambda p, o, r: o.update(last_gap=dict(p["records"][0])),
    lambda p, o, r: r.update(exit_code=True),
    lambda p, o, r: r.update(job_id="PRIVATE"),
    lambda p, o, r: r.update(nonce="PRIVATE"),
    lambda p, o, r: r.update(admission_seq=0),
    lambda p, o, r: r.update(admitted_ms=2**53),
])
def test_malformed_projection_is_refused_without_echoing_data(payload, change):
    change(*payload)
    with pytest.raises(frames.StreamRefused, match="^INVALID_STREAM_DATA$"):
        frames.encode_page(*payload)


def test_encoded_packet_budget_includes_escaped_bytes(payload, monkeypatch):
    monkeypatch.setattr(frames, "MAX_PACKET_BYTES", 200)
    with pytest.raises(frames.StreamRefused, match="INVALID_STREAM_DATA"):
        frames.encode_page(*payload)
    assert b"PRIVATE" not in frames.error_frame("PRIVATE")
    assert b"STREAM_CLOSED" in frames.error_frame("PRIVATE")


@pytest.mark.parametrize("status", ["unobserved", "unknown_tail", "unavailable"])
def test_unknown_evidence_cannot_claim_eof(payload, status):
    page, obs, row = payload
    page.update(log_status=status, eof=False)
    assert decoded(frames.encode_page(page, obs, row))[-1]["eof"] is False
    page["eof"] = True
    with pytest.raises(frames.StreamRefused):
        frames.encode_page(page, obs, row)
