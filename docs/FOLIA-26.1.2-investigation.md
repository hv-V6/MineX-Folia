# Folia 26.1.2 — Investigation Notes (working file)

Thread: thr_4gfyec6ruc · Goal: study Folia 26.1.2 + 26.3, document, find stability/perf wins, test on `fd4afc59`.

## Pinned sources

| What | Ref |
|---|---|
| Folia stable | `ver/26.1.x` @ `62dc0f257a4f5de1ef2eae8cf1627156a769c67f` ("Move Maven repository to release and update README (#455)", 2026-05-06) |
| Folia experimental | `ver/26.3.x` @ `8cffa9059ad6bbc2b28510c38ed61a51098d4971` ("Update Paper", 2026-10-20) |
| Paper upstream (stable) | `paperRef=03af8b42915773171808f20fe01b763f847a0d7d` |
| Deployed on `fd4afc59` | `26.1.2-8-62dc0f2` — **same commit** as `ver/26.1.x` tip |
| Version reported by my build | `folia-26.1.2-DEV-HEAD@62dc0f2` |

Note: `playerdeath`/`76e4bbf` style "26.1.2 build 8" is build-number metadata only; the tree at `62dc0f2` is the same.

## Source shape

Folia is **patch-based**: it does not vendor Paper. Layout:

```
folia-server/minecraft-patches/features/0001..0008   (NMS + Moonrise patches)
folia-server/paper-patches/features/0001..0007       (Paper-layer patches)
folia-api/paper-patches/features/0001..0004          (API patches)
```

`0001-Region-Threading-Base.patch` is the whole thing: **20,206 diff lines, +11,690 / -1,752, 198 files**,
of which only **13 are new files** (~5.2k lines). Everything else is invasive surgery on vanilla/Paper.

New files (all under `io/papermc/paper/threadedregions/`):

| File | Lines | Role |
|---|---|---|
| `ThreadedRegionizer.java` | 1405 | region graph: sections, merge/split, section bitsets |
| `RegionizedTaskQueue.java` | 863 | per-region priority task queues + chunk tickets |
| `RegionizedWorldData.java` | 768 | per-region world state (entities, ticks, connections) |
| `TickRegionScheduler.java` | 562 | EDF / work-stealing thread pool, tick handles, **failure handling** |
| `RegionizedServer.java` | 525 | global region: one "global tick" for the whole server |
| `TickRegions.java` | 480 | glue: region data, tick handle, region stats |
| `FoliaRegionScheduler.java` | 427 | Bukkit `RegionScheduler` API |
| `CommandServerHealth.java` | 354 | `/tps` (aliased `paper:tps`) |
| `RegionizedData.java` | 235 | region-scoped data dispatch w/ merge+split callbacks |
| `RegionShutdownThread.java` | 229 | graceful shutdown from any region |
| `CommandUtil.java` | 120 | colours |
| `TeleportUtils.java` | 82 | async teleport across regions |
| `util/ThreadLocalRandomSource.java` | 73 | per-thread RNG |
| `util/SimpleThreadLocalRandomSource.java` | 79 | per-thread RNG |

Build: **JDK 25 toolchain**, Gradle 9.4.1, paperweight-patcher 2.0.0-beta.24.
`./gradlew applyAllPatches` ≈ 14 min; `./gradlew createBundlerJar` ≈ 5 min → 96 MB bundler jar.

## Architecture (verified from source)

### Region = a set of "sections"
- `grid-exponent: 4` → region section = 2^4 = **16×16 chunks** (256 chunks/section).
- `ThreadedRegionizer` keeps `sections` (SWMR long→section) and `regionsById`.
- A `ThreadedRegionSection` holds a **chunk bitset** (`long[8]` for 16×16) and `nonEmptyNeighbours`.
- Adding a chunk creates empty *neighbour* sections within `emptySectionCreateRadius` to keep the
  adjacency invariant, then **merges** all nearby regions that aren't `STATE_TICKING`.
- Regions are **recomputed (BFS split)** when a ticking region is released and
  `deadSectionPercent >= maxDeadRegionPercent` (or all sections dead).
- So region membership is dynamic: every chunk unload can trigger a merge/split under a
  **global `StampedLock` write lock** over the whole world's region graph.

### State machine
`STATE_TRANSIENT(0) → STATE_READY(1) → STATE_TICKING(2) → STATE_DEAD(3)`
Only one thread may tick a region; a ticking region **cannot be merged into** (`tryKill()` returns false),
it becomes `mergeIntoLater` and merges when it next releases the tick.

### Scheduling
- `TickRegions.getScheduler()` = `TickRegionScheduler`, either `EDFSchedulerThreadPool` or
  `StealingScheduledThreadPool` (config `threaded-regions.scheduler`, default/here: `EDF`).
- `RegionScheduleHandle.runTick()` → marks region ticking → `tickRegion()` →
  `MinecraftServer.tickServer(startTime, scheduledEnd, targetBuffer, region)`.
- After the tick, `tickSchedule.advanceBy(tickCount, 50ms)` and
  `setScheduledStart(max(tickEnd, idealNext))` — i.e. **regions that overrun self-throttle**.

### Thread budget (this is a big one)
```java
// TickRegions.getTickThreads(config)
if (config.threads <= 0) {
    tickThreads = OSNuma.getNativeInstance().getTotalCores() / 2;
    if (tickThreads <= 4) tickThreads = 1;
    else tickThreads = tickThreads / 4;
} else tickThreads = config.threads;
```
With `threads: -1` and a 24-core container → `24/2 = 12` → `12/4 = **3**`.
Confirmed live: `Initialised EDF Folia scheduler with initial 3 target thread(s)`.
**The `threads: -1` default is catastrophically conservative for a container.** With `cpu: 0`
(unlimited) a 15 GB box still only gets 3 tick threads. This is a config-level, zero-risk win.

### The global region
`RegionizedServer` has a single `GlobalTickTickHandle` (a `RegionScheduleHandle` with `region == null`).
`globalTick()` runs, once per tick, for the **entire server**:
- adventure click-callback queues
- `FoliaGlobalRegionScheduler.tick()`
- all world clocks
- `DedicatedServer.handleConsoleInputs()`
- player sample / server status rebuild
- **per world**: `drainGlobalChunkTasks`, worldborder, weather, sleep/night-skip, **raids**, sky brightness,
  game time, `chunkHolderManager.processTicketUpdates()`, map autosave
- `tickConnections()` — a shuffled walk of **every** `Connection` (netty tick)
- `playerList.tick()`

**This single task is single-threaded and gates every region**: everything scheduled on the global
region (commands, `GlobalRegionScheduler`, weather, worldborder, raids, ticket processing) shares one
thread. If it blocks — a plugin doing sync I/O in a global task, a slow console command, a huge
`processTicketUpdates` — **every region's ticket updates and all global state stall**, and the global
handle's watchdog fires "Global region has not responded".

## Blast radius: how one region takes down the server

### 1. Any exception in a region tick = whole-server shutdown (by design)
`TickRegionScheduler`:

```java
private void regionFailed(handle, executingTasks, thr) {
    this.scheduler.halt();                       // stop ALL region threads
    LOGGER.error("Region #"+id+" centered at chunk "+center+" in world '"+world+"' failed to " +
                 (executingTasks ? "execute tasks" : "tick") + ":", thr);
    MinecraftServer.getServer().stopServer();    // shut the server down
}
private void uncaughtException(thread, thr) {    // same for any uncaught throwable
    this.scheduler.halt();
    MinecraftServer.getServer().stopServer();
}
```
Called from `runTick()` and `runTasks()` `catch (Throwable)`. There is **no** per-region quarantine,
no "restart the region", no "kill the offending chunk/entity and continue". A single
`NullPointerException` from one plugin, on one entity, in one region → **entire server stops**.

This is the single biggest production-stability gap and it is exactly the reported symptom
("one region takes down the whole server with it").

Note `stopServer()` → `halt(false)` → spawns `RegionShutdownThread`, i.e. the shutdown path itself is
designed to be safe from a scheduler thread, but the outcome is still a full stop.

### 2. A hung region is never killed — only logged
`FoliaWatchdogThread` (`0008-Add-watchdog-thread.patch`):
- every 1s, for each `RunningTick` whose `lastPrint` is >5s old, prints
  `"Tick region located in world 'X' around chunk 'Y' has not responded in Ns:"` + a thread dump.
- It **never** interrupts, kills, unloads, or quarantines the region. `max-tick-time` /
  Spigot watchdog semantics (crash the server) do not apply per region either.

Consequence: a region stuck in an infinite loop or a blocking wait pins its tick thread **forever**.
With only 3 tick threads, **3 stuck regions = complete server freeze with no crash** — the classic
"server is up but nothing ticks, watchdog spams the log" failure. Also, `FoliaWatchdogThread` is only
started from a static initialiser on `TickRegionScheduler`; it is purely advisory.

### 3. Cross-region blocking in the tick thread
`RegionizedServer.blockOn(Runnable)` does `Completable.suppliedAsync(..., this::addTask).join()` —
a join from a region thread onto the **global** queue. If the global region is stalled, every caller
of `blockOn` stalls with it, and their regions with them.

`ServerChunkCache.getChunk` on a tick thread does:
```java
if (TickThread.isTickThreadFor(level, chunkX, chunkZ)) {
    ChunkTaskScheduler.pushChunkWait(level, chunkX, chunkZ);
    this.mainThreadProcessor.managedBlock(completable::isDone);   // can spin forever
    ChunkTaskScheduler.popChunkWait();
}
final ChunkAccess ret = completable.join();
```
`pushChunkWait` only records the wait so `dumpAllChunkLoadInfo` can *log* it. If a region blocks on a
chunk owned by a region that is itself blocked (A waits on B waits on A), the only observable is
`"Chunk wait task info below:"` — the server hangs.

### 4. Global-region coupling
Because `globalTick()` is one task, a plugin that does something slow in a global task (or a slow
console command) delays: weather, worldborder, **raids (all worlds)**, ticket processing (which gates
chunk status upgrades), the player sample, and `tickConnections` for every connection.
`processTicketUpdates()` being late means chunks that every region is waiting on upgrade late.

### 5. Forced sync saves on the region thread
`tickServer` runs per region:
```java
final boolean fullSave = autosavePeriod>0 && RegionizedServer.getCurrentTick() % autosavePeriod == 0;
if (region!=null) { playerList.saveAll(interval); level.saveIncrementally(false); }
```
Incremental chunk saving is done **on the region tick thread**, not on the I/O pool. With big
regions this is a periodic MSPT spike proportional to region size.

### 6. Region coalescing under load
Because regions merge when unowned sections appear and split only on region release,
players who walk toward each other end up in **one region on one thread**. Common SMPS with a
hub/spawn area nudge into "one giant region" — and that one region's MSPT is then the *entire*
experience for everyone in it, with no way to shed load.

## 26.1.2 → 26.3 experimental delta

24 commits between `62dc0f2` and `8cffa90`. Folia-specific ones:

| Commit | Date | Change |
|---|---|---|
| `2a74dac` | 26.3.0 | Update to 26.3 (Minecraft bump) |
| `6203419` | 26.2.0 | Update to 26.2 |
| `95c3f27` | 2026-08-19 | **Thread check eigencraft redstone and game events** — prevents out-of-region actions (correctness) |
| `302d056` | 2026-08-05 | **Re-enable flush consolidation** — netty flush batching was re-enabled (perf on player-dense servers) |
| `602048c` | 2026-07-17 | Do not allow out-of-region teleport accept (fixes passenger desync crash path) |
| `6559852` | 2026-07-17 | **Teleport desynced passengers to root vehicle** — every tick, if a passenger is >1 chunk from its root vehicle, teleport it back |
| `10488af` | 2026-10-14 | Respect `allow_entering_nether_using_portals` in async portal travel |
| `cd92bab` | 2026-10-14 | Fix `/effect clear <effect>` using stale entity reference |
| `14b7fee`, `f2ee872`, `57f643f`, `24c5c95`, `dfe4091`, `68b2af1`, `8afd916`, `05dca87`, `8cffa90`, `1ecebbe`, `e48800d` | | Paper upstream bumps |

Patch-set structural change: profiler shrunk (`0007-Region-profiler.patch` 871 → ~510 lines refactor),
`0002-Update-Logo.patch` removed, patches renumbered, two new feature patches added
(`0010-Report-desynced-passengers-to-root-vehicle`, `0011-Do-not-allow-out-of-region-teleport-accept`).

**Nothing in 26.3 addresses the blast radius.** `regionFailed`/`uncaughtException` still `stopServer()`,
the watchdog still only prints. 26.3 is a *correctness* release (thread checks, teleport desync) +
Minecraft bump, not a resilience release.

## Live server facts — `fd4afc59` (MineX_MMO_PRUEBAS)

| | |
|---|---|
| Name | MineX_MMO_PRUEBAS |
| RAM limit | 15,240 MB (15 GB) |
| CPU limit | `0` = unlimited |
| Disk | 200,000 MB, currently ~14 GB used |
| Docker image | `ghcr.io/pterodactyl/yolks:java_25` |
| Startup | `bash /home/container/start-with-credentials.sh` |
| JVM | `-Xms${SERVER_MEMORY}M -Xmx${SERVER_MEMORY}M -XX:+UseG1GC …` (typical Aikar-ish G1 set) |
| Plugins | 52 loaded (41 Bukkit + 11 Paper) |
| Worlds | `Spawn` (+ `worlds_*` managed by Worlds; `Buildbattle`, `ReachTheSky`) |

### Findings on this box
1. **`SERVER_MEMORY` is passed as 8192** in the startup command, so `-Xmx8G` — but the container is
   allowed 15 GB. live `spark health` confirms **3.4 GB / 8.0 GB**. Roughly **7 GB of paid RAM is idle**.
2. `threaded-regions.threads: -1` → **3 tick threads** on this box.
3. `threaded-regions.scheduler: EDF`, `grid-exponent: 4` (16×16-chunk sections).
4. `chunk-system.io-threads: -1`, `worker-threads: -1` (auto).
5. `view-distance: 3`, `simulation-distance: 4` in `server.properties` — insanely low, so the
   fresh world idles at 20 TPS trivially; **not** a useful benchmark as-is.
6. `sync-chunk-writes: true` and `region-file-compression: deflate` — both costly defaults.
7. `timings.enabled: false` (forced off by Folia — expected).
8. `max-tick-time: 60000` is meaningless under Folia (no single tick loop).
9. **52 plugins, of which only ~15 are actually Folia-marked**; `LPC` flat-out refused to load
   (`not marked as supporting Folia`). Several others (CMI, ProtocolLib, Vulcan, Plan, Citizens,
   Skript, FAWE, voicechat, packetevents…) load but are not region-safe in principle.

### World state hazard found
`/Spawn` was in a **half-migrated, unbootable state**:
- `level.dat` present but `world_gen_settings.dat` missing → `IllegalStateException: Overworld settings missing`
  → *"Failed to load datapacks, can't proceed with server load"*, server then **hangs in `starting`**
  (panel state never leaves `starting`; needs `kill`).
- `region/`, `poi/`, `entities/`, `playerdata/`, `stats/`, `advancements/` were **absent** from `Spawn`
  while sibling worlds (`Buildbattle`, `ReachTheSky`) still had the full legacy layout.
- Preserved as `/Spawn.broken-20261026`; a fresh world now boots (64.5 s to `Done`).
- This is a **real 26.1.2 production risk**: the new world-storage migration
  (`WorldFolderMigration` → `VanillaWorldMigration`) is **non-atomic and not idempotent-safe on failure**.
  `migrateSavedData()` copies `WorldGenSettings` etc. into `dimensions/<ns>/<dim>/data/`, then
  `deleteLegacyRootCopyIfMigrated()` **deletes the legacy copy at the world root** — but the server then
  aborts before `level.dat` is rewritten (`Overworld settings missing`). You are left with data moved out
  of the legacy location, legacy deleted, and no usable `world_gen_settings.dat`.
  `classifyStartupMigration()` re-enters `VANILLA_MIGRATION` forever because
  `hasCurrentPaperData()` still returns false → **every subsequent boot re-runs a destructive partial migration.**

## Reproduce / operate

```powershell
$env:MX_SFTP_USER='CHANGEME_SFTP_USER'; $env:MX_SFTP_PASS='...'
node E:\minex\_tools\sftp\sftp.js ls /
node E:\minex\_tools\sftp\sftp.js get /logs/latest.log .\latest.log
$env:MX_LISTEN_MS='9000'; node E:\minex\_tools\sftp\console.js "paper:tps server 5"
```

- `/tps` is **shadowed by CMI**; the Folia health report is `paper:tps`.
- Panel API: `/api/client/servers/fd4afc59/{resources,command,power,files/*,websocket}`.
- Build: `$env:JAVA_HOME='C:\Users\91984\.gradle\jdks\jetbrains_s_r_o_-25-amd64-windows.2'; .\gradlew.bat createBundlerJar`
  → `folia-server/build/libs/folia-bundler-26.1.2.local-SNAPSHOT.jar` (96 MB).
- Deployed and verified: server logs `Loading Folia 26.1.2-DEV-HEAD@62dc0f2`.
- Backup of original: `/server.jar.bak-folia2612-62dc0f2` (53 MB).
