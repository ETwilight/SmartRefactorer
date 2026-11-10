package research.agent;

import java.util.*;
import net.minecraft.core.BlockPos;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.boss.wither.WitherBoss;
import net.minecraft.world.level.block.Blocks;

/** Read-only origin/proximity observations for real summoned and star-dropping withers. */
public final class WitherSourceLedger {
    public static final int BEDROCK_RADIUS=8;
    private static final int LIMIT=65536;
    private static final String EPOCH=UUID.randomUUID().toString();
    private static final Set<WitherBoss> PENDING=Collections.newSetFromMap(new WeakHashMap<>());
    private static final Map<UUID,Trace> TRACES=new LinkedHashMap<>();
    private static final Set<String> ERRORS=new LinkedHashSet<>();
    private static boolean complete=true;
    private static final class Event {
        final String dimension;
        final List<Double> position;
        final boolean nearBedrock;
        Event(ServerLevel world,WitherBoss boss) {
            dimension=world.dimension().identifier().toString();
            position=List.of(boss.getX(),boss.getY(),boss.getZ());
            nearBedrock=nearBedrock(world,boss);
        }
        boolean in(ServerLevel world,BlockPos min,BlockPos max) {
            if(!dimension.equals(world.dimension().identifier().toString()))return false;
            return position.get(0)>=min.getX()&&position.get(0)<max.getX()+1.0
                &&position.get(1)>=min.getY()&&position.get(1)<max.getY()+1.0
                &&position.get(2)>=min.getZ()&&position.get(2)<max.getZ()+1.0;
        }
    }
    private static final class Trace {
        final UUID uuid;
        final boolean naturalSpawn;
        Event spawn,death;
        Trace(UUID uuid,boolean naturalSpawn){this.uuid=uuid;this.naturalSpawn=naturalSpawn;}
    }
    public static void unavailable(String reason) {
        complete=false;
        if(ERRORS.size()<32)ERRORS.add(reason);
    }
    private static boolean nearBedrock(ServerLevel world,WitherBoss boss) {
        BlockPos center=boss.blockPosition();boolean unloaded=false;
        for(BlockPos pos:BlockPos.betweenClosed(center.offset(-BEDROCK_RADIUS,-BEDROCK_RADIUS,-BEDROCK_RADIUS),center.offset(BEDROCK_RADIUS,BEDROCK_RADIUS,BEDROCK_RADIUS))) {
            double dx=pos.getX()+0.5-boss.getX(),dy=pos.getY()+0.5-boss.getY(),dz=pos.getZ()+0.5-boss.getZ();
            if(dx*dx+dy*dy+dz*dz>BEDROCK_RADIUS*BEDROCK_RADIUS||world.isOutsideBuildHeight(pos))continue;
            if(!world.hasChunkAt(pos)){unloaded=true;continue;}
            if(world.getBlockState(pos).is(Blocks.BEDROCK))return true;
        }
        if(unloaded)unavailable("Wither bedrock proximity crossed unloaded chunks");
        return false;
    }
    /** makeInvulnerable has a single vanilla caller: the soul-block/skull pattern. */
    public static void markPatternSpawn(WitherBoss boss) {
        try {
            if(!(boss.level() instanceof ServerLevel))return;
            if(PENDING.size()>=LIMIT){unavailable("Wither pending-source budget exceeded");return;}
            PENDING.add(boss);
        } catch(RuntimeException ex){unavailable("Wither pattern observation failed: "+ex.getClass().getSimpleName());}
    }
    /** Called only after ServerLevel confirms that addFreshEntity succeeded. */
    public static void added(ServerLevel world,Entity entity,boolean accepted) {
        if(!(entity instanceof WitherBoss boss))return;
        try {
            boolean fromPattern=PENDING.remove(boss);
            if(!accepted)return;
            if(TRACES.size()>=LIMIT&&!TRACES.containsKey(boss.getUUID())){unavailable("Wither source-ledger budget exceeded");return;}
            if(TRACES.containsKey(boss.getUUID())){unavailable("Duplicate wither spawn observation");return;}
            Trace trace=new Trace(boss.getUUID(),fromPattern);trace.spawn=new Event(world,boss);TRACES.put(trace.uuid,trace);
        } catch(RuntimeException ex){unavailable("Wither spawn observation failed: "+ex.getClass().getSimpleName());}
    }
    /** WitherBoss.dropCustomDeathLoot is the vanilla path that emits a nether star. */
    public static void starDeath(ServerLevel world,WitherBoss boss) {
        try {
            Trace trace=TRACES.get(boss.getUUID());
            if(trace==null) {
                if(TRACES.size()>=LIMIT){unavailable("Wither source-ledger budget exceeded");return;}
                trace=new Trace(boss.getUUID(),false);TRACES.put(trace.uuid,trace);
            }
            if(trace.death!=null){unavailable("Duplicate wither star-death observation");return;}
            trace.death=new Event(world,boss);
        } catch(RuntimeException ex){unavailable("Wither death observation failed: "+ex.getClass().getSimpleName());}
    }
    public static Map<String,Object> read(ServerLevel world,BlockPos min,BlockPos max) {
        long spawned=0,spawnedNear=0,deaths=0,deathsNear=0,untrackedDeaths=0;
        List<Object> events=new ArrayList<>(),nodes=new ArrayList<>();
        for(Trace trace:TRACES.values()) {
            boolean spawnHere=trace.spawn!=null&&trace.spawn.in(world,min,max);
            boolean deathHere=trace.death!=null&&trace.death.in(world,min,max);
            if(!spawnHere&&!deathHere)continue;
            Map<String,Object> node=new LinkedHashMap<>();node.put("entity_uuid",trace.uuid.toString());node.put("natural_spawn",trace.naturalSpawn);
            if(trace.spawn!=null) {node.put("spawn_dimension",trace.spawn.dimension);node.put("spawn_position",trace.spawn.position);node.put("spawn_near_bedrock",trace.spawn.nearBedrock);}
            if(trace.death!=null) {node.put("death_dimension",trace.death.dimension);node.put("death_position",trace.death.position);node.put("death_near_bedrock",trace.death.nearBedrock);}
            nodes.add(node);
            if(spawnHere) {
                spawned++;if(trace.spawn.nearBedrock)spawnedNear++;
                events.add(Map.of("kind","spawn","entity_uuid",trace.uuid.toString(),"dimension",trace.spawn.dimension,"position",trace.spawn.position,"near_bedrock",trace.spawn.nearBedrock,"natural_spawn",trace.naturalSpawn));
            }
            if(deathHere) {
                deaths++;if(trace.death.nearBedrock)deathsNear++;if(!trace.naturalSpawn)untrackedDeaths++;
                events.add(Map.of("kind","star_death","entity_uuid",trace.uuid.toString(),"dimension",trace.death.dimension,"position",trace.death.position,"near_bedrock",trace.death.nearBedrock,"natural_spawn",trace.naturalSpawn));
            }
        }
        Map<String,Object> result=new LinkedHashMap<>();
        result.put("version","wither-sources-v1");result.put("epoch",EPOCH);result.put("complete",complete);
        result.put("natural_world",Boolean.getBoolean("minecraft.agent.natural_world"));result.put("radius",BEDROCK_RADIUS);
        result.put("spawned",spawned);result.put("spawned_near_bedrock",spawnedNear);result.put("deaths",deaths);result.put("deaths_near_bedrock",deathsNear);result.put("untracked_deaths",untrackedDeaths);
        result.put("events",events);result.put("nodes",nodes);result.put("observer_errors",List.copyOf(ERRORS));return result;
    }
}
