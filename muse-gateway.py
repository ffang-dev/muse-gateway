#!/usr/bin/env python3
"""Thin HTTP gateway in front of muse-cli.

Why this shape:
- muse-cli has one Noise-XX websocket per invocation and its protocol notes say
  two concurrent frame readers corrupt the decrypt state (fatal BAD_DECRYPT),
  so every call is serialised through a single worker thread.
- The agent is an agent, not a chat model: a reply can take minutes, and
  muse-cli's own ceiling is 300s. A synchronous web request would hit the
  100s Cloudflare read timeout, so submissions return a job id and the caller
  polls.
- muse-cli is invoked as a subprocess rather than imported: the CLI owns auth,
  VM discovery and reconnect, and a fresh process per job cannot leak a stale
  tunnel across the 8h sandbox restart.
"""
import hashlib
import hmac
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.environ.get("MUSE_HOME", os.path.expanduser("~"))
BIN = os.environ.get("MUSE_CLI", os.path.join(HOME, "muse-venv", "bin", "muse-cli"))
TOKEN = os.environ.get("MUSE_GATEWAY_TOKEN", "")
PORT = int(os.environ.get("PORT", "8081"))
DEFAULT_WAIT = int(os.environ.get("MUSE_DEFAULT_WAIT", "180"))
MAX_WAIT = 290
STATE_FILE = os.path.join(HOME, ".cache", "muse-gateway-jobs.json")
MAX_JOBS = 200
TEXT_LIMIT = 8000

env = dict(os.environ)
env["HOME"] = HOME
env["MUSE_NO_UPDATE_CHECK"] = "1"

jobs = OrderedDict()
lock = threading.Lock()
JOB_QUEUE = queue.Queue()


def load_state():
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        for j in data.get("jobs", []):
            if j.get("state") in ("queued", "running"):
                j.update(state="failed", error="interrupted by gateway restart")
            jobs[j["id"]] = j
    except (OSError, ValueError, KeyError):
        pass


def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as fh:
            json.dump({"jobs": list(jobs.values())[-MAX_JOBS:]}, fh)
    except OSError:
        pass


def run_job(job_id):
    with lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["state"] = "running"
        job["started"] = time.time()
    text = job["text"]
    wait = min(int(job.get("wait", DEFAULT_WAIT)), MAX_WAIT)
    cmd = [BIN, "send", text, "--wait", str(wait)]
    if job.get("thread"):
        cmd += ["--thread", job["thread"]]
    timeout = wait + 90
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        stdout = (p.stdout or "").strip()
        if p.returncode != 0 and not stdout.startswith("{"):
            raise RuntimeError((p.stderr or stdout or "muse-cli exited %d" % p.returncode)[-800:])
        data = json.loads(stdout)
        reply = data.get("reply")
        with lock:
            if reply:
                job.update(state="done", result=reply, note=data.get("note"))
            else:
                job.update(state="no_reply",
                           error=data.get("note", "agent sent nothing within %ds" % wait),
                           sent=data.get("sent"), stream=data.get("stream"))
    except subprocess.TimeoutExpired:
        with lock:
            job.update(state="timeout", error="agent did not answer within %ds" % wait)
    except (RuntimeError, ValueError, OSError) as e:
        with lock:
            job.update(state="failed", error=str(e))
    finally:
        with lock:
            job["finished"] = time.time()
        save_state()


def worker():
    while True:
        run_job(JOB_QUEUE.get())


def ok(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(code)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(body)))
    handler.send_header("cache-control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def bad(handler, msg, code=400):
    ok(handler, {"error": msg}, code)


JOB_RE = re.compile(r"^/v1/job/([0-9a-f]{12})$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

    def authorised(self):
        if not TOKEN:
            return True
        got = self.headers.get("x-muse-token", "")
        return hmac.compare_digest(hashlib.sha256(got.encode()).digest(),
                                   hashlib.sha256(TOKEN.encode()).digest())

    def do_GET(self):
        if self.path == "/v1/health":
            cookies = os.path.join(HOME, ".config", "muse-cli", "cookies.txt")
            age = None
            try:
                age = int(time.time() - os.path.getmtime(cookies))
            except OSError:
                pass
            return ok(self, {
                "ok": True,
                "muse_cli": BIN,
                "executable": os.access(BIN, os.X_OK),
                "cookies_present": age is not None,
                "cookies_age_days": round(age / 86400, 2) if age is not None else None,
                "queued": JOB_QUEUE.qsize(),
                "token_required": bool(TOKEN),
            })
        m = JOB_RE.match(self.path)
        if not m:
            return bad(self, "not found", 404)
        if not self.authorised():
            return bad(self, "unauthorised", 401)
        with lock:
            j = jobs.get(m.group(1))
        if not j:
            return bad(self, "unknown job", 404)
        return ok(self, j)

    def do_POST(self):
        if self.path != "/v1/job":
            return bad(self, "not found", 404)
        if not self.authorised():
            return bad(self, "unauthorised", 401)
        try:
            n = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return bad(self, "invalid json")
        text = (payload.get("text") or "").strip()
        if not text:
            return bad(self, "text is required")
        if len(text) > TEXT_LIMIT:
            return bad(self, "text too long (max %d chars)" % TEXT_LIMIT)
        thread = payload.get("thread")
        if thread and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(thread)):
            return bad(self, "invalid thread id")
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "state": "queued", "text": text,
               "thread": str(thread) if thread else None,
               "wait": min(int(payload.get("wait") or DEFAULT_WAIT), MAX_WAIT),
               "created": time.time()}
        with lock:
            jobs[job_id] = job
            while len(jobs) > MAX_JOBS:
                jobs.popitem(last=False)
        JOB_QUEUE.put(job_id)
        return ok(self, {"job_id": job_id, "state": "queued", "poll": "/v1/job/" + job_id}, 202)


if __name__ == "__main__":
    if not os.access(BIN, os.X_OK):
        sys.stderr.write("muse-cli not found at %s\n" % BIN)
        sys.exit(3)
    load_state()
    threading.Thread(target=worker, daemon=True).start()
    print("muse-gateway on :%d serial-worker=1 token=%s" % (PORT, "on" if TOKEN else "OFF"))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
