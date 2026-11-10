package research.agent.mixin;

import java.util.ArrayDeque;
import java.util.Deque;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Unique;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.util.RandomSource;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.BuddingAmethystBlock;
import net.minecraft.world.level.block.state.BlockState;
import research.agent.HarvestLedger;

@Mixin(value=BuddingAmethystBlock.class,remap=false)
public abstract class AmethystMaturityObservationMixin {
    @Unique private static final ThreadLocal<Deque<byte[]>> techmc_neighbors=ThreadLocal.withInitial(ArrayDeque::new);
    @Inject(method="randomTick",at=@At("HEAD"),require=1)
    private void techmc_before_growth(BlockState state,ServerLevel world,BlockPos pos,RandomSource random,CallbackInfo ci) {
        byte[] before=new byte[Direction.values().length];
        try {
            int index=0;
            for(Direction direction:Direction.values()) {
                BlockPos neighbor=pos.relative(direction);
                // Observation must not cause a neighboring chunk to load.
                before[index++]=!world.hasChunkAt(neighbor)?-1:(byte)(world.getBlockState(neighbor).is(Blocks.AMETHYST_CLUSTER)?1:0);
            }
        } catch(RuntimeException error){HarvestLedger.unavailable("Amethyst pre-growth observation failed: "+error.getClass().getSimpleName());}
        techmc_neighbors.get().push(before);
    }
    @Inject(method="randomTick",at=@At("RETURN"),require=1)
    private void techmc_after_growth(BlockState state,ServerLevel world,BlockPos pos,RandomSource random,CallbackInfo ci) {
        try {
            var stack=techmc_neighbors.get();
            if(stack.isEmpty()){HarvestLedger.unavailable("Missing amethyst pre-growth observation");return;}
            byte[] before=stack.pop();int index=0;
            for(Direction direction:Direction.values()) {
                BlockPos neighbor=pos.relative(direction);
                if(before[index++]==0&&world.hasChunkAt(neighbor)&&world.getBlockState(neighbor).is(Blocks.AMETHYST_CLUSTER))HarvestLedger.matured(world,neighbor);
            }
            if(stack.isEmpty())techmc_neighbors.remove();
        } catch(RuntimeException error){HarvestLedger.unavailable("Amethyst post-growth observation failed: "+error.getClass().getSimpleName());}
    }
}
