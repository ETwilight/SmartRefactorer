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
public abstract class CookingObservationMixin {
    @Unique private static final ThreadLocal<ItemStack> techmc_input=new ThreadLocal<>();
    @Inject(method="serverTick",at=@At(value="INVOKE",target="Lnet/minecraft/world/level/block/entity/AbstractFurnaceBlockEntity;consumeFuel(Lnet/minecraft/core/NonNullList;Lnet/minecraft/world/item/ItemStack;)V"),require=1)
    private static void techmc_fuel(ServerLevel world,BlockPos pos,BlockState state,AbstractFurnaceBlockEntity entity,CallbackInfo ci) {
        CookingLedger.fuel(world,pos,entity.getItem(1));
    }
    @Inject(method="serverTick",at=@At(value="INVOKE",target="Lnet/minecraft/world/level/block/entity/AbstractFurnaceBlockEntity;burn(Lnet/minecraft/core/NonNullList;Lnet/minecraft/world/item/ItemStack;Lnet/minecraft/world/item/ItemStack;)V"),require=1)
    private static void techmc_input(ServerLevel world,BlockPos pos,BlockState state,AbstractFurnaceBlockEntity entity,CallbackInfo ci) {
        techmc_input.set(entity.getItem(0).copy());
    }
    @Inject(method="setRecipeUsed",at=@At("HEAD"),require=1)
    private void techmc_cooked(RecipeHolder<?> recipe,CallbackInfo ci) {
        var self=(AbstractFurnaceBlockEntity)(Object)this;
        ItemStack input=techmc_input.get();techmc_input.remove();
        if(input!=null&&self.getLevel() instanceof ServerLevel world&&recipe!=null&&recipe.value() instanceof AbstractCookingRecipe cooking)
            CookingLedger.cooked(world,self.getBlockPos(),input,cooking.assemble(new SingleRecipeInput(input)));
    }
}
