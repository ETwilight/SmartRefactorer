package research.agent;

import com.google.gson.*;
import fi.dy.masa.litematica.data.DataManager;
import fi.dy.masa.litematica.schematic.LitematicaSchematic;
import fi.dy.masa.litematica.schematic.placement.*;
import fi.dy.masa.litematica.selection.*;
import fi.dy.masa.litematica.world.SchematicWorldHandler;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.level.block.Rotation;
import net.minecraft.world.level.block.Mirror;
import java.nio.file.*;
import java.util.*;
import static research.agent.RpcServer.*;

/** Version-specific Java mod API calls, never world paste or arbitrary reflection. */
public final class ModAdapter {
    private static final Map<String,LitematicaSchematic> schematics=new HashMap<>();
    private static final Map<String,SchematicPlacement> placements=new HashMap<>();
    private static Path directory() {
        // Match the active Litematica instance, including HMCL version isolation.
        Path root=DataManager.getSchematicsBaseDirectory().toAbsolutePath().normalize();
        try {Files.createDirectories(root);}catch(Exception e){throw new RuntimeException(e);}return root;
    }
    private static Path file(String filename) {
        Path root=directory(),path=root.resolve(filename).normalize();
        if(!path.startsWith(root)||!path.getFileName().toString().endsWith(".litematic"))throw new SecurityException("Invalid schematic path");
        try {if(Files.exists(path)&&!path.toRealPath().startsWith(root.toRealPath()))throw new SecurityException("Schematic symlink outside root");}
        catch(java.io.IOException e){throw new RuntimeException(e);}return path;
    }
    public static JsonElement schematic(String method,JsonObject p,Minecraft mc) {
        switch(method) {
            case "schematic.list":
                try(var files=Files.list(directory())) {return json(files.filter(f->f.getFileName().toString().endsWith(".litematic")).map(f->f.getFileName().toString()).toList());}
                catch(Exception e){throw new RuntimeException(e);}
            case "schematic.load": {
                Path path=file(str(p,"file"));var schematic=LitematicaSchematic.createFromFile(path.getParent(),path.getFileName().toString());
                if(schematic==null)throw new IllegalArgumentException("Could not load schematic");
                String id=UUID.randomUUID().toString();schematics.put(id,schematic);
                return json(Map.of("schematic_id",id,"regions",schematic.getAreaSizes().keySet(),"data_version",schematic.getMetadata().getMinecraftDataVersion(),"litematic_version",schematic.getMetadata().getSchematicVersion(),"source","Litematica"));
            }
            case "schematic.place": {
                String id=p.has("placement_id")?str(p,"placement_id"):UUID.randomUUID().toString();
                var schematic=p.has("placement_id")?placement(p).getSchematic():schematics.get(str(p,"schematic_id"));if(schematic==null)throw new IllegalArgumentException("Unknown schematic");
                var placement=p.has("placement_id")?placement(p):SchematicPlacement.createFor(schematic,ServerBridge.position(p),id,true,true);
                if(p.has("x"))placement.setOrigin(ServerBridge.position(p),message->{});
                placement.setRotation(Rotation.valueOf(p.has("rotation")?str(p,"rotation"):"NONE"),null);
                placement.setMirror(Mirror.valueOf(p.has("mirror")?str(p,"mirror"):"NONE"),null);
                if(p.has("subregions"))for(var entry:p.getAsJsonObject("subregions").entrySet()){
                    var sub=placement.getRelativeSubRegionPlacement(entry.getKey());if(sub==null)throw new IllegalArgumentException("Unknown subregion: "+entry.getKey());var settings=entry.getValue().getAsJsonObject();
                    if(settings.has("enabled"))placement.setSubRegionsEnabledState(settings.get("enabled").getAsBoolean(),List.of(sub),null);
                    if(settings.has("rotation"))placement.setSubRegionRotation(entry.getKey(),Rotation.valueOf(str(settings,"rotation")),null);
                    if(settings.has("mirror"))placement.setSubRegionMirror(entry.getKey(),Mirror.valueOf(str(settings,"mirror")),null);
                    if(settings.has("x"))placement.moveSubRegionTo(entry.getKey(),ServerBridge.position(settings),message->{});
                }
                if(!p.has("placement_id"))DataManager.getSchematicPlacementManager().addSchematicPlacement(placement,true);placements.put(id,placement);
                return json(Map.of("placement_id",id,"world_changed",false,"projection_only",true,"placement",placement.toJson()));
            }
            case "schematic.materials": {
                var placement=placement(p);var list=placement.getMaterialList();
                if(list==null)throw new IllegalStateException("Material list not ready");
                if(p.has("refresh")&&p.get("refresh").getAsBoolean())list.reCreateMaterialList();
                return json(Map.of("materials",list.getMaterialsAll().stream().map(e->Map.of("item",BuiltInRegistries.ITEM.getKey(e.getStack().getItem()).toString(),"total",e.getCountTotal(),"missing",e.getCountMissing(),"mismatched",e.getCountMismatched(),"available",e.getCountAvailable())).toList(),"source","Litematica","asynchronous_refresh",true));
            }
            case "schematic.diff": {
                var placement=placement(p);var schematicWorld=SchematicWorldHandler.getSchematicWorld();
                if(schematicWorld==null)throw new IllegalStateException("Schematic world unavailable");
                if(DataManager.getSchematicPlacementManager().getAllSchematicsPlacements().stream().anyMatch(other->other!=placement&&other.isEnabled()))throw new IllegalStateException("Disable other projections before comparing; rendered schematic world contains all placements");
                List<Object> diff=new ArrayList<>();int checked=0,unloaded=0;Set<BlockPos> seen=new HashSet<>();
                BlockPos rangeMin=p.has("min")?BlockPos.containing(ClientBridge.vector(p.getAsJsonArray("min"))):null,rangeMax=p.has("max")?BlockPos.containing(ClientBridge.vector(p.getAsJsonArray("max"))):null;
                if((rangeMin==null)!=(rangeMax==null))throw new IllegalArgumentException("Both min and max required");
                for(var box:placement.getSubRegionBoxes(SubRegionPlacement.RequiredEnabled.PLACEMENT_ENABLED).values()) {
                    var a=box.getPos1();var b=box.getPos2();
                    int x0=Math.min(a.getX(),b.getX()),y0=Math.min(a.getY(),b.getY()),z0=Math.min(a.getZ(),b.getZ()),x1=Math.max(a.getX(),b.getX()),y1=Math.max(a.getY(),b.getY()),z1=Math.max(a.getZ(),b.getZ());
                    if(rangeMin!=null){x0=Math.max(x0,rangeMin.getX());y0=Math.max(y0,rangeMin.getY());z0=Math.max(z0,rangeMin.getZ());x1=Math.min(x1,rangeMax.getX());y1=Math.min(y1,rangeMax.getY());z1=Math.min(z1,rangeMax.getZ());}
                    if(x0>x1||y0>y1||z0>z1)continue;
                    for(var mutable:BlockPos.betweenClosed(x0,y0,z0,x1,y1,z1)) {
                        BlockPos pos=mutable.immutable();if(!seen.add(pos))continue;
                        if(seen.size()>32768)throw new IllegalArgumentException("Diff exceeds bounded 32768 block limit");
                        if(!mc.level.hasChunkAt(pos)||!schematicWorld.hasChunkAt(pos)){unloaded++;continue;}
                        checked++;var expected=schematicWorld.getBlockState(pos);var actual=mc.level.getBlockState(pos);
                        if(!expected.equals(actual))diff.add(Map.of("position",List.of(pos.getX(),pos.getY(),pos.getZ()),"expected",expected.toString(),"actual",actual.toString(),"kind",expected.isAir()?"extra":actual.isAir()?"missing":"state_mismatch"));
                    }
                }
                return json(Map.of("differences",diff,"checked",checked,"unloaded",unloaded,"complete",unloaded==0,"scope",rangeMin==null?"all enabled regions":"requested region only","projection_readiness","sampled rendered cache; caller must allow placement rebuild","source","Litematica + client cache","functional_success",false));
            }
            case "schematic.save": {
                Path path=file(str(p,"file"));BlockPos min=BlockPos.containing(ClientBridge.vector(p.getAsJsonArray("min"))),max=BlockPos.containing(ClientBridge.vector(p.getAsJsonArray("max")));
                long volume=(long)(max.getX()-min.getX()+1)*(max.getY()-min.getY()+1)*(max.getZ()-min.getZ()+1);
                if(volume<1||volume>32768)throw new IllegalArgumentException("Invalid or excessive region");
                for(var pos:BlockPos.betweenClosed(min,max))if(!mc.level.hasChunkAt(pos))throw new IllegalStateException("Cannot save unloaded region");
                AreaSelection selection=new AreaSelection();selection.setName(path.getFileName().toString());selection.setExplicitOrigin(min);selection.addSubRegionBox(new Box(min,max,"main"),true);
                var result=LitematicaSchematic.createFromWorld(mc.level,selection,new LitematicaSchematic.SchematicSaveInfo(false,false,true,false),mc.player.getPlainTextName(),message->{});
                if(result==null||!result.writeToFile(path.getParent(),path.getFileName().toString(),false))throw new IllegalStateException("Save failed or destination exists");
                return json(Map.of("file",path.toString(),"source","client cache","region_blocks",volume));
            }
            default:throw new UnsupportedOperationException(method);
        }
    }
    private static SchematicPlacement placement(JsonObject p){var placement=placements.get(str(p,"placement_id"));if(placement==null)throw new IllegalArgumentException("Unknown placement");return placement;}
    private static final Map<String,String[]> CONFIGS=Map.of(
        "litematica.easy_place",new String[]{"fi.dy.masa.litematica.config.Configs$Generic","EASY_PLACE_MODE"},
        "litematica.render",new String[]{"fi.dy.masa.litematica.config.Configs$Visuals","ENABLE_RENDERING"},
        "tweakeroo.fast_right_click",new String[]{"fi.dy.masa.tweakeroo.config.FeatureToggle","TWEAK_FAST_RIGHT_CLICK"},
        "tweakeroo.fast_left_click",new String[]{"fi.dy.masa.tweakeroo.config.FeatureToggle","TWEAK_FAST_LEFT_CLICK"},
        "minihud.render",new String[]{"fi.dy.masa.minihud.config.Configs$Generic","MAIN_RENDERING_TOGGLE"}
    );
    public static JsonElement config(String method,JsonObject p){
        String key=str(p,"key");var spec=CONFIGS.get(key);if(spec==null)throw new SecurityException("Mod setting not whitelisted");
        try {
            Object config=Class.forName(spec[0]).getField(spec[1]).get(null);
            if(method.endsWith("set"))config.getClass().getMethod("setBooleanValue",boolean.class).invoke(config,p.get("enabled").getAsBoolean());
            Object value=config.getClass().getMethod("getBooleanValue").invoke(config);
            return json(Map.of("key",key,"enabled",value,"source",key.split("\\.")[0],"scope","current client session"));
        } catch(ReflectiveOperationException e){throw new IllegalStateException("Pinned mod config API unavailable: "+key,e);}
    }
}
