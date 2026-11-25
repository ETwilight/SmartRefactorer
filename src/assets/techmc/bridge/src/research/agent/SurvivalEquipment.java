package research.agent;

import java.util.*;
import net.minecraft.core.component.DataComponents;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.item.enchantment.Enchantment;
import net.minecraft.world.item.enchantment.ItemEnchantments;

/**
 * Inspect item state against ordinary survival equipment constraints.
 *
 * Each instance accumulates violations, item totals, and component-specific totals
 * across inspected stacks. Path and object-identity sets prevent repeated counting.
 * Inspection follows container, bundle, and charged-projectile contents with bounded
 * depth and stack count. Enchantment limits and compatibility come from the running
 * game definitions. The report describes current item state and encountered items.
 *
 * <pre>{@code
 * SurvivalEquipment equipment = new SurvivalEquipment();
 * equipment.inspect(stack, "mainhand");
 * Map<String, Object> report = equipment.report();
 * }</pre>
 * The report collects the supplied stack and its nested contents for acceptance readback.
 */
public final class SurvivalEquipment {
    private final Set<String> encounteredItems=new TreeSet<>();
    private final Map<String,Integer> itemCounts=new TreeMap<>();
    private final Map<String,Map<String,Object>> componentCounts=new TreeMap<>();
    /** Return the accumulated item/component count rows. */
    public List<Map<String,Object>> componentCounts(){return List.copyOf(componentCounts.values());}
    private final Set<String> inspectedPaths=new HashSet<>();
    // Identity comparison distinguishes separate equal stacks while deduplicating shared stack objects.
    private final Set<ItemStack> inspectedStacks=Collections.newSetFromMap(new IdentityHashMap<>());
    /** Return an independent map of accumulated item quantities. */
    public Map<String,Integer> itemCounts(){return Map.copyOf(itemCounts);}
    public static final String VERSION = "survival-equipment-v1";
    private static final Set<String> MUTABLE_EQUIPMENT_COMPONENTS = Set.of(
        "damage", "enchantments", "repair_cost", "custom_name", "dyed_color", "trim",
        "charged_projectiles", "lodestone_tracker", "banner_patterns");
    private final List<Object> violations = new ArrayList<>();
    private int stacks;
    /** Append a bounded violation with its source path and item ID. */
    private void violation(String path, ItemStack stack, String reason) {
        if(violations.size()>=128)return;
        violations.add(Map.of("location",path,"item",BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),"reason",reason));
    }
    /** Inspect a root stack and its nested contents. */
    public void inspect(ItemStack stack, String path) { inspect(stack,path,0); }
    /** Deduplicate a stack, count its contents, and check equipment components and enchantments. */
    private void inspect(ItemStack stack, String path, int depth) {
        if(stack.isEmpty())return;
        if(!inspectedPaths.add(path)||!inspectedStacks.add(stack))return;
        if(depth>16 || ++stacks>65536)throw new IllegalStateException("Equipment inspection budget exceeded");
        encounteredItems.add(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
        itemCounts.merge(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),stack.getCount(),Integer::sum);
        var components=ItemComponents.read(stack);
        if(!components.isEmpty()) {
            String item=BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            // Group quantities by item ID and the supported component snapshot.
            String key=item+"|"+RpcServer.json(components);
            var row=componentCounts.computeIfAbsent(key,k->new LinkedHashMap<>(Map.of("item",item,"components",components,"count",0)));
            row.put("count",((Number)row.get("count")).intValue()+stack.getCount());
        }
        if(stack.has(DataComponents.UNBREAKABLE))violation(path,stack,"Unbreakable component is not survival-obtainable");
        // A fresh item stack supplies the default component values used for equipment comparison.
        ItemStack base=new ItemStack(stack.getItem());
        boolean equipment=base.has(DataComponents.MAX_DAMAGE)||base.has(DataComponents.TOOL);
        if(equipment) {
            if(base.has(DataComponents.DAMAGE)&&!stack.has(DataComponents.DAMAGE))violation(path,stack,"Equipment damage tracking was removed");
            if(stack.getCount()>base.getMaxStackSize())violation(path,stack,"Equipment stack exceeds survival limit");
            var types=new HashSet<>(stack.getComponents().keySet());types.addAll(base.getComponents().keySet());
            for(var type:types) {
                String name=BuiltInRegistries.DATA_COMPONENT_TYPE.getKey(type).getPath();
                if(!MUTABLE_EQUIPMENT_COMPONENTS.contains(name) && !Objects.equals(stack.get(type),base.get(type)))
                    violation(path,stack,"Modified equipment component: "+name);
            }
            int damage=stack.getOrDefault(DataComponents.DAMAGE,0);
            if(damage<0 || damage>=base.getMaxDamage())violation(path,stack,"Equipment damage outside usable survival range");
        }
        enchantments(stack,stack.getEnchantments(),path,false);
        var stored=stack.get(DataComponents.STORED_ENCHANTMENTS);
        if(stored!=null&&!stored.isEmpty()) {
            if(!stack.is(Items.ENCHANTED_BOOK))violation(path,stack,"Stored enchantments require an enchanted book");
            enchantments(stack,stored,path,true);
        }
        var container=stack.get(DataComponents.CONTAINER);
        if(container!=null) {
            int slot=0;
            for(var child:container.nonEmptyItems())inspect(child.create(),path+"/container/"+slot++,depth+1);
        }
        var bundle=stack.get(DataComponents.BUNDLE_CONTENTS);
        if(bundle!=null) {
            int slot=0;
            for(var child:bundle.items())inspect(child.create(),path+"/bundle/"+slot++,depth+1);
        }
        var charged=stack.get(DataComponents.CHARGED_PROJECTILES);
        if(charged!=null) {
            int slot=0;
            for(var child:charged.items())inspect(child.create(),path+"/projectile/"+slot++,depth+1);
        }
    }
    /** Check levels, item support, and applicable pairwise enchantment compatibility. */
    private void enchantments(ItemStack stack, ItemEnchantments values, String path, boolean stored) {
        var entries=new ArrayList<>(values.entrySet());
        for(int i=0;i<entries.size();i++) {
            var entry=entries.get(i);var enchantment=entry.getKey();int level=entry.getIntValue();
            if(level<1 || level>enchantment.value().getMaxLevel())violation(path,stack,"Enchantment level outside survival range: "+enchantment.getRegisteredName());
            if(!stored && !enchantment.value().canEnchant(stack))violation(path,stack,"Enchantment does not support this item: "+enchantment.getRegisteredName());
            // Stored book enchantments are checked for levels; pairwise compatibility applies to equipped items.
            if(!stored)for(int j=0;j<i;j++)if(!Enchantment.areCompatible(enchantment,entries.get(j).getKey()))
                violation(path,stack,"Incompatible equipment enchantments");
        }
    }
    /** Summarize checked stacks, encountered item IDs, and equipment violations. */
    public Map<String,Object> report() {
        return Map.of("version",VERSION,"checked_stacks",stacks,"passed",violations.isEmpty(),"violations",violations,"encountered_items",List.copyOf(encounteredItems));
    }
}
