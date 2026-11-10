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

/** Observes returned vanilla block loot and natural maturation; never rolls loot. */
public final class HarvestLedger {
    private static final String EPOCH=UUID.randomUUID().toString();
    private static final int MAX_NODES=65536,MAX_MATURITIES=65536,MAX_EVENTS=262144;
    private static final Map<String,Map<String,Object>> DROP_NODES=new HashMap<>();
    private static final Map<String,Maturity> MATURITIES=new HashMap<>();
    private static final List<Map<String,Object>> AMETHYST_EVENTS=new ArrayList<>();
    private static final Set<String> ERRORS=new LinkedHashSet<>();
    private static long sequence;
    private static boolean complete=true;
    private record Maturity(long tick,long sequence) {}
    private static String key(ServerLevel world,BlockPos pos){return world.dimension().identifier()+":"+pos.asLong();}
    private static List<Integer> xyz(BlockPos pos){return List.of(pos.getX(),pos.getY(),pos.getZ());}
    public static void unavailable(String reason){complete=false;if(ERRORS.size()<32)ERRORS.add(reason);}
    public static void matured(ServerLevel world,BlockPos pos) {
        if(!complete)return;
        String key=key(world,pos);
        if(!MATURITIES.containsKey(key)&&MATURITIES.size()>=MAX_MATURITIES){unavailable("Natural maturation budget exceeded");return;}
        MATURITIES.put(key,new Maturity(world.getGameTime(),++sequence));
    }
    @SuppressWarnings("unchecked")
    public static void drops(ServerLevel world,BlockPos pos,BlockState state,List<ItemStack> actualDrops) {
        if(!complete)return;
        String key=key(world,pos);Maturity maturity=state.is(Blocks.AMETHYST_CLUSTER)?MATURITIES.remove(key):null;
        // Vanilla suppresses item spawning when block drops are disabled.
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
        var counts=(Map<String,Long>)node.get("drops");quantities.forEach((item,n)->counts.merge(item,n,Long::sum));
        if(state.is(Blocks.AMETHYST_CLUSTER)&&shards>0) {
            if(AMETHYST_EVENTS.size()>=MAX_EVENTS){unavailable("Amethyst harvest-event budget exceeded");return;}
            AMETHYST_EVENTS.add(Map.of("dimension",world.dimension().identifier().toString(),"position",xyz(pos),
                "harvest_tick",world.getGameTime(),"harvest_sequence",harvestedSequence,
                "matured_tick",maturity==null?0L:maturity.tick(),
                "matured_sequence",maturity==null?0L:maturity.sequence(),"count",shards));
        }
    }
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
    public static Map<String,Object> read(ServerLevel world,BlockPos min,BlockPos max) {
        return Map.of("version","harvest-ledger-v1","epoch",EPOCH,"complete",complete,"sequence",sequence,
            "drop_nodes",DROP_NODES.values().stream().filter(n->inside(n,world,min,max)).toList(),
            "amethyst_events",AMETHYST_EVENTS.stream().filter(n->inside(n,world,min,max)).toList(),
            "observer_errors",List.copyOf(ERRORS));
    }
}
