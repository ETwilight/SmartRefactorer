package research.agent;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.util.Map;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.block.ButtonBlock;
import net.minecraft.world.level.block.LeverBlock;
import static research.agent.RpcServer.*;

/**
 * Operate existing machine levers and buttons through their vanilla block methods.
 *
 * Requests name a loaded, ticking block position and either a target lever state
 * or one button press. The response includes before/after block state and game tick.
 *
 * <pre>{@code
 * JsonElement result = MachineControls.apply(world, params);
 * }</pre>
 * params carries the position, action, and optional boolean lever state.
 */
public final class MachineControls {
    /** Select the existing control, perform its vanilla action, and return state readback. */
    public static JsonElement apply(ServerLevel world,JsonObject p) {
        BlockPos pos=ServerBridge.position(p);
        if(!world.hasChunkAt(pos)||!world.isPositionEntityTicking(pos))throw new IllegalStateException("Machine control is not loaded and ticking");
        var state=world.getBlockState(pos);String action=str(p,"action");
        if(action.equals("set_lever")) {
            if(!(state.getBlock() instanceof LeverBlock lever)||!p.has("powered")||!p.get("powered").isJsonPrimitive()||!p.get("powered").getAsJsonPrimitive().isBoolean())
                throw new IllegalArgumentException("set_lever requires an existing lever and a boolean powered state");
            // Pull a lever when its current state differs from the requested state.
            if(state.getValue(LeverBlock.POWERED)!=p.get("powered").getAsBoolean())lever.pull(state,world,pos,null);
        } else if(action.equals("press_button")) {
            if(!(state.getBlock() instanceof ButtonBlock button)||p.has("powered"))throw new IllegalArgumentException("press_button requires an existing button and no powered override");
            button.press(state,world,pos,null);
        } else throw new IllegalArgumentException("Only existing lever selection and button presses are permitted");
        var after=world.getBlockState(pos);
        return json(Map.of("source","vanilla_machine_control","action",action,"block",BuiltInRegistries.BLOCK.getKey(after.getBlock()).toString(),
            "before",state.toString(),"after",after.toString(),"tick",world.getGameTime()));
    }
}
