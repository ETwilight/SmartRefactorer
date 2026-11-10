package research.agent.mixin;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.damagesource.DamageSource;
import net.minecraft.world.entity.boss.wither.WitherBoss;
import research.agent.WitherSourceLedger;

/** Does not cancel, replace, damage, heal or reward anything. */
@Mixin(value=WitherBoss.class,remap=false)
public abstract class WitherSourceObservationMixin {
    @Inject(method="makeInvulnerable",at=@At("RETURN"),require=1)
    private void techmc_pattern(CallbackInfo ci) {
        WitherSourceLedger.markPatternSpawn((WitherBoss)(Object)this);
    }
    @Inject(method="dropCustomDeathLoot",at=@At("RETURN"),require=1)
    private void techmc_star_death(ServerLevel world,DamageSource damage,boolean byPlayer,CallbackInfo ci) {
        WitherSourceLedger.starDeath(world,(WitherBoss)(Object)this);
    }
}
