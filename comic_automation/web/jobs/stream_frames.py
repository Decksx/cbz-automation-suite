"""Bounded SSE bytes from explicit DTOs; no raw ledger/page dictionary escapes."""
from __future__ import annotations

import json
import math
from typing import NamedTuple

from . import logs, views
from .store import JobRefused, _NONCE, validate_job_id

MAX_PACKET_BYTES = 512 * 1024
HEARTBEAT = b": heartbeat\n\n"


class StreamRefused(JobRefused):
    """A stream input, capacity or source could not be safely accepted."""


class EncodedPage(NamedTuple):
    """Deeply immutable packet, shared without re-encoding between subscribers."""
    cursor: str
    next_cursor: str
    frames: tuple[bytes, ...]
    signature: tuple


def _number(value, upper=2**63 - 1):
    if type(value) is not int or not 0 <= value <= upper:
        raise ValueError("Invalid stream number")
    return value


def _frame(event, data, cursor=None):
    encoded = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    identity = b"" if cursor is None else b"id: " + logs.encode_cursor(logs.decode_cursor(cursor)).encode() + b"\n"
    return b"event: " + event.encode("ascii") + b"\n" + identity + b"data: " + encoded + b"\n\n"


def _record(raw):
    start, end = logs.decode_cursor(raw["from"]), logs.decode_cursor(raw["to"])
    if end <= start:
        raise ValueError("Empty source record")
    result = {"type": raw["type"], "from": str(start), "to": str(end)}
    if raw["type"] == "gap":
        if raw["reason"] not in {"retention", "dropped", "drain_timeout"}:
            raise ValueError("Invalid gap")
        result["reason"] = raw["reason"]
    elif raw["type"] == "line":
        if (type(raw["text"]) is not str or len(raw["text"]) > logs.CHUNK_BYTES
                or type(raw["continued"]) is not bool or type(raw["fragment"]) is not bool):
            raise ValueError("Invalid line")
        result.update(text=raw["text"], continued=raw["continued"], fragment=raw["fragment"])
    else:
        raise ValueError("Invalid record kind")
    return result


def encode_page(page: dict, observation: dict, row: dict) -> EncodedPage:
    """Project bounded DTOs and encode once, preserving source-byte event IDs.

    Only log/gap frames have IDs. State/progress identities stay in JSON and
    never change the source cursor. Unknown fields are never serialized.
    """
    try:
        start, end, proven = (logs.decode_cursor(page[key]) for key in ("cursor", "next_cursor", "proven_offset"))
        if (not start <= end <= proven or type(page["eof"]) is not bool
                or page["log_status"] not in {"complete", "live", "unknown_tail", "unobserved", "unavailable"}
                or page["eof"] and (page["log_status"] != "complete" or end != proven)
                or type(page["records"]) is not list or len(page["records"]) > 1000):
            raise ValueError("Invalid page")
        frames, used = [], 0
        def emit(event, data, cursor=None):
            nonlocal used
            frame = _frame(event, data, cursor)
            used += len(frame)
            if used > MAX_PACKET_BYTES:
                raise ValueError("Stream packet budget exceeded")
            frames.append(frame)
        position = start
        for raw in page["records"]:
            record = _record(raw)
            if int(record["from"]) != position or int(record["to"]) > end:
                raise ValueError("Noncontiguous page")
            position = int(record["to"])
            emit("gap" if record["type"] == "gap" else "log", record, record["to"])
        if position != end:
            raise ValueError("Unaccounted page bytes")
        validate_job_id(row["job_id"])
        if not isinstance(row["nonce"], str) or not _NONCE.fullmatch(row["nonce"]):
            raise ValueError("Invalid stream nonce")
        if _number(row["admission_seq"]) == 0:
            raise ValueError("Invalid admission sequence")
        state = views._summary(row)
        state["state_version"] = str(_number(row["state_version"]))
        for key in ("admitted_ms", "updated_ms"):
            _number(state[key], 2**53 - 1)
        if state["exit_code"] is not None and (type(state["exit_code"]) is not int
                or not -(2**31) <= state["exit_code"] < 2**32):
            raise ValueError("Invalid exit code")
        emit("state", state)
        generation = _number(observation["generation"])
        progress = observation["progress"]
        if progress is not None:
            done, total = _number(progress["done"], 10**12), _number(progress["total"], 10**12)
            if (not 0 <= done <= total or total == 0 or type(progress["percent"]) not in (int, float)
                    or not math.isfinite(progress["percent"]) or not 0 <= progress["percent"] <= 100
                    or type(progress["label"]) is not str or len(progress["label"].encode("utf-8")) > 4096):
                raise ValueError("Invalid progress")
            progress = {key: progress[key] for key in ("done", "total", "percent", "label", "cursor")}
            logs.decode_cursor(progress["cursor"])
        gap = observation["last_gap"]
        if gap is not None:
            gap = _record(gap)
            if gap["type"] != "gap":
                raise ValueError("Invalid progress gap")
        emit("progress", {"generation": str(generation), "cursor": str(logs.decode_cursor(observation["cursor"])),
                          "progress": progress, "gap_count": str(_number(observation["gap_count"])), "last_gap": gap})
        emit("log_status", {"proven_offset": str(proven), "eof": page["eof"], "log_status": page["log_status"]})
        signature = (str(end), str(proven), page["log_status"], page["eof"], row["state_version"], generation)
        return EncodedPage(str(start), str(end), tuple(frames), signature)
    except (KeyError, TypeError, ValueError, OverflowError, JobRefused, logs.LogRefused):
        raise StreamRefused("INVALID_STREAM_DATA") from None


def error_frame(reason: str) -> bytes:
    """Use only fixed public errors; exceptions and private paths stay private."""
    allowed = {"LOG_UNAVAILABLE", "SLOW_SUBSCRIBER", "INVALID_LOG_CURSOR", "READER_START_FAILED"}
    return _frame("error", {"code": reason if reason in allowed else "STREAM_CLOSED"})
