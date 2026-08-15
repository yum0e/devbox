#!/usr/bin/env python3
"""Project one message-oriented affordance as a local Unix stream socket."""

import os
import socket
import socketserver
import threading


MAX_MESSAGE = 4096
UPSTREAM = socket.socket(fileno=int(os.environ["AFFORDANCE_LINK_FD"]))
LOCK = threading.Lock()


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        chunks = bytearray()
        while len(chunks) <= MAX_MESSAGE:
            block = self.request.recv(min(1024, MAX_MESSAGE + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if not chunks or len(chunks) > MAX_MESSAGE:
            return
        with LOCK:
            UPSTREAM.send(bytes(chunks))
            response = UPSTREAM.recv(MAX_MESSAGE + 1)
        if response and len(response) <= MAX_MESSAGE:
            self.request.sendall(response)


class Server(socketserver.UnixStreamServer):
    pass


if __name__ == "__main__":
    path = os.environ["PROJECTION_SOCKET"]
    with Server(path, Handler) as server:
        os.chmod(path, 0o600)
        print("native signer affordance projected as a Unix socket", flush=True)
        server.serve_forever()
