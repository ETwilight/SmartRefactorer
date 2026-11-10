package research.agent;

import java.util.*;
import java.util.function.Consumer;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.core.component.DataComponents;
import net.minecraft.world.effect.MobEffects;
import net.minecraft.world.item.Items;
import net.minecraft.stats.Stats;

/** One stationary player: attack normally, drink a delivered bottle when needed. */
public final class CombinedOperation {
    private final int initialDrinks;
    private final long initialRaids;
    private boolean drinking,attacking;
    private String error="";
    public CombinedOperation(ServerPlayer player){initialDrinks=drinks(player);initialRaids=RaidLedger.count(player);}
    private static int drinks(ServerPlayer p){return p.getStats().getValue(Stats.ITEM_USED.get(Items.OMINOUS_BOTTLE));}
    public void tick(ServerPlayer player,Consumer<String> command) {
        if(!error.isEmpty())return;
        String name=player.getPlainTextName();var inventory=player.getInventory();
        if(drinking) {
            if(player.isUsingItem())return;
            drinking=false;inventory.setSelectedSlot(0);command.accept("player "+name+" stop");attacking=false;
        }
        var raid=player.level().getRaidAt(player.blockPosition());
        boolean occupied=player.hasEffect(MobEffects.BAD_OMEN)||player.hasEffect(MobEffects.RAID_OMEN)||player.hasEffect(MobEffects.TRIAL_OMEN)||(raid!=null&&!raid.isOver()&&!raid.isStopped());
        if(!occupied)for(int slot=0;slot<36;slot++) {
            var stack=inventory.getItem(slot);
            if(!stack.is(Items.OMINOUS_BOTTLE))continue;
            var component=stack.get(DataComponents.OMINOUS_BOTTLE_AMPLIFIER);
            int amplifier=component==null?0:component.value();
            if(amplifier<0||amplifier>4){error="Ominous bottle amplifier is outside the vanilla survival range";command.accept("player "+name+" stop");return;}
            if(slot>=9){var old=inventory.getItem(1);inventory.setItem(1,stack);inventory.setItem(slot,old);slot=1;}
            command.accept("player "+name+" stop");inventory.setSelectedSlot(slot);command.accept("player "+name+" use continuous");drinking=true;attacking=false;return;
        }
        inventory.setSelectedSlot(0);
        if(!attacking){command.accept("player "+name+" attack continuous");attacking=true;}
    }
    public Map<String,Object> status(ServerPlayer player){return Map.of("version","combined-operation-v1","bottles_drunk",drinks(player)-initialDrinks,"raids_triggered",RaidLedger.count(player)-initialRaids,"drinking",drinking,"error",error,"replenished",false);}
}
