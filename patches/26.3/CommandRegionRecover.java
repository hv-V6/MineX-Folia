package io.papermc.paper.threadedregions.commands;

import io.papermc.paper.threadedregions.RegionizedServer;
import io.papermc.paper.threadedregions.ThreadedRegionizer;
import io.papermc.paper.threadedregions.TickRegionScheduler;
import io.papermc.paper.threadedregions.TickRegions;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.ChunkPos;
import org.bukkit.Bukkit;
import org.bukkit.World;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.craftbukkit.CraftWorld;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * MineX - operator tooling for regions quarantined by threaded-regions.failure-mode=ISOLATE
 * or by the hung-region watchdog.
 *
 * <p>Upstream Folia has no such concept: a failing region takes the server down, so there is nothing
 * to recover. Once failures are isolated, operators need to be able to see what was quarantined and
 * put it back into rotation after fixing the cause (removing the offending entity, updating the
 * plugin, etc.).</p>
 */
public final class CommandRegionRecover extends Command {

    public CommandRegionRecover() {
        super("mxsr");
        this.setUsage("/<command> [status|list|recover <world> <chunkX> <chunkZ>|recoverall]");
        this.setDescription("Inspect and recover quarantined Folia regions.");
        this.setPermission("bukkit.command.tps");
    }

    private static List<ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData>>
    allRegions() {
        final List<ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData>> ret = new ArrayList<>();
        for (final World bukkitWorld : Bukkit.getWorlds()) {
            final ServerLevel world = ((CraftWorld) bukkitWorld).getHandle();
            world.regioniser.computeForAllRegions(ret::add);
        }
        return ret;
    }

    private static List<ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData>>
    quarantinedRegions() {
        final List<ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData>> ret = new ArrayList<>();
        for (final ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData> region : allRegions()) {
            if (region.getData() != null && region.getData().isQuarantined()) {
                ret.add(region);
            }
        }
        return ret;
    }

    private static boolean status(final CommandSender sender) {
        final int quarantined = quarantinedRegions().size();
        final int failures = TickRegionScheduler.getRegionFailureCount();

        sender.sendMessage(Component.text()
            .append(Component.text("Region isolation status\n", NamedTextColor.AQUA, net.kyori.adventure.text.format.TextDecoration.BOLD))
            .append(Component.text(" - Mode: ", NamedTextColor.BLUE))
            .append(Component.text(String.valueOf(io.papermc.paper.configuration.GlobalConfiguration.get().threadedRegions.failureMode), NamedTextColor.WHITE))
            .append(Component.text("\n - Quarantined regions: ", NamedTextColor.BLUE))
            .append(Component.text(String.valueOf(quarantined), quarantined == 0 ? NamedTextColor.GREEN : NamedTextColor.RED))
            .append(Component.text("\n - Total region failures this session: ", NamedTextColor.BLUE))
            .append(Component.text(String.valueOf(failures), failures == 0 ? NamedTextColor.GREEN : NamedTextColor.YELLOW))
            .append(Component.text("\n - Tick threads: ", NamedTextColor.BLUE))
            .append(Component.text(String.valueOf(TickRegions.getScheduler().getTotalThreadCount()), NamedTextColor.WHITE))
            .append(Component.text("\n - Total regions: ", NamedTextColor.BLUE))
            .append(Component.text(String.valueOf(allRegions().size()), NamedTextColor.WHITE))
            // MineX start - join watchdog counters
            .append(Component.text("\nJoin watchdog\n", NamedTextColor.AQUA, net.kyori.adventure.text.format.TextDecoration.BOLD))
            .append(Component.text(" - Ghost joins detected this session: ", NamedTextColor.BLUE))
            .append(Component.text(String.valueOf(io.papermc.paper.threadedregions.JoinWatchdog.getGhostsDetected()),
                io.papermc.paper.threadedregions.JoinWatchdog.getGhostsDetected() == 0 ? NamedTextColor.GREEN : NamedTextColor.YELLOW))
            .append(Component.text("\n - Ghost joins healed: ", NamedTextColor.BLUE))
            .append(Component.text(String.valueOf(io.papermc.paper.threadedregions.JoinWatchdog.getGhostsHealed()), NamedTextColor.WHITE))
            .append(Component.text("\n - Joins currently being watched: ", NamedTextColor.BLUE))
            .append(Component.text(String.valueOf(io.papermc.paper.threadedregions.JoinWatchdog.getCurrentlyWatched()), NamedTextColor.WHITE))
            // MineX end - join watchdog counters
            .build()
        );

        // MineX start - join watchdog detail
        for (final String line : io.papermc.paper.threadedregions.JoinWatchdog.describeWatched()) {
            sender.sendMessage(Component.text("   watching: " + line, NamedTextColor.GRAY));
        }
        // MineX end - join watchdog detail
        return true;
    }

    private static boolean list(final CommandSender sender) {
        final List<ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData>> regions = quarantinedRegions();
        if (regions.isEmpty()) {
            sender.sendMessage(Component.text("No quarantined regions.", NamedTextColor.GREEN));
            return true;
        }

        sender.sendMessage(Component.text("Quarantined regions (" + regions.size() + "):", NamedTextColor.AQUA));
        for (final ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData> region : regions) {
            final ChunkPos center = region.getCenterChunk();
            final ServerLevel world = region.regioniser.world;
            final String name = world.getWorld().getName();
            final int chunks = region.getOwnedChunks().size();

            sender.sendMessage(Component.text()
                .append(Component.text(" - #" + region.id + " ", NamedTextColor.GRAY))
                .append(Component.text("world='" + name + "' ", NamedTextColor.WHITE))
                .append(Component.text("center=" + (center == null ? "?" : (center.x() + "," + center.z())), NamedTextColor.WHITE))
                .append(Component.text(" chunks=" + chunks, NamedTextColor.GRAY))
                .append(Component.text("\n   recover with: /mxsr recover " + name
                    + " " + (center == null ? 0 : center.x()) + " " + (center == null ? 0 : center.z()), NamedTextColor.DARK_AQUA))
                .build()
            );
        }
        return true;
    }

    private static boolean recover(final CommandSender sender, final String worldName, final int chunkX, final int chunkZ) {
        final World bukkitWorld = Bukkit.getWorld(worldName);
        if (bukkitWorld == null) {
            sender.sendMessage(Component.text("No such world: " + worldName, NamedTextColor.RED));
            return true;
        }
        final ServerLevel world = ((CraftWorld) bukkitWorld).getHandle();
        final ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData> region =
            world.regioniser.getRegionAtSynchronised(chunkX, chunkZ);

        if (region == null) {
            sender.sendMessage(Component.text("No region owns chunk " + chunkX + "," + chunkZ + " in world '" + worldName + "'.", NamedTextColor.RED));
            return true;
        }

        if (!region.getData().isQuarantined()) {
            sender.sendMessage(Component.text("Region #" + region.id + " is not quarantined; nothing to do.", NamedTextColor.YELLOW));
            return true;
        }

        region.getData().recoverFromQuarantine();
        sender.sendMessage(Component.text("Region #" + region.id + " has been recovered and rescheduled.", NamedTextColor.GREEN));
        return true;
    }

    private static boolean recoverAll(final CommandSender sender) {
        final List<ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData>> regions = quarantinedRegions();
        if (regions.isEmpty()) {
            sender.sendMessage(Component.text("No quarantined regions.", NamedTextColor.GREEN));
            return true;
        }
        for (final ThreadedRegionizer.ThreadedRegion<TickRegions.TickRegionData, TickRegions.TickRegionSectionData> region : regions) {
            region.getData().recoverFromQuarantine();
        }
        sender.sendMessage(Component.text("Recovered " + regions.size() + " quarantined region(s).", NamedTextColor.GREEN));
        return true;
    }

    @Override
    public boolean execute(final CommandSender sender, final String commandLabel, final String[] args) {
        // Region graph mutation must happen on the global region.
        if (args.length == 0) {
            RegionizedServer.getInstance().blockOn(() -> status(sender));
            return true;
        }

        switch (args[0].toLowerCase(Locale.ROOT)) {
            case "status" -> RegionizedServer.getInstance().blockOn(() -> status(sender));
            case "list" -> RegionizedServer.getInstance().blockOn(() -> list(sender));
            case "recoverall" -> RegionizedServer.getInstance().blockOn(() -> recoverAll(sender));
            case "recover" -> {
                if (args.length < 4) {
                    sender.sendMessage(Component.text("Usage: /mxsr recover <world> <chunkX> <chunkZ>", NamedTextColor.RED));
                    return true;
                }
                final int chunkX;
                final int chunkZ;
                try {
                    chunkX = Integer.parseInt(args[2]);
                    chunkZ = Integer.parseInt(args[3]);
                } catch (final NumberFormatException ex) {
                    sender.sendMessage(Component.text("chunkX and chunkZ must be integers.", NamedTextColor.RED));
                    return true;
                }
                RegionizedServer.getInstance().blockOn(() -> recover(sender, args[1], chunkX, chunkZ));
            }
            default -> sender.sendMessage(Component.text(
                "Unknown subcommand '" + args[0] + "'. Use one of: status, list, recover, recoverall", NamedTextColor.RED));
        }
        return true;
    }

    @Override
    public List<String> tabComplete(final CommandSender sender, final String alias, final String[] args) {
        if (args.length == 0) {
            return CommandUtil.getSortedList(List.of("status", "list", "recover", "recoverall"));
        }
        if (args.length == 1) {
            return CommandUtil.getSortedList(List.of("status", "list", "recover", "recoverall"), args[0]);
        }
        if (args.length == 2 && args[0].equalsIgnoreCase("recover")) {
            return CommandUtil.getSortedList(Bukkit.getWorlds(), World::getName, args[1]);
        }
        return new ArrayList<>();
    }
}
