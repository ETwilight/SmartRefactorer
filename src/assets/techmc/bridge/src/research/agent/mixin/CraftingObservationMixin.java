package research.agent.mixin;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import net.minecraft.core.BlockPos;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.CrafterBlock;
import net.minecraft.world.level.block.entity.CrafterBlockEntity;
import net.minecraft.world.level.block.state.BlockState;
import research.agent.CookingLedger;

/** Records a craft only after vanilla selected a valid, nonempty result. */
@Mixin(value=CrafterBlock.class,remap=false)
public abstract class CraftingObservationMixin {
    @Inject(method="dispenseFrom",at=@At(value="INVOKE",target="Lnet/minecraft/world/level/block/entity/CrafterBlockEntity;setCraftingTicksRemaining(I)V"),require=1)
    private void techmc_successful_craft(BlockState state,ServerLevel level,BlockPos pos,CallbackInfo ci) {
        try {
            if(!(level.getBlockEntity(pos) instanceof CrafterBlockEntity crafter)){CookingLedger.unavailable("Successful crafter has no block entity");return;}
            var input=crafter.asCraftInput();
            var holder=CrafterBlock.getPotentialResults(level,input).orElse(null);
            if(holder==null){CookingLedger.unavailable("Successful craft lost its recipe");return;}
            ItemStack result=holder.value().assemble(input);
            if(result.isEmpty()){CookingLedger.unavailable("Successful craft became empty");return;}
            CookingLedger.crafted(level,pos,crafter.getItems(),result,holder.value().getRemainingItems(input));
        } catch(RuntimeException error) {
            CookingLedger.unavailable("Crafting observation failed: "+error.getClass().getSimpleName());
        }
    }
}
