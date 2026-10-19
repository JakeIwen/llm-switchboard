"""Bounded full-duplex subprocess I/O for the untrusted SSH reply."""

import os
import selectors
import subprocess
import time

from .host_protocol import MAX_METADATA_BYTES


def bounded_ssh(argv, *, input: str, text=True, capture_output=True, timeout=25, check=True):
    """Like run(..., capture_output=True), but stop reading at the protocol cap."""
    payload = input.encode()
    buffers = {"out": bytearray(), "err": bytearray()}
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            with selectors.DefaultSelector() as selector:
                for stream, name, events in ((process.stdin, "in", selectors.EVENT_WRITE),
                                             (process.stdout, "out", selectors.EVENT_READ),
                                             (process.stderr, "err", selectors.EVENT_READ)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, events, name)
                _transfer(selector, payload, buffers, deadline)
            process.wait(timeout=max(.001, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            process.wait()
            raise
        stdout, stderr = buffers["out"].decode(), buffers["err"].decode(errors="replace")
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, argv, stdout, stderr)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _transfer(selector, payload, buffers, deadline):
    sent = 0
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("SSH exchange timed out")
        for key, _ in selector.select(min(remaining, 1)):
            stream, name = key.fileobj, key.data
            if name == "in":
                try:
                    sent += os.write(stream.fileno(), payload[sent:sent + 65536])
                except BrokenPipeError:
                    sent = len(payload)
                if sent == len(payload):
                    selector.unregister(stream)
                    stream.close()
                continue
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                selector.unregister(stream)
                continue
            buffers[name].extend(chunk)
            limit = MAX_METADATA_BYTES if name == "out" else 65536
            if len(buffers[name]) > limit:
                raise ValueError("SSH reply exceeds the output limit")
