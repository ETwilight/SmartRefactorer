package research.agent.mixin;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Unique;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.core.BlockPos;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.AbstractCookingRecipe;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.SingleRecipeInput;
import net.minecraft.world.level.block.entity.AbstractFurnaceBlockEntity;
import net.minecraft.world.level.block.state.BlockState;
import research.agent.CookingLedger;

@Mixin(value=AbstractFurnaceBlockEntity.class,remap=false)
/**
 * Connect furnace fuel and completed recipe callbacks to CookingLedger.
 *
 * The pre-burn hook copies the input stack. setRecipeUsed consumes that thread-local
 * copy and derives the completed output from the running recipe definition.
 * Fuel is recorded immediately before the vanilla consumeFuel call.
 *
 * <pre>{@code
 * Map<String, Object> sample = CookingLedger.read(world, min, max);
 * }</pre>
 * The sample exposes cumulative furnace consumption and production in the region.
 */
public abstract class CookingObservationMixin {
    @Unique private static final ThreadLocal<ItemStack> techmc_input=new ThreadLocal<>();
    @Inject(method="serverTick",at=@At(value="INVOKE",target="Lnet/minecraft/world/level/block/entity/AbstractFurnaceBlockEntity;consumeFuel(Lnet/minecraft/core/NonNullList;Lnet/minecraft/world/item/ItemStack;)V"),require=1)
    /** Read the furnace fuel slot at the consumption callback. */
    private static void techmc_fuel(ServerLevel world,BlockPos pos,BlockState state,AbstractFurnaceBlockEntity entity,CallbackInfo ci) {
        CookingLedger.fuel(world,pos,entity.getItem(1));
    }
    @Inject(method="serverTick",at=@At(value="INVOKE",target="Lnet/minecraft/world/level/block/entity/AbstractFurnaceBlockEntity;burn(Lnet/minecraft/core/NonNullList;Lnet/minecraft/world/item/ItemStack;Lnet/minecraft/world/item/ItemStack;)V"),require=1)
    /** Copy the input stack before vanilla mutates the cooking inventory. */
    private static void techmc_input(ServerLevel world,BlockPos pos,BlockState state,AbstractFurnaceBlockEntity entity,CallbackInfo ci) {
        techmc_input.set(entity.getItem(0).copy());
    }
    @Inject(method="setRecipeUsed",at=@At("HEAD"),require=1)
    /** Consume the saved input and record the completed cooking recipe output. */
    private void techmc_cooked(RecipeHolder<?> recipe,CallbackInfo ci) {
        // Mixin applies this callback to a furnace instance; recover that target type.
        var self=(AbstractFurnaceBlockEntity)(Object)this;
        ItemStack input=techmc_input.get();techmc_input.remove();
        if(input!=null&&self.getLevel() instanceof ServerLevel world&&recipe!=null&&recipe.value() instanceof AbstractCookingRecipe cooking)
            CookingLedger.cooked(world,self.getBlockPos(),input,cooking.assemble(new SingleRecipeInput(input)));
    }
}
