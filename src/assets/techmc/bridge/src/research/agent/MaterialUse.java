package research.agent;
import java.util.*;
import java.util.function.Consumer;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.item.ItemStack;

/** Select only delivered material; never use an incidental picked-up item. */
public final class MaterialUse {
    private final String item;
    private long nextUse;
    public MaterialUse(String item){this.item=item;}
    public void tick(ServerPlayer player,Consumer<String> command) {
        long tick=player.level().getGameTime();if(tick<nextUse)return;nextUse=tick+4;
        var wanted=BuiltInRegistries.ITEM.getOptional(Identifier.parse(item)).orElseThrow();var inventory=player.getInventory();
        int found=-1;
        for(int slot=0;slot<36;slot++)if(inventory.getItem(slot).is(wanted)){found=slot;break;}
        if(found<0){command.accept("player "+player.getPlainTextName()+" stop");return;}
        if(found!=0){ItemStack old=inventory.getItem(0);inventory.setItem(0,inventory.getItem(found));inventory.setItem(found,old);}
        inventory.setSelectedSlot(0);command.accept("player "+player.getPlainTextName()+" use once");
    }
    public Map<String,Object> status(){return Map.of("version","material-use-v1","item",item,"items_injected",0,"minimum_use_interval_ticks",4);}
}
