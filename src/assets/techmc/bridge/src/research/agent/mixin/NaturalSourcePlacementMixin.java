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
/**
 * Apply protected-source placement policy to block items in natural worlds.
 *
 * The place head callback checks the running world flag and the block's registry ID.
 * A protected block returns InteractionResult.FAIL before ordinary placement.
 *
 * <pre>{@code
 * boolean protectedSource = CommandPolicy.isProtectedNaturalSource(blockId);
 * }</pre>
 * The callback uses this same classification when a block item attempts placement.
 */
public abstract class NaturalSourcePlacementMixin {
    @Inject(method="place",at=@At("HEAD"),cancellable=true,require=1)
    /** Return placement failure for a protected source block on a natural-world server. */
    private void techmc_natural_source(BlockPlaceContext context,CallbackInfoReturnable<InteractionResult> ci) {
        if(Boolean.getBoolean("minecraft.agent.natural_world")&&context.getLevel() instanceof ServerLevel
            &&CommandPolicy.isProtectedNaturalSource(BuiltInRegistries.BLOCK.getKey(((BlockItem)(Object)this).getBlock()).toString()))
            ci.setReturnValue(InteractionResult.FAIL);
    }
}
