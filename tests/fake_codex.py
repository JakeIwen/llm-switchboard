"""A stand-in for Codex's app-server daemon: JSON-RPC over WebSocket on a Unix
socket, with threads kept in memory. Used by the unit tests (in a thread) and
by the browser test (as a script: fake_codex.py SOCKET SEED.json).

It implements just the calls the picker makes, with the behavior observed
from the real daemon: a started thread is listed only once it has a turn,
archived threads are listed only with archived=true, and a thread a fork's
history points at can't be deleted.
"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import uuid

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def thread(tid, cwd, name="", preview="hello", status="notLoaded", **extra):
    now = int(time.time())
    return {"id": tid, "cwd": cwd, "name": name or None, "preview": preview, "status": {"type": status},
            "parentThreadId": None, "ephemeral": False, "forkedFromId": None, "path": None,
            "recencyAt": now, "updatedAt": now, "archived": False, "persisted": True, "turns": [], **extra}


class FakeCodexDaemon:
    def __init__(self, path, threads=()):
        self.path = str(path)
        self.threads = {t["id"]: t for t in threads}
        self.calls = []
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.sock.bind(self.path)
        self.sock.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.sock.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
            head, buf = buf.split(b"\r\n\r\n", 1)
            key = next(line.split(b":", 1)[1].strip() for line in head.split(b"\r\n")
                       if line.lower().startswith(b"sec-websocket-key"))
            accept = base64.b64encode(hashlib.sha1(key + _GUID).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            reader = _Reader(conn, buf)
            while True:
                msg = json.loads(reader.frame())
                if "id" not in msg:
                    continue
                try:
                    reply = {"jsonrpc": "2.0", "id": msg["id"], "result": self.handle(msg["method"], msg.get("params") or {})}
                except _RpcError as e:
                    reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32600, "message": str(e)}}
                # A notification first, as the real daemon interleaves them.
                _send(conn, {"jsonrpc": "2.0", "method": "fake/notice", "params": {}})
                _send(conn, reply)
        except (OSError, ValueError, StopIteration):
            pass
        finally:
            conn.close()

    def handle(self, method, p):
        with self.lock:
            self.calls.append((method, p))
            if method == "initialize":
                return {}
            if method == "thread/list":
                rows = [t for t in self.threads.values() if t["persisted"] and t["archived"] == bool(p.get("archived"))]
                return {"data": [{k: v for k, v in t.items() if k not in ("archived", "persisted")} for t in rows],
                        "nextCursor": None}
            if method == "thread/start":
                tid = str(uuid.uuid4())
                self.threads[tid] = thread(tid, p["cwd"], preview="", status="idle", persisted=False)
                return {"thread": self.threads[tid]}
            if method == "turn/start":
                t = self._get(p["threadId"])
                text = p["input"][0]["text"]
                t.update(persisted=True, preview=t["preview"] or text)
                t["status"] = {"type": "active"}
                t["turns"].append({"user": text, "agent": "On it."})
                return {"turn": {"id": str(uuid.uuid4()), "status": "inProgress"}}
            if method == "thread/name/set":
                self._get(p["threadId"])["name"] = p["name"]
                return {}
            if method == "thread/fork":
                src = self._get(p["threadId"])
                tid = str(uuid.uuid4())
                self.threads[tid] = thread(tid, p.get("cwd") or src["cwd"], name=src["name"], preview=src["preview"],
                                           forkedFromId=src["id"], turns=list(src["turns"]))
                return {"thread": self.threads[tid]}
            if method == "thread/archive":
                self._get(p["threadId"])["archived"] = True
                return {}
            if method == "thread/unarchive":
                t = self._get(p["threadId"], archived=True)
                t["archived"] = False
                return {"thread": t}
            if method == "thread/delete":
                t = self._get(p["threadId"], archived=None)
                if any(o["forkedFromId"] == t["id"] for o in self.threads.values()):
                    raise _RpcError(f"cannot delete thread {t['id']}: forked history still references it")
                del self.threads[t["id"]]
                return {}
            if method == "thread/unsubscribe":
                return {"status": "unsubscribed"}
            if method == "thread/turns/list":
                t = self._get(p["threadId"])
                turns = [{"startedAt": 1, "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": x["user"]}]},
                    {"type": "agentMessage", "text": x["agent"]},
                ]} for x in reversed(t["turns"])]
                return {"data": turns, "nextCursor": None}
            raise _RpcError(f"unknown method {method}")

    def _get(self, tid, archived=False):
        t = self.threads.get(tid)
        if t is None or (archived is not None and t["archived"] != archived):
            raise _RpcError(f"no rollout found for thread id {tid}")
        return t


class _RpcError(Exception):
    pass


class _Reader:
    def __init__(self, conn, buf):
        self.conn, self.buf = conn, buf

    def take(self, n):
        while len(self.buf) < n:
            data = self.conn.recv(65536)
            if not data:
                raise ValueError("closed")
            self.buf += data
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frame(self):
        b1, b2 = self.take(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self.take(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.take(8))[0]
        mask = self.take(4)
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self.take(n)))
        if b1 & 0x0F == 0x8:
            raise ValueError("closed")
        return payload


def _send(conn, obj):
    data = json.dumps(obj).encode()
    n = len(data)
    head = struct.pack(">BB", 0x81, n) if n < 126 else (
        struct.pack(">BBH", 0x81, 126, n) if n < 65536 else struct.pack(">BBQ", 0x81, 127, n))
    conn.sendall(head + data)


if __name__ == "__main__":
    seed = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else []
    FakeCodexDaemon(sys.argv[1], [thread(**t) for t in seed])
    threading.Event().wait()
