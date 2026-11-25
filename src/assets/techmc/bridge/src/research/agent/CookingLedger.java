package research.agent;

import java.util.*;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.item.ItemStack;

/**
 * Accumulate observed vanilla processing inputs, outputs, and fuel by machine position.
 *
 * Server-thread observers add successful cooking, crafting, and brewing transactions.
 * Nodes retain dimension, position, and item-count maps for consumed, produced, and
 * fuel items. A process-local epoch identifies a continuous ledger lifetime;
 * observer failures and the node limit mark its coverage incomplete.
 *
 * <pre>{@code
 * Map<String, Object> sample = CookingLedger.read(world, min, max);
 * }</pre>
 * Two samples from the same epoch provide cumulative counts for the selected region.
 */
public final class CookingLedger {
    private static final String EPOCH=UUID.randomUUID().toString();
    private static final Map<String,Map<String,Object>> NODES=new HashMap<>();
    private static boolean complete=true;
    private static final Set<String> observerErrors=new LinkedHashSet<>();
    /** Mark ledger coverage incomplete and retain a bounded set of observer errors. */
    public static void unavailable(String reason){complete=false;if(observerErrors.size()<32)observerErrors.add(reason);}
    /** Get or initialize a dimension/position node with item-count maps. */
    private static Map<String,Object> node(ServerLevel world,BlockPos pos) {
        String key=world.dimension().identifier()+":"+pos.toShortString();
        if(NODES.size()>65536&&!NODES.containsKey(key)){complete=false;return new LinkedHashMap<>(Map.of("fuel",new TreeMap<String,Long>(),"consumed",new TreeMap<String,Long>(),"produced",new TreeMap<String,Long>()));}
        return NODES.computeIfAbsent(key,k->new LinkedHashMap<>(Map.of("dimension",world.dimension().identifier().toString(),"position",List.of(pos.getX(),pos.getY(),pos.getZ()),"consumed",new TreeMap<String,Long>(),"produced",new TreeMap<String,Long>(),"fuel",new TreeMap<String,Long>())));
    }
    @SuppressWarnings("unchecked")
    /** Add a positive item quantity to one named node counter map. */
    private static void add(Map<String,Object> node,String field,ItemStack stack,int count) {
        if(stack.isEmpty()||count<=0)return;
        // The named fields contain Map<String, Long> values within the heterogeneous node map.
        ((Map<String,Long>)node.get(field)).merge(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),(long)count,Long::sum);
    }
    /** Accumulate the consumed and produced stacks of one observed transaction. */
    public static void transaction(ServerLevel world,BlockPos pos,List<ItemStack> consumed,List<ItemStack> produced) {
        var value=node(world,pos);
        for(ItemStack stack:consumed)add(value,"consumed",stack,stack.getCount());
        for(ItemStack stack:produced)add(value,"produced",stack,stack.getCount());
    }
    /** Count one fuel item and the container remainder produced when its stack is exhausted. */
    public static void fuel(ServerLevel world,BlockPos pos,ItemStack fuel) {
        var value=node(world,pos);add(value,"fuel",fuel,1);
        var remainder=fuel.getItem().getCraftingRemainder();
        if(fuel.getCount()==1&&remainder!=null)add(value,"produced",remainder.create(),remainder.count());
    }
    /** Count one cooked input and the recipe output stack. */
    public static void cooked(ServerLevel world,BlockPos pos,ItemStack input,ItemStack result) {
        var value=node(world,pos);add(value,"consumed",input,1);add(value,"produced",result,result.getCount());
    }
    /** Count one item from each used ingredient slot, plus output and remainders. */
    public static void crafted(ServerLevel world,BlockPos pos,List<ItemStack> ingredients,ItemStack result,List<ItemStack> remainders) {
        List<ItemStack> consumed=new ArrayList<>(),produced=new ArrayList<>();
        for(ItemStack ingredient:ingredients)if(!ingredient.isEmpty())consumed.add(ingredient.copyWithCount(1));
        produced.add(result);produced.addAll(remainders);
        transaction(world,pos,consumed,produced);
    }
    /** Return cumulative nodes within inclusive bounds and the ledger coverage metadata. */
    public static Map<String,Object> read(ServerLevel world,BlockPos min,BlockPos max) {
        var values=NODES.values().stream().filter(n->{
            if(!n.get("dimension").equals(world.dimension().identifier().toString()))return false;
            var p=(List<?>)n.get("position");
            for(int i=0;i<3;i++){int v=((Number)p.get(i)).intValue(),a=i==0?min.getX():i==1?min.getY():min.getZ(),b=i==0?max.getX():i==1?max.getY():max.getZ();if(v<a||v>b)return false;}
            return true;
        }).toList();
        return Map.of("version","cooking-ledger-v1","epoch",EPOCH,"complete",complete,"observers",List.of("cooking","crafting","brewing"),"observer_errors",List.copyOf(observerErrors),"nodes",values);
    }
}
