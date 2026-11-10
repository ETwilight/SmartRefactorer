package research.agent;

import java.util.*;
import net.minecraft.core.component.DataComponents;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.item.enchantment.Enchantment;
import net.minecraft.world.item.enchantment.ItemEnchantments;

/** Task-independent equipment constraints, using the running game's enchantment definitions.
 * This checks equipment state, not historical acquisition of resources or experience.
 */
public final class SurvivalEquipment {
    private final Set<String> encounteredItems=new TreeSet<>();
    private final Map<String,Integer> itemCounts=new TreeMap<>();
    private final Map<String,Map<String,Object>> componentCounts=new TreeMap<>();
    public List<Map<String,Object>> componentCounts(){return List.copyOf(componentCounts.values());}
    private final Set<String> inspectedPaths=new HashSet<>();
    private final Set<ItemStack> inspectedStacks=Collections.newSetFromMap(new IdentityHashMap<>());
    public Map<String,Integer> itemCounts(){return Map.copyOf(itemCounts);}
    public static final String VERSION = "survival-equipment-v1";
    private static final Set<String> MUTABLE_EQUIPMENT_COMPONENTS = Set.of(
        "damage", "enchantments", "repair_cost", "custom_name", "dyed_color", "trim",
        "charged_projectiles", "lodestone_tracker", "banner_patterns");
    private final List<Object> violations = new ArrayList<>();
    private int stacks;
    private void violation(String path, ItemStack stack, String reason) {
        if(violations.size()>=128)return;
        violations.add(Map.of("location",path,"item",BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),"reason",reason));
    }
    public void inspect(ItemStack stack, String path) { inspect(stack,path,0); }
    private void inspect(ItemStack stack, String path, int depth) {
        if(stack.isEmpty())return;
        if(!inspectedPaths.add(path)||!inspectedStacks.add(stack))return;
        if(depth>16 || ++stacks>65536)throw new IllegalStateException("Equipment inspection budget exceeded");
        encounteredItems.add(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
        itemCounts.merge(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),stack.getCount(),Integer::sum);
        var components=ItemComponents.read(stack);
        if(!components.isEmpty()) {
            String item=BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            String key=item+"|"+RpcServer.json(components);
            var row=componentCounts.computeIfAbsent(key,k->new LinkedHashMap<>(Map.of("item",item,"components",components,"count",0)));
            row.put("count",((Number)row.get("count")).intValue()+stack.getCount());
        }
        if(stack.has(DataComponents.UNBREAKABLE))violation(path,stack,"Unbreakable component is not survival-obtainable");
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
    private void enchantments(ItemStack stack, ItemEnchantments values, String path, boolean stored) {
        var entries=new ArrayList<>(values.entrySet());
        for(int i=0;i<entries.size();i++) {
            var entry=entries.get(i);var enchantment=entry.getKey();int level=entry.getIntValue();
            if(level<1 || level>enchantment.value().getMaxLevel())violation(path,stack,"Enchantment level outside survival range: "+enchantment.getRegisteredName());
            if(!stored && !enchantment.value().canEnchant(stack))violation(path,stack,"Enchantment does not support this item: "+enchantment.getRegisteredName());
            // Books can carry enchantments that cannot coexist on the receiving equipment.
            if(!stored)for(int j=0;j<i;j++)if(!Enchantment.areCompatible(enchantment,entries.get(j).getKey()))
                violation(path,stack,"Incompatible equipment enchantments");
        }
    }
    public Map<String,Object> report() {
        return Map.of("version",VERSION,"checked_stacks",stacks,"passed",violations.isEmpty(),"violations",violations,"encountered_items",List.copyOf(encounteredItems));
    }
}
