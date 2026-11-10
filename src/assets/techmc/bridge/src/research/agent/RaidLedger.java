package research.agent;
import java.util.*;
import net.minecraft.server.level.ServerPlayer;
public final class RaidLedger {
    private static final Map<UUID,Long> TRIGGERS=new HashMap<>();
    public static void triggered(ServerPlayer player){TRIGGERS.merge(player.getUUID(),1L,Long::sum);}
    public static long count(ServerPlayer player){return TRIGGERS.getOrDefault(player.getUUID(),0L);}
}
