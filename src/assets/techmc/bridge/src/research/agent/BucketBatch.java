package research.agent;

import java.util.*;
import java.util.function.Consumer;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;

/** Fixed inventory batch. Slot selection and normal use only; never refill or empty a filled bucket. */
public final class BucketBatch {
    private final String product;
    private String error="";
    private boolean complete;
    public BucketBatch(ServerPlayer player,String product) {
        if(!Set.of("minecraft:lava_bucket","minecraft:powder_snow_bucket","minecraft:milk_bucket").contains(product))throw new IllegalArgumentException("Unsupported bucket product");
        this.product=product;
        if(!player.gameMode.isSurvival())throw new IllegalArgumentException("Bucket batch requires survival mode");
        player.getInventory().clearContent();
        for(int slot=0;slot<36;slot++)player.getInventory().setItem(slot,new ItemStack(Items.BUCKET));
        player.getInventory().setSelectedSlot(0);
    }
    public void tick(ServerPlayer player,Consumer<String> command) {
        if(complete||!error.isEmpty())return;
        var inventory=player.getInventory();int emptySlot=-1,filled=0;
        var output=BuiltInRegistries.ITEM.getValue(Identifier.parse(product));
        for(int slot=0;slot<36;slot++) {
            ItemStack stack=inventory.getItem(slot);
            if(stack.getCount()!=1 || (!stack.is(Items.BUCKET)&&!stack.is(output))) {error="Batch inventory must contain exactly the original 36 buckets";return;}
            if(stack.is(output))filled++;else if(emptySlot<0)emptySlot=slot;
        }
        for(int slot=36;slot<inventory.getContainerSize();slot++)if(!inventory.getItem(slot).isEmpty()){error="Batch equipment and offhand must remain empty";return;}
        if(filled==36){complete=true;return;}
        if(emptySlot>=9) {
            ItemStack old=inventory.getItem(0);inventory.setItem(0,inventory.getItem(emptySlot));inventory.setItem(emptySlot,old);emptySlot=0;
        }
        inventory.setSelectedSlot(emptySlot);
        // One attempt per game tick. Filled buckets are never used to place their contents.
        command.accept("player "+player.getPlainTextName()+" use once");
    }
    public Map<String,Object> status(){return Map.of("version","bucket-batch-v1","product",product,"count",36,"complete",complete,"error",error);}
    public static Map<String,Object> inventory(ServerPlayer player,BucketBatch batch) {
        List<Object> slots=new ArrayList<>();Map<String,Integer> counts=new TreeMap<>();
        for(int slot=0;slot<player.getInventory().getContainerSize();slot++) {
            var stack=player.getInventory().getItem(slot);String item=BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            slots.add(Map.of("index",slot,"item",item,"count",stack.getCount(),"item_components",ItemComponents.read(stack)));
            if(!stack.isEmpty())counts.merge(item,stack.getCount(),Integer::sum);
        }
        Map<String,Object> result=new LinkedHashMap<>(Map.of("version","fake-inventory-v1","slots",slots,"counts",counts,"main_slots",36,"tick",player.level().getGameTime()));
        if(batch!=null)result.put("batch",batch.status());return result;
    }
}
