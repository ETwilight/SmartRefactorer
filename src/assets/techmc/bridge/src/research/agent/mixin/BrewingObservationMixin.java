package research.agent.mixin;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Unique;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import net.minecraft.core.BlockPos;
import net.minecraft.core.NonNullList;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.Level;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.entity.BrewingStandBlockEntity;
import net.minecraft.world.level.block.state.BlockState;
import research.agent.CookingLedger;
import java.util.*;

@Mixin(value=BrewingStandBlockEntity.class,remap=false)
public abstract class BrewingObservationMixin {
    @Unique private static final ThreadLocal<List<ItemStack>> techmc_before=new ThreadLocal<>();
    @Inject(method="serverTick",at=@At(value="INVOKE",target="Lnet/minecraft/world/item/ItemStack;shrink(I)V",ordinal=0),require=1)
    private static void techmc_fuel(Level level,BlockPos pos,BlockState state,BrewingStandBlockEntity entity,CallbackInfo ci) {
        if(level instanceof ServerLevel world)CookingLedger.fuel(world,pos,entity.getItem(4));
    }
    @Inject(method="doBrew",at=@At("HEAD"),require=1)
    private static void techmc_before(Level level,BlockPos pos,NonNullList<ItemStack> items,CallbackInfo ci) {
        techmc_before.set(items.subList(0,4).stream().map(ItemStack::copy).toList());
    }
    @Inject(method="doBrew",at=@At("RETURN"),require=1)
    private static void techmc_after(Level level,BlockPos pos,NonNullList<ItemStack> items,CallbackInfo ci) {
        var before=techmc_before.get();techmc_before.remove();
        if(!(level instanceof ServerLevel world)||before==null)return;
        List<ItemStack> consumed=new ArrayList<>(),produced=new ArrayList<>();
        for(int i=0;i<3;i++)if(!ItemStack.isSameItemSameComponents(before.get(i),items.get(i))) {
            if(!before.get(i).isEmpty())consumed.add(before.get(i));
            if(!items.get(i).isEmpty())produced.add(items.get(i).copy());
        }
        consumed.add(before.get(3).copyWithCount(1));
        var remainder=before.get(3).getItem().getCraftingRemainder();
        if(remainder!=null)produced.add(remainder.create());
        CookingLedger.transaction(world,pos,consumed,produced);
    }
}
