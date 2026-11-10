package research.agent;

import java.util.*;
import net.minecraft.core.component.DataComponents;
import net.minecraft.world.item.ItemStack;

/** Registry identities needed by component-sensitive product acceptance. */
public final class ItemComponents {
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
