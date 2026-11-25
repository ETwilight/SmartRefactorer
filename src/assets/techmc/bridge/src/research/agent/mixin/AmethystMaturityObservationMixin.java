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
/**
 * Observe amethyst-cluster maturation across a budding block's random tick.
 *
 * A thread-local stack pairs neighbor states before and after each callback,
 * including nested calls. A transition from a loaded non-cluster to a cluster
 * records maturation in HarvestLedger.
 *
 * <pre>{@code
 * Map<String, Object> sample = HarvestLedger.read(world, min, max);
 * }</pre>
 * The resulting amethyst events can pair a later harvest with this growth observation.
 */
public abstract class AmethystMaturityObservationMixin {
    @Unique private static final ThreadLocal<Deque<byte[]>> techmc_neighbors=ThreadLocal.withInitial(ArrayDeque::new);
    @Inject(method="randomTick",at=@At("HEAD"),require=1)
    /** Capture the six neighbor cluster states before the random tick. */
    private void techmc_before_growth(BlockState state,ServerLevel world,BlockPos pos,RandomSource random,CallbackInfo ci) {
        byte[] before=new byte[Direction.values().length];
        try {
            int index=0;
            for(Direction direction:Direction.values()) {
                BlockPos neighbor=pos.relative(direction);
                // An unloaded neighbor receives -1; loaded non-clusters receive 0 and clusters 1.
                before[index++]=!world.hasChunkAt(neighbor)?-1:(byte)(world.getBlockState(neighbor).is(Blocks.AMETHYST_CLUSTER)?1:0);
            }
        } catch(RuntimeException error){HarvestLedger.unavailable("Amethyst pre-growth observation failed: "+error.getClass().getSimpleName());}
        // Pair snapshots in LIFO order when random-tick observation is nested.
        techmc_neighbors.get().push(before);
    }
    @Inject(method="randomTick",at=@At("RETURN"),require=1)
    /** Consume the matching snapshot and record newly matured loaded clusters. */
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
