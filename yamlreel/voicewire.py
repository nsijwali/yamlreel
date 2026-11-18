"""What the voice server sends back, checked before anything is read from it.

The server is a separate program (VoiceStudio, or anything speaking the same MCP JSON-RPC). Every reply shape it
could send by mistake (no `result`, a wrong id, a list where an object belongs, text that is not JSON, audio that is
not base64) becomes a VoiceError that says what was wrong, instead of a KeyError or TypeError from deep inside.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

MAX_SHOWN = 300  # characters of server-supplied text quoted in an error


class VoiceError(RuntimeError):
    pass


def _one_line(text: object) -> str:
    return " ".join(str(text).split())[:MAX_SHOWN]


def parse_json(text: str, method: str) -> Any:
    """`text` as JSON; anything that does not parse is a VoiceError naming the call."""
    try:
        return json.loads(text)
    except ValueError as e:   # json.JSONDecodeError, or a number too large to hold
        raise VoiceError(f"voice server reply to {method} is not valid JSON ({e})") from e


def rpc_result(msg: Any, rid: int, method: str) -> dict[str, Any]:
    """The `result` object of a JSON-RPC reply to request `rid`, after checking the envelope."""
    if msg is None:
        raise VoiceError(f"voice server error on {method}: empty reply")
    if not isinstance(msg, dict):
        raise VoiceError(f"voice server reply to {method} is not a JSON-RPC object")
    if "error" in msg:
        err = msg["error"]
        text = err.get("message", err) if isinstance(err, dict) else err
        raise VoiceError(f"voice server error on {method}: {_one_line(text)}")
    if msg.get("id") != rid:
        raise VoiceError(f"voice server reply to {method} has id {_one_line(repr(msg.get('id')))}, expected {rid}")
    if "result" not in msg:
        raise VoiceError(f"voice server reply to {method} has neither a result nor an error")
    result = msg["result"]
    if not isinstance(result, dict):
        raise VoiceError(f"voice server reply to {method} has a result that is not an object")
    return result


def find_wav_b64(result: dict[str, Any]) -> str | None:
    """The tool reply carries `wav_base64` as JSON text (or as an audio/resource block)."""
    content = result.get("content", [])
    if not isinstance(content, list):
        raise VoiceError("voice server reply has a `content` that is not a list")
    for block in content:
        if not isinstance(block, dict):
            raise VoiceError("voice server reply has a content block that is not an object")
        if block.get("type") == "audio" and block.get("data"):
            return block["data"]
        text = block.get("text")
        if isinstance(text, str):
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if isinstance(data, dict):
                if data.get("error"):
                    raise VoiceError(f"voice server: {_one_line(data['error'])}")
                if data.get("wav_base64"):
                    return data["wav_base64"]
    return None


def decode_audio(b64: Any) -> bytes:
    """The audio bytes of a base64 field; anything else is a VoiceError."""
    if not isinstance(b64, str):
        raise VoiceError("voice server reply has audio that is not base64 text")
    try:
        return base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError) as e:
        raise VoiceError(f"voice server reply has audio that is not valid base64 ({e})") from e
