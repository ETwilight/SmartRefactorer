package research.agent.mixin;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.entity.Entity;
import research.agent.WitherSourceLedger;

@Mixin(value=ServerLevel.class,remap=false)
/**
 * Connect successful server entity addition to retained wither source traces.
 *
 * The callback passes the target ServerLevel, supplied entity, and vanilla return
 * flag to the ledger. The ledger recognizes withers and consumes a pending pattern
 * marker when available.
 *
 * <pre>{@code
 * Map<String, Object> sample = WitherSourceLedger.read(world, min, max);
 * }</pre>
 * The spawn entries in the sample retain the source marker observed before addition.
 */
public abstract class WitherSpawnObservationMixin {
    @Inject(method="addFreshEntity",at=@At("RETURN"),require=1)
    /** Forward the entity-add result and its server-level target to the source ledger. */
    private void techmc_added(Entity entity,CallbackInfoReturnable<Boolean> ci) {
        WitherSourceLedger.added((ServerLevel)(Object)this,entity,Boolean.TRUE.equals(ci.getReturnValue()));
    }
}
