package io.papermc.paper.threadedregions;

import ca.spottedleaf.concurrentutil.numa.OSNuma;
import ca.spottedleaf.concurrentutil.scheduler.EDFSchedulerThreadPool;
import ca.spottedleaf.concurrentutil.scheduler.SchedulableTick;
import ca.spottedleaf.concurrentutil.scheduler.Scheduler;
import ca.spottedleaf.concurrentutil.scheduler.StealingScheduledThreadPool;
import ca.spottedleaf.concurrentutil.util.TimeUtil;
import ca.spottedleaf.moonrise.common.time.Schedule;
import ca.spottedleaf.moonrise.common.time.TickData;
import ca.spottedleaf.moonrise.common.time.TickTime;
import ca.spottedleaf.moonrise.common.util.MoonriseConstants;
import ca.spottedleaf.moonrise.common.util.TickThread;
import com.mojang.logging.LogUtils;
import io.papermc.paper.util.TraceUtil;
import it.unimi.dsi.fastutil.ints.Int2IntLinkedOpenHashMap;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.ChunkPos;
import org.slf4j.Logger;
import java.lang.management.ManagementFactory;
import java.lang.management.ThreadMXBean;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;

public final class TickRegionScheduler {

    private static final Logger LOGGER = LogUtils.getLogger();
    private static final ThreadMXBean THREAD_MX_BEAN = ManagementFactory.getThreadMXBean();
    private static final boolean MEASURE_CPU_TIME;
    static {
        MEASURE_CPU_TIME = THREAD_MX_BEAN.isThreadCpuTimeSupported();
        if (MEASURE_CPU_TIME) {
            THREAD_MX_BEAN.setThreadCpuTimeEnabled(true);
        } else {
            LOGGER.warn("TickRegionScheduler CPU time measurement is not available");
        }
    }

    public static final int TICK_RATE = 20;
    public static final long TIME_BETWEEN_TICKS = 1_000_000_000L / TICK_RATE; // ns
    // Folia start - watchdog
    public static final FoliaWatchdogThread WATCHDOG_THREAD = new FoliaWatchdogThread();
    static {
        WATCHDOG_THREAD.start();
    }
    // Folia end - watchdog

    private final Scheduler scheduler;

    public static enum SchedulerType {
        EDF,
        WORK_STEALING;
    }

    public TickRegionScheduler(final SchedulerType schedulerType, final int initialThreads) {
        final ThreadFactory threadFactory = new ThreadFactory() {
            private final AtomicInteger idGenerator = new AtomicInteger();
            // on Linux, thread affinity is copied from the parent thread - but we do not want that, so we need
            // to adjust the thread affinity of child threads
            // the group allows the numa instance to accurately collect the child threads
            private final ThreadGroup threadGroup = new ThreadGroup("Folia Region Scheduler ThreadGroup");

            @Override
            public Thread newThread(final Runnable run) {
                final Thread ret = new TickThreadRunner(this.threadGroup, run, "Folia Region Scheduler Thread #" + this.idGenerator.getAndIncrement());
                ret.setUncaughtExceptionHandler(TickRegionScheduler.this::uncaughtException);
                return ret;
            }
        };

        switch (schedulerType) {
            case EDF: {
                this.scheduler = new EDFSchedulerThreadPool(initialThreads, threadFactory);
                break;
            }
            case WORK_STEALING: {
                this.scheduler = new StealingScheduledThreadPool(
                        threadFactory, MoonriseConstants.NUMA_ENABLE ? OSNuma.getNativeInstance() : OSNuma.NoOp.INSTANCE
                );
                ((StealingScheduledThreadPool)this.scheduler).setFlags(StealingScheduledThreadPool.FLAG_SCHEDULE_EVENLY);
                break;
            }
            default: {
                throw new IllegalStateException("Unknown scheduler type: " + schedulerType);
            }
        }
    }

    public void start() {
        if (this.scheduler instanceof EDFSchedulerThreadPool edfSchedulerThreadPool) {
            edfSchedulerThreadPool.start();
        }
    }

    public void setThreads(final int threads) {
        if (this.scheduler instanceof StealingScheduledThreadPool stealingScheduledThreadPool) {
            final Int2IntLinkedOpenHashMap threadAllocation;
            final long stealThresholdNS = TimeUnit.MILLISECONDS.toNanos(3L);
            final long taskTimeSliceNS = TimeUnit.MILLISECONDS.toNanos(2L);

            if (!MoonriseConstants.NUMA_ENABLE) {
                threadAllocation = new Int2IntLinkedOpenHashMap();
                threadAllocation.put(0, threads);

                LOGGER.info("Folia is using " + threads + " tick threads");
            } else {
                final int nodes = stealingScheduledThreadPool.getNuma().getTotalNumaNodes();

                final int threadsPerNode = Math.max(1, threads / nodes);

                threadAllocation = new Int2IntLinkedOpenHashMap(nodes);
                for (int i = 0; i < nodes; ++i) {
                    threadAllocation.put(i, threadsPerNode);
                }

                LOGGER.info("Folia is using " + threadsPerNode + " tick threads per NUMA node, with " + nodes + " NUMA nodes detected");
            }

            stealingScheduledThreadPool.setThreadAllocation(threadAllocation, stealThresholdNS, taskTimeSliceNS);
        }
    }

    public int getTotalThreadCount() {
        return this.scheduler.getAliveThreads().length;
    }

    private static void setTickingRegion(final ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData> region) {
        final Thread currThread = Thread.currentThread();
        if (!(currThread instanceof TickThreadRunner tickThreadRunner)) {
            throw new IllegalStateException("Must be tick thread runner");
        }
        if (region != null && tickThreadRunner.currentTickingRegion != null) {
            throw new IllegalStateException("Trying to double set ticking region!");
        }
        if (region == null && tickThreadRunner.currentTickingRegion == null) {
            throw new IllegalStateException("Trying to double unset ticking region!");
        }
        tickThreadRunner.currentTickingRegion = region;
        if (region != null) {
            tickThreadRunner.currentTickingWorldRegionizedData = region.regioniser.world.worldRegionData.get();
            // Folia start - profiler
            final ca.spottedleaf.leafprofiler.RegionizedProfiler.Handle profiler = region.getData().profiler;
            tickThreadRunner.profiler = profiler == null ? ca.spottedleaf.leafprofiler.RegionizedProfiler.Handle.NO_OP_HANDLE : profiler;
            // Folia end - profiler
        } else {
            tickThreadRunner.currentTickingWorldRegionizedData = null;
            tickThreadRunner.profiler = ca.spottedleaf.leafprofiler.RegionizedProfiler.Handle.NO_OP_HANDLE; // Folia - profiler
        }
    }

    private static void setTickTask(final SchedulableTick task) {
        final Thread currThread = Thread.currentThread();
        if (!(currThread instanceof TickThreadRunner tickThreadRunner)) {
            throw new IllegalStateException("Must be tick thread runner");
        }
        if (task != null && tickThreadRunner.currentTickingTask != null) {
            throw new IllegalStateException("Trying to double set ticking task!");
        }
        if (task == null && tickThreadRunner.currentTickingTask == null) {
            throw new IllegalStateException("Trying to double unset ticking task!");
        }
        tickThreadRunner.currentTickingTask = task;
    }

    /**
     * Returns the current ticking region, or {@code null} if there is no ticking region.
     * If this thread is not a TickThread, then returns {@code null}.
     */
    public static ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData> getCurrentRegion() {
        final Thread currThread = Thread.currentThread();
        if (!(currThread instanceof TickThreadRunner tickThreadRunner)) {
            return RegionShutdownThread.getRegion();
        }
        return tickThreadRunner.currentTickingRegion;
    }

    /**
     * Returns the current ticking region's world regionised data, or {@code null} if there is no ticking region.
     * This is a faster alternative to calling the {@link RegionizedData#get()} method.
     * If this thread is not a TickThread, then returns {@code null}.
     */
    public static RegionizedWorldData getCurrentRegionizedWorldData() {
        final Thread currThread = Thread.currentThread();
        if (!(currThread instanceof TickThreadRunner tickThreadRunner)) {
            return RegionShutdownThread.getWorldData();
        }
        return tickThreadRunner.currentTickingWorldRegionizedData;
    }

    /**
     * Returns the current ticking task, or {@code null} if there is no ticking region.
     * If this thread is not a TickThread, then returns {@code null}.
     */
    public static SchedulableTick getCurrentTickingTask() {
        final Thread currThread = Thread.currentThread();
        if (!(currThread instanceof TickThreadRunner tickThreadRunner)) {
            return null;
        }
        return tickThreadRunner.currentTickingTask;
    }

    // Folia start - profiler
    public static ca.spottedleaf.leafprofiler.RegionizedProfiler.Handle getProfiler() {
        final Thread currThread = Thread.currentThread();
        if (!(currThread instanceof TickThreadRunner tickThreadRunner)) {
            return ca.spottedleaf.leafprofiler.RegionizedProfiler.Handle.NO_OP_HANDLE;
        }
        return tickThreadRunner.profiler;
    }
    // Folia end - profiler


    /**
     * Schedules the given region
     * @throws IllegalStateException If the region is already scheduled or is ticking
     */
    public void scheduleRegion(final RegionScheduleHandle region) {
        region.scheduler = this;
        this.scheduler.schedule(region);
    }

    /**
     * Attempts to de-schedule the provided region. If the current region cannot be cancelled for its next tick or task
     * execution, then it will be cancelled after.
     */
    public void descheduleRegion(final RegionScheduleHandle region) {
        // To avoid acquiring any of the locks the scheduler may be using, we
        // simply cancel the next action.
        region.markNonSchedulable();
    }

    public boolean halt(final boolean sync, final long maxWaitNS) {
        this.scheduler.halt();
        if (!sync) {
            return this.scheduler.getAliveThreads().length == 0;
        }

        return this.scheduler.join(maxWaitNS == 0L ? 0L : Math.max(1L, TimeUnit.NANOSECONDS.toMillis(maxWaitNS)));
    }

    void dumpAliveThreadTraces(final String reason) {
        for (final Thread thread : this.scheduler.getAliveThreads()) {
            if (thread.isAlive()) {
                TraceUtil.dumpTraceForThread(thread, reason);
            }
        }
    }

    public void setHasTasks(final RegionScheduleHandle region) {
        this.scheduler.notifyTasks(region);
    }

    private void uncaughtException(final Thread thread, final Throwable thr) {
        LOGGER.error("Uncaught exception in tick thread \"" + thread.getName() + "\"", thr);

        // prevent further ticks from occurring
        // we CANNOT sync, because WE ARE ON A SCHEDULER THREAD
        this.scheduler.halt();

        MinecraftServer.getServer().stopServer();
    }

    // MineX start - region failure isolation
    /**
     * Handles a throwable that escaped a region's tick or task-drain loop.
     *
     * <p>
     * Upstream Folia treats <b>every</b> region failure as fatal: it halts the entire scheduler and
     * stops the server. That means one plugin throwing on one entity, in one region, takes the whole
     * network down. For a live service that trade-off is wrong - it converts a localised, recoverable
     * fault into a global outage.
     * </p>
     *
     * <p>
     * Instead, when {@code threaded-regions.failure-mode} is {@code ISOLATE} (the default) we:
     * <ol>
     *     <li>quarantine only the failing region,</li>
     *     <li>leave every other region ticking normally,</li>
     *     <li>stop the region from being rescheduled so it cannot keep faulting,</li>
     *     <li>keep its chunks loaded but frozen,</li>
     *     <li>give the server a repair path via {@code /mxsr region recover}.</li>
     * </ol>
     * If the failure rate crosses the configured threshold we fall back to the upstream behaviour
     * (halt + stop) so that a systemic fault still fails loudly instead of silently rotting the world.
     * </p>
     */
    void regionFailed(final RegionScheduleHandle handle, final boolean executingTasks, final Throwable thr) {
        final ChunkPos center = handle.region == null ? null : handle.region.region.getCenterChunk();
        final ServerLevel world = handle.region == null ? null : handle.region.world;
        final String worldName = world == null ? "null" : world.getWorld().getName();

        if (handle.region == null) {
            // The global region is not isolatable - every region depends on it. Keep upstream behaviour.
            this.scheduler.halt();
            LOGGER.error("Global region failed to " + (executingTasks ? "execute tasks" : "tick") + ":", thr);
            MinecraftServer.getServer().stopServer();
            return;
        }

        final io.papermc.paper.configuration.GlobalConfiguration.ThreadedRegions config =
            io.papermc.paper.configuration.GlobalConfiguration.get().threadedRegions;

        final long regionId = handle.region.id;

        if (config.failureMode == io.papermc.paper.configuration.GlobalConfiguration.ThreadedRegions.FailureMode.HALT) {
            this.scheduler.halt();
            LOGGER.error("Region #" + regionId + " centered at chunk " + center + " in world '" + worldName
                + "' failed to " + (executingTasks ? "execute tasks" : "tick") + ":", thr);
            MinecraftServer.getServer().stopServer();
            return;
        }

        // ISOLATE mode
        final int failures = FAILURE_COUNTER.incrementAndGet();
        // Stop this region from being rescheduled. The region-level flag survives merge/split
        // (which replaces the tick handle), and markNonSchedulable poisons the current handle.
        handle.region.markQuarantined("failed to " + (executingTasks ? "execute tasks" : "tick") + ": " + thr);
        final int quarantined = QUARANTINED_REGIONS.incrementAndGet();

        LOGGER.error("Region #" + regionId + " centered at chunk " + center + " in world '" + worldName
            + "' failed to " + (executingTasks ? "execute tasks" : "tick") + ".", thr);
        LOGGER.error("Region #" + regionId + " has been QUARANTINED (threaded-regions.failure-mode=ISOLATE). "
            + "Its chunks (" + safeChunkCount(handle) + ") are frozen but loaded; other regions continue to tick. "
            + "Players inside it will appear frozen until it is recovered with '/mxsr recover "
            + worldName + " " + (center == null ? 0 : center.x()) + " " + (center == null ? 0 : center.z())
            + "' or the server is restarted. Total quarantined: " + quarantined + ".");

        if (config.maxIsolatedRegionFailures > 0 && failures > config.maxIsolatedRegionFailures) {
            LOGGER.error("Region failure count " + failures + " exceeded threaded-regions.max-isolated-region-failures ("
                + config.maxIsolatedRegionFailures + "); treating this as a systemic fault and halting the server.");
            this.scheduler.halt();
            MinecraftServer.getServer().stopServer();
        }
    }

    private static int safeChunkCount(final RegionScheduleHandle handle) {
        try {
            return handle.region == null ? 0 : handle.region.region.getOwnedChunks().size();
        } catch (final Throwable thr) {
            return -1;
        }
    }

    private static final java.util.concurrent.atomic.AtomicInteger FAILURE_COUNTER = new java.util.concurrent.atomic.AtomicInteger();
    private static final java.util.concurrent.atomic.AtomicInteger QUARANTINED_REGIONS = new java.util.concurrent.atomic.AtomicInteger();

    /** How far into the future a quarantined region's handle is parked, so it never looks overdue to EDF. */
    private static final long QUARANTINE_RETRY_DELAY_NS = TimeUnit.SECONDS.toNanos(3600L);

    public static int getQuarantinedRegionCount() {
        return QUARANTINED_REGIONS.get();
    }

    public static int getRegionFailureCount() {
        return FAILURE_COUNTER.get();
    }
    // MineX end - region failure isolation

    // By using our own thread object, we can use a field for the current region rather than a ThreadLocal.
    // This is much faster than a thread local, since the thread local has to use a map lookup.
    private static final class TickThreadRunner extends TickThread {

        private ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData> currentTickingRegion;
        private RegionizedWorldData currentTickingWorldRegionizedData;
        private SchedulableTick currentTickingTask;
        // Folia start - profiler
        private ca.spottedleaf.leafprofiler.RegionizedProfiler.Handle profiler = ca.spottedleaf.leafprofiler.RegionizedProfiler.Handle.NO_OP_HANDLE;
        // Folia end - profiler

        public TickThreadRunner(final ThreadGroup group, final Runnable run, final String name) {
            super(group, run, name);
        }
    }

    public static abstract class RegionScheduleHandle extends SchedulableTick {

        protected long currentTick;
        protected long lastTickStart;

        protected final TickData tickTimes5s;
        protected final TickData tickTimes15s;
        protected final TickData tickTimes1m;
        protected final TickData tickTimes5m;
        protected final TickData tickTimes15m;
        protected TickTime currentTickData;
        protected long intermediateTaskTime = 0L;
        protected long intermediateTaskTimeCPU = 0L;
        protected Thread currentTickingThread;

        public final TickRegions.TickRegionData region;
        private final AtomicBoolean cancelled = new AtomicBoolean();

        protected final Schedule tickSchedule;

        private TickRegionScheduler scheduler;

        public RegionScheduleHandle(final TickRegions.TickRegionData region, final long firstStart) {
            this.currentTick = 0L;
            this.lastTickStart = TimeUtil.DEADLINE_NOT_SET;
            this.tickTimes5s = new TickData(TimeUnit.SECONDS.toNanos(5L));
            this.tickTimes15s = new TickData(TimeUnit.SECONDS.toNanos(15L));
            this.tickTimes1m = new TickData(TimeUnit.MINUTES.toNanos(1L));
            this.tickTimes5m = new TickData(TimeUnit.MINUTES.toNanos(5L));
            this.tickTimes15m = new TickData(TimeUnit.MINUTES.toNanos(15L));
            this.region = region;

            this.setScheduledStart(firstStart);
            this.tickSchedule = new Schedule(firstStart == TimeUtil.DEADLINE_NOT_SET ? firstStart : firstStart - TIME_BETWEEN_TICKS);
        }

        /**
         * Subclasses should call this instead of {@link #setScheduledStart(long)}
         * so that the tick schedule and scheduled start remain synchronised
         */
        protected final void updateScheduledStart(final long to) {
            this.setScheduledStart(to);
            this.tickSchedule.setLastPeriod(to == TimeUtil.DEADLINE_NOT_SET ? to : to - TIME_BETWEEN_TICKS);
        }

        public final void markNonSchedulable() {
            this.cancelled.set(true);
        }

        public final boolean isMarkedAsNonSchedulable() {
            return this.cancelled.get();
        }

        protected abstract boolean tryMarkTicking();

        protected abstract boolean markNotTicking();

        protected abstract void tickRegion(final long tickCount, final long startTime, final long scheduledEnd);

        protected abstract void runRegionTasks(final BooleanSupplier canContinue);

        protected abstract boolean hasIntermediateTasks();

        @Override
        public final boolean hasTasks() {
            return this.hasIntermediateTasks();
        }

        @Override
        public final boolean runTasks(final BooleanSupplier canContinue) {
            if (this.cancelled.get()) {
                return false;
            }

            final long cpuStart = MEASURE_CPU_TIME ? THREAD_MX_BEAN.getCurrentThreadCpuTime() : 0L;
            final long tickStart = System.nanoTime();

            if (!this.tryMarkTicking()) {
                if (!this.cancelled.get()) {
                    throw new IllegalStateException("Scheduled region should be acquirable");
                }
                // region was killed
                return false;
            }

            TickRegionScheduler.setTickTask(this);
            if (this.region != null) {
                TickRegionScheduler.setTickingRegion(this.region.region);
            }

            synchronized (this) {
                this.currentTickingThread = Thread.currentThread();
            }

            final FoliaWatchdogThread.RunningTick runningTick = new FoliaWatchdogThread.RunningTick(tickStart, this, Thread.currentThread()); // Folia - watchdog
            WATCHDOG_THREAD.addTick(runningTick); // Folia - watchdog
            try {
                this.runRegionTasks(() -> {
                    return !RegionScheduleHandle.this.cancelled.get() && canContinue.getAsBoolean();
                });
            } catch (final Throwable thr) {
                this.scheduler.regionFailed(this, true, thr);
                // don't release region for another tick
                // MineX start - region failure isolation
                // The region may have been quarantined rather than the whole server halted; we still must
                // release the tick state, otherwise the region stays in STATE_TICKING forever and can never
                // be merged, split or recovered.
                if (this.cancelled.get()) {
                    try {
                        this.markNotTicking();
                    } catch (final Throwable releaseThr) {
                        LOGGER.error("Failed to release quarantined region", releaseThr);
                    }
                }
                // MineX end - region failure isolation
                return false;
            } finally {
                WATCHDOG_THREAD.removeTick(runningTick); // Folia - watchdog
                final long tickEnd = System.nanoTime();
                final long cpuEnd = MEASURE_CPU_TIME ? THREAD_MX_BEAN.getCurrentThreadCpuTime() : 0L;

                synchronized (this) {
                    this.intermediateTaskTime += (tickEnd - tickStart);
                    this.intermediateTaskTimeCPU += (cpuEnd - cpuStart);
                }

                TickRegionScheduler.setTickTask(null);
                if (this.region != null) {
                    TickRegionScheduler.setTickingRegion(null);
                }
            }

            return this.markNotTicking() && !this.cancelled.get();
        }

        @Override
        public final boolean runTick() {
            // Remember, we are supposed use setScheduledStart if we return true here, otherwise
            // the scheduler will try to schedule for the same time.
            if (this.cancelled.get()) {
                return false;
            }

            final long cpuStart = MEASURE_CPU_TIME ? THREAD_MX_BEAN.getCurrentThreadCpuTime() : 0L;
            final long tickStart = System.nanoTime();

            // use max(), don't assume that tickStart >= scheduledStart
            final long tickCount = Math.max(1L, this.tickSchedule.getPeriodsAhead(TIME_BETWEEN_TICKS, tickStart));

            if (!this.tryMarkTicking()) {
                if (!this.cancelled.get()) {
                    throw new IllegalStateException("Scheduled region should be acquirable");
                }
                // region was killed
                return false;
            }
            if (this.cancelled.get()) {
                this.markNotTicking();
                // region should be killed
                return false;
            }

            TickRegionScheduler.setTickTask(this);
            if (this.region != null) {
                TickRegionScheduler.setTickingRegion(this.region.region);
            }
            this.incrementTickCount();
            final long lastTickStart = this.lastTickStart;
            this.lastTickStart = tickStart;

            final long scheduledStart = this.getScheduledStart();
            final long scheduledEnd = scheduledStart + TIME_BETWEEN_TICKS;

            final long intermediateTaskTime;
            final long intermediateTaskTimeCPU;

            synchronized (this) {
                intermediateTaskTime = this.intermediateTaskTime;
                intermediateTaskTimeCPU = this.intermediateTaskTimeCPU;
                this.intermediateTaskTime = 0L;
                this.intermediateTaskTimeCPU = 0L;

                this.currentTickData = new TickTime(
                    lastTickStart, scheduledStart, tickStart, cpuStart,
                    TimeUtil.DEADLINE_NOT_SET, TimeUtil.DEADLINE_NOT_SET,
                    intermediateTaskTime, intermediateTaskTimeCPU,
                    MEASURE_CPU_TIME
                );
                this.currentTickingThread = Thread.currentThread();
            }

            final FoliaWatchdogThread.RunningTick runningTick = new FoliaWatchdogThread.RunningTick(tickStart, this, Thread.currentThread()); // Folia - region threading
            WATCHDOG_THREAD.addTick(runningTick); // Folia - region threading
            try {
                // next start isn't updated until the end of this tick
                this.tickRegion(tickCount, tickStart, scheduledEnd);
            } catch (final Throwable thr) {
                this.scheduler.regionFailed(this, false, thr);
                // regionFailed will schedule a shutdown, so we should avoid letting this region tick further
                // MineX start - region failure isolation
                if (this.cancelled.get()) {
                    try {
                        this.markNotTicking();
                    } catch (final Throwable releaseThr) {
                        LOGGER.error("Failed to release quarantined region", releaseThr);
                    }
                    // Reschedule into the far future with a fixed, penalised delay instead of leaving the
                    // handle at its stale scheduled start. Without this the EDF pool sees the tick as
                    // perpetually overdue and can spin on it, starving healthy regions of a thread.
                    this.updateScheduledStart(System.nanoTime() + QUARANTINE_RETRY_DELAY_NS);
                }
                // MineX end - region failure isolation
                return false;
            } finally {
                WATCHDOG_THREAD.removeTick(runningTick); // Folia - region threading
                final long tickEnd = System.nanoTime();
                final long cpuEnd = MEASURE_CPU_TIME ? THREAD_MX_BEAN.getCurrentThreadCpuTime() : 0L;

                // in order to ensure all regions get their chance at scheduling, we have to ensure that regions
                // that exceed the max tick time are not always prioritised over everything else. Thus, we use the greatest
                // of the current time and "ideal" next tick start.
                this.tickSchedule.advanceBy(tickCount, TIME_BETWEEN_TICKS);
                this.setScheduledStart(TimeUtil.getGreatestTime(tickEnd, this.tickSchedule.getDeadline(TIME_BETWEEN_TICKS)));

                final TickTime time = new TickTime(
                    lastTickStart, scheduledStart, tickStart, cpuStart, tickEnd, cpuEnd,
                    intermediateTaskTime, intermediateTaskTimeCPU,
                    MEASURE_CPU_TIME
                );

                this.addTickTime(time);
                TickRegionScheduler.setTickTask(null);
                if (this.region != null) {
                    TickRegionScheduler.setTickingRegion(null);
                }
            }

            // Only AFTER updating the tickStart
            return this.markNotTicking() && !this.cancelled.get();
        }

        /**
         * Only safe to call if this tick data matches the current ticking region.
         */
        protected void addTickTime(final TickTime time) {
            synchronized (this) {
                this.currentTickData = null;
                this.currentTickingThread = null;
                this.tickTimes5s.addDataFrom(time);
                this.tickTimes15s.addDataFrom(time);
                this.tickTimes1m.addDataFrom(time);
                this.tickTimes5m.addDataFrom(time);
                this.tickTimes15m.addDataFrom(time);
            }
        }

        private TickTime adjustCurrentTickData(final long tickEnd) {
            final TickTime currentTickData = this.currentTickData;
            if (currentTickData == null) {
                return null;
            }

            final long cpuEnd = MEASURE_CPU_TIME ? THREAD_MX_BEAN.getThreadCpuTime(this.currentTickingThread.threadId()) : 0L;

            return new TickTime(
                currentTickData.previousTickStart(), currentTickData.scheduledTickStart(),
                currentTickData.tickStart(), currentTickData.tickStartCPU(),
                tickEnd, cpuEnd, 0L, 0L, // TODO
                MEASURE_CPU_TIME
            );
        }

        public final TickData.TickReportData getTickReport5s(final long currTime) {
            synchronized (this) {
                return this.tickTimes5s.generateTickReport(this.adjustCurrentTickData(currTime), currTime, TIME_BETWEEN_TICKS);
            }
        }

        public final TickData.TickReportData getTickReport15s(final long currTime) {
            synchronized (this) {
                return this.tickTimes15s.generateTickReport(this.adjustCurrentTickData(currTime), currTime, TIME_BETWEEN_TICKS);
            }
        }

        public final TickData.TickReportData getTickReport1m(final long currTime) {
            synchronized (this) {
                return this.tickTimes1m.generateTickReport(this.adjustCurrentTickData(currTime), currTime, TIME_BETWEEN_TICKS);
            }
        }

        public final TickData.TickReportData getTickReport5m(final long currTime) {
            synchronized (this) {
                return this.tickTimes5m.generateTickReport(this.adjustCurrentTickData(currTime), currTime, TIME_BETWEEN_TICKS);
            }
        }

        public final TickData.TickReportData getTickReport15m(final long currTime) {
            synchronized (this) {
                return this.tickTimes15m.generateTickReport(this.adjustCurrentTickData(currTime), currTime, TIME_BETWEEN_TICKS);
            }
        }

        /**
         * Only safe to call if this tick data matches the current ticking region.
         */
        private void incrementTickCount() {
            ++this.currentTick;
        }

        /**
         * Only safe to call if this tick data matches the current ticking region.
         */
        public final long getCurrentTick() {
            return this.currentTick;
        }

        protected final void setCurrentTick(final long value) {
            this.currentTick = value;
        }
    }
}