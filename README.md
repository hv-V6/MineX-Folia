# MineX Folia Fork — what we changed and why

This repository documents the MineX build of **Folia**, the region-threaded Paper fork. It exists because
we maintain our own build rather than waiting for upstream: Folia's 26.3 is on the `ALPHA` channel with
roughly two months to beta and two more to stable, and we needed specific fixes on a much shorter
timescale.

Two baselines are covered:

| Build | Upstream base | What we add |
|---|---|---|
| **26.1.2** (production today) | Folia `26.1.2.build.8-stable` @ `62dc0f2` | 7 files, 16 change blocks, 373 lines |
| **26.3** (the migration target) | Folia `26.3-6-ver/26.3.x` ALPHA @ `8664923`, ours built from `8cffa90` | 12 files, 25 change blocks, 693 lines, +2 new files |

Everything below is measured on a real Folia deployment, not estimated. Where a change did **not** work,
or a theory was disproved, it is recorded as such — the failures are as load-bearing as the fixes.

---

## 1. The headline bug: "in the tab list but never spawns"

### Symptom

A player joins through the proxy from the auth server. Their data reaches the backend. They appear in
`/list` and the tab list, and you can `/tp` to them — but no entity exists, and their client stays on the
auth server. A restart fixes it. So does resetting spawn *and* restarting. It always comes back.

The working theory on the server was "the player cache isn't clearing / memory isn't refreshing".

### What it actually is

`PlayerList.placeNewPlayer` performs two separate, observable steps:

```java
this.players.add(player);            // (d) tab list, /list, every for-loop over players
...
level.addNewPlayer(player);          // (e) the entity actually exists
chunkMap.addEntity(player);          // (f) tracker/visibility registered
```

`ChunkMap.addEntity` has an early-return that **logs and does not throw** when the entity is not valid:

```java
if (!entity.valid || entity.level() != this.level || entity.moonrise$getTrackedEntity() != null) {
    LOGGER.error("Illegal ChunkMap::addEntity ... ALREADY CONTAINED", new Throwable());
    return;
}
```

So a join that dies between (d) and (f) produces exactly the reported shape: **tab yes, entity no.**

Why it dies there: `Connection.syncAfterConfigurationChange` called `future.syncUninterruptibly()`. On Folia
the caller is the **global region thread** (`handleLoginAcknowledgement` runs there via
`PacketUtils.ensureRunningOnSameThread`). Parking that thread on a Netty channel promise freezes logins,
keepalives and packet dispatch **for every player** until the promise resolves. If the proxy times out during
that window, the promise completes as a closed channel, the `ClosedChannelException` is swallowed, and
`placeNewPlayer` continues against a dead connection.

That is why "reset spawn + restart" appeared to work and a plain restart did not: moving spawn made the join
synchronise on a **different, healthy chunk**. The stale thing was never a cache — it was the spawn chunk's
POI/ticket state, which is persisted in the world and reproduces on every login.

**The "player cache" theory was a symptom description, not a cause.**

### The three fixes

**1. `Connection.syncAfterConfigurationChange` — non-blocking** (`Connection.java`, 41 lines)

Never park a region thread on a Netty promise. The protocol switch is scheduled on the channel's event loop
and the continuation runs from the promise listener. This is the root-cause fix and it unblocks logins for
*every* player, not just the one joining.

**2. `PlayerList` join integrity guard** (29 lines)

After `chunkMap.addEntity`, verify the join actually completed — `moonrise$getTrackedEntity() != null`,
`player.valid`, `player.level() == level`. If not, remove from the player list and disconnect with a clear
message. A player must never be visible before they are real.

**3. `PrepareSpawnTask` bounded spawn wait** (55–57 lines)

`Preparing.tick()` previously returned `null` forever while the spawn chunk had not reached
`FULL` — no timeout, no failure state, so a bad spawn chunk hung the login indefinitely. Now bounded by
`-Dminex.spawnTimeoutSeconds` (default 60), with `hasFailed()` / `isTimedOut()` and a diagnostic naming the
spawn level and position. `ServerConfigurationPacketListenerImpl` disconnects on failure instead of hanging.

---

## 2. Region failure isolation — "one region takes down the whole server"

Stock Folia runs every region on a shared pool with no isolation. A region that throws takes down its thread
and can cascade. We changed three files:

**`TickRegionScheduler.regionFailed`** (104 lines in 26.3, 120 in 26.1.2)

Rewritten around a configurable failure mode:

- `failure-mode: ISOLATE` — contain the failure to that region, keep the server running (default)
- `failure-mode: HALT` — upstream behaviour, for debugging
- `FAILURE_COUNTER` for observability
- `QUARANTINED_REGIONS` with `QUARANTINE_RETRY_DELAY_NS = 3600s`

**Hung-region quarantine** (`FoliaWatchdogThread.java`, 39–43 lines)

`maybeQuarantineHungRegion(tick, totalElapsedS)` — a region that stops responding is quarantined rather than
left to wedge the scheduler. Configurable via `hung-region-action-after-seconds` (default 60).

**Quarantine state must live on the region, not the handle** (`TickRegions.java`, 65–76 lines)

A handle-scoped flag is **lost on merge/split**, because `tickHandle.copy()` produces a new object. The flag
was therefore moved to a region-level `volatile boolean quarantined` with
`markQuarantined()` / `recoverFromQuarantine()` / `isQuarantined()`, and `onRegionActive` refuses to schedule a
quarantined region. Get this wrong and quarantine silently disappears the first time regions merge.

**`RegionizedServer`** (28 lines) and **`GlobalConfiguration`** (40 lines) expose `threadedRegions.failureMode`,
`maxIsolatedRegionFailures` (25) and `hungRegionActionAfterSeconds`.

---

## 3. Join watchdog and operator tooling

**`JoinWatchdog.java` — new file, 11.3 KB** (26.3 only)

Watches for players who are listed but never tracked — the §1 failure, detected rather than suffered. After
`minex.joinWatchdogSeconds` (default 15) it dumps a thread dump, `dumpAllChunkLoadInfo` and region state,
removes the ghost from `PlayerList` and disconnects. Wired into `RegionizedServer.globalTick()`. Counters are
exposed via `/mxsr status`.

**`CommandRegionRecover.java` — new file, 11.1 KB** (26.3 only)

`/mxsr status|list|recover <world> <cx> <cz>|recoverall` — uses `RegionizedServer.blockOn()` to force a
quarantined region back into service.

**`PaperCommands`** registers `mxsr`.

This is what let the ghost path be **verified rather than asserted**: a `FaultInjector` plugin (`fi ghost`)
fabricates the exact §1 failure, and the watchdog demonstrably heals it.

---

## 4. Network and connection handling

**Keepalive grace** (`ServerCommonPacketListenerImpl.java`, 221 lines in 26.3)

Stock Folia kicks on a single missed keepalive window. With real-world latency that produces kick storms —
one incident had **1,737 kick lines in 3 seconds**, and the auto-reconnect stampede then serialised logins
through the global region, amplifying §1.

Now a kick requires `unheard >= minex.keepaliveGraceMisses` (default 3) **and** `elapsed > KEEPALIVE_LIMIT`,
plus a `keepaliveStallWarned` flag so a slow client warns before it dies.

**Allocation-free connection tick** (`RegionizedServer.java`)

`tickConnections()` previously allocated a `new ArrayList` and `Collections.shuffle`d it **every tick**. Replaced
with a rotating `connectionTickOffset` start index — no allocation, same fairness.

**Lazy per-region caches** (`RegionizedWorldData.java`, 49 lines)

`getPathTypesByPosCache()`, `getExplosionDensityCache()`, `clearExplosionDensityCache()` are now allocated on
first use rather than per region. Cuts RAM proportional to region count, which matters because a real
deployment runs 15–20 regions across 6 worlds.

---

## 5. Thread sizing

**`TickRegions.getTickThreads`** (65–76 lines)

Upstream, with `threads: -1`:

```java
tickThreads = getTotalCores() / 2;
if (tickThreads <= 4) tickThreads = 1;
else                  tickThreads = tickThreads / 4;
```

| Cores | Upstream | MineX |
|---|---|---|
| 8 | 1 | 6 |
| 16 | 2 | 12 |
| **24** | **3** | **19** |
| 32 | 4 | 25 |

A 24-core node gets **3** tick threads upstream. MineX sizes to 80% of visible cores with a floor of 2 and
headroom reserved for netty, chunk-system and GC.

**Honest status: the benefit is not proven.** A controlled sweep at 80 creative flying bots gave:

| Threads | Regions formed | MSPT med | MSPT p95 | TPS min |
|---|---|---|---|---|
| 3 | 5 | 13.87 | 29.55 | 19.82 |
| 6 | 5 | 11.63 | 32.39 | 19.34 |
| 12 | **2** | 13.12 | 18.78 | 20.00 |

**This is inconclusive**, and it is reported as such: the 12-thread run formed only 2 regions instead of 5, so
it did less total work. Region count varies independently of thread count, and the variance exceeded the
effect. What the data *does* suggest is that thread count does not improve **per-region** MSPT — which is
consistent with Folia's model, that threads buy parallelism across regions, not faster regions.

**Implication:** the "3 threads is starving us" theory is not supported. A separate measurement of production
found 20 regions on 3 threads at 20.00 TPS, top region 19.6 of 50 ms — healthy.

---

## 6. A/B against upstream 26.3

Measured against the real upstream ALPHA jar (`26.3-6`, commit `8664923`), same world, same 30 creative
flying bots, same 240 s hold:

| Metric | Upstream | MineX | Delta |
|---|---|---|---|
| Players held | 30/30 | 30/30 | — |
| Regions | 2 | 2 | — |
| TPS min | 19.93 | 19.87 | ~equal |
| MSPT avg | 29.42 ms | 28.65 ms | 2.6% lower |
| MSPT max | 31.64 ms | 33.91 ms | 7% **higher** |
| RSS avg | 4,887 MB | 4,667 MB | 4.5% lower |
| CPU avg | 383.3% | 375.7% | 2.0% lower |

**The two builds are equivalent within noise at this load.** Averages favour MineX by 2–5%, maxima move the
other way — that is variance, not a win. This is a **negative result** and should be read as one: our changes
target *failure paths* (isolation, quarantine, join integrity, keepalive grace), not steady-state tick cost,
which is dominated by chunk sending in code we do not touch. The fork's value is survivability, and
survivability cannot be measured in a run where nothing fails.

---

## 7. Measured defects found in production (not yet all fixed)

Production (stock `26.1.2.build.8-stable`) was audited over several days of logs.

### 7.1 Stable baseline is genuinely good

```
median tick: 2.8 – 3.1 ms     (spread 0.3 ms)
p95:         4.3 – 4.8 ms     (spread 0.5 ms)
max:         57 – 66 ms       (every single 10s window)
```

No crashes, no OOM, no watchdog stalls, no region tick failures across the audited period.

### 7.2 `TrimNativeHeapInterval=60000` — confirmed 60-second hitch

Measured from `stall.log`: **11,132 `Periodic Trim` events, exact 60.0 s period, 35–40 ms each** (p95 62 ms,
p99 79 ms, max 1,010 ms), reclaiming ~**0.01 GB** on a heap pinned at 14 GB by `-Xms14G -XX:-ZUncommit`.
Net useful effect ≈ zero; cost ≈ one hitch a minute.

**Action taken:** the flag was removed from the production start script (one line, `bash -n` verified,
LF-only). Trims went to **zero** and the 62 ms ceiling collapsed. *Caveat:* the post-change measurement was
taken with 0 players online, so the with-load confirmation is still outstanding.

### 7.3 Vulcan's Folia shim is broken

```
me.frep.vulcan.spigot.Vulcan_Om.lambda$onMove$4(Vulcan_Om.java:558)
  → CraftEntity.getNearbyEntities
  → Level.getEntities            ← region is worlds:world, entity is in worlds:duelsarena
  → TickThread.ensureTickThread
```

~25–41 cross-thread violations per day. Vulcan schedules a "location task" per movement via
`FoliaRegionScheduler.wrap`, hops onto a region thread correctly, then dereferences a **stale entity in
another world**. Every one is a thrown exception inside `tickServer`.

Vulcan is being replaced by an in-house anticheat, which resolves this.

### 7.4 Things that were ruled out

- **GC pauses are not the problem.** ZGC major pauses average **0.02 ms** (10,655 samples). The
  multi-second "major cycles" seen in GC logs are *concurrent* — max safepoint was 14.5 ms.
- **No memory leak.** Old generation is flat at 2.0 GB with 12–28 MB promoted per cycle. The live set is
  genuinely ~2 GB; 11 GB of young-gen churn is allocation rate, not retention.
- **`-XX:+UseStringDeduplication` is a no-op under ZGC.** G1-only flag, harmless but misleading.

---

## 8. Tooling

Everything used to produce the numbers above. **All credentials have been stripped** — these read from
environment variables (`MX_PANEL`, `MX_TOKEN`, `MX_SERVER`, `MX_SFTP_HOST`, `MX_SFTP_USER`, `MX_SFTP_PASS`).

| Tool | Purpose |
|---|---|
| `mcjoin.py` | Complete 26.3 client: handshake → login → config → PLAY, incl. Velocity modern forwarding |
| `mcsoak.py` | Multi-bot soak harness |
| `mcfill.py` | Staged capacity ramp |
| `mcflight.py` | Creative-flight bots (client-driven; see limitations) |
| `abflight.py` | A/B load comparison, server-driven flight |
| `threadsweep.py` | Tick-thread count sweep |
| `moveprobe.py` | Probes which move-player packet variants the server accepts |
| `pktids.py` | Derives packet IDs from `GameProtocols.java`, validated against observed traffic |
| `audit.js` | Classifies a log into stability signals and grouped error signatures |
| `gcanalyze.js`, `trimdist.js` | GC pause and trim-stall distributions |
| `extract_changes.py` | Pulls `// MineX start/end` blocks out of the generated source tree |
| `scrub_secrets.py` | Replaces credentials with placeholders before publishing |

### Protocol notes worth keeping

Getting a bot to PLAY on 26.3 took four separate fixes, each failing differently:

1. Status ping omitted the **protocol-version varint** in the handshake
2. Bot never answered `velocity:player_info` (HMAC-SHA256 over a specific field order)
3. Bot never answered **`select_known_packs` (0x0f)** — server waits indefinitely for it
4. Bot echoed the **clientbound** keepalive id (0x2d) as serverbound; the reply is **0x1c**, and 0x2d is read
   as `place_recipe`

The last one is the general trap: **clientbound and serverbound packet ID tables are different.**
`pktids.py` derives both and validates against observed traffic — note `.withBundlePacket(...)` consumes an ID
slot, which makes naive line-counting off by one.

---

## 9. Known limitations

- **Bots cannot fly themselves.** Every sustained client-driven movement test produced
  `Invalid move player packet received`, at 20 Hz, 12.5 Hz and 4 Hz, with all seven flag/packet variants
  (`moveprobe.py` shows each passes as a single packet, all fail when sustained). Load tests therefore use
  server-driven flight (`/tppos`), which exercises chunk load/send/entity tracking but not physics, pathfinding,
  AI or block interaction. A real 80-player survival population costs more per player than the numbers here.
- **`spark`'s Folia adapter is broken on 26.3** (`NoClassDefFoundError: TickData$TickReportData`), so 26.3
  measurements use `paper:tps` instead. Production (26.1.2) can use `spark`. **These are different instruments
  and are not directly comparable** — a mistake made and corrected during this work.
- **The 26.1.2 jar was never rebuilt** after the join fixes were written (built 15:50, sources edited
  16:17–16:32). The fixes compile cleanly, but no jar containing them exists yet.
- **Regional-thread sweep is inconclusive** (§5) — region count varied between stages.

---

## 10. Plugin compatibility status (26.3)

| Plugin | Status |
|---|---|
| PacketEvents 2.14.0 | ✅ Works — loads `V_26_3/23` block mappings |
| PlaceholderAPI 2.12.3 | ✅ Works — 2.12.3 fixes the version-parsing crash |
| FAWE 2.15.3 | ❌ Blocks boot — `NoCapablePlatformException` in help-topic registration |
| WorldGuard 7.0.17 | ❌ Blocks boot — depends on FAWE |
| nightcore 2.16.2 | ❌ `asBukkitCopy` missing from `CraftItemStack` |
| worlds 4.3.0-pre1 | ❌ Hardcoded `No implementation found for version: 26.3` |
| LPC 3.7.1 | ❌ Not marked as Folia-compatible |
| FancyNpcs 2.10.0 | ⚠️ Loads with "unsupported version" warning |

**Testing note:** do not validate plugin compatibility with `plugman load` alone. FAWE and WorldGuard both
"enabled" via hot-load and then **blocked the boot** of the server on a real restart. Verify at boot.
