"""mitmproxy addon: append one JSON line per response to a flows file.

Runs under mitmproxy's own interpreter (see /opt/mitmvenv), NOT the API venv, so
it may import mitmproxy freely. The API reads the JSONL file it produces.

The output path comes from the MITM_FLOWS_FILE environment variable.
"""

import json
import os
import time

FLOWS_FILE = os.environ.get("MITM_FLOWS_FILE", "/home/androiduser/mitm/flows.jsonl")
MAX_BODY = int(os.environ.get("MITM_MAX_BODY", "8192"))


def _body(message) -> dict:
    raw = message.raw_content or b""
    truncated = len(raw) > MAX_BODY
    text = raw[:MAX_BODY].decode("utf-8", "replace")
    return {"size": len(raw), "truncated": truncated, "text": text}


def response(flow) -> None:  # noqa: ANN001 - mitmproxy passes an HTTPFlow
    record = {
        "ts": time.time(),
        "method": flow.request.method,
        "scheme": flow.request.scheme,
        "host": flow.request.host,
        "port": flow.request.port,
        "path": flow.request.path,
        "url": flow.request.pretty_url,
        "status": flow.response.status_code,
        "req_headers": dict(flow.request.headers),
        "res_headers": dict(flow.response.headers),
        "req_body": _body(flow.request),
        "res_body": _body(flow.response),
        "content_type": flow.response.headers.get("content-type", ""),
    }
    try:
        with open(FLOWS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
