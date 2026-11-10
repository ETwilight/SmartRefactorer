package research.agent;

import com.google.gson.*;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.fabric.api.networking.v1.ServerPlayConnectionEvents;
import net.minecraft.stats.Stats;
import net.minecraft.commands.CommandSource;
import net.minecraft.commands.arguments.coordinates.BlockPosArgument;
import net.minecraft.commands.arguments.DimensionArgument;
import net.minecraft.network.chat.Component;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.level.block.entity.CommandBlockEntity;
import net.minecraft.world.level.gamerules.*;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.core.BlockPos;
import net.minecraft.world.phys.Vec3;
import net.minecraft.world.phys.Vec2;
import net.minecraft.resources.Identifier;
import net.minecraft.resources.ResourceKey;
import net.minecraft.core.registries.Registries;
import net.minecraft.world.level.Level;
import java.util.*;
import static research.agent.RpcServer.*;

public class ServerBridge implements ModInitializer {
    private RpcServer rpc;
    private MinecraftServer server;
    private final Map<String, String> fakeOwners = new HashMap<>();
    private final Map<String, Long> leases = new HashMap<>();
    private final Set<String> retiringFakes = new HashSet<>();
    private final Map<String,BucketBatch> bucketBatches=new HashMap<>();
    private final Map<String,CombinedOperation> combinedOperations=new HashMap<>();
    private final Map<String,OffhandUse> offhandUses=new HashMap<>();
    private final Map<String,MaterialUse> materialUses=new HashMap<>();
    private final Map<String,String> completedFakeOwners=new LinkedHashMap<>() {
        protected boolean removeEldestEntry(Map.Entry<String,String> entry){return size()>4096;}
    };
    private EnvironmentGuard environment;
    private String currentRun;
    private final PerformanceTelemetry performanceTelemetry=new PerformanceTelemetry();
    private final List<String> cleanupFailures=new ArrayList<>();
    private final Map<UUID,Long> connectionSerials=new HashMap<>();
    private final Map<String,UUID> knownActorIds=new HashMap<>();
    public void onInitialize() {
        ServerPlayConnectionEvents.JOIN.register((handler,sender,s)->{
            recordConnection(handler.player.getUUID(),handler.player.getPlainTextName());
            // Human clients are observers. Carpet actors retain their task-controlled modes.
            if(!handler.player.getClass().getName().equals("carpet.patches.EntityPlayerMPFake"))
                handler.player.setGameMode(net.minecraft.world.level.GameType.SPECTATOR);
        });
        ServerPlayConnectionEvents.DISCONNECT.register((handler,s)->recordConnection(handler.player.getUUID(),handler.player.getPlainTextName()));
        ServerLifecycleEvents.SERVER_STARTED.register(s -> {
            server = s;
            environment=new EnvironmentGuard(s);
            try { rpc = new RpcServer(Integer.getInteger("minecraft.agent.server.port", 8766), "server", s::execute, this::dispatch); }
            catch (Exception e) { throw new RuntimeException("Server bridge failed", e); }
        });
        ServerLifecycleEvents.SERVER_STOPPING.register(s -> {
            for (String name : List.copyOf(fakeOwners.keySet())) {
                try { if(isFake(name)){raw("player " + name + " stop"); raw("player " + name + " kill");} } catch(Exception e) {cleanupFailure(e);}
            }
            for(String run:List.copyOf(leases.keySet()))environment.restore(run,Set.of());
            fakeOwners.clear(); if (rpc != null) rpc.close();
        });
        ServerTickEvents.END_SERVER_TICK.register(s -> {
            for(String name:List.copyOf(fakeOwners.keySet())) {
                if(!retiringFakes.contains(name)&&isFake(name)&&offhandUses.containsKey(name)
                    &&System.nanoTime()-leases.getOrDefault(fakeOwners.get(name),0L)<=30_000_000_000L)
                    offhandUses.get(name).tick(server.getPlayerList().getPlayerByName(name),this::raw);
                if(!retiringFakes.contains(name)&&isFake(name)&&materialUses.containsKey(name)
                    &&System.nanoTime()-leases.getOrDefault(fakeOwners.get(name),0L)<=30_000_000_000L)
                    materialUses.get(name).tick(server.getPlayerList().getPlayerByName(name),this::raw);
                if(!retiringFakes.contains(name)&&isFake(name)&&combinedOperations.containsKey(name)
                    &&System.nanoTime()-leases.getOrDefault(fakeOwners.get(name),0L)<=30_000_000_000L) {
                    try{combinedOperations.get(name).tick(server.getPlayerList().getPlayerByName(name),this::raw);}
                    catch(Exception e){retiringFakes.add(name);cleanupFailure(e);}
                }
                if(!retiringFakes.contains(name)&&isFake(name)&&bucketBatches.containsKey(name)
                        &&System.nanoTime()-leases.getOrDefault(fakeOwners.get(name),0L)<=30_000_000_000L) {
                    try{bucketBatches.get(name).tick(server.getPlayerList().getPlayerByName(name),this::raw);}
                    catch(Exception e){retiringFakes.add(name);cleanupFailure(e);}
                }
                if(retiringFakes.contains(name)||System.nanoTime()-leases.getOrDefault(fakeOwners.get(name),0L)>30_000_000_000L) {
                    bucketBatches.remove(name);
                    combinedOperations.remove(name);
                    offhandUses.remove(name);materialUses.remove(name);
                    var player=server.getPlayerList().getPlayerByName(name);
                    try{if(player!=null&&isFake(name)){raw("player "+name+" stop");raw("player "+name+" kill");}}catch(Exception e){cleanupFailure(e);continue;}
                    if(!isSpawning(name)&&server.getPlayerList().getPlayerByName(name)==null){completedFakeOwners.put(name,fakeOwners.remove(name));retiringFakes.remove(name);}
                }
            }
            for(String run:List.copyOf(leases.keySet()))if(System.nanoTime()-leases.get(run)>30_000_000_000L){environment.restore(run,Set.of());leases.remove(run);performanceTelemetry.remove(run);}
        });
    }
    private void recordConnection(UUID id,String name){
        connectionSerials.merge(id,1L,Long::sum);knownActorIds.put(name,id);knownActorIds.put(id.toString(),id);
    }
    private JsonElement actorState(String actor){
        var player=server.getPlayerList().getPlayerByName(actor);
        if(player==null){try{player=server.getPlayerList().getPlayer(UUID.fromString(actor));}catch(IllegalArgumentException ignored){}}
        UUID id=player==null?knownActorIds.get(actor):player.getUUID();
        Map<String,Object> value=new LinkedHashMap<>();
        value.put("source","server");value.put("actor_id",actor);value.put("connected",player!=null);
        value.put("alive",player!=null&&player.getHealth()>0&&!player.isDeadOrDying());
        value.put("game_port",server.getPort());value.put("server_tick",server.getTickCount());
        value.put("connection_serial",id==null?0:connectionSerials.getOrDefault(id,0L));
        if(id!=null)value.put("uuid",id.toString());
        if(player!=null){
            value.put("name",player.getPlainTextName());value.put("health",player.getHealth());value.put("dead",player.isDeadOrDying());
            value.put("fake",player.getClass().getName().equals("carpet.patches.EntityPlayerMPFake"));
            value.put("game_mode",player.gameMode.getGameModeForPlayer().toString().toLowerCase(Locale.ROOT));
            value.put("spectator",player.isSpectator());value.put("flying",player.getAbilities().flying);
            value.put("deaths",player.getStats().getValue(Stats.CUSTOM,Stats.DEATHS));
            value.put("dimension",player.level().dimension().identifier().toString());
            value.put("position",List.of(player.getX(),player.getY(),player.getZ()));
        }
        return json(value);
    }
    private int raw(String command) {
        var source = server.createCommandSourceStack().withSuppressedOutput();
        var parsed = server.getCommands().getDispatcher().parse(command, source);
        if (parsed.getReader().canRead()) throw new IllegalArgumentException("Parse failure: " + parsed.getExceptions());
        final int[] result = {0};
        final boolean[] succeeded={false};
        source = source.withCallback((success, value) -> {result[0] = value;succeeded[0]|=success;});
        server.getCommands().performPrefixedCommand(source, command);
        if(!succeeded[0])throw new IllegalStateException("Carpet operation failed: "+command);
        return result[0];
    }
    private ServerLevel level(JsonObject p) {
        String dimension = p.has("dimension") ? str(p, "dimension") : "minecraft:overworld";
        var world = server.getLevel(ResourceKey.create(Registries.DIMENSION, Identifier.parse(dimension)));
        if (world == null) throw new IllegalArgumentException("Unknown dimension");
        return world;
    }
    private JsonElement dispatch(JsonObject request) {
        String method = str(request, "method"); JsonObject p = params(request);
        String run = str(request, "run_id");
        currentRun=run;
        leases.put(run,System.nanoTime());
        if(Set.of("clock.freeze","clock.unfreeze","clock.step","clock.rate","clock.sprint","world.teleport","world.set_block","world.fill","entity.summon","entity.kill","inventory.give").contains(method))return structured(method,p);
        return switch (method) {
            case "observe.performance" -> {
                var manager=server.tickRateManager();
                yield json(performanceTelemetry.read(run,server.getTickCount(),System.nanoTime(),manager.tickrate(),
                    manager.isFrozen(),manager.isSteppingForward(),manager.isSprinting(),
                    server.getAverageTickTimeNanos(),server.getTickTimesNanos()));
            }
            case "observe.health" -> {
                var runtime=Runtime.getRuntime();var data=new LinkedHashMap<String,Object>();
                data.put("heap_used_bytes",runtime.totalMemory()-runtime.freeMemory());data.put("heap_max_bytes",runtime.maxMemory());
                data.put("heap_committed_bytes",runtime.totalMemory());
                var nonheap=java.lang.management.ManagementFactory.getMemoryMXBean().getNonHeapMemoryUsage();
                data.put("nonheap_used_bytes",nonheap.getUsed());data.put("nonheap_committed_bytes",nonheap.getCommitted());
                data.put("processors",runtime.availableProcessors());data.put("server_tick",server.getTickCount());
                data.put("fake_players",fakeOwners.size());
                var worlds=new ArrayList<Map<String,Object>>();
                for(var level:server.getAllLevels())worlds.add(Map.of("dimension",level.dimension().identifier().toString(),"loaded_chunks",level.getChunkSource().getLoadedChunksCount()));
                data.put("worlds",worlds);yield json(data);
            }
            case "session.heartbeat" -> json(Map.of("lease_seconds",30,"tick",server.overworld().getGameTime(),"cleanup_errors",List.copyOf(cleanupFailures)));
            case "session.describe" -> json(Map.of("minecraft", server.getServerVersion(), "source", "server", "tick", server.getTickCount(), "execution_identity", "server console unless context.executor is explicit", "capabilities", List.of("command.run", "command.describe", "observe.blocks", "observe.actor_state", "clock.query", "counter.read", "counter.reset", "fake_player.*")));
            case "clock.query" -> json(Map.of("tick", server.overworld().getGameTime(), "server_tick",server.getTickCount(),"stepping",server.tickRateManager().isSteppingForward(),"step_ticks_remaining",server.tickRateManager().frozenTicksToRun(), "frozen", server.tickRateManager().isFrozen(), "target_tps", server.tickRateManager().tickrate(), "sprinting", server.tickRateManager().isSprinting(), "source", "server"));
            case "experiment.conditions" -> conditions();
            case "experiment.restore" -> {Set<String> keep=new HashSet<>();if(p.has("keep"))p.getAsJsonArray("keep").forEach(v->keep.add(v.getAsString()));yield json(environment.restore(run,keep));}
            case "command.describe" -> describe(p);
            case "command.run" -> command(p);
            case "observe.blocks" -> blocks(p);
            case "observe.container" -> EvaluationReadback.container(level(p),p);
            case "machine.control" -> MachineControls.apply(level(p),p);
            case "observe.natural_source" -> EvaluationReadback.naturalSource(level(p),p);
            case "observe.containers" -> {
                JsonArray snapshots=new JsonArray();
                var refs=p.getAsJsonArray("containers");if(refs.size()>128)throw new IllegalArgumentException("At most 128 containers per read");
                for(var ref:refs){var params=ref.getAsJsonObject().deepCopy();if(!params.has("dimension")&&p.has("dimension"))params.add("dimension",p.get("dimension"));if(p.has("products"))params.add("products",p.get("products"));snapshots.add(EvaluationReadback.container(level(params),params));}
                yield json(Map.of("containers",snapshots,"tick",level(p).getGameTime(),"sample_monotonic_ns",System.nanoTime(),"source","server"));
            }
            case "observe.evaluation_snapshot" -> {
                JsonArray regions=new JsonArray(),containers=new JsonArray();long total=0;
                for(var entry:p.getAsJsonArray("regions")) {
                    var tile=entry.getAsJsonObject();var a=tile.getAsJsonArray("min");var b=tile.getAsJsonArray("max");
                    long volume=1;for(int i=0;i<3;i++)volume*=b.get(i).getAsLong()-a.get(i).getAsLong()+1;
                    if(volume<1||(total+=volume)>1048576)throw new IllegalArgumentException("Atomic inspection exceeds block budget");
                    regions.add(EvaluationReadback.region(level(tile),tile));
                }
                if(p.getAsJsonArray("containers").size()>4096)throw new IllegalArgumentException("Too many output containers");
                for(var entry:p.getAsJsonArray("containers")) {
                    var query=entry.getAsJsonObject().deepCopy();query.add("products",p.getAsJsonArray("products"));
                    containers.add(EvaluationReadback.container(level(query),query));
                }
                Map<String,Object> result=new LinkedHashMap<>();long tick=server.overworld().getGameTime(),sample=System.nanoTime();
                result.put("version","evaluation-snapshot-v1");result.put("regions",regions);result.put("tick",tick);result.put("sample_monotonic_ns",sample);
                result.put("storage",Map.of("containers",containers,"tick",tick,"sample_monotonic_ns",sample));
                if(p.has("actor")) {
                    String name=str(p,"actor");
                    if(!run.equals(fakeOwners.get(name))||!isFake(name))throw new SecurityException("Snapshot actor is not owned by this run");
                    result.put("player_inventory",BucketBatch.inventory(server.getPlayerList().getPlayerByName(name),bucketBatches.get(name)));
                }
                yield json(result);
            }
            case "observe.region" -> EvaluationReadback.region(level(p),p);
            case "observe.actors" -> EvaluationReadback.actors(server);
            case "observe.actor_state" -> actorState(str(request,"actor_id"));
            case "observe.afk_site" -> afkSite(p);
            case "observe.biome" -> {
                var world = level(p); var pos = position(p);
                if (!world.hasChunkAt(pos)) yield json(Map.of("loaded", false, "source", "server"));
                yield json(Map.of("loaded", true, "biome", world.getBiome(pos).unwrapKey().orElseThrow().identifier().toString(), "position", List.of(pos.getX(), pos.getY(), pos.getZ()), "dimension", world.dimension().identifier().toString(), "source", "server", "tick", server.getTickCount()));
            }
            case "observe.block_entity" -> {
                var world = level(p); BlockPos pos = position(p);
                if (!world.hasChunkAt(pos)) yield json(Map.of("loaded", false));
                var entity = world.getBlockEntity(pos);
                yield json(Map.of("loaded", true, "type", entity == null ? "none" : entity.getClass().getName(), "nbt",entity==null?"none":entity.saveWithFullMetadata(world.registryAccess()).toString(), "source", "server", "tick", server.getTickCount()));
            }
            case "counter.read", "counter.reset", "carpet.counter.read", "carpet.counter.reset" -> counter(method, p);
            case "fake_player.list", "carpet.fake_player.list" -> json(fakeOwners.entrySet().stream().filter(e -> e.getValue().equals(run)).map(e->Map.of("id",e.getKey(),"status",isFake(e.getKey())?"ready":isSpawning(e.getKey())?"spawning":"not_online")).toList());
            default -> {
                if (method.startsWith("carpet.fake_player.")) method = method.substring(7);
                if (method.startsWith("fake_player.")) yield fake(method.substring(12), p, run);
                throw new UnsupportedOperationException(method);
            }
        };
    }
    private JsonElement describe(JsonObject p) {
        var dispatcher=server.getCommands().getDispatcher();var root=dispatcher.getRoot();
        if(!p.has("command")) {
            if(p.has("path")||p.has("offset")||p.has("limit"))throw new IllegalArgumentException("command is required with path/offset/limit");
            var names=root.getChildren().stream().map(node->node.getName()).filter(CommandPolicy.allowed()::contains).sorted().map(name->Map.of("name",name)).toList();
            return json(Map.of("commands",names,"view","index","identity","console","game_version","26.1","policy",CommandPolicy.description(),"details","Pass command and optional path of literal/argument node names; offset/limit paginate permitted usage"));
        }
        String name=str(p,"command");
        if(!CommandPolicy.allowed().contains(name)||root.getChild(name)==null)throw new IllegalArgumentException("Unknown or disallowed command: "+name);
        com.mojang.brigadier.tree.CommandNode<CommandSourceStack> node=root.getChild(name);
        List<String> path=new ArrayList<>();
        if(p.has("path")&&p.getAsJsonArray("path").size()>32)throw new IllegalArgumentException("At most 32 command path nodes");
        if(p.has("path"))for(var part:p.getAsJsonArray("path")) {
            if(node.getRedirect()!=null)node=node.getRedirect();
            String key=part.getAsString();var next=node.getChild(key);
            if(next==null)throw new IllegalArgumentException("Unknown command node: "+key+"; query parent children first");
            path.add(key);node=next;
        }
        List<String> policyPath=new ArrayList<>();policyPath.add(name);policyPath.addAll(path);
        if(!CommandPolicy.helpPathAllowed(policyPath))throw new SecurityException("Command branch is disabled by environment policy");
        int offset=p.has("offset")?integer(p,"offset"):0,limit=p.has("limit")?integer(p,"limit"):32;
        if(offset<0||limit<1||limit>256)throw new IllegalArgumentException("Invalid usage page: offset>=0, limit=1..256");
        List<String> usage=new ArrayList<>();
        for(String line:dispatcher.getAllUsage(node,server.createCommandSourceStack(),true)) {
            List<String> candidate=new ArrayList<>(policyPath);candidate.addAll(Arrays.asList(line.split("\\s+")));
            if(CommandPolicy.helpPathAllowed(candidate))usage.add(line);
        }
        List<Object> children=new ArrayList<>();
        var childSource=node.getRedirect()==null?node:node.getRedirect();
        for(var child:childSource.getChildren()) {
            var candidate=new ArrayList<>(policyPath);candidate.add(child.getName());
            if(CommandPolicy.helpPathAllowed(candidate))children.add(Map.of("name",child.getName(),"kind",child instanceof com.mojang.brigadier.tree.LiteralCommandNode<?>?"literal":"argument","executable",child.getCommand()!=null));
        }
        Map<String,Object> result=new LinkedHashMap<>();
        result.put("command",name);result.put("path",path);result.put("view","usage");result.put("identity","console");result.put("game_version","26.1");
        result.put("children",children);result.put("executable",node.getCommand()!=null);result.put("redirect",node.getRedirect()==null?null:node.getRedirect().getName());
        result.put("usage",usage.subList(Math.min(offset,usage.size()),Math.min((int)Math.min((long)offset+limit,Integer.MAX_VALUE),usage.size())));
        result.put("offset",offset);result.put("limit",limit);result.put("total_usage",usage.size());result.put("has_more",(long)offset+limit<usage.size());
        result.put("policy",CommandPolicy.description());
        result.put("permission_source","server command tree");
        return json(result);
    }
    private JsonElement command(JsonObject p) {
        String text = CommandPolicy.validate(str(p, "text"),Boolean.getBoolean("minecraft.agent.natural_world"));
        var source = server.createCommandSourceStack();
        String identity = "server console";
        if (p.has("context")) {
            JsonObject c = p.getAsJsonObject("context");
            if (c.has("executor")) {
                var player = server.getPlayerList().getPlayerByName(str(c, "executor"));
                if (player == null) throw new IllegalArgumentException("Executor not online");
                source = player.createCommandSourceStack(); identity = player.getPlainTextName();
            }
            if (c.has("dimension")) source = source.withLevel(level(c));
            if (c.has("position")) {
                var a = c.getAsJsonArray("position"); source = source.withPosition(new Vec3(a.get(0).getAsDouble(), a.get(1).getAsDouble(), a.get(2).getAsDouble()));
            }
            if (c.has("yaw")) source = source.withRotation(new Vec2(c.has("pitch") ? c.get("pitch").getAsFloat() : 0, c.get("yaw").getAsFloat()));
        }
        List<Object> callbacks = new ArrayList<>();List<String> messages=new ArrayList<>();List<String> messageKeys=new ArrayList<>();
        source=source.withSource(new CommandSource(){public void sendSystemMessage(Component c){messages.add(c.getString());commandMessageKeys(c,messageKeys);}public boolean acceptsSuccess(){return true;}public boolean acceptsFailure(){return true;}public boolean shouldInformAdmins(){return false;}});
        source = source.withCallback((ok, value) -> callbacks.add(Map.of("success", ok, "value", value)));
        var parsed = server.getCommands().getDispatcher().parse(text, source);
        if (parsed.getReader().canRead()) throw new IllegalArgumentException("Brigadier parse failure at " + parsed.getReader().getCursor() + ": " + parsed.getExceptions());
        if(CommandPolicy.tokens(text).getFirst().replace("minecraft:","").equals("clone")) {
            var context=parsed.getContext().build(text);var from=source.getLevel();
            if(CommandPolicy.tokens(text).contains("from"))try{from=DimensionArgument.getDimension(context,"sourceDimension");}catch(Exception e){throw new IllegalArgumentException(e);}
            BlockPos begin=BlockPosArgument.getBlockPos(context,"begin"),end=BlockPosArgument.getBlockPos(context,"end");
            long volume=(Math.abs((long)begin.getX()-end.getX())+1)*(Math.abs((long)begin.getY()-end.getY())+1)*(Math.abs((long)begin.getZ()-end.getZ())+1);
            if(volume>32768)throw new SecurityException("clone preflight limited to 32768 source blocks");
            for(var pos:BlockPos.betweenClosed(begin,end)) {
                if(!from.hasChunkAt(pos))throw new SecurityException("clone preflight source must be loaded");
                if(from.getBlockEntity(pos) instanceof CommandBlockEntity)throw new SecurityException("Cannot clone command blocks");
                String block=BuiltInRegistries.BLOCK.getKey(from.getBlockState(pos).getBlock()).toString();
                if(Set.of("minecraft:spawner","minecraft:trial_spawner").contains(block))throw new SecurityException("Cannot clone spawners");
                if(Boolean.getBoolean("minecraft.agent.natural_world")&&CommandPolicy.isProtectedNaturalSource(block))throw new SecurityException("Cannot clone protected natural source blocks");
            }
        }
        if(!p.has("keep_conditions")||!p.get("keep_conditions").getAsBoolean())environment.before(currentRun,text);
        server.getCommands().performPrefixedCommand(source, text);
        // Only vanilla's zero-filled-block result is classified here. A no-op
        // does not prove the requested postcondition (e.g. replace found no match).
        var words=CommandPolicy.tokens(text);
        boolean noChange=words.getFirst().replace("minecraft:","").equals("fill")&&!words.contains("destroy")
            &&messageKeys.equals(List.of("commands.fill.failed"));
        return json(Map.of("command", text, "execution_identity", identity, "callbacks", callbacks, "tick", server.getTickCount(),
            "actual_changes",noChange?0:"unavailable: compare observations","messages", messages,"message_keys",messageKeys,
            "effect",noChange?"no_change":"see_callbacks","postcondition_verified",false));
    }
    private static void commandMessageKeys(Component component,List<String> keys){
        if(component.getContents() instanceof net.minecraft.network.chat.contents.TranslatableContents translated)keys.add(translated.getKey());
        for(Component child:component.getSiblings())commandMessageKeys(child,keys);
    }
    private JsonElement structured(String method,JsonObject p){
        String text=switch(method){
            case "clock.freeze"->"tick freeze";case "clock.unfreeze"->"tick unfreeze";
            case "clock.step"->"tick step "+integer(p,"ticks");
            case "clock.rate"->"tick rate "+p.get("tps").getAsDouble();
            case "clock.sprint"->"tick sprint "+integer(p,"ticks");
            case "world.teleport"->"tp "+str(p,"target")+" "+p.get("x").getAsDouble()+" "+p.get("y").getAsDouble()+" "+p.get("z").getAsDouble();
            case "world.set_block"->"setblock "+integer(p,"x")+" "+integer(p,"y")+" "+integer(p,"z")+" "+str(p,"block");
            case "world.fill"->"fill "+str(p,"from")+" "+str(p,"to")+" "+str(p,"block");
            case "entity.kill"->"kill "+str(p,"target");
            case "entity.summon"->"summon "+str(p,"type")+" "+p.get("x").getAsDouble()+" "+p.get("y").getAsDouble()+" "+p.get("z").getAsDouble();
            case "inventory.give"->"give "+str(p,"target")+" "+str(p,"item")+" "+integer(p,"count");
            default->throw new UnsupportedOperationException(method);
        };
        JsonObject args=p.deepCopy();args.addProperty("text",text);
        if(p.has("dimension")&&!p.has("context")){JsonObject context=new JsonObject();context.add("dimension",p.get("dimension"));args.add("context",context);}
        return command(args);
    }
    private JsonElement blocks(JsonObject p) {
        ServerLevel world = level(p); JsonArray min = p.getAsJsonArray("min"), max = p.getAsJsonArray("max");
        int x0=min.get(0).getAsInt(), y0=min.get(1).getAsInt(), z0=min.get(2).getAsInt();
        int x1=max.get(0).getAsInt(), y1=max.get(1).getAsInt(), z1=max.get(2).getAsInt();
        long volume = (long)(x1-x0+1)*(y1-y0+1)*(z1-z0+1);
        if (x1<x0 || y1<y0 || z1<z0 || volume>32768) throw new IllegalArgumentException("Invalid or excessive region");
        List<Object> out = new ArrayList<>();
        for (BlockPos pos : BlockPos.betweenClosed(x0,y0,z0,x1,y1,z1)) {
            boolean loaded = world.hasChunkAt(pos);
            out.add(Map.of("position", List.of(pos.getX(),pos.getY(),pos.getZ()), "loaded", loaded, "state", loaded ? world.getBlockState(pos).toString() : "unavailable"));
        }
        return json(Map.of("blocks", out, "tick", server.getTickCount(), "source", "server", "dimension", world.dimension().identifier().toString()));
    }
    static BlockPos position(JsonObject p) { return new BlockPos(integer(p,"x"),integer(p,"y"),integer(p,"z")); }
    private JsonElement counter(String method, JsonObject p) {
        String color = str(p,"color");
        if (!Set.of("white","orange","magenta","light_blue","yellow","lime","pink","gray","light_gray","cyan","purple","blue","brown","green","red","black").contains(color)) throw new IllegalArgumentException("Invalid wool color");
        try {
            var type = Class.forName("carpet.helpers.HopperCounter");
            var counter = type.getMethod("getCounter", String.class).invoke(null, color);
            if (method.endsWith("reset")) {
                type.getMethod("reset", MinecraftServer.class).invoke(counter, server);
            }
            Map<String,Object> data = new LinkedHashMap<>();
            data.put("color",color); data.put("tick",server.overworld().getGameTime());data.put("server_tick",server.getTickCount());data.put("sample_monotonic_ns",System.nanoTime());data.put("sample_wall_ms",System.currentTimeMillis()); data.put("source","Carpet");
            for (var field : type.getDeclaredFields()) {
                if (Set.of("counter", "startTick", "startMillis").contains(field.getName())) { field.setAccessible(true); data.put(field.getName(), field.get(counter).toString()); }
            }
            try{
                var field=type.getDeclaredField("counter");field.setAccessible(true);Map<?,?> counts=(Map<?,?>)field.get(counter);Map<String,Object> items=new TreeMap<>();
                for(var entry:counts.entrySet())items.put(BuiltInRegistries.ITEM.getKey((net.minecraft.world.item.Item)entry.getKey()).toString(),entry.getValue());data.put("items",items);
            }catch(NoSuchFieldException e){data.put("items","unavailable: field changed");}
            data.put("total", type.getMethod("getTotalItems").invoke(counter));
            data.put("items_consumed",true);
            return json(data);
        } catch (ReflectiveOperationException e) { throw new IllegalStateException("Carpet API mismatch",e); }
    }
    private JsonElement fake(String operation, JsonObject p, String run) {
        String name = str(p,"id");
        if (!name.matches("[A-Za-z0-9_]{1,16}")) throw new IllegalArgumentException("Invalid player name");
        if (operation.equals("spawn")) {
            if (server.getPlayerList().getPlayerByName(name)!=null || fakeOwners.containsKey(name)||isSpawning(name)) throw new IllegalStateException("Name already in use");
            String cmd = "player " + name + " spawn";
            fakeOwners.put(name,run);
            if (p.has("x")) cmd += " at " + p.get("x").getAsDouble() + " " + p.get("y").getAsDouble() + " " + p.get("z").getAsDouble()+" facing "+(p.has("yaw")?p.get("yaw").getAsFloat():0)+" "+(p.has("pitch")?p.get("pitch").getAsFloat():0)+" in "+level(p).dimension().identifier();
            try{raw(cmd);}catch(Exception e){retiringFakes.add(name);throw e;}
        } else {
            if (!run.equals(fakeOwners.get(name))) {
                if(fakeOwners.get(name)==null&&run.equals(completedFakeOwners.get(name))&&server.getPlayerList().getPlayerByName(name)==null&&!isSpawning(name)&&Set.of("stop","remove","status").contains(operation))
                    return json(Map.of("id",name,"status","already_removed","connected",false,"alive",false,"cleanup_confirmed",true));
                throw new SecurityException("Fake player not owned by run");
            }
            if(operation.equals("status")) {
                JsonObject state=actorState(name).getAsJsonObject();state.addProperty("id",name);
                state.addProperty("status",retiringFakes.contains(name)?"retiring":isFake(name)?"ready":isSpawning(name)?"spawning":"failed_or_offline");return state;
            }
            if(!isFake(name)) {
                if(operation.equals("remove")||operation.equals("stop")){retiringFakes.add(name);return json(Map.of("id",name,"status","cleanup_pending"));}
                throw new IllegalStateException("Owned fake not online yet; poll fake_player.status");
            }
            switch (operation) {
                case "equip" -> {
                    JsonObject payload=new JsonObject();payload.addProperty("id",str(p,"item"));payload.addProperty("count",1);payload.add("components",p.getAsJsonObject("components"));
                    var stack=net.minecraft.world.item.ItemStack.CODEC.parse(server.registryAccess().createSerializationContext(com.mojang.serialization.JsonOps.INSTANCE),payload).getOrThrow();
                    var validity=new SurvivalEquipment();validity.inspect(stack,"mainhand");
                    if(!validity.report().get("passed").equals(true))throw new SecurityException("Equipment violates survival constraints: "+validity.report().get("violations"));
                    server.getPlayerList().getPlayerByName(name).setItemInHand(net.minecraft.world.InteractionHand.MAIN_HAND,stack);
                    return json(validity.report());
                }
                case "inventory" -> {
                    var player=server.getPlayerList().getPlayerByName(name);
                    var value=new LinkedHashMap<>(BucketBatch.inventory(player,bucketBatches.get(name)));
                    if(combinedOperations.containsKey(name))value.put("combined_operation",combinedOperations.get(name).status(player));
                    if(offhandUses.containsKey(name))value.put("offhand_use",offhandUses.get(name).status());
                    if(materialUses.containsKey(name))value.put("material_use",materialUses.get(name).status());
                    return json(value);
                }
                case "attack_and_use" -> {
                    if(!str(p,"mode").equals("continuous"))throw new IllegalArgumentException("Combined operation requires continuous mode");
                    if(combinedOperations.containsKey(name))throw new IllegalStateException("Combined operation already initialized");
                    raw("player "+name+" stop");combinedOperations.put(name,new CombinedOperation(server.getPlayerList().getPlayerByName(name)));
                }
                case "use_material" -> {
                    if(!str(p,"mode").equals("continuous"))throw new IllegalArgumentException("Material use requires continuous mode");
                    String item=str(p,"item");BuiltInRegistries.ITEM.getOptional(Identifier.parse(item)).orElseThrow();
                    materialUses.put(name,new MaterialUse(item));raw("player "+name+" stop");
                }
                case "drop_inventory" -> {
                    var player=server.getPlayerList().getPlayerByName(name);int dropped=0;
                    for(int slot=0;slot<player.getInventory().getContainerSize();slot++) {
                        var stack=player.getInventory().removeItemNoUpdate(slot);
                        if(stack.isEmpty())continue;
                        int count=stack.getCount();
                        if(player.drop(stack,false,true)==null)player.getInventory().setItem(slot,stack);else dropped+=count;
                    }
                    return json(Map.of("version","inventory-return-v1","dropped_existing_items",dropped,"items_created",0));
                }
                case "use_with_offhand" -> {
                    if(!str(p,"mode").equals("continuous"))throw new IllegalArgumentException("Offhand use requires continuous mode");
                    String item=str(p,"item");BuiltInRegistries.ITEM.getOptional(Identifier.parse(item)).orElseThrow(()->new IllegalArgumentException("Unknown offhand item"));
                    offhandUses.put(name,new OffhandUse(item,server.getPlayerList().getPlayerByName(name)));raw("player "+name+" stop");
                }
                case "bucket_batch" -> {
                    if(bucketBatches.containsKey(name))throw new IllegalStateException("Bucket batch already initialized; replenishment is forbidden");
                    raw("player "+name+" stop");
                    var batch=new BucketBatch(server.getPlayerList().getPlayerByName(name),str(p,"product"));bucketBatches.put(name,batch);
                    return json(BucketBatch.inventory(server.getPlayerList().getPlayerByName(name),batch));
                }
                case "stop" -> {bucketBatches.remove(name);combinedOperations.remove(name);offhandUses.remove(name);materialUses.remove(name);raw("player " + name + " stop");}
                case "remove" -> {bucketBatches.remove(name);combinedOperations.remove(name);offhandUses.remove(name);materialUses.remove(name);raw("player " + name + " stop"); raw("player " + name + " kill"); retiringFakes.add(name); }
                case "attack", "use" -> {
                    String mode = str(p,"mode");
                    if (!Set.of("once","continuous","interval").contains(mode)) throw new IllegalArgumentException("Invalid action mode");
                    if (mode.equals("interval")) { int n=integer(p,"interval_ticks"); if(n<1||n>72000) throw new IllegalArgumentException("Invalid interval"); mode += " " + n; }
                    raw("player " + name + " " + operation + " " + mode);
                }
                case "look" -> {
                    float yaw=p.get("yaw").getAsFloat(),pitch=p.get("pitch").getAsFloat();
                    if(!Float.isFinite(yaw)||!Float.isFinite(pitch)||pitch< -90||pitch>90)throw new IllegalArgumentException("Invalid player rotation");
                    var player=server.getPlayerList().getPlayerByName(name);
                    player.setYRot(yaw%360);player.setYHeadRot(yaw%360);player.setXRot(pitch);
                }
                case "move" -> {
                    String direction = str(p,"direction");
                    if(!Set.of("forward","backward","left","right").contains(direction)) throw new IllegalArgumentException("Invalid direction");
                    raw("player " + name + " move " + direction);
                }
                default -> throw new UnsupportedOperationException(operation);
            }
        }
        return json(Map.of("id",name,"operation",operation,"status",isFake(name)?"ready":isSpawning(name)?"spawning":"not_online","tick",server.getTickCount()));
    }
    private boolean isFake(String name){var player=server.getPlayerList().getPlayerByName(name);return player!=null&&player.getClass().getName().equals("carpet.patches.EntityPlayerMPFake");}
    private JsonElement afkSite(JsonObject p) {
        var world=level(p);var point=p.getAsJsonArray("position");double x=point.get(0).getAsDouble(),y=point.get(1).getAsDouble(),z=point.get(2).getAsDouble();
        var min=BlockPos.containing(x-.3,y-.06,z-.3);var max=BlockPos.containing(x+.3,y+1.8,z+.3);
        for(var pos:BlockPos.betweenClosed(min,max))if(!world.hasChunkAt(pos))return json(Map.of("loaded",false,"position",List.of(x,y,z),"dimension",world.dimension().identifier().toString()));
        var body=new net.minecraft.world.phys.AABB(x-.299,y+.001,z-.299,x+.299,y+1.799,z+.299);
        boolean clear=true,support=false;
        for(var shape:world.getBlockCollisions(null,body))if(!shape.isEmpty())clear=false;
        for(var shape:world.getBlockCollisions(null,body.move(0,-.06,0)))if(!shape.isEmpty())support=true;
        return json(Map.of("loaded",true,"position",List.of(x,y,z),"dimension",world.dimension().identifier().toString(),"body_clear",clear,"has_support",support,"fluid_at_feet",!world.getFluidState(BlockPos.containing(x,y,z)).isEmpty(),"tick",world.getGameTime()));
    }
    private boolean isSpawning(String name){try{return (boolean)Class.forName("carpet.patches.EntityPlayerMPFake").getMethod("isSpawningPlayer",String.class).invoke(null,name);}catch(ReflectiveOperationException e){throw new IllegalStateException("Carpet fake API unavailable",e);}}
    private JsonElement conditions(){
        List<Object> worlds=new ArrayList<>();
        for(var level:server.getAllLevels()) {
            Map<String,Object> rules=new TreeMap<>();
            level.getGameRules().visitGameRuleTypes(new GameRuleTypeVisitor(){public <T>void visit(GameRule<T> rule){rules.put(rule.getIdentifier().toString(),level.getGameRules().get(rule));}});
            worlds.add(Map.of("dimension",level.dimension().identifier().toString(),"game_tick",level.getGameTime(),"default_clock_ticks",level.getDefaultClockTime(),"raining",level.isRaining(),"thundering",level.isThundering(),"difficulty",level.getDifficulty().getSerializedName(),"gamerules",rules));
        }
        return json(Map.of("worlds",worlds,"players",server.getPlayerList().getPlayers().stream().map(player->Map.of("id",player.getPlainTextName(),"effects",player.getActiveEffects().stream().map(Object::toString).toList())).toList(),"clock",Map.of("game_tick",server.overworld().getGameTime(),"server_tick",server.getTickCount(),"frozen",server.tickRateManager().isFrozen(),"target_tps",server.tickRateManager().tickrate(),"sprinting",server.tickRateManager().isSprinting()),"source","server"));
    }
    private void cleanupFailure(Exception error){if(cleanupFailures.size()==32)cleanupFailures.removeFirst();cleanupFailures.add(error.toString());}
}
