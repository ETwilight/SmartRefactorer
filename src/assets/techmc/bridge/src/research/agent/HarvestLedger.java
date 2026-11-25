package research.agent;

import java.util.*;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.gamerules.GameRules;

/**
 * Record observed block loot and pair amethyst harvests with natural maturation.
 *
 * Drop nodes accumulate actual returned item quantities by dimension and position.
 * Maturation markers are consumed when a cluster produces a harvest observation.
 * Tick and sequence values distinguish natural growth from later loot events,
 * including events occurring in the same tick. Coverage limits and observer errors
 * are included with each region read.
 *
 * <pre>{@code
 * Map<String, Object> sample = HarvestLedger.read(world, min, max);
 * }</pre>
 * The sample supplies cumulative drops and paired amethyst events for that region.
 */
public final class HarvestLedger {
    private static final String EPOCH=UUID.randomUUID().toString();
    private static final int MAX_NODES=65536,MAX_MATURITIES=65536,MAX_EVENTS=262144;
    private static final Map<String,Map<String,Object>> DROP_NODES=new HashMap<>();
    private static final Map<String,Maturity> MATURITIES=new HashMap<>();
    private static final List<Map<String,Object>> AMETHYST_EVENTS=new ArrayList<>();
    private static final Set<String> ERRORS=new LinkedHashSet<>();
    private static long sequence;
    private static boolean complete=true;
    /** Retain the game tick and ledger sequence of one natural maturation. */
    private record Maturity(long tick,long sequence) {}
    /** Identify a block position within its dimension. */
    private static String key(ServerLevel world,BlockPos pos){return world.dimension().identifier()+":"+pos.asLong();}
    /** Encode a block position as an ordered coordinate list. */
    private static List<Integer> xyz(BlockPos pos){return List.of(pos.getX(),pos.getY(),pos.getZ());}
    /** Mark coverage incomplete and retain a bounded set of observer errors. */
    public static void unavailable(String reason){complete=false;if(ERRORS.size()<32)ERRORS.add(reason);}
    /** Record the latest observed natural maturation at a block position. */
    public static void matured(ServerLevel world,BlockPos pos) {
        if(!complete)return;
        String key=key(world,pos);
        if(!MATURITIES.containsKey(key)&&MATURITIES.size()>=MAX_MATURITIES){unavailable("Natural maturation budget exceeded");return;}
        MATURITIES.put(key,new Maturity(world.getGameTime(),++sequence));
    }
    @SuppressWarnings("unchecked")
    /** Accumulate returned loot and attach the consumed maturation marker to amethyst shards. */
    public static void drops(ServerLevel world,BlockPos pos,BlockState state,List<ItemStack> actualDrops) {
        if(!complete)return;
        // Remove the growth marker on a cluster harvest so a later cluster needs its own maturation.
        String key=key(world,pos);Maturity maturity=state.is(Blocks.AMETHYST_CLUSTER)?MATURITIES.remove(key):null;
        // Returned loot contributes counts when the block-drops gamerule permits item spawning.
        if(!world.getGameRules().get(GameRules.BLOCK_DROPS))return;
        long shards=0;Map<String,Long> quantities=new TreeMap<>();
        for(ItemStack stack:actualDrops)if(!stack.isEmpty()) {
            quantities.merge(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),(long)stack.getCount(),Long::sum);
            if(stack.is(Items.AMETHYST_SHARD))shards+=stack.getCount();
        }
        if(quantities.isEmpty())return;
        long harvestedSequence=++sequence;
        if(!DROP_NODES.containsKey(key)&&DROP_NODES.size()>=MAX_NODES){unavailable("Block-drop position budget exceeded");return;}
        var node=DROP_NODES.computeIfAbsent(key,k->new LinkedHashMap<>(Map.of(
            "dimension",world.dimension().identifier().toString(),"position",xyz(pos),"drops",new TreeMap<String,Long>())));
        // drops holds Map<String, Long> inside the heterogeneous position record.
        var counts=(Map<String,Long>)node.get("drops");quantities.forEach((item,n)->counts.merge(item,n,Long::sum));
        if(state.is(Blocks.AMETHYST_CLUSTER)&&shards>0) {
            if(AMETHYST_EVENTS.size()>=MAX_EVENTS){unavailable("Amethyst harvest-event budget exceeded");return;}
            AMETHYST_EVENTS.add(Map.of("dimension",world.dimension().identifier().toString(),"position",xyz(pos),
                "harvest_tick",world.getGameTime(),"harvest_sequence",harvestedSequence,
                "matured_tick",maturity==null?0L:maturity.tick(),
                "matured_sequence",maturity==null?0L:maturity.sequence(),"count",shards));
        }
    }
    /** Check an event dimension and its position against inclusive block bounds. */
    private static boolean inside(Map<String,Object> entry,ServerLevel world,BlockPos min,BlockPos max) {
        if(!entry.get("dimension").equals(world.dimension().identifier().toString()))return false;
        var pos=(List<?>)entry.get("position");
        for(int axis=0;axis<3;axis++) {
            int n=((Number)pos.get(axis)).intValue();
            int lo=axis==0?min.getX():axis==1?min.getY():min.getZ();
            int hi=axis==0?max.getX():axis==1?max.getY():max.getZ();
            if(n<lo||n>hi)return false;
        }
        return true;
    }
    /** Return cumulative drop nodes, amethyst events, and coverage for a bounded region. */
    public static Map<String,Object> read(ServerLevel world,BlockPos min,BlockPos max) {
        return Map.of("version","harvest-ledger-v1","epoch",EPOCH,"complete",complete,"sequence",sequence,
            "drop_nodes",DROP_NODES.values().stream().filter(n->inside(n,world,min,max)).toList(),
            "amethyst_events",AMETHYST_EVENTS.stream().filter(n->inside(n,world,min,max)).toList(),
            "observer_errors",List.copyOf(ERRORS));
    }
}
