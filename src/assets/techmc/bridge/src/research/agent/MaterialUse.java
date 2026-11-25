package research.agent;
import java.util.*;
import java.util.function.Consumer;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.item.ItemStack;

/**
 * Select and use a declared material already present in the player inventory.
 *
 * At four-tick intervals, the helper finds the requested item, moves its stack to
 * hotbar slot zero, and submits one ordinary use action. If it is absent, the
 * helper submits stop. The declared item ID stays fixed for this operation.
 *
 * <pre>{@code
 * MaterialUse operation = new MaterialUse(materialId);
 * operation.tick(player, command);
 * }</pre>
 * Repeated ticks use delivered matching material as it becomes available.
 */
public final class MaterialUse {
    private final String item;
    private long nextUse;
    /** Store the material item ID for this operation. */
    public MaterialUse(String item){this.item=item;}
    /** Select matching inventory material and submit use, or stop when none is available. */
    public void tick(ServerPlayer player,Consumer<String> command) {
        long tick=player.level().getGameTime();if(tick<nextUse)return;nextUse=tick+4;
        var wanted=BuiltInRegistries.ITEM.getOptional(Identifier.parse(item)).orElseThrow();var inventory=player.getInventory();
        // found is the first matching main-inventory slot, or -1 when none exists.
        int found=-1;
        for(int slot=0;slot<36;slot++)if(inventory.getItem(slot).is(wanted)){found=slot;break;}
        if(found<0){command.accept("player "+player.getPlainTextName()+" stop");return;}
        if(found!=0){ItemStack old=inventory.getItem(0);inventory.setItem(0,inventory.getItem(found));inventory.setItem(found,old);}
        inventory.setSelectedSlot(0);command.accept("player "+player.getPlainTextName()+" use once");
    }
    /** Report the declared item and the four-tick minimum use interval. */
    public Map<String,Object> status(){return Map.of("version","material-use-v1","item",item,"items_injected",0,"minimum_use_interval_ticks",4);}
}
