package research.agent.mixin;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.world.entity.raid.Raid;
import net.minecraft.world.entity.raid.Raids;
import research.agent.RaidLedger;
@Mixin(value=Raids.class,remap=false)
/**
 * Count a player raid trigger when vanilla returns a raid instance.
 *
 * The createOrExtendRaid return callback increments the triggering player's
 * process-local counter for a non-null raid result.
 *
 * <pre>{@code
 * long observed = RaidLedger.count(player);
 * }</pre>
 * The evaluator reads this counter to compare raid activity across an operating trial.
 */
public abstract class RaidObservationMixin {
    @Inject(method="createOrExtendRaid",at=@At("RETURN"),require=1)
    /** Increment the player counter for a returned raid instance. */
    private void techmc_trigger(ServerPlayer player,BlockPos pos,CallbackInfoReturnable<Raid> ci){
        if(ci.getReturnValue()!=null)RaidLedger.triggered(player);
    }
}
