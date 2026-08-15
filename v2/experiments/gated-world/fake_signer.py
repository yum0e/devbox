#!/usr/bin/env python3
"""A native socket-shaped affordance holding a secret unavailable to callers."""

import hashlib
import hmac
import os
import socket


MAX_REQUEST = 4096


if __name__ == "__main__":
    descriptor = int(os.environ["SIGNER_LINK_FD"])
    secret = bytes.fromhex(os.environ["SIGNER_SECRET"])
    os.environ.clear()
    with socket.socket(fileno=descriptor) as link:
        print("signer World received one private Link", flush=True)
        while True:
            request = link.recv(MAX_REQUEST + 1)
            if not request:
                break
            if len(request) > MAX_REQUEST or not request.startswith(b"sign\0"):
                link.send(b"error\0invalid request")
                continue
            signature = hmac.new(secret, request[5:], hashlib.sha256).hexdigest().encode()
            link.send(b"ok\0" + signature)
