#!/usr/bin/env python3
"""Minimal RCON client (Source RCON protocol). Stdlib only."""
import socket
import struct
import sys


class Rcon:
    def __init__(self, host, port, password, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._req_id = 0
        self._auth(password)

    def _send(self, type_, body):
        self._req_id += 1
        payload = struct.pack('<ii', self._req_id, type_) + body.encode('utf-8') + b'\x00\x00'
        self.sock.sendall(struct.pack('<i', len(payload)) + payload)
        return self._req_id

    def _recv(self):
        raw = self._recvn(4)
        (length,) = struct.unpack('<i', raw)
        data = self._recvn(length)
        req_id, type_ = struct.unpack('<ii', data[:8])
        body = data[8:-2].decode('utf-8', 'replace')
        return req_id, type_, body

    def _recvn(self, n):
        buf = b''
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('rcon eof')
            buf += chunk
        return buf

    def _auth(self, password):
        self._send(3, password)
        req_id, type_, body = self._recv()
        if req_id == -1:
            raise ConnectionError('rcon auth failed')

    def cmd(self, command):
        self._send(2, command)
        _, _, body = self._recv()
        return body

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


if __name__ == '__main__':
    host = sys.argv[1] if len(sys.argv) > 1 else '127.0.0.1'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 25598
    pw = sys.argv[3] if len(sys.argv) > 3 else 'minextest'
    r = Rcon(host, port, pw)
    for cmd in sys.argv[4:]:
        print(f'> {cmd}')
        print(r.cmd(cmd))
    r.close()
