package research.agent.mixin;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.entity.Entity;
import research.agent.WitherSourceLedger;

@Mixin(value=ServerLevel.class,remap=false)
public abstract class WitherSpawnObservationMixin {
    @Inject(method="addFreshEntity",at=@At("RETURN"),require=1)
    private void techmc_added(Entity entity,CallbackInfoReturnable<Boolean> ci) {
        WitherSourceLedger.added((ServerLevel)(Object)this,entity,Boolean.TRUE.equals(ci.getReturnValue()));
    }
}
