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
public abstract class RaidObservationMixin {
    @Inject(method="createOrExtendRaid",at=@At("RETURN"),require=1)
    private void techmc_trigger(ServerPlayer player,BlockPos pos,CallbackInfoReturnable<Raid> ci){
        if(ci.getReturnValue()!=null)RaidLedger.triggered(player);
    }
}
