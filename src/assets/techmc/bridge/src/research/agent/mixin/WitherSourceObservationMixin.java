package research.agent.mixin;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.damagesource.DamageSource;
import net.minecraft.world.entity.boss.wither.WitherBoss;
import research.agent.WitherSourceLedger;


@Mixin(value=WitherBoss.class,remap=false)
/**
 * Observe wither pattern initialization and the vanilla star-drop death callback.
 *
 * makeInvulnerable supplies a pending pattern-origin marker. The death-loot return
 * callback records the wither's death position and nearby-bedrock observation.
 *
 * <pre>{@code
 * Map<String, Object> sample = WitherSourceLedger.read(world, min, max);
 * }</pre>
 * The sample links these callbacks with successful entity addition by wither UUID.
 */
public abstract class WitherSourceObservationMixin {
    @Inject(method="makeInvulnerable",at=@At("RETURN"),require=1)
    /** Mark the target wither as observed through the pattern-initialization path. */
    private void techmc_pattern(CallbackInfo ci) {
        WitherSourceLedger.markPatternSpawn((WitherBoss)(Object)this);
    }
    @Inject(method="dropCustomDeathLoot",at=@At("RETURN"),require=1)
    /** Record the completed vanilla star-drop death observation. */
    private void techmc_star_death(ServerLevel world,DamageSource damage,boolean byPlayer,CallbackInfo ci) {
        WitherSourceLedger.starDeath(world,(WitherBoss)(Object)this);
    }
}
