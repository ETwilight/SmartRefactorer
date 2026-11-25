package research.agent;
import java.util.*;
import net.minecraft.server.level.ServerPlayer;
/**
 * Count observed vanilla raid triggers by player UUID for the server process.
 *
 * The raid observation mixin increments the counter after a successful trigger.
 * Region evaluation reads the operating player's counter before and after its trial.
 *
 * <pre>{@code
 * long observed = RaidLedger.count(player);
 * }</pre>
 * The value counts raid triggers observed for this player in the current server process.
 */
public final class RaidLedger {
    private static final Map<UUID,Long> TRIGGERS=new HashMap<>();
    /** Increment the triggering player's cumulative raid count. */
    public static void triggered(ServerPlayer player){TRIGGERS.merge(player.getUUID(),1L,Long::sum);}
    /** Read the player's observed trigger count, defaulting to zero. */
    public static long count(ServerPlayer player){return TRIGGERS.getOrDefault(player.getUUID(),0L);}
}
