package research.agent.mixin;

import java.util.List;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;
import net.minecraft.core.BlockPos;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.state.BlockBehaviour;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.storage.loot.LootParams;
import net.minecraft.world.level.storage.loot.parameters.LootContextParams;
import research.agent.HarvestLedger;

@Mixin(value=BlockBehaviour.BlockStateBase.class,remap=false)
public abstract class HarvestObservationMixin {
    @Inject(method="getDrops",at=@At("RETURN"),require=1)
    private void techmc_actual_drops(LootParams.Builder params,CallbackInfoReturnable<List<ItemStack>> ci) {
        try {
            var origin=params.getOptionalParameter(LootContextParams.ORIGIN);
            if(origin==null){if(!ci.getReturnValue().isEmpty())HarvestLedger.unavailable("Block loot has no origin");return;}
            HarvestLedger.drops(params.getLevel(),BlockPos.containing(origin),(BlockState)(Object)this,ci.getReturnValue());
        } catch(RuntimeException error) {
            HarvestLedger.unavailable("Block-drop observation failed: "+error.getClass().getSimpleName());
        }
    }
}
