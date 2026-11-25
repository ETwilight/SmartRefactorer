package research.agent;
import java.util.*;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.InteractionHand;
import java.util.function.Consumer;

/**
 * Supply a declared offhand material from the player's own inventory.
 *
 * The initial main-hand item ID identifies the operating tool. At four-tick
 * intervals, the helper selects slot zero, checks that tool, moves a matching stack
 * to an empty offhand, and submits an ordinary use action. moved counts items
 * transferred from the main inventory to the offhand.
 *
 * <pre>{@code
 * OffhandUse operation = new OffhandUse(materialId, player);
 * operation.tick(player, command);
 * }</pre>
 * The evaluator repeats tick while the same player operates with the declared tool.
 */
public final class OffhandUse {
    private final String item;
    private int moved;
    private final String tool;
    private long nextUse;
    /** Store the declared material and the initial main-hand tool item ID. */
    public OffhandUse(String item,ServerPlayer player){this.item=item;this.tool=BuiltInRegistries.ITEM.getKey(player.getMainHandItem().getItem()).toString();}
    /** Check the tool, supply an empty offhand, and submit use at the scheduled interval. */
    public void tick(ServerPlayer player,Consumer<String> command) {
        long tick=player.level().getGameTime();if(tick<nextUse)return;nextUse=tick+4;
        var inventory=player.getInventory();inventory.setSelectedSlot(0);
        if(player.getMainHandItem().isEmpty()||!BuiltInRegistries.ITEM.getKey(player.getMainHandItem().getItem()).toString().equals(tool)){
            command.accept("player "+player.getPlainTextName()+" stop");return;
        }
        var required=BuiltInRegistries.ITEM.getOptional(Identifier.parse(item)).orElseThrow();
        for(int slot=0;player.getOffhandItem().isEmpty()&&slot<36;slot++)if(inventory.getItem(slot).is(required)) {
            // Transfer the existing ItemStack reference after clearing its original inventory slot.
            var stack=inventory.getItem(slot);inventory.setItem(slot,net.minecraft.world.item.ItemStack.EMPTY);
            player.setItemInHand(InteractionHand.OFF_HAND,stack);moved+=stack.getCount();break;
        }
        if(player.getOffhandItem().is(required))command.accept("player "+player.getPlainTextName()+" use once");
    }
    /** Report the declared material and cumulative internal inventory transfer count. */
    public Map<String,Object> status(){return Map.of("version","offhand-use-v1","item",item,"moved_from_own_inventory",moved,"items_injected",0);}
}
