package research.agent;

import java.util.*;
import net.minecraft.core.component.DataComponents;
import net.minecraft.world.item.ItemStack;

/**
 * Read component identities used by component-sensitive item accounting.
 *
 * The returned map includes registered instruments, potion identity and custom-effect
 * count, ominous-bottle amplifier, and applied or stored enchantment levels when
 * present. Keys use registry IDs and values retain their component-specific shape.
 *
 * <pre>{@code
 * Map<String, Object> components = ItemComponents.read(stack);
 * }</pre>
 * The evaluator attaches this map to slot and aggregate item observations.
 */
public final class ItemComponents {
    /** Read supported component identities and levels from the supplied stack. */
    public static Map<String,Object> read(ItemStack stack) {
        Map<String,Object> value=new TreeMap<>();
        var instrument=stack.get(DataComponents.INSTRUMENT);
        if(stack.has(DataComponents.OMINOUS_BOTTLE_AMPLIFIER))value.put("minecraft:ominous_bottle_amplifier",stack.get(DataComponents.OMINOUS_BOTTLE_AMPLIFIER).value());
        if(instrument!=null)value.put("minecraft:instrument",instrument.instrument().unwrapKey().map(k->k.identifier().toString()).orElse("unregistered"));
        var potion=stack.get(DataComponents.POTION_CONTENTS);
        if(potion!=null) {
            Map<String,Object> contents=new TreeMap<>();
            potion.potion().ifPresent(p->contents.put("potion",p.unwrapKey().map(k->k.identifier().toString()).orElse("unregistered")));
            contents.put("custom_effect_count",potion.customEffects().size());
            value.put("minecraft:potion_contents",contents);
        }
        // Applied and stored enchantments have separate component keys but share the level-map shape.
        for(boolean stored:List.of(false,true)) {
            var ench=stored?stack.get(DataComponents.STORED_ENCHANTMENTS):stack.get(DataComponents.ENCHANTMENTS);
            if(ench!=null&&!ench.isEmpty()) {
                Map<String,Integer> levels=new TreeMap<>();
                for(var entry:ench.entrySet())levels.put(entry.getKey().unwrapKey().map(k->k.identifier().toString()).orElse("unregistered"),entry.getIntValue());
                value.put(stored?"minecraft:stored_enchantments":"minecraft:enchantments",levels);
            }
        }
        return value;
    }
}
