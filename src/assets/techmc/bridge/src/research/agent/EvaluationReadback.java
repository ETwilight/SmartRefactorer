package research.agent;

import com.google.gson.*;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.core.BlockPos;
import net.minecraft.core.NonNullList;
import net.minecraft.core.component.DataComponents;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.core.registries.Registries;
import net.minecraft.resources.Identifier;
import net.minecraft.world.Container;
import net.minecraft.world.SimpleContainer;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.EquipmentSlot;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.entity.npc.InventoryCarrier;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.entity.item.FallingBlockEntity;
import net.minecraft.world.entity.monster.EnderMan;
import net.minecraft.world.level.block.ChestBlock;
import net.minecraft.world.level.block.entity.AbstractFurnaceBlockEntity;
import net.minecraft.world.level.block.state.properties.ChestType;
import net.minecraft.world.phys.AABB;
import java.util.*;
import static research.agent.RpcServer.*;

/** Read-only evaluator observations. No layout templates, item injection or goals. */
public final class EvaluationReadback {
    public static JsonElement naturalSource(ServerLevel world,JsonObject p) {
        BlockPos pos=ServerBridge.position(p);
        var structure=world.registryAccess().lookupOrThrow(Registries.STRUCTURE).getValue(Identifier.parse(str(p,"structure")));
        if(structure==null)throw new IllegalArgumentException("Unknown structure");
        if(!world.hasChunkAt(pos))return json(Map.of("loaded",false,"natural_world",Boolean.getBoolean("minecraft.agent.natural_world")));
        var start=world.structureManager().getStructureAt(pos,structure);
        return json(Map.of("loaded",true,"natural_world",Boolean.getBoolean("minecraft.agent.natural_world"),"valid_structure",start.isValid()&&start.getBoundingBox().isInside(pos),"position",xyz(pos),"structure",str(p,"structure")));
    }
    private static List<Integer> xyz(BlockPos p) { return List.of(p.getX(),p.getY(),p.getZ()); }
    private static String key(ServerLevel world, BlockPos p) { return world.dimension().identifier()+":"+p.getX()+","+p.getY()+","+p.getZ(); }
    private static Map<String,Object> inventory(Container c, String identity, List<String> products) {
        return inventory(c,identity,products,true);
    }
    private static Map<String,Object> inventory(Container c, String identity, List<String> products, boolean unpack) {
        List<Object> slots = new ArrayList<>();
        for(int slot=0;slot<c.getContainerSize();slot++) {
            ItemStack stack=c.getItem(slot);
            Map<String,Object> capacities=new TreeMap<>();
            for(String product:products) {
                var item=BuiltInRegistries.ITEM.getOptional(Identifier.parse(product)).orElseThrow(()->new IllegalArgumentException("Unknown product: "+product));
                ItemStack probe=new ItemStack(item);
                int max=c.getMaxStackSize(probe);
                boolean accepts=c.canPlaceItem(slot,probe);
                boolean compatible=stack.isEmpty()||ItemStack.isSameItemSameComponents(stack,probe)||(probe.getMaxStackSize()==1&&stack.is(item));
                capacities.put(product,Map.of("max_stack",probe.getMaxStackSize(),"capacity",accepts&&compatible?max:0,"free",accepts&&compatible?Math.max(0,max-stack.getCount()):0));
            }
            Map<String,Object> entry=new LinkedHashMap<>(Map.of("slot_id",identity+":"+slot,"index",slot,"item",BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),"count",stack.getCount(),"products",capacities));
            entry.put("accepts_shulker",c.canPlaceItem(slot,new ItemStack(Items.SHULKER_BOX)));
            entry.put("item_components",ItemComponents.read(stack));
            String itemId=BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            if(unpack && itemId.endsWith("shulker_box")) {
                var contents=NonNullList.withSize(27,ItemStack.EMPTY);
                var packed=stack.get(DataComponents.CONTAINER);
                if(packed!=null)packed.copyInto(contents);
                SimpleContainer box=new SimpleContainer(27);
                for(int inner=0;inner<27;inner++)box.setItem(inner,contents.get(inner));
                entry.put("shulker_slots",inventory(box,identity+":"+slot+"/shulker",products,false).get("slots"));
            }
            slots.add(entry);
        }
        return new LinkedHashMap<>(Map.of("container_id",identity,"slots",slots,"slot_count",c.getContainerSize()));
    }
    public static JsonElement container(ServerLevel world,JsonObject p) {
        List<String> products=p.has("products")?p.getAsJsonArray("products").asList().stream().map(JsonElement::getAsString).toList():List.of();
        if(products.size()>32)throw new IllegalArgumentException("At most 32 products");
        List<Object> slots=new ArrayList<>();List<String> members=new ArrayList<>();List<Object> locations=new ArrayList<>();String type;
        Boolean openable=null;
        if(p.has("entity_uuid")) {
            Entity e=world.getEntity(UUID.fromString(str(p,"entity_uuid")));
            if(!(e instanceof Container c))throw new IllegalArgumentException("Loaded entity is not a container");
            String identity=world.dimension().identifier()+":entity:"+e.getUUID();
            locations.add(List.of(e.getX(),e.getY(),e.getZ()));
            var one=inventory(c,identity,products);slots.addAll((List<?>)one.get("slots"));members.add(identity);type=BuiltInRegistries.ENTITY_TYPE.getKey(e.getType()).toString();
        } else {
            BlockPos pos=ServerBridge.position(p);
            if(!world.hasChunkAt(pos))return json(Map.of("loaded",false,"position",xyz(pos),"source","server"));
            var state=world.getBlockState(pos);type=BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
            List<BlockPos> positions=new ArrayList<>();positions.add(pos);
            if(state.getBlock() instanceof ChestBlock && state.getValue(ChestBlock.TYPE)!=ChestType.SINGLE)positions.add(pos.relative(ChestBlock.getConnectedDirection(state)));
            if(state.getBlock() instanceof ChestBlock)openable=true;
            positions.sort(Comparator.comparing(BlockPos::toShortString));
            for(BlockPos member:positions) {
                if(!world.hasChunkAt(member))throw new IllegalStateException("Other chest half not loaded");
                if(openable!=null && ChestBlock.isChestBlockedAt(world,member))openable=false;
                if(!(world.getBlockEntity(member) instanceof Container c))throw new IllegalArgumentException("Block is not a usable container");
                String identity=key(world,member);members.add(identity);slots.addAll((List<?>)inventory(c,identity,products).get("slots"));
                locations.add(xyz(member));
            }
        }
        Map<String,Object> result=new LinkedHashMap<>();result.put("loaded",true);result.put("dimension",world.dimension().identifier().toString());result.put("container_id",String.join("|",members));result.put("members",members);result.put("locations",locations);result.put("type",type);result.put("openable",openable);result.put("slots",slots);result.put("slot_count",slots.size());result.put("inventory_accounting_version","nested-items-v1");result.put("item_components_version","item-components-v1");result.put("source","server");result.put("tick",world.getGameTime());result.put("sample_monotonic_ns",System.nanoTime());return json(result);
    }
    public static JsonElement region(ServerLevel world,JsonObject p) {
        var a=p.getAsJsonArray("min");var b=p.getAsJsonArray("max");
        BlockPos min=new BlockPos(a.get(0).getAsInt(),a.get(1).getAsInt(),a.get(2).getAsInt()),max=new BlockPos(b.get(0).getAsInt(),b.get(1).getAsInt(),b.get(2).getAsInt());
        long volume=(long)(max.getX()-min.getX()+1)*(max.getY()-min.getY()+1)*(max.getZ()-min.getZ()+1);
        if(volume<1||volume>32768||min.getX()>max.getX()||min.getY()>max.getY()||min.getZ()>max.getZ())throw new IllegalArgumentException("Invalid or excessive read region");
        Map<String,Integer> blocks=new TreeMap<>(),items=new TreeMap<>();List<Object> containers=new ArrayList<>(),furnaces=new ArrayList<>(),budding=new ArrayList<>();int unloaded=0,unticking=0;
        SurvivalEquipment equipment=new SurvivalEquipment();
        SurvivalEquipment stock=new SurvivalEquipment();
        for(BlockPos pos:BlockPos.betweenClosed(min,max)) {
            if(!world.hasChunkAt(pos)){unloaded++;continue;}
            if(!world.isPositionEntityTicking(pos))unticking++;
            String id=BuiltInRegistries.BLOCK.getKey(world.getBlockState(pos).getBlock()).toString();blocks.merge(id,1,Integer::sum);
            if(id.equals("minecraft:budding_amethyst"))budding.add(xyz(pos));
            if(world.getBlockEntity(pos) instanceof AbstractFurnaceBlockEntity furnace) {
                var data=furnace.saveWithoutMetadata(world.registryAccess());
                furnaces.add(Map.of("position",xyz(pos),"lit_time_remaining",data.getShortOr("lit_time_remaining",(short)0),"fuel_count",furnace.getItem(1).getCount()));
            }
            if(world.getBlockEntity(pos) instanceof Container c){containers.add(Map.of("position",xyz(pos),"id",key(world,pos),"type",id));int slot=0;for(ItemStack stack:c){equipment.inspect(stack,key(world,pos)+":"+slot);stock.inspect(stack,key(world,pos)+":"+slot++);if(!stack.isEmpty())items.merge(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),stack.getCount(),Integer::sum);}}
        }
        Map<String,Integer> loose=new TreeMap<>();int looseEntities=0;
        AABB area=new AABB(min.getX(),min.getY(),min.getZ(),max.getX()+1,max.getY()+1,max.getZ()+1);
        for(ItemEntity e:world.getEntitiesOfClass(ItemEntity.class,area,e->area.contains(e.position()))) {
            ItemStack stack=e.getItem();equipment.inspect(stack,"entity:"+e.getUUID());stock.inspect(stack,"entity:"+e.getUUID());loose.merge(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),stack.getCount(),Integer::sum);looseEntities++;
        }
        Map<String,Integer> entities=new TreeMap<>(),falling=new TreeMap<>();
        for(Entity e:world.getEntities((Entity)null,area,e->area.contains(e.position()))) {
            if(e instanceof FallingBlockEntity block)falling.merge(BuiltInRegistries.BLOCK.getKey(block.getBlockState().getBlock()).toString(),1,Integer::sum);
            if(e instanceof EnderMan enderman&&enderman.getCarriedBlock()!=null)falling.merge(BuiltInRegistries.BLOCK.getKey(enderman.getCarriedBlock().getBlock()).toString(),1,Integer::sum);
            if(!(e instanceof LivingEntity livingEntity)||livingEntity.isAlive())entities.merge(BuiltInRegistries.ENTITY_TYPE.getKey(e.getType()).toString(),1,Integer::sum);
            if(e instanceof Container c){int slot=0;for(ItemStack stack:c){equipment.inspect(stack,"entity:"+e.getUUID()+":"+slot);stock.inspect(stack,"entity:"+e.getUUID()+":"+slot++);if(!stack.isEmpty())items.merge(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),stack.getCount(),Integer::sum);}}
            if(e instanceof Player player){int slot=0;for(ItemStack stack:player.getInventory())equipment.inspect(stack,"player:"+e.getUUID()+":"+slot++);}
            if(e instanceof InventoryCarrier carrier && !(e instanceof Container)){int slot=0;for(ItemStack stack:carrier.getInventory()){equipment.inspect(stack,"entity:"+e.getUUID()+":inventory:"+slot);stock.inspect(stack,"entity:"+e.getUUID()+":inventory:"+slot++);if(!stack.isEmpty())items.merge(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),stack.getCount(),Integer::sum);}}
            if(e instanceof LivingEntity living)for(EquipmentSlot slot:EquipmentSlot.VALUES){var stack=living.getItemBySlot(slot);equipment.inspect(stack,"entity:"+e.getUUID()+":"+slot.getName());if(!(e instanceof Player))stock.inspect(stack,"entity:"+e.getUUID()+":"+slot.getName());if(!(e instanceof Player)&&!stack.isEmpty())items.merge(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),stack.getCount(),Integer::sum);}
        }
        Map<String,Object> result=new LinkedHashMap<>(Map.of("block_counts",blocks,"inventory_counts",items,"containers",containers,"loose_item_counts",loose,"loose_item_entities",looseEntities,"unloaded_blocks",unloaded,"blocks_scanned",volume,"source","server","tick",world.getGameTime()));
        result.put("survival_equipment",equipment.report());
        result.put("total_item_counts",stock.itemCounts());
        result.put("component_item_counts",stock.componentCounts());
        result.put("item_components_version","item-components-v1");
        result.put("cooking_ledger",CookingLedger.read(world,min,max));
        result.put("furnace_states",furnaces);
        result.put("fuel_item_ids",world.fuelValues().fuelItems().stream().map(i->BuiltInRegistries.ITEM.getKey(i).toString()).toList());
        result.put("inflight_block_counts",falling);
        result.put("unticking_blocks",unticking);
        result.put("natural_source_policy",Map.of("version","natural-sources-v1","natural_world",Boolean.getBoolean("minecraft.agent.natural_world"),"creation_disabled",Boolean.getBoolean("minecraft.agent.natural_world")));
        result.put("budding_positions",budding);
        result.put("harvest_ledger",HarvestLedger.read(world,min,max));
        result.put("wither_source_ledger",WitherSourceLedger.read(world,min,max));
        result.put("inventory_accounting_version","nested-items-v1");
        result.put("entity_counts",entities);return json(result);
    }
    public static JsonElement actors(MinecraftServer server) {
        return json(server.getPlayerList().getPlayers().stream().map(p->Map.of("name",p.getPlainTextName(),"uuid",p.getUUID().toString(),"position",List.of(p.getX(),p.getY(),p.getZ()),"dimension",p.level().dimension().identifier().toString(),"fake",p.getClass().getName().equals("carpet.patches.EntityPlayerMPFake"),"alive",p.getHealth()>0&&!p.isDeadOrDying(),"health",p.getHealth(),"spectator",p.isSpectator())).toList());
    }
}
