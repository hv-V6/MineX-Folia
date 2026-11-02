package io.papermc.paper.threadedregions;

import com.mojang.logging.LogUtils;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.server.players.PlayerList;
import org.slf4j.Logger;

import java.lang.management.ManagementFactory;
import java.lang.management.ThreadInfo;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;

/**
 * MineX - join watchdog.
 *
 * <p><b>Why this exists.</b> The "player is in the tab list but never spawns" bug is rare, triggers on
 * world state we cannot recreate on demand, and has historically only been visible when a player
 * complained. That is the worst possible position to debug from: by the time a report arrives the
 * interesting stack is gone.</p>
 *
 * <p>Rather than hoping for more reports, this class makes the failure <b>self-detecting</b>: it watches
 * every player that has been registered in the player list but has not become a tracked, valid entity,
 * and when one stays that way past a threshold it dumps a complete forensic bundle (thread dump, region
 * state, chunk-wait stack, player record) and then <b>repairs it automatically</b> by removing the ghost
 * entry and disconnecting the connection so the proxy can retry.</p>
 *
 * <p>That converts the failure mode from "plausibly rare, needs a restart and a spawn reset" into
 * "logged in full, healed in seconds, and counted in {@code /mxsr status}". If it is happening to more
 * players than reported, the counters will now show it.</p>
 */
public final class JoinWatchdog {

    private static final Logger LOGGER = LogUtils.getLogger();

    /** A join that has not produced a tracked entity within this window is considered a ghost. */
    private static final long GHOST_THRESHOLD_NANOS = TimeUnit.SECONDS.toNanos(
        Long.getLong("minex.joinWatchdogSeconds", 15L));

    /** How often we scan. Cheap: it walks the player list, which is small. */
    private static final long SCAN_INTERVAL_NANOS = TimeUnit.SECONDS.toNanos(2L);

    /** Players currently being watched: uuid -> first time we observed them as "listed but not real". */
    private static final Map<java.util.UUID, WatchEntry> WATCHING = new ConcurrentHashMap<>();

    private static volatile long lastScan;
    private static volatile boolean started;

    private static final java.util.concurrent.atomic.AtomicLong GHOSTS_DETECTED = new java.util.concurrent.atomic.AtomicLong();
    private static final java.util.concurrent.atomic.AtomicLong GHOSTS_HEALED = new java.util.concurrent.atomic.AtomicLong();

    private JoinWatchdog() {}

    private static final class WatchEntry {
        final long firstSeenNanos;
        final String name;
        final String describe;

        WatchEntry(final long firstSeenNanos, final String name, final String describe) {
            this.firstSeenNanos = firstSeenNanos;
            this.name = name;
            this.describe = describe;
        }
    }

    public static long getGhostsDetected() {
        return GHOSTS_DETECTED.get();
    }

    public static long getGhostsHealed() {
        return GHOSTS_HEALED.get();
    }

    public static int getCurrentlyWatched() {
        return WATCHING.size();
    }

    public static List<String> describeWatched() {
        final List<String> ret = new ArrayList<>();
        final long now = System.nanoTime();
        for (final Map.Entry<java.util.UUID, WatchEntry> entry : WATCHING.entrySet()) {
            final double seconds = (double)(now - entry.getValue().firstSeenNanos) / 1.0E9;
            ret.add(entry.getValue().name + " (" + entry.getKey() + ") unspawned for " + String.format("%.1f", seconds) + "s");
        }
        return ret;
    }

    /**
     * Called once per global tick (cheap early-out when not yet time to scan).
     */
    public static void tick() {
        final MinecraftServer server = MinecraftServer.getServer();
        if (server == null || server.hasStopped()) {
            return;
        }

        final long now = System.nanoTime();
        if (now - lastScan < SCAN_INTERVAL_NANOS) {
            return;
        }
        lastScan = now;

        final PlayerList playerList = server.getPlayerList();
        if (playerList == null) {
            return;
        }

        // Build the current set of "listed players that are not real entities".
        final List<ServerPlayer> players;
        try {
            players = new ArrayList<>(playerList.getPlayers());
        } catch (final Throwable thr) {
            return;
        }

        for (final ServerPlayer player : players) {
            try {
                if (isGhost(player)) {
                    final WatchEntry existing = WATCHING.get(player.getUUID());
                    if (existing == null) {
                        WATCHING.put(player.getUUID(), new WatchEntry(now, player.getScoreboardName(), describe(player)));
                    } else if (now - existing.firstSeenNanos >= GHOST_THRESHOLD_NANOS) {
                        handleGhost(player, existing);
                    }
                } else {
                    // Healthy - stop watching.
                    WATCHING.remove(player.getUUID());
                }
            } catch (final Throwable thr) {
                LOGGER.error("JoinWatchdog failed while inspecting {}", player.getScoreboardName(), thr);
            }
        }

        // Drop entries for players who are no longer in the list at all.
        for (final Iterator<java.util.UUID> it = WATCHING.keySet().iterator(); it.hasNext();) {
            final java.util.UUID id = it.next();
            boolean present = false;
            for (final ServerPlayer player : players) {
                if (player.getUUID().equals(id)) {
                    present = true;
                    break;
                }
            }
            if (!present) {
                it.remove();
            }
        }
    }

    /**
     * A "ghost" is a player who is registered in the player list (so shows in tab / {@code /list} and is
     * counted) but who is not a valid, tracked entity in their world.
     */
    private static boolean isGhost(final ServerPlayer player) {
        if (!player.isRealPlayer) {
            // Still mid-login; the normal placeNewPlayer path handles this.
            return false;
        }
        if (player.hasDisconnected()) {
            return false;
        }
        if (!player.valid) {
            return true;
        }
        try {
            return ((ca.spottedleaf.moonrise.patches.entity_tracker.EntityTrackerEntity)player)
                .moonrise$getTrackedEntity() == null;
        } catch (final Throwable thr) {
            return false;
        }
    }

    private static String describe(final ServerPlayer player) {
        try {
            return "level=" + (player.level() == null ? "null" : player.level().getWorld().getName())
                + " pos=" + player.position()
                + " valid=" + player.valid
                + " removed=" + player.isRemoved()
                + " real=" + player.isRealPlayer;
        } catch (final Throwable thr) {
            return "<describe failed: " + thr + ">";
        }
    }

    private static void handleGhost(final ServerPlayer player, final WatchEntry entry) {
        GHOSTS_DETECTED.incrementAndGet();

        LOGGER.error("================================================================");
        LOGGER.error("JOIN WATCHDOG: '{}' has been listed for {}+s without becoming a tracked entity.",
            entry.name, TimeUnit.NANOSECONDS.toSeconds(GHOST_THRESHOLD_NANOS));
        LOGGER.error("This is the 'appears on the tab list but never spawns' failure.");
        LOGGER.error("Player record at detection: {}", describe(player));
        LOGGER.error("uuid={} name={}", player.getUUID(), entry.name);

        dumpJoinForensics(player);

        // ---- self-heal ------------------------------------------------------
        try {
            final PlayerList playerList = MinecraftServer.getServer().getPlayerList();
            // Remove from the player list so no ghost entry is left in tab / /list / counts.
            playerList.remove(player);
            GHOSTS_HEALED.incrementAndGet();
            LOGGER.error("JOIN WATCHDOG: removed ghost entry for '{}' from the player list.", entry.name);
        } catch (final Throwable thr) {
            LOGGER.error("JOIN WATCHDOG: failed to remove ghost entry for '{}'.", entry.name, thr);
        }

        try {
            // Kick so the proxy retries cleanly rather than leaving a dead session.
            player.connection.disconnect(net.minecraft.network.chat.Component.literal(
                "Your connection did not finish loading. Please reconnect."));
            LOGGER.error("JOIN WATCHDOG: disconnected '{}' so the proxy can retry.", entry.name);
        } catch (final Throwable thr) {
            LOGGER.error("JOIN WATCHDOG: failed to disconnect '{}'.", entry.name, thr);
        }

        LOGGER.error("JOIN WATCHDOG: healed. Run '/mxsr status' for cumulative counts.");
        LOGGER.error("================================================================");

        WATCHING.remove(player.getUUID());
    }

    /**
     * Dump everything that would let someone diagnose the stop from the log alone - no attached debugger,
     * no reproduction, no player report.
     */
    private static void dumpJoinForensics(final ServerPlayer player) {
        try {
            final ThreadInfo[] infos = ManagementFactory.getThreadMXBean()
                .dumpAllThreads(true, true, Integer.MAX_VALUE);
            final StringBuilder sb = new StringBuilder(8192);
            sb.append("--- thread dump at ghost detection (").append(infos.length).append(" threads) ---\n");
            for (final ThreadInfo info : infos) {
                sb.append('"').append(info.getThreadName()).append("\" id=").append(info.getThreadId())
                  .append(" state=").append(info.getThreadState());
                if (info.getLockName() != null) {
                    sb.append(" waiting on ").append(info.getLockName());
                }
                sb.append('\n');
                for (final StackTraceElement frame : info.getStackTrace()) {
                    sb.append("    at ").append(frame).append('\n');
                }
            }
            LOGGER.error(sb.toString());
        } catch (final Throwable thr) {
            LOGGER.error("JOIN WATCHDOG: thread dump failed", thr);
        }

        // Chunk-wait stack: if any region is blocked waiting on a chunk, this names it.
        try {
            ca.spottedleaf.moonrise.patches.chunk_system.scheduling.ChunkTaskScheduler
                .dumpAllChunkLoadInfo(MinecraftServer.getServer(), true);
        } catch (final Throwable thr) {
            LOGGER.error("JOIN WATCHDOG: chunk wait dump failed", thr);
        }

        // Region health: which regions are quarantined / utilising badly right now.
        try {
            LOGGER.error("JOIN WATCHDOG: quarantined regions = {}, region failures = {}, tick threads = {}",
                TickRegionScheduler.getQuarantinedRegionCount(),
                TickRegionScheduler.getRegionFailureCount(),
                TickRegions.getScheduler() == null ? -1 : TickRegions.getScheduler().getTotalThreadCount());
        } catch (final Throwable thr) {
            LOGGER.error("JOIN WATCHDOG: region state dump failed", thr);
        }
    }
}
