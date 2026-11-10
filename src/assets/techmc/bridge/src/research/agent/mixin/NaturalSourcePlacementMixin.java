package research.agent.mixin;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.item.BlockItem;
import net.minecraft.world.item.context.BlockPlaceContext;
import research.agent.CommandPolicy;

@Mixin(value=BlockItem.class,remap=false)
public abstract class NaturalSourcePlacementMixin {
    @Inject(method="place",at=@At("HEAD"),cancellable=true,require=1)
    private void techmc_natural_source(BlockPlaceContext context,CallbackInfoReturnable<InteractionResult> ci) {
        if(Boolean.getBoolean("minecraft.agent.natural_world")&&context.getLevel() instanceof ServerLevel
            &&CommandPolicy.isProtectedNaturalSource(BuiltInRegistries.BLOCK.getKey(((BlockItem)(Object)this).getBlock()).toString()))
            ci.setReturnValue(InteractionResult.FAIL);
    }
}
