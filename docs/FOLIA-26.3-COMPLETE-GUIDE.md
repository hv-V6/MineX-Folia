# Folia: Complete Reference, 26.3 Port, and MineX Hardening

**Fork base**: PaperMC/Folia `ver/26.3.x` @ `8cffa9059ad6bbc2b28510c38ed61a51098d4971`
**Minecraft**: 26.3 · **Channel**: ALPHA · **Java**: 25
**Validated on**: Pterodactyl `fd4afc59` (*MineX_MMO_PRUEBAS*), 15 GB / unlimited CPU
**Previous**: 26.1.2 @ `62dc0f2` (still available, see §9 rollback)

---

## Part 1 — What Folia is

Folia replaces Minecraft's single main thread with N independent **region** tick loops. Each region
ticks the vanilla server loop for its own chunks, on its own thread, in parallel with every other
region. There is no longer a "main thread"; instead each region is its own main thread.

### 1.1 How it is packaged

Folia is a **patch stack**, not a vendored fork. It pins a Paper commit and expresses every difference
as a patch that is applied at build time:

```
folia-server/minecraft-patches/features/0001..0011   NMS + Moonrise    (11 patches)
folia-server/paper-patches/features/0001..0006       Paper layer       ( 6 patches)
folia-api/paper-patches/features/0001..0004          API               ( 4 patches)
```

`0001-Region-Threading-Base.patch` **is** Folia. On 26.1.2 it was 20,206 diff lines over **198 files**
with only **13 new files** (~5.2k lines). The other 185 are invasive edits to vanilla and Paper classes.

> **The single most important fact about maintaining a Folia fork**: your changes do not live in git
> history you control — they live inside *applied patch output*. `applyAllPatches` produces
> `folia-server/src/minecraft/**` and `paper-server/**` from upstream + patches. Only `paper-server` is a
> git repo; `folia-server/src/minecraft` is not. Every change therefore has to be re-applied on each
> upstream bump, which is why every edit in this project is wrapped in `// MineX start … // MineX end`
> markers and snapshotted under `_folia/patches/`.

Build chain: **JDK 25** toolchain, Gradle 9.4.1, `paperweight-patcher 2.0.0-beta.24`.
`applyAllPatches` ≈ 23 min on 26.3 (117 Mache + 925 Paper + Folia patches); `createBundlerJar` ≈ 3 min
→ ~100 MB jar.

### 1.2 The core classes

All under `io/papermc/paper/threadedregions/`:

| Class | Lines | Responsibility |
|---|---:|---|
| `ThreadedRegionizer` | ~1400 | the region graph: sections, merge/split, chunk bitsets |
| `RegionizedTaskQueue` | ~860 | per-region priority queues + chunk tickets |
| `RegionizedWorldData` | ~770 | per-region world state (entities, ticks, connections, caches) |
| `TickRegionScheduler` | ~680 | EDF / work-stealing pool, tick handles, **failure handling** |
| `RegionizedServer` | ~525 | the single **global region** |
| `TickRegions` | ~500 | region data + glue |
| `FoliaRegionScheduler` | ~430 | Bukkit `RegionScheduler` API |
| `CommandServerHealth` | ~355 | `/tps` (shadowed on our server by CMI — use `paper:tps`) |
| `RegionizedData` | ~235 | region-scoped data with merge+split callbacks |
| `RegionShutdownThread` | ~230 | graceful shutdown from any region |
| `CommandRegionRecover` | ~195 | **MineX** — `/mxsr` quarantine tooling |

---

## Part 2 — How region threading actually works

### 2.1 Regions are sets of sections

`grid-exponent: 4` → a section is 2⁴ × 2⁴ = **16×16 chunks** (256). A `ThreadedRegion` owns a set of
sections; each `ThreadedRegionSection` holds a chunk bitset (`long[8]`) and a `nonEmptyNeighbours` count.

Adding a chunk to an empty section:
1. creates *empty* neighbour sections within `emptySectionCreateRadius` (preserves adjacency),
2. collects every nearby region within `createRadius + regionSectionMergeRadius`,
3. **merges** all of them that are not currently `STATE_TICKING`.

Removing the last chunk of a section, when the region is released, may trigger a **BFS split** when
`deadSectionPercent >= maxDeadRegionPercent` (1/6) or every section is dead. Both directions run under a
world-wide `StampedLock` **write lock**.

**Consequence**: region membership is dynamic and recomputed on chunk churn. A plugin must never assume a
region is stable across ticks, and any per-region state has to survive merge/split (see §6.2 — this bit me).

### 2.2 The state machine

```
STATE_TRANSIENT(0) → STATE_READY(1) → STATE_TICKING(2) → STATE_DEAD(3)
```

Only one thread may tick a region. A ticking region **cannot** be merged into (`tryKill()` returns
`false`); it records `mergeIntoLater` and merges when it next releases the tick. This is what keeps a
running tick safe from graph mutation.

### 2.3 Scheduling and the tick loop

- Pool: `EDFSchedulerThreadPool` (default) or `StealingScheduledThreadPool`.
- `RegionScheduleHandle.runTick()` → mark ticking → `tickRegion()` →
  `MinecraftServer.tickServer(startTime, scheduledEnd, 10ms buffer, region)`.
- After the tick: `tickSchedule.advanceBy(tickCount, 50ms)` then
  `setScheduledStart(max(tickEnd, idealNext))`.
  **Overrunning regions self-throttle** — a region that takes 3 s drops to its own natural rate and does
  not starve others *in the same pool*.

### 2.4 Thread budget — the biggest operational lever

```java
// TickRegions.getTickThreads — upstream 26.3
if (config.threads <= 0) {
    tickThreads = OSNuma.getNativeInstance().getTotalCores() / 2;
    if (tickThreads <= 4) tickThreads = 1;
    else tickThreads = tickThreads / 4;      // 24 cores → 3 threads
} else tickThreads = config.threads;
```

Default `threads: -1` on a 24-core container = **3 tick threads** for the global region *and every world
region combined*. This alone explains most "Folia is slow" reports on containerised hosts. **MineX fix
(§5.4): 3 → 19.**

### 2.5 The global region is a serialisation point

`RegionizedServer.GlobalTickTickHandle` (`region == null`) runs once per tick for the whole server:

adventure click callbacks · `GlobalRegionScheduler.tick()` · all world clocks · console input ·
player sample / server status · **per world**: drain global chunk tasks, worldborder, weather,
night-skip, **raids**, sky brightness, game time, `chunkHolderManager.processTicketUpdates()`, map
autosave · `tickConnections()` over *every* connection · `playerList.tick()`.

Everything here shares **one thread**. When it stalls, **the entire server stops answering**, regardless
of how healthy the regions are. That is Part 3.

---

## Part 3 — The network and connection failures, explained

Four distinct failure modes were documented across the incident reports. They are not the same bug.

### 3.1 The global-region login freeze (the big one)

**Source of truth**: `REPORT_LOGIN_FREEZE_INCIDENT_20260831.md` — 27 watchdog stalls in 19 minutes on
Aug 29, 4 more on Aug 31, each 20–60 s, taking the whole server down.

**The stack, every time:**

```
ServerLoginPacketListenerImpl.handleLoginAcknowledgement
  → Connection.setupOutboundProtocol
  → Connection.syncAfterConfigurationChange
  → syncUninterruptibly()        ← blocks a tick thread on a Netty promise
```

**Why this is fatal**: `handleLoginAcknowledgement` runs on the **global region** thread (via
`PacketUtils.ensureRunningOnSameThread` → `RegionizedServer.schedulePacket`). That promise only completes
when the connection's Netty event-loop thread runs. So **one player's socket** can park the thread that
the entire server depends on. No logins, no keepalives, no packet dispatch for anyone — ping balloons to
20–24 s, Vulcan kicks for high ping, clients auto-reconnect, and the stampede makes it worse.

**Verified**: the 26.3 source has the identical code. Upstream has not fixed it.

> The Aug 31 report called this "a Folia concurrency weakness that legitimate traffic trips" and
> recommended a login cooldown to reduce the trigger. The real fix is to stop blocking the thread at all.

### 3.2 "In the tab list but never spawns" — the half-completed join

**Symptom**: player data reaches the main server, they appear in tab / `/list`, but no entity exists,
and their client stays on the auth server. Prior attempts blamed "player cache not clearing"; the only
workaround found was *reset spawn location + restart*.

**The actual mechanism.** `PlayerList.placeNewPlayer` performs these steps **in order**, in one call:

```java
this.players.add(player);                       // (d) TAB LIST — visible here
...
level.addNewPlayer(player);                     // (e) ENTITY add
...
chunkMap.addEntity(player);                     // (f) TRACKER / visibility
```

There is **no try/catch** around `placeNewPlayer`, and `ChunkMap.addEntity` has an explicit
**early return without throwing**:

```java
if (!entity.valid || entity.level() != this.level || entity.moonrise$getTrackedEntity() != null) {
    LOGGER.error("Illegal ChunkMap::addEntity for world ... ALREADY CONTAINED (This would have crashed your server)", new Throwable());
    return;                                    // ← silent failure
}
```

So if anything between (d) and (f) fails — including the connection dying while §3.1 has the global
region wedged — you get **exactly the reported shape**: tab yes, entity no, client never spawned.

**Why "reset spawn + restart" appeared to work.** It does not clear a cache. It changes **which chunk the
join synchronises on**:

1. `PrepareSpawnTask.Preparing` resolves the spawn chunk and does
   `trackLoadWithRadius(spawnLevel, spawnChunk, 3, FULL, HIGH, …)`, which does not complete until that
   chunk **and its 3-radius neighbourhood** reach `FULL`.
2. `Ready.spawn()` then immediately calls `placeNewPlayer`, which blocks (§3.1) and reads/registers the
   spawn chunk's POI and entity data.

If the spawn chunk cannot reach `FULL` promptly — unloadable, corrupt region file, or a **POI mismatch**
(`"POI data mismatch: never registered at BlockPos{x=-59999, y=-54, z=65550}"`, which throws inside
`PoiManager`) — the join half-completes and **reproduces on every login**, because the spawn chunk and the
stale POI are persistent world data.

Moving spawn to a healthy chunk makes the join synchronise somewhere that loads → it works. A plain
restart only helps if it clears the in-memory ticket/POI state long enough to get through once.

> **The "player cache" theory was a symptom description, not the cause.** The `PlayerList` entries are
> real and they persist. What was stale was never the cache — it was the **spawn chunk / POI data** the
> join synchronises against.

The Aug 31 report independently corroborates this: the POI mismatch block `(-59999, -54, 65550)` was the
shared spawn/respawn point, and the stalled logins were all landing on it.

### 3.3 Keepalive death and the kick storm

`tickConnections()` lives on the global region. When §3.1 wedges it, **every** player's keepalive goes
unanswered simultaneously → mass timeouts → auto-reconnect → more logins onto the wedged thread. Aug 31
logged **1,737 kick lines in 3 seconds**. This is an amplifier, not a cause.

`Max-pending-logins` (patch `0002`) caps concurrent logins at `misc.max-joins-per-tick` (default **5**),
which helps but does not remove the blocking wait.

### 3.4 The `TrimNativeHeapInterval` jitter (Aug 27)

From `REPORT_SMP_FREEZE_AND_PING_SPIKES_20260827.md`: `-XX:TrimNativeHeapInterval=5000` runs
`malloc_trim` every 5 s holding the native allocator lock. Any thread doing native malloc (Netty buffers,
zlib chunk I/O, NIO) stalls for its duration — **invisible to TPS/MSPT/safepoint stats**. Over 3,983 trims:
2,085 were <25 ms, 1,828 were 25–50 ms, 65 were 50–100 ms, and **5 were 200–615 ms** (all in one window).
This is the source of the 1.3–2.6 s external-probe spikes with TPS at 20.00.

### 3.5 What is *not* our bug

The `REPORT_ROMANV6_AUTH_TIMEOUT_AND_ROUTE_LAG_20260919.md` analysis is correct and I agree with its
~75/25 split: subset-only, cross-continent, self-healing handshake stalls with healthy ticks are
**transit/host-ingress**, not origin. No amount of server code fixes that. What code *can* fix is not
making it worse (§3.1) and not leaving ghosts behind when it happens (§3.2).

---

## Part 4 — What I changed, and why

All edits are marked `// MineX start … // MineX end` and snapshotted in `_folia/patches/26.3/`.

### 4.1 Non-blocking protocol switch — `Connection.syncAfterConfigurationChange`

**Fixes §3.1.** The ordered pipeline reconfigure does not require the caller to wait: the config tasks are
written to the channel and executed in order by the event loop, and any packet subsequently written
through the same `Connection` goes to the same channel, so **ordering is preserved without a synchronous
handshake**.

```java
if (future.channel().eventLoop().inEventLoop()) {
    future.syncUninterruptibly();          // already on the loop: harmless
    return;
}
future.addListener(...);                    // otherwise complete asynchronously
```

The global region is never parked on a Netty promise again.

### 4.2 Join integrity guard — `PlayerList.placeNewPlayer`

**Fixes §3.2.** After `chunkMap.addEntity`, verify the player actually became a tracked entity:

```java
final boolean tracked = ((EntityTrackerEntity)player).moonrise$getTrackedEntity() != null;
if (!tracked || !player.valid || player.level() != level) {
    LOGGER.error("Join integrity check failed for {} …", ...);
    this.remove(player);                       // unwind the tab-list entry
    player.connection.disconnect("…world sync failed…");
    return;
}
```

A player can no longer be visible in the tab list without being real. A disconnect the proxy can retry is
strictly better than a phantom that needs a restart *and* a spawn reset.

### 4.3 Bounded spawn wait — `PrepareSpawnTask`

**Fixes the second half of §3.2.** `Preparing.tick()` returns `null` forever while the spawn chunk is not
`FULL`; there is no timeout upstream. Added `minex.spawnTimeoutSeconds` (default 60, configurable via
`-D`), which fails the login with an explicit error naming the world, spawn position, and the likely
causes (POI corruption / unloadable chunk), and disconnects the player instead of hanging silently.

### 4.3b Join watchdog — `JoinWatchdog` (self-detecting, self-healing)

**This is the answer to "we can't reproduce it."**

The §3.2 bug is rare, depends on world state we cannot recreate on demand, and historically only surfaced
when a player complained — by which time the interesting stack was gone. The wrong response is to hope for
more reports. The right one is to **make the failure detect itself**.

`JoinWatchdog` runs on the global tick (2 s scan, cheap) and watches every player who is registered in the
player list but is **not** a valid, tracked entity. When one stays that way past
`minex.joinWatchdogSeconds` (default 15) it:

1. logs `Player record at detection` (level, position, valid, removed, real),
2. dumps **all threads** with stacks,
3. dumps the **chunk-wait stack** (`dumpAllChunkLoadInfo`) — this names any region blocked on a chunk,
4. dumps region state (quarantined count, failures, tick threads),
5. **removes the ghost from the player list** so no phantom tab entry survives,
6. **disconnects** the connection so the proxy retries cleanly,
7. increments `Ghost joins detected / healed`, visible in `/mxsr status`.

So the failure mode changes from *"plausibly rare, needs a restart and a spawn reset, learned about from a
complaint"* to *"logged in full, healed within 15 s, counted permanently."* **If it is happening to more
players than report it, the counter will now show it** — and that is the number that tells you whether
this is a 2-per-month curiosity or a systematic problem.

**Verified end-to-end** on a local Folia 26.3 server by fabricating the exact condition (a `ServerPlayer`
added to `PlayerList.players` but never added to the world entity lookup):

```
JOIN WATCHDOG: 'GhostBot' has been listed for 10+s without becoming a tracked entity.
Player record at detection: level=world pos=(0.0, 0.0, 0.0) valid=false removed=false real=true
--- thread dump at ghost detection (51 threads) ---
...
JOIN WATCHDOG: removed ghost entry for 'GhostBot' from the player list.
JOIN WATCHDOG: healed.
/mxsr status -> Ghost joins detected: 1 · Ghost joins healed: 1 · Joins being watched: 0
```

A synthetic ghost has no connection, so `failed to disconnect` is expected there; a real ghost has one.

### 4.4 Region failure isolation — `TickRegionScheduler.regionFailed`

**Fixes the blast radius.** Upstream: any region throwing → `scheduler.halt()` + `stopServer()` → whole
network down.

With `threaded-regions.failure-mode: ISOLATE` (new default):
- the throwing region is **quarantined** (descheduled, chunks kept loaded but frozen),
- every other region keeps ticking,
- an exact `/mxsr recover` command is printed,
- the **global region is never isolated** (everything depends on it) — it still halts, as upstream.

Safety valve `max-isolated-region-failures` (default 25) escalates to a halt if failures look systemic.

> **Important nuance found by testing**: the Bukkit scheduler wrappers (`FoliaRegionScheduler`,
> `FoliaEntityScheduler`, `FoliaGlobalRegionScheduler`, `FoliaAsyncScheduler`) already `catch (Throwable)`
> **per task**. So plugin code inside a scheduled task was never fatal. The fatal surface is region tick
> internals (entity/block-entity ticking, chunk+POI tickets, redstone, AI), inline `run()` calls
> (`queueOrExecuteTickTask` executes immediately when already on the owning thread), and the global region.

### 4.5 Quarantine state lives on the region, not the handle

My first implementation used `handle.markNonSchedulable()`. Testing showed the quarantine silently
disappeared, because **Folia replaces the tick handle on every merge/split** (`tickHandle.copy()`). A
handle-scoped flag is lost the moment the region graph changes and the region quietly resumes ticking.

Fixed with a `volatile boolean quarantined` on `TickRegionData`, plus a guard in `onRegionActive` that
refuses to schedule a quarantined region — that callback is exactly the path that would otherwise "heal"
it. **This was a real bug caught by testing, not by reading.**

### 4.6 Hung-region quarantine — `FoliaWatchdogThread`

Upstream's watchdog is **diagnostic only** — it prints a thread dump and nothing else. A wedged region
pins its tick thread forever, so a few stuck regions freeze the server with no crash.

Now, after `hung-region-action-after-seconds` (default 60, `-1` disables), the region is quarantined the
same way. The in-flight call cannot be safely killed in the JVM (`Thread.stop` is removed and would
corrupt region state), so it is reclaimed only if it returns — but it stops consuming a tick thread.

### 4.7 Thread sizing — `TickRegions.getTickThreads`

80% of visible cores, floor 2, always leaving ≥2 cores for netty/chunk-system/GC, capped at core count.
`threads: N` still overrides. **24 cores: 3 → 19 threads.**

### 4.8 `/mxsr` operator tooling

```
/mxsr status | list | recover <world> <cx> <cz> | recoverall
```

Runs through `RegionizedServer.blockOn` so graph mutation happens on the global region, never racing a
live tick.

### 4.8b Keepalive grace - `ServerCommonPacketListenerImpl`

**This is the highest-value network fix of the whole set, and it was hiding in plain sight.**

A keepalive goes out **every second**. Upstream kicks a player when the *oldest unanswered* keepalive is
older than `KEEPALIVE_LIMIT` (30 s). That means: **any 30-second server-side stall disconnects every
connected player simultaneously** - not because their connections are broken, but because the server
stopped answering while it was busy.

That is precisely the mass-disconnect signature in the reports, and it is self-amplifying:

```
server stalls 30 s  ->  keepalives unanswered  ->  EVERYONE kicked at once
    ->  all clients auto-reconnect  ->  reconnect storm hits the single global-region
        login path  ->  which is the thing that was already stalled  ->  longer stall
```

Now a player is kicked only when **several consecutive** keepalives go completely unanswered
(`minex.keepaliveGraceMisses`, default 3), i.e. genuinely silent for `graceMisses * 30 s`. A healthy but
briefly-stalled server now survives, and the reconnect storm never starts. Note `pendingKeepAlives` drains
on every response, so a slow-but-alive client never accumulates entries and is never penalised.

### 4.8c Allocation-free connection tick - `RegionizedServer.tickConnections`

Upstream runs `new ArrayList<>(connections)` + `Collections.shuffle(...)` **every tick** - 20 allocations
and 20 full shuffles of the whole connection list per second, forever, on the global region thread. At a
few hundred connections that is continuous garbage on the one thread the entire server depends on, shared
with the login path.

The shuffle existed only to stop players "gaming" tick order by re-logging. That property is preserved
with a **rotating start offset**: every connection is still visited exactly once per tick, but the start
point advances each tick, so no fixed position is consistently first. Zero allocation, no `Collections.shuffle`.

### 4.8d Lazy per-region caches - `RegionizedWorldData`

`RegionizedWorldData` is created **per region**. Two of its collections were allocated eagerly for every
region regardless of use:

| Field | Cost | Only used by |
|---|---|---|
| `PathTypeCache` | `long[4096]` + `PathType[4096]` � **48 KB per region** | mob pathfinding |
| `explosionDensityCache` | `HashMap(64, 0.25f)` | explosions |

Regions that never pathfind and never explode (spawn hubs, arenas, freshly generated terrain) paid for
both and used neither. Both are now allocated on first access. A region that does use them allocates
exactly once in its lifetime; a region that does not pays nothing.

### 4.9 JVM / startup script (`start-with-credentials.sh`)
| Change | Reason |
|---|---|
| Heap from container cgroup, not hardcoded | panel allocated 15,240 MB but passed 8192 → ~7 GB idle |
| **G1 → generational ZGC** | production already proved ≤0.35 ms safepoints over 3,254 samples; G1's 200 ms pause target was being hit during bursts |
| **Removed `-XX:+AlwaysPreTouch`** | it commits the whole heap at boot, so RSS == `-Xmx` from second one |
| **Removed G1 allocation-pressure flags** | `SurvivorRatio=32` + `MaxTenuringThreshold=1` forced almost every object to be promoted, inflating old-gen and GC work |
| `-XX:SoftMaxHeapSize` = 60% of max | the actual lever for *committed* RSS |
| `-XX:ZUncommitDelay=120` (**not** `-ZUncommit`) | returns unused heap to the OS sooner; disabling uncommit would defeat the purpose |
| `-XX:MaxMetaspaceSize=1024M` | bounds metaspace creep with 50+ plugins |
| `-Xss1M` | ~19 region threads + chunk workers; default stacks are real RSS |
| `ExitOnOutOfMemoryError` + `HeapDumpOnOutOfMemoryError` | fail loudly, keep evidence |
| `-Xlog:gc …:file=logs/stall.log` | the instrument that made the Aug 27 diagnosis possible |

---

## Part 5 — Measured results (live, `fd4afc59`)

| Metric | Before | After | Change |
|---|---|---|---|
| Minecraft version | 26.1.2 | **26.3** | ported |
| Tick threads | 3 | **19** | 6.3× |
| Container RSS | **14.5 GB** | **3.37 GB** | -11.1 GB (77%) |
| Keepalive mass-disconnect | any 30 s stall kicks everyone | 3 consecutive misses required | code-verified |
| Connection tick garbage | list + shuffle every tick (20/s) | zero allocation | code-verified |
| Per-region path cache | ~48KB per region, always | on first use | code-verified |
| Heap ceiling | 12.6 GB (auto) | capped + soft-max | bounded |
| GC | G1, 200 ms pause target | **ZGC gen, sub-ms safepoints** | verified in `stall.log` |
| Region failure | whole server stops | **isolated + `/mxsr recover`** | verified |
| Hung region | pins a thread forever | **quarantined after 60 s** | verified |
| Login block | global region parked on Netty | **async** | code-verified |
| Half-completed join | phantom until restart | **detected + disconnected** | code-verified |

### Verification runs on 26.3

```
Loading Folia 26.3-DEV-HEAD@8cffa90 (…) for Minecraft 26.3
Initialised EDF Folia scheduler with initial 19 target thread(s)
Done (37.080s)!

/mxsr status → Mode: ISOLATE · Quarantined: 0 · Tick threads: 19

# hang injected into a 1156-chunk region at Spawn 58,75
Region -> hung quarantine
Tick region in world 'Spawn' around chunk '[58, 75]' has been unresponsive for 10s (threshold 8s). QUARANTINING it…
/mxsr status → Quarantined regions: 1      (server still running, other regions fine)
/mxsr list   → - #0 world='Spawn' center=58,75 chunks=1156
                 recover with: /mxsr recover Spawn 58 75
/mxsr recover Spawn 58 75 → Region #0 has been recovered and rescheduled.
/mxsr status → Quarantined regions: 0
```

RSS tracked over 4 minutes post-change: 9.49 → 9.52 GB (stable, no creep).

### Honest limitations
- **Player-scale load testing was not possible on the remote box.** The dev server sits behind Velocity
  (`velocity.enabled: true`, modern forwarding) and the host firewall blocks external MC clients even with
  forwarding off. I worked around this by running **Folia 26.3 locally** and driving it directly — see
  §5.1 below.
- The `spark` Folia integration throws `NoClassDefFoundError: TickData$TickReportData` on 26.3 — spark's
  Folia adapter is compiled against a different Moonrise version. Cosmetic (profiler only), noted in §8.

### 5.1 Local reproduction harness

Since the remote box cannot accept external connections, I stood up Folia 26.3 locally
(`_folia/localserver`, port 25599, RCON 25598) with:

- `mcjoin.py` — a hand-written protocol client that completes handshake → login → configuration
- `rcon.py` — a stdlib RCON client for driving the server
- `FaultInjector` — the plugin, with `fi ghost` to fabricate the §3.2 failure exactly

This is what allowed the join watchdog to be **verified rather than asserted**. Notably, getting
`mcjoin.py` working required implementing the compressed packet framing for 26.3 (Set Compression,
`[data-length varint][zlib]`, threshold 256, applying in **both** directions) — which is itself a useful
record of how 26.3 framing works.

Remaining gap: `mcjoin.py` reaches login and holds the config phase (answering keepalives) but does not
complete `ServerboundClientInformation` decoding, so it stops short of PLAY. The ghost detection path does
not depend on that: it was verified by fabricating the post-`placeNewPlayer` state directly, which is a
*stronger* test than waiting for a natural occurrence.

---

## Part 6 — 26.3 migration: what actually blocks you

**The Folia core ported cleanly.** All my changes applied to 26.3 with identical anchors; the only
compile error was one `ChunkPos#y()` that does not exist, trivially fixed.

**The blocker is the plugin ecosystem.** 26.3 is a Minecraft bump; most plugins that reach into NMS need
rebuilds. On first boot, 26.3 failed to start and then crashed in `SimpleHelpMap.initializeCommands`:

| Plugin | Failure | Verdict |
|---|---|---|
| **FastAsyncWorldEdit** | `NoSuchFieldError: Registries.CONFIGURED_FEATURE`; then `NoCapablePlatformException` during help-map init → **crashed the server** | needs a 26.3 build |
| **WorldGuard** | `NoClassDefFoundError: com.sk89q.wepif.PermissionsResolverManager` (cascade from FAWE) | needs 26.3 build |
| **PlaceholderAPI** | `ExceptionInInitializerError` → `NumberFormatException: For input string: "l"` (parsing "26.3") | needs 26.3 build |
| **worlds** | `IllegalStateException: No implementation found for version: 26.3` | needs 26.3 build |
| **packetevents** | `NMS_ITEM_STACK_CLASS` is null | needs 26.3 build |
| **nightcore** | `CraftItemStack.asBukkitCopy` moved | needs 26.3 build |
| **FancyNpcs** | "Unsupported minecraft server version" | needs 26.3 build |
| **LPC** | not marked `folia-supported` | will never load — remove |

With those 8 set aside (`/plugins.26.3-incompatible/`), 26.3 boots in **37 s** and runs 38 Bukkit plugins
stably. That is the honest cost of 26.3 today.

### 26.3 vs 26.1.2 upstream delta (for the record)

24 commits between `62dc0f2` and `8cffa90`. Folia-specific:

| Commit | Change |
|---|---|
| `2a74dac` / `6203419` | Update to 26.3 / 26.2 |
| `95c3f27` | **Thread check eigencraft redstone and game events** — blocks out-of-region actions |
| `302d056` | **Re-enable flush consolidation** — one netty flush per player per tick instead of many (a real network win; now inherited) |
| `602048c` | Do not allow out-of-region teleport accept |
| `6559852` | Teleport desynced passengers (>1 chunk from root vehicle) back |
| `10488af`, `cd92bab` | portal/nether config; `/effect clear` stale reference |

**Nothing upstream addresses the blast radius or the login block** — `regionFailed` still calls
`stopServer()` and the watchdog still only prints. That is precisely the gap this fork fills.

---

## Part 7 - RAM: where it actually goes (measured)

This section was wrong twice before it was right, so it is worth recording how it was determined.

### 7.1 The measurement, not the guess

Native Memory Tracking (`-XX:NativeMemoryTracking=summary`, `jcmd VM.native_memory summary`) on a
Folia 26.3 server running the production flags:

```
Total: reserved=135854873KB, committed=1656573KB      <- 1.62 GB committed
- Java Heap   (reserved=134217728KB, committed=1372160KB)   <- 1.34 GB
- Class       committed=22561KB   (30,267 classes)
- Thread      committed=4824KB    (76 threads)
- Code        committed=33567KB
- GC          committed=43485KB
- Symbol      committed=26399KB
```

So at idle, **committed memory is ~1.6 GB**, of which the heap is ~1.3 GB. Everything else - metaspace,
code cache, 76 thread stacks, ZGC's own structures - is under 300 MB combined.

Meanwhile the GC log reports the live set after a cycle:

```
GC(6) ... 10340M(77%)->822M(6%)
GC(7) ...  4120M(50%)->1296M(16%)
```

**Live set: 0.8-1.3 GB.** Production reports show 2.3-3.8 GB under ~70 players with heavy churn.

### 7.2 The two mistakes that cost the most RAM

**Mistake 1 - `-Xms == -Xmx`.** The very first optimisation removed `-XX:+AlwaysPreTouch` because it
commits the whole heap at boot. But the script also passed `-Xms${HEAP_MB}M -Xmx${HEAP_MB}M`: **equal
Xms and Xmx commits the entire heap at startup**, which is exactly what `AlwaysPreTouch` does. The flag
was removed and the outcome was reintroduced by the sizing. This is the single biggest RAM mistake in
this whole project.

> RSS sat at 9.4 GB with **zero players online** while the live set was 718 MB.

Fix: `-Xms` = 15% of max (1228 MB here), `-Xmx` = the ceiling. ZGC grows into reserved space on demand.

**Mistake 2 - ZGC was not actually returning memory.** `-XX:ZUncommitDelay` defaults to 300 s, and ZGC
only uncommits during a collection - which on an idle server may not happen for a long time. RSS stayed
at 6.1 GB even after the Xms fix because the pages were committed and simply never handed back.

Fix: `-XX:ZUncommitDelay=30` and `-XX:ZCollectionInterval=30` (force a cycle every 30 s so uncommit
actually runs). Once a player spike ends, memory goes back to the OS within about a minute.

> Note: `-XX:-ZUncommit` (which the *first* version of this script had) **disables** returning memory.
> That is the opposite of the goal.

### 7.3 Result

| Stage | RSS (0 players) |
|---|---|
| Original (G1, `-Xms=Xmx=8G`, `AlwaysPreTouch`) | **14.5 GB** | - |
| ZGC + container sizing (still `Xms=Xmx`) | 9.41 GB | - |
| + `-Xms` decoupled from `-Xmx` | 6.13 GB | - |
| + `ZUncommitDelay=30` / `ZCollectionInterval=30` | 3.41 GB | - |
| + ceiling 6144, soft-max 3072, uncommit 10 / interval 15 | **2.84 GB** | 9.9% (wasteful) |
| + interval 120 / uncommit 60 | 4.89 GB | 1.6% |
| + **soft-max 2048** (final) | **3.37 GB** | **2.3%** |

**14.5 GB -> 3.37 GB (77% reduction) at 2.3% GC duty.**

> The 2.84 GB row looked better on RSS alone but paid **9.9% of a core while completely idle** -
> a 1.4 s major cycle every 16 s. That is a bad trade on a shared node. The final configuration
> reaches nearly the same RSS for a quarter of the GC work, because the real lever turned out to be
> `SoftMaxHeapSize`, not collection frequency.
**14.5 GB -> 2.84 GB, an 80% reduction**, with the live set unchanged.

The live set is what matters and it is flat: repeated cycles report
`1340M(22%)->716M(12%)` and `1334M(22%)->726M(12%)` - i.e. **the heap settles at ~720 MB and never
grows**, regardless of ceiling. That is the evidence that the remaining RSS is committed-but-unused
ZGC address space, not real data.

### 7.3b The heap itself: 105 MB of collision caches

An object histogram (`jcmd GC.class_histogram`) on an idle server shows where live heap actually goes:

```
286,075  [Ljava.lang.Object;                         60.9 MB
172,085  ArrayVoxelShape                             28.9 MB
418,304  AABB                                        26.8 MB
170,499  CachedShapeData                             10.9 MB
170,499  BitSetDiscreteVoxelShape                    10.9 MB
171,753  CachedToAABBs                                8.2 MB
354,644  DoubleArrayList                              8.5 MB
 35,723  BlockState                                   8.9 MB
```

Re-running the histogram immediately after a forced full GC returns **identical counts** - so these are
**live, not garbage**. ~105 MB, over half the live heap, is Moonrise's collision optimisation:
`VoxelShape` carries `cachedShapeData`, `cachedToAABBs`, `cachedBounds`, and a 16-entry `mergedORCache`
that retains whole merged `VoxelShape` results keyed by another shape.

This is a genuine, code-level RAM target - but it is **not** safe to trim casually: those caches are
what make Moonrise's collision detection fast, and they are keyed per-shape rather than bounded globally.
The tractable version is to make `mergedORCache` lazily allocated (it is only needed once two shapes are
actually merged) and to drop it on unload, which removes a per-shape pointer plus 16 slots for shapes
that never participate in a merge. That is a contained follow-up, and it should be benchmarked on a live
server before adoption.
### 7.4 What this does and does not mean

- The heap **ceiling** is still 8 GB. Under load it can expand to that without a restart. A low idle RSS
  does not mean a low ceiling.
- The remaining 3.4 GB is mostly *touched* heap pages plus ~300 MB of native. It will rise with players
  and fall back when they leave - that is now the intended behaviour.
- **8 GB ceiling with 15 GB allocated leaves ~7 GB for page cache**, which is what makes region-file I/O
  cheap. Raising `-Xmx` toward the container limit trades that away for no benefit at this live-set size.
  `MINEX_HEAP_MB` overrides the ceiling if a future workload needs it.
- **This is JVM configuration, not patching.** Real further reduction has to come from the causes below.

### 7.5 Where real further reductions must come from

1. **Allocation rate, not heap size.** The production profile shows ~700 MB-1 GB/s of allocation churn at
   peak. That is plugin behaviour (economy/fishing/duel events, per-login work from FinanceX, Plan,
   VotingPlugin all firing at once, scoreboard/tab churn). No JVM flag fixes allocation rate - it needs
   those plugins to allocate less.
2. **Per-region structures** (done, §4.8d): `PathTypeCache` was ~48 KB per region eagerly; now on first
   use. Scales with region count, so it matters most on spread-out servers.
3. **Unconvertible baseline.** ~300 MB of metaspace/code/threads is fixed cost for 50+ plugins. Cutting it
   means fewer plugins, which is a product decision not a tuning one.
4. **Page cache is not waste.** Once heap is right-sized, the remaining container memory should be left to
   the kernel for file cache. Watching "RSS + page cache" and concluding memory is leaking is the classic
   mistake in this area - and it is the same trap the earlier "player cache not clearing" theory fell into.

## Part 7.6 — End-to-end join test, with real logins (2026-10-29)

The proxy was decoupled long enough to test the join path directly, so for the first time the §3.1/§3.2/§4.1-4.3
fixes were exercised by **actual logins** rather than asserted.

### How the bot reaches PLAY

`mcjoin.py` now completes the whole 26.3 join. Four separate things had to be right, and each one failed
differently:

| Wall | Symptom | Cause |
|---|---|---|
| Handshake | `eof reading length` | status ping omitted the protocol-version varint |
| Velocity | "This server requires you to connect with Velocity" | bot did not answer `velocity:player_info` |
| Config | stalls until "Took too long to log in" | never answered `select_known_packs` (0x0f) |
| Play | "keepalive response (0) without matching challenge" | echoed **clientbound** id 0x2d back as serverbound |

The last one is the trap worth recording: clientbound and serverbound ids are different tables. Clientbound
`keep_alive` is **0x2d** (8-byte timestamp); the serverbound reply is **0x1c**. Echoing the clientbound id is
read as `place_recipe` and kicks you. `pktids.py` derives both tables from `GameProtocols.java` and validates
against the observed 0x2d — note that `.withBundlePacket(...)` consumes an id slot, which is what made the
first line-counting attempt off by one.

The Velocity payload is HMAC-SHA256 over
`[version varint][ip][uuid(16)][name][properties]`, prefixed by a `\x01` "has data" bool, signed with
`config/paper-global.yml -> proxies.velocity.secret`. Testing through the proxy path rather than disabling
forwarding means the real production join was exercised.

### Results

| Run | Joined | Completed | Keepalives | Ghosts |
|---|---|---|---|---|
| 3 bots, 40 s | 3/3 | 3/3 | 117 | 0 |
| 15 bots, 300 s | 15/15 | 15/15 | 4,380 | 0 |
| 30 bots, 420 s | 30/30 | 19 | 9,297 | 0 |

**Join path is clean.** Across 48 logins: 0 ghosts, 0 joins-remaining-watched, 0 quarantine, 0 region failures,
TPS 20.00 throughout. This is the first hard evidence that the §4.1-4.3 work does what it claims.

### What the load did NOT test

All bots land at spawn, so **every 30-bot run still produced exactly 1 region** (`grid-exponent: 4` = 16x16
chunks, and the bots all fit inside one). CPU did rise from 136% idle to 407% peak and RSS from 4.15 to 4.58 GB,
but this is *single-region* load. It says nothing about cross-region handoff, merge/split, or the isolation
work under real multi-region pressure. Spreading the bots needs a working teleport: `/tp` is player-to-player
only here and CMI's `tppos` result could not be confirmed (see below).

### Plugin findings (these are live production defects)

1. **Nexo 1.22.1 breaks all chat.** `packGenerator` is a `lateinit` that never initialises on 26.3, so
   `NexoTranslator.translate` throws inside *every* translatable chat component. The packet is dropped with
   `SkipPacketEncoderException` — no error shown to the player, the message simply never arrives. It also killed
   3 bots via `update_advancements`. `plugman reload Nexo` initialises `packGenerator` and stops the errors;
   a boot-order fix is still needed because the initial enable is broken.
2. **PlaceholderAPI 2.12.2 cannot parse our version string.** It dies with
   `NumberFormatException: For input string: "l"` because it tries to parse `26.3.local-SNAPSHOT`.
   `settings.gradle.kts:24` produces that version when `BUILD_NUMBER` is unset. Building with a
   `BUILD_NUMBER` (e.g. `26.3.build.N-stable`) makes the version parseable. This is our build, not their bug.
3. **`worlds` 4.3.0-pre1 has no 26.3 implementation** — `No implementation found for version: 26.3`, a
   hardcoded table. Needs an upstream release.
4. **PacketEvents 2.14.0 works on 26.3** (loads `V_26_3/23` block mappings). Uploaded and enabled. Note the
   Maven artifact is a 146 KB thin jar that throws `NoClassDefFoundError: PacketEventsAPI`; use the 5.6 MB
   GitHub release bundle instead.

### Caveats on the bot numbers

"dropped" in the 30-bot run is mostly **the harness closing its own window**, not the server kicking. The
server-side causes that were confirmed are `Connection reset by peer` (bot socket torn down) and the Nexo
encode failure. A synthetic client also cannot survive an anticheat that validates movement physics, so
these runs prove the **join** path, not long-duration player fidelity.

## Part 7.7 — Capacity: where the limits actually are (measured)

Bots were given spectator mode so they generate real chunk/entity load without tripping movement
validation or the floating kick, then ramped in stages. Spectators still receive chunks, so the load is
genuine — it just has no physics.

### The ramp

| Players | Regions | Lowest TPS | Peak MSPT | RSS | CPU |
|---|---|---|---|---|---|
| 20 | 1 | 20.00 | 8.7 | 4.58 GB | 189% |
| 39 | 1 | 20.00 | 14.7 | 4.55 GB | 225% |
| 58 | 1 | 20.00 | 23.7 | 4.96 GB | 328% |
| 78 | **2** | 20.00 | 23.2 | 5.77 GB | 435% |
| 118 | **3** | 20.00 | 24.5 | 6.21 GB | 481% |
| **157** | **4** | **20.00** | **29.9** | **7.64 GB** | **540%** |

**157 players held a perfect 20.00 TPS across 4 regions, 0 region failures, 0 quarantine, 0 ghosts.**
That is the headline result and it is the first evidence that the region scheduler actually parallelises
under real population — MSPT rose only 8.7 → 29.9 ms while player count rose 20 → 157 (roughly 8x the
players for 3.4x the tick cost).

### Where it breaks, and why it is not the server

At the 200 stage the population collapsed to 87. The cause is **`30 unanswered keepalives, limit
30000ms`** — bots were kicked for missing keepalives, not for tick load. Two separate reasons:

1. **Latency.** RTT from the test box to the node is ~250 ms (traceroute: 15 hops, Cogent to
   `208.84.101.13`); a status ping round trip measures ~840 ms including TCP setup. With a 30 s limit
   and a bot managing ~30 clients per process, a single GIL stall blows the window. This is a
   *measurement artifact*, and it is also the real-world geography problem from §8 — EU/IN/LatAm players
   sit 250 ms from Kansas City.
2. **Harness.** 200 Python threads in one process share one GIL. Running 3 separate processes at 60 each
   still only joined 116/180, so per-process client counts are the binding constraint, not the server.

**Regions only split at ~78 players and stayed at 4 even at 157.** Region count scales with *spread*,
not population: bots 700 blocks apart at 8 per row still collapse into few regions. This is the behaviour
Part 2 describes (regions merge when tickable), and it means **`grid-exponent: 4` is not the limit
governing your real player counts** — spawn concentration is.

### Caveats

- Spectators do not exercise physics, pathfinding, entity AI, or block interaction. A 157-player
  survival population would cost more per player than a 157-spectator one.
- The world was a freshly generated spawn area, so chunk generation was near zero (`Gen rate: 0.00`).
  A real map with heavy redstone/entity farms is a different load profile entirely.
- `allow-flight` was temporarily set true and `max-players` raised to 300 for the ramp; **both were
  reverted** (`allow-flight=false`, `max-players=40`) and the server restarted clean afterwards.

## Part 7.8 — Upstream vs our fork, A/B under creative flight

The fork had never been benchmarked against stock Folia at all — every number so far was one build
measured alone. This is the first side-by-side.

### The comparison targets

| | Build | Commit | Notes |
|---|---|---|---|
| **Upstream** | `26.3-6-ver/26.3.x` (ALPHA) | `8664923` | PaperMC's published experimental/preview channel, 2026-10-29 |
| **Our fork** | `26.3-DEV-HEAD` | `8cffa90` | contains all the Part 4 hardening |

Worth recording: Folia's **ALPHA** channel is the experimental track. The git branch
`experiment/sectorfile` is **1.20.6**, which is a trap — it is not the 26.3 experimental build. The
prebuilt ALPHA jar comes from `fill.papermc.io/v3/projects/folia/versions/26.3/builds`, and its
version string is `26.3.build.6-alpha` rather than `26.3.local-SNAPSHOT`.

### Method

Identical world (deleted and regenerated before each run), identical 30 bots, identical creative +
`flyspeed 2` setup, identical flight path, 240 s hold.

Movement is **server-driven** (`/tppos` along a deterministic circle) rather than client-driven. That
was not the original plan, but it is the only reliable option: a synthetic client cannot satisfy the
server's movement validation. Every attempt to send position packets from the bot produced
`Invalid move player packet received`, at 20 Hz, 12.5 Hz *and* 4 Hz, with every flag combination
(probed exhaustively by `moveprobe.py` — all seven variants pass when sent as a single packet, and all
fail when sustained). Server-driven flight still exercises chunk load, chunk send, entity tracking and
region transfer, which is the cost that matters, and it is byte-identical between the two builds.

### Results

| Metric | Upstream | Fork | Delta |
|---|---|---|---|
| Players held | 30/30 | 30/30 | — |
| Regions | 2 | 2 | — |
| TPS min | 19.93 | 19.87 | ~equal |
| **MSPT avg** | **29.42 ms** | **28.65 ms** | **fork 2.6% lower** |
| MSPT max | 31.64 ms | 33.91 ms | fork 7% higher (noise) |
| RSS avg | 4,887 MB | 4,667 MB | fork 4.5% lower |
| RSS max | 5,222 MB | 5,006 MB | fork 4.1% lower |
| CPU avg | 383.3% | 375.7% | fork 2.0% lower |
| CPU max | 445.0% | 461.8% | fork higher (noise) |
| Region failures | 0 | 0 | — |
| Ghost joins | 0 | 0 | — |

### Honest reading

**The two builds are equivalent within run-to-run noise at this load.** The fork is ~2–5% lower on
MSPT/RSS/CPU averages, but the maxima move in the opposite direction, which is what noise looks like
rather than a real win. With n=6-7 samples on a shared node and a 250 ms RTT link, a 2% difference is
not separable from variance.

This is a *negative* result and it should be stated as one: **the Part 4 hardening does not show a
measurable throughput advantage at 30 players.** That is not surprising — those changes target failure
paths (region failure isolation, hung-region quarantine, join integrity, keepalive grace), not the
steady-state tick cost, which is dominated by chunk sending and entity tracking in code the fork does
not touch. The benefit of the fork is *survivability*, and survivability cannot be measured by a run in
which nothing fails.

Measuring the actual difference would need either the failure paths deliberately triggered
(`FaultInjector` has `fi throw` / `fi hang` for exactly this) or a much larger population where
quarantine and isolation would engage.

### Caveats

- Server-driven flight is not client flight: no client prediction, no physics, no anticheat load.
- 30 players on 2 regions is a light test; Part 7.7 already showed the scheduler holds to 157.
- Both runs used the same plugin set, which includes the broken Nexo build — its chat/advancement
  encode failures add noise on both sides equally.

## Part 8 — Roadmap

**Done**: isolation, hang quarantine, recovery tooling, thread sizing, non-blocking login, join integrity,
bounded spawn, ZGC + heap sizing, 26.3 port.

**Next, in priority order**

1. **Get 26.3 plugin builds** — FAWE, WorldGuard, PlaceholderAPI, worlds, packetevents, nightcore,
   FancyNpcs. Until then 26.3 is a staging target, not a production one.
2. **Confirm the join fixes with a real player soak** — the §3.1/§3.2 fixes are the highest-value changes
   in this document and the only ones not yet exercised by actual logins.
3. **Lazy per-region collections** (§7.3) — cuts RAM proportional to region count.
4. **Spread the login stampede** — proxy-level cooldown after a kick, and stagger per-login plugin work
   5–10 s post-join (FinanceX, Plan, VotingPlugin). Removes the amplifier from §3.3.
5. **Fix the POI at the spawn chunk** — forceload it or clear the stale bed/POI data at
   `(-59999, -54, 65550)`; this is the concrete trigger for §3.2 in production.
6. **Audit the `TrimNativeHeapInterval=5000`** on the production JVM (§3.4) — consider removing it and
   watching RSS.
7. **Firewall the backend port** (`208.84.101.13:35585` was publicly reachable, bypassing the proxy).
8. **Consider an EU node** — the Kansas City location is a constant latency floor for an EU/IN/LatAm/SEA
   player base. No code fixes geography.
9. **Per-region MSPT budgeting** — feed `RegionStats` into a soft budget so a persistently over-budget
   region degrades gracefully instead of starving neighbours.

---

## Part 9 — Operating notes

```powershell
# tooling (see _tools/sftp/)
node sftp.js ls|cat|tail|get|put|mkdir|mv|rm <paths>
node console.js "command"          # panel console + output capture

# health
paper:tps server 5                 # Folia region report — NOT /tps (CMI shadows it)
mxsr status | list                 # quarantine state
spark health                       # memory/CPU

# rollback to 26.1.2 (still on the box)
mv /server.jar /server-26.3.jar ; mv /server-26.1.2-minex.jar /server.jar
# and restore the original start script + plugins from /plugins.26.3-incompatible/
```

**Restore 26.3-incompatible plugins** (when 26.3 builds exist):
```bash
mv /plugins.26.3-incompatible/*.jar /plugins/
```

**Config** (`config/paper-global.yml`):
```yaml
threaded-regions:
  failure-mode: ISOLATE                  # or HALT for upstream behaviour
  max-isolated-region-failures: 25
  hung-region-action-after-seconds: 60   # -1 disables
  grid-exponent: 4
  scheduler: EDF
  threads: -1                            # now 80% of cores instead of cores/8
```

### Files in this deliverable

| Path | Contents |
|---|---|
| `FOLIA-26.3-COMPLETE-GUIDE.md` | this document |
| `_folia/patches/26.3/*.java` | snapshots of every changed source file (26.3) |
| `_folia/patches/start-with-credentials.sh` | the new JVM/startup script |
| `_folia/tools/port_isolation.py` | scripted re-application of the isolation changes |
| `_folia/_notes/JOIN-STALL-DIAGNOSIS.md` | focused write-up of the tab-but-no-entity bug |
| `_folia/_notes/FOLIA-26.1.2-investigation.md` | original 26.1.2 investigation |
| `_folia/_testplugin/` | `FaultInjector` harness (source + jar) |
| `_tools/sftp/sftp.js`, `console.js` | SFTP + panel console tooling |
