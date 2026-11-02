#!/usr/bin/env python3
"""Status ping to determine protocol version. Stdlib only.

Handshake (state -> 1) fields, in this exact order:
    packet id (0x00) | protocol version (varint) | server address (string)
    | server port (u16 BE) | next state (varint, 1 = status)

The earlier version of this script omitted the protocol-version varint, so the
address length was read as the protocol version and the server dropped the
connection ("eof reading length").
"""
import json
import socket
import struct
import sys

# 26.3 is protocol 777; 26.1.2 was 767. Only used to let the server pick a
# status response shape, so any known value works for a status ping.
DEFAULT_PROTOCOL = 777


def write_varint(n):
    out = b''
    n &= 0xFFFFFFFF
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out


def read_varint(sock):
    n = 0
    shift = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise SystemExit('eof reading varint')
        v = b[0]
        n |= (v & 0x7F) << shift
        shift += 7
        if not (v & 0x80):
            return n


def main():
    host = sys.argv[1]
    port = int(sys.argv[2])
    protocol = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_PROTOCOL

    s = socket.create_connection((host, port), timeout=15)
    s.settimeout(15)

    addr = host.encode()
    hs = (write_varint(0x00)            # packet id
          + write_varint(protocol)      # protocol version  <-- was missing
          + write_varint(len(addr)) + addr
          + struct.pack('>H', port)
          + write_varint(1))            # next state: status
    s.sendall(write_varint(len(hs)) + hs)

    # status request: length 1, packet id 0
    s.sendall(write_varint(1) + write_varint(0x00))

    n = read_varint(s)
    data = b''
    while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    s.close()

    # packet id varint, then the JSON string length varint
    i = 0
    while data[i] & 0x80:
        i += 1
    i += 1
    slen = 0
    sh = 0
    while True:
        v = data[i]
        i += 1
        slen |= (v & 0x7F) << sh
        sh += 7
        if not (v & 0x80):
            break
    payload = data[i:i + slen]
    print(json.dumps(json.loads(payload.decode('utf-8', 'replace')), indent=2))


if __name__ == '__main__':
    main()
