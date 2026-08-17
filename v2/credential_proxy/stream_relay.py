"""Bounded single-owner duplex relay for plain and TLS sockets."""
from __future__ import annotations

import selectors
import socket
import ssl
import threading


MAX_PENDING = 1024 * 1024


def duplex_stream(
    left: socket.socket,
    right: socket.socket,
    stopping: threading.Event,
) -> None:
    selector = selectors.DefaultSelector()
    peers = {left: right, right: left}
    pending = {left: bytearray(), right: bytearray()}
    readable = {left: True, right: True}
    write_closed = {left: False, right: False}
    receive_wants_write = {left: False, right: False}
    send_wants_read = {left: False, right: False}

    def retire(connection: socket.socket) -> None:
        # A receive reset closes the endpoint. Preserve bytes already queued
        # toward its peer, but discard bytes destined for the dead endpoint.
        readable[connection] = False
        readable[peers[connection]] = False
        pending[connection].clear()
        write_closed[connection] = True
        receive_wants_write[connection] = False
        receive_wants_write[peers[connection]] = False
        send_wants_read[connection] = False

    def retire_write(connection: socket.socket) -> None:
        # A broken write can still leave the reverse direction usable. Stop
        # reading its peer, whose bytes are no longer deliverable, without
        # discarding bytes already traveling in the reverse direction.
        readable[peers[connection]] = False
        pending[connection].clear()
        write_closed[connection] = True
        receive_wants_write[peers[connection]] = False
        send_wants_read[connection] = False

    def failed_write(connection: socket.socket) -> None:
        # TLS does not expose a safe independent write half after a transport
        # failure; retaining it can leave a relay permanently half-open.
        if isinstance(connection, ssl.SSLSocket):
            retire(connection)
        else:
            retire_write(connection)

    def refresh(connection: socket.socket) -> None:
        if connection.fileno() < 0:
            return
        events = 0
        if ((readable[connection] and len(pending[peers[connection]]) < MAX_PENDING)
                or send_wants_read[connection]):
            events |= selectors.EVENT_READ
        if pending[connection] or receive_wants_write[connection]:
            events |= selectors.EVENT_WRITE
        try:
            if events:
                selector.modify(connection, events)
            else:
                selector.unregister(connection)
        except KeyError:
            if events:
                selector.register(connection, events)
        except ValueError:
            pass

    def receive(connection: socket.socket) -> None:
        peer = peers[connection]
        capacity = MAX_PENDING - len(pending[peer])
        if not readable[connection] or capacity <= 0:
            return
        try:
            data = connection.recv(min(64 * 1024, capacity))
        except ssl.SSLWantWriteError:
            receive_wants_write[connection] = True
            return
        except ssl.SSLWantReadError:
            receive_wants_write[connection] = False
            return
        except BlockingIOError:
            return
        except ConnectionError:
            retire(connection)
            return
        receive_wants_write[connection] = False
        if data:
            pending[peer].extend(data)
        elif data == b"":
            readable[connection] = False

    def send(connection: socket.socket) -> None:
        if not pending[connection]:
            send_wants_read[connection] = False
            return
        try:
            sent = connection.send(pending[connection])
        except ssl.SSLWantReadError:
            send_wants_read[connection] = True
            return
        except ssl.SSLWantWriteError:
            send_wants_read[connection] = False
            return
        except BlockingIOError:
            return
        except ConnectionError:
            failed_write(connection)
            return
        send_wants_read[connection] = False
        if sent == 0:
            failed_write(connection)
            return
        del pending[connection][:sent]

    try:
        # One event loop owns both directions. Concurrent operations on the
        # same SSLSocket are not a safe transport primitive.
        left.setblocking(False)
        right.setblocking(False)
        refresh(left)
        refresh(right)
        while not stopping.is_set():
            if not any(readable.values()) and not any(pending.values()):
                return
            ready = list(selector.select(0.5))
            for connection in (left, right):
                if (isinstance(connection, ssl.SSLSocket)
                        and readable[connection] and connection.pending()):
                    try:
                        key = selector.get_key(connection)
                    except KeyError:
                        continue
                    if all(candidate.fileobj is not connection for candidate, _mask in ready):
                        ready.append((key, selectors.EVENT_READ))
            for key, mask in ready:
                connection = key.fileobj
                retried_send = bool(mask & selectors.EVENT_READ and send_wants_read[connection])
                retried_receive = bool(mask & selectors.EVENT_WRITE and receive_wants_write[connection])
                if retried_send:
                    send(connection)
                if retried_receive:
                    receive(connection)
                if mask & selectors.EVENT_READ and not retried_send:
                    receive(connection)
                if mask & selectors.EVENT_WRITE and not retried_receive:
                    send(connection)
            for connection in (left, right):
                peer = peers[connection]
                if (not readable[peer] and not pending[connection]
                        and not write_closed[connection]):
                    try:
                        connection.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    write_closed[connection] = True
            for connection in (left, right):
                refresh(connection)
    finally:
        selector.close()
