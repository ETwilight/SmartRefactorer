package research.agent;

import com.google.gson.*;
import com.mojang.serialization.JsonOps;
import net.minecraft.resources.RegistryOps;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Screenshot;
import net.minecraft.client.input.*;
import net.minecraft.client.gui.components.debug.*;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.inventory.ContainerInput;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.TooltipFlag;
import net.minecraft.world.phys.*;
import java.nio.file.*;
import java.util.*;
import static research.agent.RpcServer.*;

public class ClientBridge implements ClientModInitializer {
    private static final boolean CLIENT_INTERFACES_DISABLED = true;
    private Minecraft mc;
    private RpcServer rpc;
    private final Map<String, JsonObject> actions = new LinkedHashMap<>();
    private String active, owner;
    private long deadline;
    private Vec3 destination;
    private BlockPos breaking;
    private Direction breakingFace;
    private final ArrayDeque<Vec3> waypoints=new ArrayDeque<>();
    private BlockHitResult interacting;
    private InteractionHand interactionHand;
    private int interactionDelay;
    private boolean respawning;
    public void onInitializeClient() {
        if (CLIENT_INTERFACES_DISABLED) return;
        mc = Minecraft.getInstance();
        try { rpc = new RpcServer(Integer.getInteger("minecraft.agent.client.port",8765), "client", mc::execute, this::dispatch); }
        catch (Exception e) { throw new RuntimeException("Client bridge failed", e); }
        ClientTickEvents.END_CLIENT_TICK.register(client -> tick());
        ClientLifecycleEvents.CLIENT_STOPPING.register(client -> { cancel("shutdown"); rpc.close(); });
    }
    private void requireWorld(JsonObject request, JsonObject p) {
        if (mc.player == null || mc.level == null || mc.gameMode == null) throw new IllegalStateException("Client not in world");
        if (p.has("dimension") && !str(p,"dimension").equals(mc.level.dimension().identifier().toString())) throw new IllegalArgumentException("Dimension mismatch");
        if (!str(request,"actor_id").equals(mc.player.getPlainTextName()) && !str(request,"actor_id").equals(mc.player.getUUID().toString())) throw new IllegalArgumentException("actor_id must match local player name or UUID");
    }
    private JsonElement dispatch(JsonObject request) {
        String method = str(request,"method"); JsonObject p = params(request);
        if (method.equals("session.describe")) {
            List<Object> mods = FabricLoader.getInstance().getAllMods().stream().map(m -> (Object)Map.of("id",m.getMetadata().getId(),"version",m.getMetadata().getVersion().getFriendlyString())).toList();
            return json(Map.of("minecraft","26.1","source","client","mods",mods,"controller","fabric-client","world_loaded",mc.level != null,"actor_id",mc.player == null ? "unavailable" : mc.player.getPlainTextName(),"capabilities",List.of("observe.player","observe.player_state","observe.blocks","observe.entities","action.look","action.move","action.interact","action.break","action.respawn","action.cancel","action.status","inventory.transfer","debug.*","schematic.*","mod.config_get","mod.config_set")));
        }
        if(method.equals("client.capture")||method.equals("debug.capture"))return capture();
        if(method.equals("observe.player_state")){
            if(mc.player==null||mc.level==null||mc.getConnection()==null)return json(Map.of("source","client","connected",false,"alive",false));
            requireWorld(request,p);
            var remote=mc.getConnection().getConnection().getRemoteAddress();
            var peer=remote instanceof java.net.InetSocketAddress address?address:null;
            Map<String,Object> state=new LinkedHashMap<>();
            state.put("source","client");state.put("connected",true);state.put("alive",mc.player.getHealth()>0&&!mc.player.isDeadOrDying());
            state.put("name",mc.player.getPlainTextName());state.put("uuid",mc.player.getUUID().toString());
            state.put("health",mc.player.getHealth());state.put("dead",mc.player.isDeadOrDying());
            state.put("position",List.of(mc.player.getX(),mc.player.getY(),mc.player.getZ()));
            state.put("dimension",mc.level.dimension().identifier().toString());state.put("client_tick",mc.player.tickCount);
            state.put("flying",mc.player.getAbilities().flying);state.put("on_ground",mc.player.onGround());
            state.put("server_port",peer==null?-1:peer.getPort());
            state.put("server_loopback",peer!=null&&peer.getAddress()!=null&&peer.getAddress().isLoopbackAddress());
            return json(state);
        }
        if(method.equals("mod.config_get")||method.equals("mod.config_set"))return ModAdapter.config(method,p);
        requireWorld(request,p);
        if ((method.startsWith("action.") || method.startsWith("inventory.")) && active != null && !Objects.equals(owner,str(request,"run_id"))) throw new IllegalStateException("Actor controlled by another run");
        return switch (method) {
            case "observe.player", "debug.snapshot" -> snapshot();
            case "observe.blocks" -> blocks(p);
            case "observe.entities", "debug.entity_geometry" -> entities(p);
            case "action.look" -> {
                if (active != null) throw new IllegalStateException("Cancel current action before changing view");
                if (p.has("target")) look(vector(p.getAsJsonArray("target")));
                else { mc.player.setYRot(p.get("yaw").getAsFloat()); mc.player.setXRot(Math.clamp(p.get("pitch").getAsFloat(),-90,90)); }
                yield snapshot();
            }
            case "action.move" -> {
                List<Vec3> path=new ArrayList<>();
                if(p.has("path")){if(p.getAsJsonArray("path").isEmpty()||p.getAsJsonArray("path").size()>256)throw new IllegalArgumentException("Path requires 1..256 waypoints");for(var point:p.getAsJsonArray("path"))path.add(vector(point.getAsJsonArray()));}
                else path.add(vector(p.getAsJsonArray("target")));
                start(request,p);waypoints.addAll(path);destination=waypoints.removeFirst();
                yield actions.get(active).deepCopy();
            }
            case "action.break" -> {
                BlockPos candidate=ServerBridge.position(p);reach(candidate);Direction direction=face(p);
                start(request,p); breaking = candidate; breakingFace = direction;
                look(Vec3.atCenterOf(breaking));
                mc.gameMode.startDestroyBlock(breaking, breakingFace);
                yield actions.get(active).deepCopy();
            }
            case "action.interact" -> {
                if (active != null) throw new IllegalStateException("Actor busy");
                BlockPos pos = ServerBridge.position(p); reach(pos); Direction direction=face(p);
                Vec3 hit = p.has("hit") ? vector(p.getAsJsonArray("hit")) : Vec3.atCenterOf(pos).add(Vec3.atLowerCornerOf(direction.getUnitVec3i()).scale(.5));
                if(!new AABB(pos).inflate(.001).contains(hit))throw new IllegalArgumentException("Hit outside target block");
                InteractionHand hand=p.has("hand")&&str(p,"hand").equals("off")?InteractionHand.OFF_HAND:InteractionHand.MAIN_HAND;
                start(request,p);
                look(hit);
                interacting=new BlockHitResult(hit,direction,pos,false);interactionHand=hand;interactionDelay=2;
                yield actions.get(active).deepCopy();
            }
            case "action.respawn" -> {
                if(active!=null)throw new IllegalStateException("Cancel current action before requesting respawn");
                if(!mc.player.isDeadOrDying())yield json(Map.of("status","completed","respawn_requested",false,
                    "reason","already_alive","health",mc.player.getHealth(),"source","client"));
                start(request,p);respawning=true;
                JsonObject action=actions.get(active);action.addProperty("kind","respawn");
                action.addProperty("respawn_requested",true);action.addProperty("initial_health",mc.player.getHealth());
                // Same vanilla path as DeathScreen: one packet, then wait for the server's new live player.
                mc.player.respawn();mc.setScreen(null);
                yield action.deepCopy();
            }
            case "action.cancel" -> { if(p.has("action_id") && !Objects.equals(active,str(p,"action_id"))) throw new IllegalArgumentException("Action is not active"); cancel("cancelled"); yield json(Map.of("released",true)); }
            case "action.status" -> { JsonObject action=actions.get(str(p,"action_id")); if(action==null) throw new IllegalArgumentException("Unknown action"); yield action.deepCopy(); }
            case "inventory.transfer" -> transfer(p);
            case "inventory.select" -> {int slot=integer(p,"slot");if(slot<0||slot>8)throw new IllegalArgumentException("Hotbar slot must be 0..8");mc.player.getInventory().setSelectedSlot(slot);yield json(Map.of("selected_slot",slot,"source","client"));}
            case "debug.capabilities" -> json(Map.of("source","client","server_tps","unavailable: use server bridge","fields",List.of("position","block","chunk","section","local","dimension","yaw","pitch","light","biome","target","fps","memory","item"),"overlays",List.of("overlay","chunk_borders","advanced_tooltips","hitboxes")));
            case "debug.chunk_info" -> chunkInfo(p.has("x") ? ServerBridge.position(p) : mc.player.blockPosition());
            case "debug.target" -> target();
            case "debug.performance" -> performance();
            case "debug.entries", "debug.entries.list", "debug.entries.read" -> entries(p);
            case "debug.overlay.get", "debug.overlay.set" -> {
                if(method.endsWith("set")) {
                    mc.debugEntries.setOverlayVisible(p.get("enabled").getAsBoolean());
                    if(p.has("entries")) for(var e:p.getAsJsonObject("entries").entrySet()) mc.debugEntries.setStatus(Identifier.parse(e.getKey()),DebugScreenEntryStatus.valueOf(e.getValue().getAsString()));
                    mc.debugEntries.save();
                }
                yield json(Map.of("enabled",mc.debugEntries.isOverlayVisible(),"source","client"));
            }
            case "debug.chunk_borders.get", "debug.chunk_borders.set" -> overlay(method,p,DebugScreenEntries.CHUNK_BORDERS);
            case "debug.hitboxes.get", "debug.hitboxes.set" -> overlay(method,p,DebugScreenEntries.ENTITY_HITBOXES);
            case "debug.advanced_tooltips.get", "debug.advanced_tooltips.set" -> {
                if(method.endsWith("set")) {mc.options.advancedItemTooltips=p.get("enabled").getAsBoolean();mc.options.save();}
                yield json(Map.of("enabled",mc.options.advancedItemTooltips,"source","client"));
            }
            case "debug.item" -> item(mc.player.getInventory().getItem(integer(p,"slot")));
            case "debug.capture", "client.capture" -> capture();
            case "client.gui_action" -> gui(p);
            case "mod.config_get", "mod.config_set" -> ModAdapter.config(method,p);
            default -> {
                if(method.startsWith("schematic.")) yield ModAdapter.schematic(method,p,mc);
                throw new UnsupportedOperationException(method);
            }
        };
    }
    private void start(JsonObject request, JsonObject p) {
        if(active!=null) throw new IllegalStateException("Actor already has active action");
        int timeout=p.has("timeout_ms") ? integer(p,"timeout_ms") : 15000;
        if(timeout<100||timeout>120000) throw new IllegalArgumentException("timeout_ms must be 100..120000");
        active=UUID.randomUUID().toString(); owner=str(request,"run_id"); deadline=System.nanoTime()+timeout*1000000L;
        JsonObject action=new JsonObject();action.addProperty("action_id",active);action.addProperty("status","running");action.addProperty("run_id",owner);actions.put(active,action);
    }
    private void tick() {
        try{tickActive();}catch(Exception error){if(active!=null)actions.get(active).addProperty("error",error.toString());cancel("failed");}
    }
    private void tickActive() {
        if(active==null)return;
        if(respawning){
            // A death/loading screen is expected during this action; it must not trigger ordinary GUI cancellation.
            JsonObject action=actions.get(active);
            if(mc.player!=null&&mc.level!=null&&!mc.player.isDeadOrDying()&&mc.player.getHealth()>0){
                action.addProperty("health",mc.player.getHealth());action.addProperty("alive",true);
                action.addProperty("dimension",mc.level.dimension().identifier().toString());
                action.add("position",json(List.of(mc.player.getX(),mc.player.getY(),mc.player.getZ())));
                action.addProperty("client_tick",mc.player.tickCount);cancel("completed");return;
            }
            if(System.nanoTime()>deadline){
                action.addProperty("alive",false);
                if(mc.player!=null)action.addProperty("health",mc.player.getHealth());
                cancel("timeout_waiting_for_respawn");
            }
            return;
        }
        if(mc.player==null||mc.level==null||mc.screen!=null) {cancel("interrupted");return;}
        if(System.nanoTime()>deadline) {cancel("timeout_or_blocked");return;}
        if(destination!=null) {
            if(mc.player.position().distanceTo(destination)<.65) {if(waypoints.isEmpty()){cancel("completed");return;}destination=waypoints.removeFirst();}
            look(new Vec3(destination.x,mc.player.getEyeY(),destination.z));
            mc.options.keyUp.setDown(true);mc.options.keyJump.setDown(mc.player.horizontalCollision || destination.y>mc.player.getY()+.6);
        }
        if(breaking!=null) {
            if(mc.level.getBlockState(breaking).isAir()) {cancel("completed");return;}
            if(mc.player.position().distanceTo(Vec3.atCenterOf(breaking))>6) {cancel("out_of_reach");return;}
            mc.gameMode.continueDestroyBlock(breaking,breakingFace);mc.player.swing(InteractionHand.MAIN_HAND);
        }
        if(interacting!=null&&--interactionDelay<=0){
            // Allow at least one normal movement packet to publish the new view
            // before a directional placement is handled by the server.
            var result=mc.gameMode.useItemOn(mc.player,interactionHand,interacting);
            actions.get(active).addProperty("interaction",result.toString());actions.get(active).addProperty("world_confirmation","observe.blocks required");cancel("completed");
        }
    }
    private void cancel(String status) {
        mc.options.keyUp.setDown(false);mc.options.keyJump.setDown(false);mc.options.keyAttack.setDown(false);mc.options.keyUse.setDown(false);
        if(mc.gameMode!=null) mc.gameMode.stopDestroyBlock();
        if(active!=null) {actions.get(active).addProperty("status",status);actions.get(active).addProperty("finished_wall_ms",System.currentTimeMillis());}
        active=null;owner=null;destination=null;breaking=null;interacting=null;respawning=false;waypoints.clear();
    }
    static Vec3 vector(JsonArray a) {double x=a.get(0).getAsDouble(),y=a.get(1).getAsDouble(),z=a.get(2).getAsDouble();if(!Double.isFinite(x)||!Double.isFinite(y)||!Double.isFinite(z))throw new IllegalArgumentException("Nonfinite coordinates");return new Vec3(x,y,z);}
    private Direction face(JsonObject p) {return Direction.valueOf(p.has("face") ? str(p,"face").toUpperCase(Locale.ROOT) : "UP");}
    private void reach(BlockPos p) {if(!mc.level.hasChunkAt(p)||mc.player.getEyePosition().distanceTo(Vec3.atCenterOf(p))>6) throw new IllegalArgumentException("Target unloaded or out of reach");}
    private void look(Vec3 at) {Vec3 d=at.subtract(mc.player.getEyePosition());mc.player.setYRot((float)Math.toDegrees(Math.atan2(-d.x,d.z)));mc.player.setXRot((float)-Math.toDegrees(Math.atan2(d.y,Math.sqrt(d.x*d.x+d.z*d.z))));}
    private JsonElement snapshot() {
        Map<String,Object> data=new LinkedHashMap<>();
        data.put("position",List.of(mc.player.getX(),mc.player.getY(),mc.player.getZ()));data.put("yaw",mc.player.getYRot());data.put("pitch",mc.player.getXRot());
        data.put("health",mc.player.getHealth());data.put("dimension",mc.level.dimension().identifier().toString());data.put("client_tick",mc.player.tickCount);data.put("source","client");
        data.put("dead",mc.player.isDeadOrDying());data.put("on_ground",mc.player.onGround());
        data.put("flying",mc.player.getAbilities().flying);data.put("may_fly",mc.player.getAbilities().mayfly);
        data.put("chunk",chunkInfo(mc.player.blockPosition()));data.put("target",target());data.put("screen",mc.screen==null?"none":mc.screen.getClass().getName());
        data.put("biome",mc.level.getBiome(mc.player.blockPosition()).unwrapKey().map(k->k.identifier().toString()).orElse("unavailable"));
        data.put("light",mc.level.getMaxLocalRawBrightness(mc.player.blockPosition()));
        data.put("inventory",java.util.stream.IntStream.range(0,mc.player.getInventory().getContainerSize()).mapToObj(i->Map.of("slot",i,"item",item(mc.player.getInventory().getItem(i)))).toList());
        return json(data);
    }
    private JsonElement chunkInfo(BlockPos pos) {
        int x=pos.getX(),y=pos.getY(),z=pos.getZ();int cx=Math.floorDiv(x,16),cy=Math.floorDiv(y,16),cz=Math.floorDiv(z,16);
        return json(Map.of("block",List.of(x,y,z),"chunk",List.of(cx,cz),"section",List.of(cx,cy,cz),"local",List.of(Math.floorMod(x,16),Math.floorMod(y,16),Math.floorMod(z,16)),"bounds",List.of(cx*16,cz*16,cx*16+15,cz*16+15),"client_loaded",mc.level.hasChunkAt(pos),"server_ticking","unavailable","source","client","client_tick",mc.player.tickCount));
    }
    private JsonElement overlay(String method,JsonObject p,Identifier id) {
        if(method.endsWith("set")) {mc.debugEntries.setStatus(id,p.get("enabled").getAsBoolean()?DebugScreenEntryStatus.ALWAYS_ON:DebugScreenEntryStatus.NEVER);mc.debugEntries.save();}
        return json(Map.of("enabled",mc.debugEntries.isCurrentlyEnabled(id),"configuration",mc.debugEntries.getStatus(id).toString(),"source","client"));
    }
    private JsonElement entries(JsonObject p) {
        Map<String,Object> out=new TreeMap<>();
        var pos=mc.player.blockPosition();var chunk=mc.level.hasChunkAt(pos)?mc.level.getChunkAt(pos):null;
        for(var e:DebugScreenEntries.allEntries().entrySet()) {
            if(p.has("id")&&!str(p,"id").equals(e.getKey().toString()))continue;
            boolean allowed=e.getValue().isAllowed(mc.player.isReducedDebugInfo());List<String> lines=new ArrayList<>();
            if(allowed) {
                DebugScreenDisplayer displayer=new DebugScreenDisplayer() {
                    public void addPriorityLine(String l){lines.add(l);}public void addLine(String l){lines.add(l);}
                    public void addToGroup(Identifier group,Collection<String> l){lines.addAll(l);}public void addToGroup(Identifier group,String l){lines.add(l);}
                };
                try{e.getValue().display(displayer,mc.level,chunk,null);}catch(Exception error){lines.add("unavailable: "+error);}
            }
            out.put(e.getKey().toString(),Map.of("allowed",allowed,"status",mc.debugEntries.getStatus(e.getKey()).toString(),"text",lines,"typed","use debug.snapshot for typed core values","source","client"));
        }
        return json(out);
    }
    private JsonElement item(ItemStack stack) {
        var ops=RegistryOps.create(JsonOps.INSTANCE,mc.level.registryAccess());JsonObject components=new JsonObject();
        for(var component:stack.getComponents()){
            var encoded=component.encodeValue(ops);JsonElement value=encoded.result().orElseGet(()->json(Map.of("status","unavailable","reason",encoded.error().map(Object::toString).orElse("No component codec"))));
            components.add(BuiltInRegistries.DATA_COMPONENT_TYPE.getKey(component.type()).toString(),value);
        }
        var patch=net.minecraft.core.component.DataComponentPatch.CODEC.encodeStart(ops,stack.getComponentsPatch());
        Map<String,Object> info=new LinkedHashMap<>();info.put("id",BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());info.put("count",stack.getCount());info.put("damage",stack.getDamageValue());info.put("max_damage",stack.getMaxDamage());info.put("remaining_durability",Math.max(0,stack.getMaxDamage()-stack.getDamageValue()));
        info.put("effective_components",components);info.put("component_patch",patch.result().orElseGet(()->json(Map.of("status","unavailable","reason",patch.error().map(Object::toString).orElse("codec failure")))));
        info.put("tags",stack.typeHolder().tags().map(t->t.location().toString()).toList());
        info.put("normal_tooltip",stack.getTooltipLines(Item.TooltipContext.of(mc.level),mc.player,TooltipFlag.NORMAL).stream().map(c->c.getString()).toList());info.put("advanced_tooltip",stack.getTooltipLines(Item.TooltipContext.of(mc.level),mc.player,TooltipFlag.ADVANCED).stream().map(c->c.getString()).toList());info.put("server_unsynced_data","unavailable");info.put("source","client");return json(info);
    }
    private JsonElement target() {
        var camera=mc.getCameraEntity();var hit=camera==null?mc.hitResult:mc.player.raycastHitResult(1.0f,camera);if(hit==null)return json(Map.of("type","unavailable"));
        Map<String,Object> data=new LinkedHashMap<>();data.put("type",hit.getType().toString());data.put("hit",List.of(hit.getLocation().x,hit.getLocation().y,hit.getLocation().z));
        if(hit instanceof BlockHitResult b){data.put("block",List.of(b.getBlockPos().getX(),b.getBlockPos().getY(),b.getBlockPos().getZ()));data.put("face",b.getDirection().toString());data.put("state",mc.level.getBlockState(b.getBlockPos()).toString());data.put("fluid",mc.level.getFluidState(b.getBlockPos()).toString());}
        if(hit instanceof EntityHitResult e)data.put("entity",e.getEntity().getUUID().toString());
        data.put("source","client");return json(data);
    }
    private JsonElement performance() {Runtime r=Runtime.getRuntime();return json(Map.of("fps",mc.getFps(),"frame_time_ns",mc.getFrameTimeNs(),"jvm_used_bytes",r.totalMemory()-r.freeMemory(),"jvm_max_bytes",r.maxMemory(),"server_tps","unavailable: use server bridge","source","client"));}
    private JsonElement capture() {
        String name="agent-"+UUID.randomUUID()+".png";Screenshot.grab(mc.gameDirectory,name,mc.getMainRenderTarget(),1,c->{});
        return json(Map.of("file",mc.gameDirectory.toPath().resolve("screenshots").resolve(name).toAbsolutePath().toString(),"status","capture_scheduled","client_tick",mc.player==null?-1:mc.player.tickCount,"sample_wall_ms",System.currentTimeMillis()));
    }
    private JsonElement blocks(JsonObject p) {
        BlockPos min=BlockPos.containing(vector(p.getAsJsonArray("min"))),max=BlockPos.containing(vector(p.getAsJsonArray("max")));
        long volume=(long)(max.getX()-min.getX()+1)*(max.getY()-min.getY()+1)*(max.getZ()-min.getZ()+1);
        if(max.getX()<min.getX()||max.getY()<min.getY()||max.getZ()<min.getZ()||volume>32768)throw new IllegalArgumentException("Invalid or excessive region");
        List<Object> out=new ArrayList<>();for(BlockPos pos:BlockPos.betweenClosed(min,max)){boolean loaded=mc.level.hasChunkAt(pos);out.add(Map.of("position",List.of(pos.getX(),pos.getY(),pos.getZ()),"loaded",loaded,"state",loaded?mc.level.getBlockState(pos).toString():"unavailable"));}
        return json(Map.of("blocks",out,"source","client","client_tick",mc.player.tickCount));
    }
    private JsonElement entities(JsonObject p) {
        double radius=p.has("radius")?p.get("radius").getAsDouble():16;if(radius<0||radius>128)throw new IllegalArgumentException("Invalid radius");
        return json(mc.level.getEntities(mc.player,mc.player.getBoundingBox().inflate(radius)).stream().map(e->Map.of("uuid",e.getUUID().toString(),"type",BuiltInRegistries.ENTITY_TYPE.getKey(e.getType()).toString(),"position",List.of(e.getX(),e.getY(),e.getZ()),"bbox",List.of(e.getBoundingBox().minX,e.getBoundingBox().minY,e.getBoundingBox().minZ,e.getBoundingBox().maxX,e.getBoundingBox().maxY,e.getBoundingBox().maxZ),"eye_height",e.getEyeHeight(),"source","client")).toList());
    }
    private JsonElement transfer(JsonObject p) {
        int container=integer(p,"container_id");if(mc.player.containerMenu.containerId!=container)throw new IllegalStateException("Container changed");
        int from=integer(p,"from"),to=integer(p,"to"),count=integer(p,"count");var menu=mc.player.containerMenu;
        if(from<0||to<0||from>=menu.slots.size()||to>=menu.slots.size()||from==to||count<1||count>64||!menu.getCarried().isEmpty()||menu.getSlot(from).getItem().getCount()<count)throw new IllegalArgumentException("Invalid transfer or cursor not empty");
        mc.gameMode.handleContainerInput(container,from,0,ContainerInput.PICKUP,mc.player);
        for(int i=0;i<count;i++)mc.gameMode.handleContainerInput(container,to,1,ContainerInput.PICKUP,mc.player);
        mc.gameMode.handleContainerInput(container,from,0,ContainerInput.PICKUP,mc.player);
        return json(Map.of("status","submitted","confirmation","observe.player required"));
    }
    private JsonElement gui(JsonObject p) {
        if(mc.screen==null||!mc.screen.getClass().getName().equals(str(p,"screen")))throw new IllegalStateException("GUI mismatch");
        if(str(p,"operation").equals("close")){mc.player.closeContainer();return json(Map.of("closed",true));}
        // Never use a GUI fallback as an alternate command or scripting channel.
        if(!(mc.screen instanceof net.minecraft.client.gui.screens.inventory.AbstractContainerScreen) && !mc.screen.getClass().getName().startsWith("fi.dy.masa."))throw new SecurityException("Only container and MaLiLib mod GUIs are exposed");
        boolean handled=false;
        switch(str(p,"operation")) {
            case "click" -> {double x=p.get("x").getAsDouble(),y=p.get("y").getAsDouble();if(x<0||y<0||x>=mc.screen.width||y>=mc.screen.height)throw new IllegalArgumentException("Click outside GUI");var event=new MouseButtonEvent(x,y,new MouseButtonInfo(integer(p,"button"),0));handled=mc.screen.mouseClicked(event,false);mc.screen.mouseReleased(event);}
            case "scroll" -> handled=mc.screen.mouseScrolled(p.get("x").getAsDouble(),p.get("y").getAsDouble(),0,p.get("amount").getAsDouble());
            case "text" -> {String text=str(p,"text");if(text.length()>256||text.contains("\n")||text.contains("\r"))throw new IllegalArgumentException("Invalid GUI text");for(int code:text.codePoints().toArray())handled|=mc.screen.charTyped(new CharacterEvent(code));}
            default -> throw new IllegalArgumentException("Unsupported GUI operation");
        }
        return json(Map.of("handled",handled,"screen",mc.screen.getClass().getName(),"source","client"));
    }
}
