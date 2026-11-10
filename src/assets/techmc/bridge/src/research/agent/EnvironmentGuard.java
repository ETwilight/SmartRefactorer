package research.agent;

import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.ChunkPos;
import net.minecraft.world.level.gamerules.*;
import net.minecraft.world.effect.MobEffectInstance;
import java.util.*;

/** Captures only temporary environment categories actually changed by this run.
 * Structure edits, inventory preparation, deaths and spawns are deliberately not rolled back.
 */
public final class EnvironmentGuard {
    private final MinecraftServer server;
    private final Map<String,LinkedHashMap<String,Runnable>> runs=new HashMap<>();
    public EnvironmentGuard(MinecraftServer server){this.server=server;}
    public void before(String run,String command){
        List<String> words=CommandPolicy.tokens(command);int start=0;
        for(int i=0;i<words.size();i++)if(words.get(i).equals("run"))start=i+1;
        String root=words.get(start).replace("minecraft:","");
        if(!Set.of("tick","time","weather","gamerule","forceload","effect","difficulty","gamemode").contains(root))return;
        List<String> tail=words.subList(start,words.size());
        if((root.equals("gamerule")&&tail.size()<3)||(root.equals("difficulty")&&tail.size()==1)||((root.equals("time")||root.equals("forceload")||root.equals("tick"))&&tail.contains("query")))return;
        var guards=runs.computeIfAbsent(run,k->new LinkedHashMap<>());
        if(guards.containsKey(root))return;
        Runnable undo=switch(root){
            case "tick"->{var manager=server.tickRateManager();float rate=manager.tickrate();boolean frozen=manager.isFrozen();yield ()->{manager.stopSprinting();manager.stopStepping();manager.setTickRate(rate);manager.setFrozen(frozen);};}
            case "time"->{
                var state=server.clockManager().packState();
                yield ()->state.clocks().forEach((holder,clock)->{server.clockManager().setTotalTicks(holder,clock.totalTicks());server.clockManager().setRate(holder,clock.rate());server.clockManager().setPaused(holder,clock.paused());});
            }
            case "weather"->{
                var data=server.getWeatherData();int clear=data.getClearWeatherTime(),rain=data.getRainTime(),thunder=data.getThunderTime();boolean raining=data.isRaining(),thundering=data.isThundering();
                yield ()->{data.setClearWeatherTime(clear);data.setRainTime(rain);data.setThunderTime(thunder);data.setRaining(raining);data.setThundering(thundering);};
            }
            case "forceload"->{
                Map<ServerLevel,Set<Long>> initial=new LinkedHashMap<>();for(var level:server.getAllLevels())initial.put(level,new HashSet<>(level.getForceLoadedChunks()));
                yield ()->initial.forEach((level,chunks)->{Set<Long> current=new HashSet<>(level.getForceLoadedChunks());for(long packed:current)if(!chunks.contains(packed))level.setChunkForced(ChunkPos.getX(packed),ChunkPos.getZ(packed),false);for(long packed:chunks)if(!current.contains(packed))level.setChunkForced(ChunkPos.getX(packed),ChunkPos.getZ(packed),true);});
            }
            case "gamerule"->{
                Map<ServerLevel,Map<GameRule<?>,Object>> initial=new LinkedHashMap<>();
                for(var level:server.getAllLevels()){
                    Map<GameRule<?>,Object> rules=new LinkedHashMap<>();level.getGameRules().visitGameRuleTypes(new GameRuleTypeVisitor(){public <T>void visit(GameRule<T> rule){rules.put(rule,level.getGameRules().get(rule));}});initial.put(level,rules);
                }
                yield ()->initial.forEach((level,rules)->rules.forEach((rule,value)->restoreRule(level.getGameRules(),rule,value)));
            }
            case "effect"->{
                Map<UUID,List<MobEffectInstance>> initial=new HashMap<>();for(var player:server.getPlayerList().getPlayers())initial.put(player.getUUID(),player.getActiveEffects().stream().map(MobEffectInstance::new).toList());
                yield ()->initial.forEach((uuid,effects)->{var player=server.getPlayerList().getPlayer(uuid);if(player!=null){player.removeAllEffects();effects.forEach(e->player.addEffect(new MobEffectInstance(e)));}});
            }
            case "difficulty"->{var initial=server.getWorldData().getDifficulty();yield ()->server.setDifficulty(initial,true);}
            case "gamemode"->{
                Map<UUID,net.minecraft.world.level.GameType> initial=new HashMap<>();for(var player:server.getPlayerList().getPlayers())initial.put(player.getUUID(),player.gameMode.getGameModeForPlayer());
                yield ()->initial.forEach((uuid,mode)->{var player=server.getPlayerList().getPlayer(uuid);if(player!=null)player.setGameMode(mode);});
            }
            default->throw new IllegalStateException(root);
        };
        guards.put(root,undo);
    }
    @SuppressWarnings("unchecked")private <T>void restoreRule(GameRules rules,GameRule<T> rule,Object value){rules.set(rule,(T)value,server);}
    public Map<String,Object> restore(String run,Set<String> keep){
        var guards=runs.remove(run);if(guards==null)return Map.of("restored",List.of(),"errors",List.of());
        List<String> restored=new ArrayList<>(),errors=new ArrayList<>();
        List<String> categories=new ArrayList<>(guards.keySet());Collections.reverse(categories);
        for(String category:categories)if(!keep.contains(category))try{guards.get(category).run();restored.add(category);}catch(Exception error){errors.add(category+": "+error);}
        return Map.of("restored",restored,"kept",keep,"errors",errors,"scope","temporary environment; effect restoration covers online players at first mutation");
    }
}
