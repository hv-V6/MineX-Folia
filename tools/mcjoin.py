#!/usr/bin/env python3
"""Full offline-mode Minecraft client for 1.21.x/26.x that actually completes the login and
configuration phase and reaches the PLAY state, so the server registers a real, tracked entity.

This exists to test the join path end-to-end (see FOLIA-26.3-COMPLETE-GUIDE.md Ã‚Â§4.1-4.3).

usage:
  python mcjoin.py --host 127.0.0.1 --port 25599 --name TestBot [--stay 60] [--quiet]
"""
import argparse
import socket
import struct
import sys
import time
import uuid

# ---------------------------------------------------------------- helpers
def wv(n):
    """Varint encoder. Must handle multi-byte values (protocol 777 needs two bytes)."""
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


def rv(read):
    n = 0
    for i in range(5):
        b = read(1)
        if not b:
            raise ConnectionError('eof in varint')
        b = b[0]
        n |= (b & 0x7F) << (7 * i)
        if not (b & 0x80):
            if n >= 2 ** 31:
                n -= 2 ** 32
            return n
    raise ValueError('varint too long')


class Buf:
    def __init__(self, d=b''):
        self.d, self.o = d, 0

    def read(self, n):
        r = self.d[self.o:self.o + n]
        self.o += n
        return r

    def vi(self):
        return rv(self.read)

    def string(self):
        return self.read(self.vi()).decode('utf-8', 'replace')

    def remaining(self):
        return len(self.d) - self.o


def mcs(s):
    b = s.encode('utf-8')
    return wv(len(b)) + b


# ---------------------------------------------------------------- client
class Client:
    def __init__(self, host, port, name, protocol, quiet=False, secret=None):
        self.host, self.port, self.name = host, port, name
        self.protocol = protocol
        self.quiet = quiet
        self.sock = None
        self.uid = uuid.uuid3(uuid.NAMESPACE_DNS, name)
        self.secret = secret        # Velocity modern-forwarding secret, or None
        self.state = 'handshake'
        self.keepalives = 0
        self.teleports = 0
        self.debug = False
        self.compression_threshold = -1   # -1 = no compression until server enables it
        self.log_lines = []
        self.dbg_play = 0

    def log(self, m):
        self.log_lines.append(m)
        if not self.quiet:
            print(m, flush=True)

    def velocity_payload(self):
        """Velocity modern-forwarding response body.

        Wire layout, in the exact order the server reads it
        (VelocityProxy.checkIntegrity / readAddress / createProfile):
            32 bytes  HMAC-SHA256(secret, data)
            data      = [ version varint | ip string | uuid(16) | name string | properties ]

        `properties` is a varint count followed by, per property:
        name, value, and a bool that must be set before the signature string.
        An empty property list is valid and is what an offline proxy sends.
        """
        import hashlib
        import hmac

        version = 4              # MODERN_LAZY_SESSION; must be <= MAX_SUPPORTED_FORWARDING_VERSION
        ip = '127.0.0.1'
        data = (wv(version)
                + mcs(ip)
                + self.uid.bytes
                + mcs(self.name)
                + wv(0))         # zero properties
        sig = hmac.new(self.secret.encode('utf-8'), data, hashlib.sha256).digest()
        return b'\x01' + sig + data   # bool "has data" + signature + body

    def send(self, pid, payload):
        body = wv(pid) + payload
        if self.compression_threshold < 0:
            self.sock.sendall(wv(len(body)) + body)
            return
        # Compressed framing applies to BOTH directions once Set Compression is received.
        if len(body) < self.compression_threshold:
            framed = wv(0) + body          # 0 => payload is uncompressed
        else:
            import zlib
            framed = wv(len(body)) + zlib.compress(body)
        self.sock.sendall(wv(len(framed)) + framed)

    def recvn(self, n):
        buf = b''
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise ConnectionError('eof')
            buf += c
        return buf

    def read_packet(self):
        length = rv(self.recvn)
        body = self.recvn(length)
        if self.compression_threshold >= 0:
            # Compressed framing: [data length varint][zlib payload]; data length 0 => uncompressed.
            b = Buf(body)
            data_len = b.vi()
            if data_len == 0:
                raw = body[b.o:]
            else:
                import zlib
                raw = zlib.decompress(body[b.o:])
            b = Buf(raw)
            return b.vi(), b
        b = Buf(body)
        return b.vi(), b

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=15)
        self.sock.settimeout(45)
        # handshake: next state = 2 (login)
        self.send(0x00, wv(self.protocol) + mcs(self.host)
                  + struct.pack('>H', self.port) + wv(2))
        # login start (1.20.2+ format: name + uuid)
        self.send(0x00, mcs(self.name) + self.uid.bytes)
        self.state = 'login'

    def login_until_play(self, duration=60):
        deadline = time.time() + duration
        while time.time() < deadline:
            try:
                pid, b = self.read_packet()
            except socket.timeout:
                continue
            except Exception as e:
                self.log(f'[x] {self.state}: {e}')
                return False

            if self.state == 'login':
                if self.debug:
                    self.log(f'[dbg] login packet id=0x{pid:02x} len={len(b.d)}')
                if pid == 0x03:      # set compression
                    self.compression_threshold = b.vi() if b.remaining() else 256
                    continue
                if pid == 0x02:      # login success
                    # 1.20.5+ login success carries name+uuid; then client must ack the config switch
                    self.send(0x03, b'')       # ServerboundLoginAcknowledged
                    self.state = 'config'
                    self.log('[+] login success -> config')
                    # ServerboundClientInformation is required before the server will proceed.
                    # Layout (ClientInformation#write): language, viewDistance(byte),
                    # chatVisibility(varint), chatColors(bool), modelCustomisation(byte),
                    # mainHand(varint), textFiltering(bool), allowsListing(bool),
                    # particleStatus(varint)
                    self.send(0x00,
                              mcs('en_us')      # language
                              + bytes([8])      # view distance
                              + wv(0)           # chat visibility: FULL
                              + b'\x01'         # chat colours: true
                              + bytes([0x7f])   # model customisation (all skin parts)
                              + wv(1)           # main hand: RIGHT
                              + b'\x00'         # text filtering: false
                              + b'\x00'         # allows listing: false
                              + wv(0))          # particle status: ALL
                    continue
                if pid == 0x00:      # disconnect
                    self.log(f'[x] login disconnect: {b.d[:120]!r}')
                    return False
                if pid == 0x04:      # login_plugin_request -- MUST answer, else we stall
                    # Layout: message id (varint), channel (identifier), data (rest).
                    msgid = b.vi()
                    try:
                        channel = b.string()
                    except Exception:
                        channel = '?'
                    if channel == 'velocity:player_info' and self.secret:
                        self.send(0x02, wv(msgid) + self.velocity_payload())
                        self.log('[+] sent velocity:player_info forwarding data')
                        continue
                    self.log(f'[!] login_plugin_request id={msgid} channel={channel!r} '
                             f'data={b.d[b.o:][:48]!r}')
                    # ServerboundLoginPluginResponse (0x02):
                    #   message id (varint), successful (bool), data (optional rest)
                    self.send(0x02, wv(msgid) + b'\x00')
                    continue
                if pid == 0x05:      # cookie request
                    self.send(0x04, mcs('') + b'\x00')   # key + absent payload
                    continue
                if pid == 0x01:      # encryption request => online-mode=true
                    self.log('[x] server requested encryption (online-mode); unsupported')
                    return False
                self.log(f'[dbg] unhandled login id=0x{pid:02x} len={len(b.d)}')
                continue

            elif self.state == 'config':
                if self.debug:
                    self.log(f'[dbg] config packet id=0x{pid:02x} len={len(b.d)}')
                # Clientbound configuration ids in registration order, taken from
                # ConfigurationProtocols.CLIENTBOUND_TEMPLATE (26.3):
                #   0x00 cookie_request      0x01 custom_payload   0x02 disconnect
                #   0x03 finish_configuration 0x04 keep_alive      0x05 ping
                #   0x06 reset_chat          0x07 registry_data    0x08 resource_pack_pop
                #   0x09 resource_pack_push  0x0a ...              0x0d update_tags
                #   0x0f select_known_packs  0x12 code_of_conduct
                if pid == 0x00:      # cookie request -> SOURCED response
                    self.send(0x01, mcs('') + b'\x00')   # key + absent payload
                    continue
                if pid == 0x04:      # keep_alive -> echo the id
                    try:
                        kid = b.read(8)
                        self.send(0x04, kid)     # SERVERBOUND_KEEP_ALIVE
                        self.keepalives += 1
                    except Exception:
                        pass
                    continue
                if pid == 0x05:      # ping -> pong
                    try:
                        self.send(0x05, b.read(4))
                    except Exception:
                        pass
                    continue
                if pid == 0x0f:      # select_known_packs -> reply with an empty list
                    # THE STALL: the server waits for this answer before it will send
                    # finish_configuration. ServerboundSelectKnownPacks = varint count
                    # + count * (namespace, id, version).
                    try:
                        b.read(4)        # field cap / padding, ignored
                    except Exception:
                        pass
                    self.send(0x07, wv(0))
                    self.log('[+] replied select_known_packs (empty)')
                    continue
                if pid == 0x12:      # code_of_conduct -> accept
                    self.send(0x09, b'')
                    self.log('[+] accepted code_of_conduct')
                    continue
                if pid == 0x03:      # finish_configuration
                    self.send(0x03, b'')         # SERVERBOUND_FINISH_CONFIGURATION
                    self.state = 'play'
                    self.log('[+] config finished -> play')
                    continue
                if pid == 0x02:      # disconnect
                    self.log(f'[x] config disconnect: {b.d[:120]!r}')
                    return False
                continue

            elif self.state == 'play':
                # common clientbound ids vary by version; handle by shape where possible
                if pid == 0x27 or pid == 0x26 or pid == 0x24 or pid == 0x28:  # keep alive
                    try:
                        kid = b.read(8)
                        self.send(pid, kid)
                        self.keepalives += 1
                    except Exception:
                        pass
                if len(self.log_lines) < 3:
                    self.log(f'[.] first play packet id=0x{pid:02x} len={len(b.d)}')
                # once we see any play packet we are in
                return True

        return False

    def stay(self, duration, move=True):
        """Hold the connection, answer keepalives, optionally walk, report liveness.

        Serverbound play ids for 26.3, taken from GameProtocols.SERVERBOUND_TEMPLATE
        registration order (they are NOT the clientbound ids):
            0x1c keep_alive   0x1e move_pos   0x1f move_pos_rot
            0x20 move_rot     0x21 move_status_only   0x2d pong

        Clientbound keep_alive is 0x2d (an 8-byte timestamp payload, not 0); echoing the clientbound id back (0x27 in
        older protocols) made the server read it as place_recipe and kick us.
        """
        SB_KEEPALIVE = 0x1c
        SB_MOVE_POS = 0x1e
        SB_MOVE_POS_ROT = 0x1f
        SB_MOVE_STATUS = 0x21
        SB_PONG = 0x2d
        SB_ACCEPT_TELEPORT = 0x00
        CB_KEEPALIVE = 0x2d
        CB_PING = 0x3e
        CB_PLAYER_POSITION = 0x49

        end = time.time() + duration
        last_log = time.time()
        next_move = time.time()
        x, y, z = 0.0, 76.0, 0.0
        step = 0
        import math

        while time.time() < end:
            try:
                pid, b = self.read_packet()
            except socket.timeout:
                pid = None
            except Exception as e:
                self.log(f'[x] play: {e}')
                return False

            if pid is not None:
                if self.debug and self.dbg_play < 900:
                    self.dbg_play += 1
                    if 6 <= len(b.d) <= 40:
                        self.log(f'[dbg] play id=0x{pid:02x} len={len(b.d)} '
                                 f'hex={b.d[:32].hex()}')
                if pid == CB_KEEPALIVE:
                    try:
                        kid = b.read(8)
                        self.send(SB_KEEPALIVE, kid)
                        self.keepalives += 1
                    except Exception:
                        pass
                elif pid == CB_PING:
                    try:
                        self.send(SB_PONG, b.read(4))
                    except Exception:
                        pass
                elif pid == CB_PLAYER_POSITION:
                    # ClientboundPlayerPositionPacket:
                    #   varint teleport id
                    #   position x,y,z (3 x f64)   delta movement (3 x f64)
                    #   yaw (f32), pitch (f32)
                    #   relatives (int bitmask)
                    # The server holds us in a "awaiting teleport" state until we
                    # accept, and rejects movement packets meanwhile. Not accepting
                    # was why every bot died with "Invalid move player packet".
                    try:
                        tid = b.vi()
                        px, py, pz = struct.unpack('>ddd', b.read(24))
                        b.read(24)                      # delta movement
                        yaw, pitch = struct.unpack('>ff', b.read(8))
                        b.read(4)                       # relatives
                        # ServerboundAcceptTeleportationPacket:
                        #   varint id, x, y, z (3 x f64), yaw (f32), pitch (f32)
                        self.send(SB_ACCEPT_TELEPORT,
                                  wv(tid) + struct.pack('>ddd', px, py, pz)
                                  + struct.pack('>ff', yaw, pitch))
                        x, y, z = px, py, pz
                        self.teleports += 1
                        # The server kicks clients that are "floating too long"
                        # when allow-flight=false. Re-assert on-ground status a
                        # few times so the move tracker sees a supported entity
                        # rather than a hovering one. StatusOnly = flags byte.
                        for _ in range(3):
                            self.send(SB_MOVE_STATUS, b'\x01')
                        if self.debug:
                            self.log(f'[dbg] accepted teleport {tid} -> '
                                     f'({x:.1f},{y:.1f},{z:.1f})')
                    except Exception as e:
                        if self.debug:
                            self.log(f'[dbg] teleport parse failed: {e}')
                elif pid == 0x78:                       # start_configuration
                    # Server ill-fatedly wants us back in config (reload/respawn
                    # edge); acknowledge by re-entering config.
                    self.send(0x0b, b'')                # SERVERBOUND_CONFIGURATION_ACK
                    self.state = 'config'
                    continue

            # Walk in a slow circle so regions actually load chunks and tick.
            # Do NOT move before accepting a teleport: the server places us at the
            # world spawn, and moving from our local (0,76,0) seed first looks like a
            # multi-block instant jump and trips the movement validation.
            if move and self.teleports > 0:
                now = time.time()
                if now >= next_move:
                    next_move = now + 0.20
                    step += 1
                    ang = step * 0.12
                    x += math.cos(ang) * 0.9
                    z += math.sin(ang) * 0.9
                    on_ground = b'\x01'
                    if step % 2 == 0:
                        # position only: x, y, z (f64), flags (u8 bitfield)
                        self.send(SB_MOVE_POS,
                                  struct.pack('>ddd', x, y, z) + on_ground)
                    else:
                        # position + rotation: x,y,z, yaw(f32), pitch(f32), flags
                        self.send(SB_MOVE_POS_ROT,
                                  struct.pack('>dddff', x, y, z,
                                              (step * 11.0) % 360.0, 0.0) + on_ground)

            if time.time() - last_log > 10:
                last_log = time.time()
                self.log(f'[.] alive keepalives={self.keepalives} '
                         f'teleports={self.teleports} pos=({x:.1f},{y:.1f},{z:.1f})')
        return True

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def status_protocol(host, port):
    s = socket.create_connection((host, port), timeout=10)
    s.settimeout(10)

    def exact(n):
        """recv() may return fewer bytes than asked; must loop."""
        buf = b''
        while len(buf) < n:
            c = s.recv(n - len(buf))
            if not c:
                raise ConnectionError('eof')
            buf += c
        return buf

    hs = wv(0) + wv(767) + mcs(host) + struct.pack('>H', port) + wv(1)
    s.sendall(wv(len(hs)) + hs)
    s.sendall(wv(1) + b'\x00')
    length = rv(exact)
    data = exact(length)
    s.close()
    b = Buf(data)
    b.vi()
    import json
    return json.loads(b.string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=25599)
    ap.add_argument('--name', default='TestBot')
    ap.add_argument('--stay', type=int, default=30)
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--debug', action='store_true')
    ap.add_argument('--nomove', action='store_true',
                    help='do not send movement packets')
    ap.add_argument('--secret', default=None,
                    help='Velocity modern-forwarding secret (enables proxy-path join)')
    a = ap.parse_args()

    st = status_protocol(a.host, a.port)
    proto = st['version']['protocol']
    if not a.quiet:
        print(f'[i] server {st["version"]["name"]} protocol={proto} '
              f'players={st["players"]["online"]}/{st["players"]["max"]}')

    c = Client(a.host, a.port, a.name, proto, quiet=a.quiet, secret=a.secret)
    c.debug = a.debug
    ok = False
    try:
        c.connect()
        if c.login_until_play():
            ok = True
            if not a.quiet:
                print('[+] IN PLAY STATE')
            if a.stay:
                c.stay(a.stay, move=not a.nomove)
        else:
            if not a.quiet:
                print('[x] never reached play state')
    finally:
        c.close()
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
