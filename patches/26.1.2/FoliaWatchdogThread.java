package io.papermc.paper.threadedregions;

import com.mojang.logging.LogUtils;
import io.papermc.paper.threadedregions.TickRegionScheduler.RegionScheduleHandle;
import net.minecraft.server.MinecraftServer;
import org.bukkit.Bukkit;
import org.slf4j.Logger;
import org.spigotmc.WatchdogThread;
import java.lang.management.ManagementFactory;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.concurrent.TimeUnit;

public final class FoliaWatchdogThread extends Thread {

    private static final Logger LOGGER = LogUtils.getLogger();

    public static final class RunningTick {

        public final long start;
        public final RegionScheduleHandle handle;
        public final Thread thread;

        private long lastPrint;

        public RunningTick(final long start, final RegionScheduleHandle handle, final Thread thread) {
            this.start = start;
            this.handle = handle;
            this.thread = thread;
            this.lastPrint = start;
        }
    }

    private final LinkedHashSet<RunningTick> ticks = new LinkedHashSet<>();

    public FoliaWatchdogThread() {
        super("Folia Watchdog Thread");
        this.setDaemon(true);
        this.setUncaughtExceptionHandler((final Thread thread, final Throwable throwable) -> {
            LOGGER.error("Uncaught exception in thread '" + thread.getName() + "'", throwable);
        });
    }

    @Override
    public void run() {
        for (;;) {
            try {
                Thread.sleep(1000L);
            } catch (final InterruptedException ex) {}

            if (MinecraftServer.getServer() == null || MinecraftServer.getServer().hasStopped()) {
                continue;
            }

            final List<RunningTick> ticks;
            synchronized (this.ticks) {
                if (this.ticks.isEmpty()) {
                    continue;
                }
                ticks = new ArrayList<>(this.ticks);
            }

            final long now = System.nanoTime();

            for (final RunningTick tick : ticks) {
                final long elapsed = now - tick.lastPrint;
                if (elapsed <= TimeUnit.SECONDS.toNanos(5L)) {
                    continue;
                }
                tick.lastPrint = now;

                final double totalElapsedS = (double)(now - tick.start) / 1.0E9;

                if (tick.handle instanceof TickRegions.ConcreteRegionTickHandle region) {
                    LOGGER.error(
                        "Tick region located in world '" + region.region.world.getWorld().getName() + "' around chunk '"
                            + region.region.region.getCenterChunk() + "' has not responded in " + totalElapsedS + "s:"
                    );
                } else {
                    // assume global
                    LOGGER.error("Global region has not responded in " + totalElapsedS + "s:");
                }

                WatchdogThread.dumpThread(
                    ManagementFactory.getThreadMXBean().getThreadInfo(tick.thread.threadId(), Integer.MAX_VALUE),
                    Bukkit.getServer().getLogger()
                );

                // MineX start - hung region quarantine
                this.maybeQuarantineHungRegion(tick, totalElapsedS, now);
                // MineX end - hung region quarantine
            }
        }
    }

    // MineX start - hung region quarantine
    /*
     * Upstream Folia's watchdog is purely diagnostic: a region stuck in an infinite loop or a blocking
     * wait pins its tick thread forever, and nothing ever reclaims it. With the default thread count
     * (often only 3) a handful of stuck regions freezes the entire server with no crash and no
     * automatic recovery - the process looks alive, the watchdog spams the log, and every player is
     * frozen.
     *
     * Once a region has been unresponsive for `hungRegionActionAfterSeconds`, we quarantine it the same
     * way we quarantine a throwing region: it stops being rescheduled, so the tick thread it was
     * holding is released as soon as the stuck call returns, and the remaining threads keep serving
     * healthy regions. The stuck thread itself cannot be forcibly killed safely in the JVM (Thread.stop
     * is removed and would corrupt region state), so we keep reporting it, but we stop letting one
     * wedged region consume the whole pool.
     */
    private void maybeQuarantineHungRegion(final RunningTick tick, final double totalElapsedS, final long now) {
        final io.papermc.paper.configuration.GlobalConfiguration.ThreadedRegions config =
            io.papermc.paper.configuration.GlobalConfiguration.get().threadedRegions;

        final long thresholdSeconds = config.hungRegionActionAfterSeconds;
        if (thresholdSeconds <= 0L || totalElapsedS < (double)thresholdSeconds) {
            return;
        }

        // The global region is not isolatable.
        if (!(tick.handle instanceof TickRegions.ConcreteRegionTickHandle region)) {
            return;
        }

        if (!region.region.isQuarantined()) {
            region.region.markQuarantined("unresponsive for " + (long)totalElapsedS + "s");
            LOGGER.error("Region -> hung quarantine");
            LOGGER.error("Tick region in world '" + region.region.world.getWorld().getName() + "' around chunk '"
                + region.region.region.getCenterChunk() + "' has been unresponsive for " + (long)totalElapsedS
                + "s (threshold " + thresholdSeconds + "s). QUARANTINING it so it stops being rescheduled.");
            LOGGER.error("Other regions are unaffected. The wedged thread cannot be safely killed; it will be "
                + "reclaimed when/if the blocking call returns. Restart the server to fully recover this region.");
        }
    }
    // MineX end - hung region quarantine

    public void addTick(final RunningTick tick) {
        synchronized (this.ticks) {
            this.ticks.add(tick);
        }
    }

    public void removeTick(final RunningTick tick) {
        synchronized (this.ticks) {
            this.ticks.remove(tick);
        }
    }
}
