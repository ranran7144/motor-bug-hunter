"""Local headless Chrome helper, reused from workspace browser checks."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import urlopen


GAME_DIR = Path(__file__).resolve().parents[1]


class CDP:
    """Small local-only RFC 6455 client for Chrome DevTools Protocol."""

    def __init__(self, address: str):
        url = urlsplit(address)
        if url.scheme != "ws" or url.hostname != "127.0.0.1":
            raise ValueError("The browser debugger must use loopback WebSocket")
        self.socket = socket.create_connection((url.hostname, url.port), 20)
        self.socket.settimeout(20)
        self.buffer = bytearray()
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {url.path} HTTP/1.1\r\nHost: {url.netloc}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(self.socket.recv(4096))
        headers, remainder = response.split(b"\r\n\r\n", 1)
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest())
        if not headers.startswith(b"HTTP/1.1 101") or expected.lower() not in headers.lower():
            raise RuntimeError("Chrome WebSocket handshake failed")
        self.buffer.extend(remainder)
        self.next_id = 0
        self.js_errors = []

    def _read(self, count):
        while len(self.buffer) < count:
            part = self.socket.recv(max(4096, count - len(self.buffer)))
            if not part:
                raise ConnectionError("Browser debugger closed")
            self.buffer.extend(part)
        part = bytes(self.buffer[:count])
        del self.buffer[:count]
        return part

    def _send(self, payload: bytes, opcode=1):
        length = len(payload)
        header = bytes((0x80 | opcode, 0x80 | min(length, 126)))
        if length >= 65536:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", length)
        elif length >= 126:
            header += struct.pack("!H", length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def _receive(self):
        pieces = []
        while True:
            first, second = self._read(2)
            opcode, length = first & 15, second & 127
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if second & 0x80 else None
            payload = self._read(length)
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 8:
                raise ConnectionError("Browser closed its WebSocket")
            if opcode == 9:
                self._send(payload, 10)
                continue
            if opcode == 10:
                continue
            pieces.append(payload)
            if first & 0x80:
                return json.loads(b"".join(pieces))

    def call(self, method, **params):
        self.next_id += 1
        request_id = self.next_id
        self._send(json.dumps({"id": request_id, "method": method, "params": params}).encode())
        while True:
            message = self._receive()
            if message.get("method") == "Runtime.exceptionThrown":
                self.js_errors.append(message["params"]["exceptionDetails"])
            if message.get("method") == "Runtime.consoleAPICalled" and message["params"]["type"] == "error":
                self.js_errors.append(message["params"]["args"])
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True,
                           awaitPromise=True, userGesture=True)
        if "exceptionDetails" in result:
            raise AssertionError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def wait_for(self, expression, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.evaluate(expression)
            if result:
                return result
            time.sleep(0.08)
        raise AssertionError(f"Browser condition timed out: {expression}")

    def click(self, selector):
        point = self.evaluate("(() => {const e=document.querySelector(" + json.dumps(selector) +
                              "); e.scrollIntoView({block:'center'}); const r=e.getBoundingClientRect();"
                              "return {x:r.x+r.width/2,y:r.y+r.height/2,disabled:e.disabled};})()")
        assert not point.get("disabled", False), f"Disabled button: {selector}"
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=kind, x=point["x"], y=point["y"],
                      button="left", clickCount=1)

    def key(self, key, pressed=True):
        codes = {"ArrowDown": 40, "Enter": 13, "p": 80}
        text = {"text": "\r", "unmodifiedText": "\r"} if key == "Enter" and pressed else {}
        self.call("Input.dispatchKeyEvent", type="keyDown" if pressed else "keyUp",
                  key=key, code=key if key != "p" else "KeyP",
                  windowsVirtualKeyCode=codes[key], nativeVirtualKeyCode=codes[key], **text)

    def screenshot(self, path):
        self.evaluate("window.scrollTo(0,0)")
        data = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        path.write_bytes(base64.b64decode(data["data"]))

    def close(self):
        self.socket.close()


def find_browser():
    candidates = [
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Install Chrome/Edge, or set CHROME_PATH to its executable")


