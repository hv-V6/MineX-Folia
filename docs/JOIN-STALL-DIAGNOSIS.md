# Diagnosis: "player is in the tab list but never spawns"

**Symptom (as reported)**: a player joins through the proxy from the auth server, their data reaches the
main server, they appear in tab / the player list — but no entity exists for them. Their client stays on the
auth server. Server restart "fixes" it, and only when the spawn location is also reset.

This document traces the exact code path and names the failure point.

---

## 1. The join path, in order

`PrepareSpawnTask.Ready.spawn()` → `PlayerList.placeNewPlayer()` → the following sequence **in one
region-side call**:

```java
// PlayerList.placeNewPlayer
ServerGamePacketListenerImpl playerConnection = new ServerGamePacketListenerImpl(server, connection, player, cookie);
level.getCurrentWorldData().connections.add(connection);          // (a) region now ticks this connection
connection.setupInboundProtocol(GameProtocols.SERVERBOUND..., playerConnection);  // (b) BLOCKS on netty
playerConnection.suspendFlushing();
playerConnection.send(new ClientboundLoginPacket(...));            // (c) config sent
...
this.players.add(player);                                          // (d) <-- TAB LIST
this.playersByName.put(...);
this.playersByUUID.put(...);
this.sendLevelInfo(player, level);
level.addNewPlayer(player);                                        // (e) <-- ENTITY ADD
...
((ServerLevel)player.level()).getChunkSource().chunkMap.addEntity(player);  // (f) <-- TRACKER / visibility
```

### The two independent markers

- **(d) `this.players.add(player)`** is what puts the player in `PlayerList.players` — i.e. the **tab list**
  and `/list`, and every `for (ServerPlayer p : this.players)` loop.
- **(e) `level.addNewPlayer(player)`** → `moonrise$getEntityLookup().addNewEntity(player)` is what makes an
  **entity** exist in the world.

There is **no `try/catch`** around `placeNewPlayer`. But crucially, these are **separate steps**, and
(f) has an explicit early-return path that reports failure without throwing:

```java
// ChunkMap.addEntity
if (!entity.valid || entity.level() != this.level || entity.moonrise$getTrackedEntity() != null) {
    LOGGER.error("Illegal ChunkMap::addEntity for world ... ALREADY CONTAINED (This would have crashed your server)", new Throwable());
    return;
}
```

So a half-completed join produces exactly the reported shape: **(d) succeeded, (e)/(f) did not** →
"in the tab list, no entity, client still showing the auth server".

---

## 2. Why the client stays on the auth server

Step **(b) `setupInboundProtocol`** ends in:

```java
// Connection
private static void syncAfterConfigurationChange(final ChannelFuture future) {
    future.syncUninterruptibly();     // <-- blocks the calling thread
}
```

In Folia the caller is the **global region thread** (`handleLoginAcknowledgement` runs there via
`PacketUtils.ensureRunningOnSameThread` → `RegionizedServer.schedulePacket` for
`ServerLoginPacketListenerImpl` / `ServerConfigurationPacketListenerImpl`).

This is precisely the stack recorded in the Aug 29 / Aug 31 incidents:

```
ServerLoginPacketListenerImpl.handleLoginAcknowledgement
  → Connection.setupOutboundProtocol
  → Connection.syncAfterConfigurationChange
  → syncUninterruptibly()          on a Netty channel promise
```

**While that promise is unresolved the global region is frozen.** Logins, keepalives and packet dispatch
for *every* player stop. If the connection is torn down while the global region is wedged — proxy read
timeout, player quit, backend `read timed out` — the promise completes as a failure/closed channel, the
`catch` in `syncAfterConfigurationChange` swallows `ClosedChannelException`, and execution continues into
the rest of `placeNewPlayer` **against a dead connection**. That is the "no entity, client never got the
join" state.

**Cross-verification from the Aug 31 report**: the POI mismatch was at
`BlockPos{x=-59999, y=-54, z=65550}` and the watchdog stall was on the shared spawn/respawn chunk.
Both the report and this code trace point at the **same** mechanism: the spawn chunk is the
synchronisation point, and when it is slow the global region stalls there.

---

## 3. Why "reset spawn location + restart" is the only thing that worked

That fix does **not** clear a cache. It changes **which chunk the join synchronises on**:

1. `PrepareSpawnTask.Preparing` resolves the spawn chunk and does
   `chunkLoadFuture = trackLoadWithRadius(spawnLevel, spawnChunk, 3, FULL, HIGH, ...)`.
   It does **not** complete until that chunk (and its 3-radius neighbourhood) reaches `FULL`.
2. `Ready.spawn()` then immediately does `PlayerList.placeNewPlayer(...)`, which performs the **blocking**
   `syncAfterConfigurationChange` and then reads/registers the spawn chunk's POI and entity data.

If the configured spawn lands on a chunk that cannot reach `FULL` promptly — unloadable, corrupt region
file, or a **POI mismatch** (`"POI data mismatch: never registered at ..."`, which throws inside
`PoiManager`) — then:

- the global region stalls in the login/join path,
- the join half-completes (tab yes, entity no),
- and it **reproduces identically on every subsequent login**, because the spawn chunk is the same chunk
  and the stale POI is persisted in the world's `poi/` data.

Moving spawn to a different, healthy chunk makes the join synchronise on a *different* chunk that loads
correctly → the join completes. A restart alone only helps when it clears the in-memory ticket/POI state
long enough to get through once. **That is why "reset spawn + restart" appeared to work while a plain
restart did not, and why the problem always came back.**

The "player cache / memory not refreshing" theory is therefore a *symptom description*, not the cause:
the `PlayerList` entries (d) are real and persist. What was stale was never the cache — it was the
**spawn chunk / POI data** the join synchronises against.

---

## 4. Contributing amplifier: the join stampede

`Max-pending-logins` (patch `0002`) gates concurrent logins with
`misc.max-joins-per-tick` (default **5**). After a keepalive kick storm — Aug 31 had **1,737 kick lines in
3 s** — clients auto-reconnect and slam the single global region thread with 4–6 logins at once. Each one
can block on `syncAfterConfigurationChange`, and they serialise behind each other.

This is why the incidents cluster around mass-kick events and why "spread the re-join stampede" was the
top recommendation in the Aug 31 report.

---

## 5. Fixes (ordered)

### 5.1 Stop blocking the global region on netty (root cause of the freeze)

`syncAfterConfigurationChange` must not `syncUninterruptibly()` from a region thread. The protocol switch
has to be *scheduled* on the channel's event loop and the continuation run from the promise listener,
never by parking a tick thread. This is the highest-value change and it fixes the freeze for all players,
not just the one joining.

### 5.2 Make a half-completed join impossible (root cause of "tab but no entity")

Wrap the join in an explicit state machine: only add to `PlayerList.players` (tab) **after** the entity
exists and the tracker is registered, or make every failure path between (d) and (f) roll the player back
out. A player must never be visible before they are real.

### 5.3 Bound the spawn wait (defence in depth)

`PrepareSpawnTask.Preparing.tick()` returns `null` forever while `chunkLoadFuture` is incomplete — there is
no timeout and no failure state. A spawn chunk that never reaches `FULL` means the login hangs
indefinitely. Needs a bounded wait with a deterministic fallback (pick a nearby loadable chunk) and a clear
log line.

### 5.4 Repair POI at spawn (operational)

For the specific bad spot: forceload the chunk, or clear stale bed/POI data, or simply move spawn to a
verified-healthy chunk and keep it there. Verify with a scripted login rather than by waiting for a player.

---

## 6. How to confirm this on the live server in 30 seconds

On the next occurrence, before restarting anything:

```
/mxsr status                      # our tooling: quarantined regions / tick threads
paper:tps server 5                # is a region at ~0 TPS while others are fine?
```
then check the log for, in this order:

1. `Global region has not responded` (watchdog) — confirms the global-region stall.
2. `handleLoginAcknowledgement` / `syncAfterConfigurationChange` in the accompanying thread dump.
3. `POI data mismatch` naming the spawn block — confirms the spawn-chunk/POI link.
4. `Illegal ChunkMap::addEntity ... ALREADY CONTAINED` — confirms the half-completed join.
5. The player's name having a `logged in with entity id` line or not — **no `entity id` line plus a tab
   presence is the definitive fingerprint of this bug.**

If all five line up, the diagnosis in this document is confirmed and the fix in 5.1/5.2 is the correct one.
