package research.agent;
import java.util.*;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.InteractionHand;
import java.util.function.Consumer;

/** Performs inventory moves only; the existing use action does all world interaction. */
public final class OffhandUse {
    private final String item;
    private int moved;
    private final String tool;
    private long nextUse;
    public OffhandUse(String item,ServerPlayer player){this.item=item;this.tool=BuiltInRegistries.ITEM.getKey(player.getMainHandItem().getItem()).toString();}
    public void tick(ServerPlayer player,Consumer<String> command) {
        long tick=player.level().getGameTime();if(tick<nextUse)return;nextUse=tick+4;
        var inventory=player.getInventory();inventory.setSelectedSlot(0);
        if(player.getMainHandItem().isEmpty()||!BuiltInRegistries.ITEM.getKey(player.getMainHandItem().getItem()).toString().equals(tool)){
            command.accept("player "+player.getPlainTextName()+" stop");return;
        }
        var required=BuiltInRegistries.ITEM.getOptional(Identifier.parse(item)).orElseThrow();
        for(int slot=0;player.getOffhandItem().isEmpty()&&slot<36;slot++)if(inventory.getItem(slot).is(required)) {
            var stack=inventory.getItem(slot);inventory.setItem(slot,net.minecraft.world.item.ItemStack.EMPTY);
            player.setItemInHand(InteractionHand.OFF_HAND,stack);moved+=stack.getCount();break;
        }
        if(player.getOffhandItem().is(required))command.accept("player "+player.getPlainTextName()+" use once");
    }
    public Map<String,Object> status(){return Map.of("version","offhand-use-v1","item",item,"moved_from_own_inventory",moved,"items_injected",0);}
}
