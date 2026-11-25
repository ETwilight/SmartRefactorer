package research.agent;

import java.util.*;

/**
 * Validate commands exposed by the server bridge.
 *
 * The policy tokenizes quoted and nested data as single arguments, checks the
 * command root and supported subcommands, and recursively checks execute run.
 * Natural-world validation also checks supplied block and entity data for protected
 * source blocks. The returned command has its optional leading slash removed.
 *
 * <pre>{@code
 * String command = CommandPolicy.validate("tick query", naturalWorld);
 * }</pre>
 * The server passes this validated text to its command dispatcher.
 */
public final class CommandPolicy {
    private static final Set<String> ALLOWED = Set.of("tp", "teleport", "kill", "tick", "weather", "time", "effect", "give", "setblock", "fill", "execute", "data", "forceload", "summon", "clone", "clear", "item", "gamemode", "difficulty", "gamerule", "tag", "scoreboard", "help", "locate");
    // Protected IDs cover source and containment blocks unavailable through ordinary acquisition.


    private static final Set<String> PROTECTED_NATURAL_SOURCES = Set.of(
        "minecraft:barrier", "minecraft:bedrock", "minecraft:end_portal",
        "minecraft:end_portal_frame", "minecraft:end_gateway", "minecraft:command_block",
        "minecraft:repeating_command_block", "minecraft:chain_command_block",
        "minecraft:structure_block", "minecraft:jigsaw", "minecraft:moving_piston",
        "minecraft:light", "minecraft:reinforced_deepslate", "minecraft:test_block",
        "minecraft:test_instance_block", "minecraft:budding_amethyst", "minecraft:structure_void");
    /** Normalize a block ID and check the protected-source set. */
    public static boolean isProtectedNaturalSource(String block) {
        if(block==null)return false;
        String id=block.toLowerCase(Locale.ROOT);
        if(!id.contains(":"))id="minecraft:"+id;
        return PROTECTED_NATURAL_SOURCES.contains(id);
    }
    /** Split command arguments while retaining quoted strings and nested data. */
    public static List<String> tokens(String text) {
        List<String> out = new ArrayList<>(); StringBuilder current = new StringBuilder();
        // quote is the active quote character or zero; depth counts nested data delimiters.
        char quote = 0; boolean escaped = false; int depth = 0;
        for (char c : text.toCharArray()) {
            if (c == '\n' || c == '\r' || c == '\0') throw new IllegalArgumentException("Multiple commands forbidden");
            if (escaped) { current.append(c); escaped = false; continue; }
            if (quote != 0) {
                current.append(c);
                if (c == '\\') escaped = true;
                else if (c == quote) quote = 0;
                continue;
            }
            if (c == '\'' || c == '"') { quote = c; current.append(c); continue; }
            if (c == '[' || c == '{' || c == '(') depth++;
            if (c == ']' || c == '}' || c == ')') { if (--depth < 0) throw new IllegalArgumentException("Unbalanced data"); }
            if (Character.isWhitespace(c) && depth == 0) {
                if (!current.isEmpty()) { out.add(current.toString()); current.setLength(0); }
            } else current.append(c);
        }
        if (quote != 0 || depth != 0) throw new IllegalArgumentException("Unbalanced command");
        if (!current.isEmpty()) out.add(current.toString());
        return out;
    }
    /** Validate a command using the default world policy. */
    public static String validate(String command) {
        return validate(command,false);
    }
    /** Validate syntax and command policy, then apply natural-source restrictions when requested. */
    public static String validate(String command,boolean naturalWorld) {
        String text = command.startsWith("/") ? command.substring(1) : command;
        if (text.length() > 32767) throw new IllegalArgumentException("Command too long");
        // Check executable payload markers before splitting the command into arguments.
        String normalized = text.toLowerCase(Locale.ROOT).replace("\\", "");
        if(text.contains("\\u") || text.contains("\\U")) throw new SecurityException("Unicode-escaped executable payloads require explicit decoding before validation");
        for (String term : List.of("command_block", "commandblock", "command:", "command\":", "command':", "run_command", "click_event", "clickevent", "minecraft:function")) {
            if (normalized.contains(term)) throw new SecurityException("Executable or custom-code payload forbidden: " + term);
        }
        List<String> words = tokens(text);
        validateTokens(words, 0);
        if(naturalWorld)validateNaturalSources(words);
        return text;
    }
    /** Inspect the supplied item, block, or entity payload, descending through execute run. */
    private static void validateNaturalSources(List<String> words) {
        String root=words.getFirst().replaceFirst("^minecraft:","");
        if(root.equals("execute")){validateNaturalSources(words.subList(words.indexOf("run")+1,words.size()));return;}
        // The payload argument position depends on the command root.
        int first=switch(root){case "setblock"->4;case "fill"->7;case "give"->2;case "item"->words.indexOf("with")+1;case "summon"->1;default->-1;};
        if(first<0||first>=words.size())return;
        String destination=root.equals("summon")?String.join(" ",words.subList(first,words.size())):words.get(first);
        for(String protectedId:PROTECTED_NATURAL_SOURCES) {
            String name=protectedId.substring("minecraft:".length());
            if(destination.matches("(?is).*(?<![a-z0-9_])(?:minecraft:)?"+java.util.regex.Pattern.quote(name)+"(?![a-z0-9_]).*"))
                throw new SecurityException("Protected natural source or unavailable containment blocks cannot be created or supplied: "+protectedId);
        }
    }
    /** Check one command root and recursively validate its execute run branch. */
    private static void validateTokens(List<String> words, int nesting) {
        if (words.isEmpty() || nesting > 16) throw new IllegalArgumentException("Empty or deeply nested command");
        String root = words.getFirst();
        if (root.startsWith("minecraft:")) root = root.substring(10);
        if (!ALLOWED.contains(root)) throw new SecurityException("Command not authorized: " + root);
        if(Set.of("setblock","fill","give","item","summon").contains(root)) {
            for(String word:words.subList(1,words.size()))
                if(word.matches("(?is).*(?<![a-z0-9_])(?:minecraft:)?(?:trial_)?spawner(?![a-z0-9_]).*"))
                    throw new SecurityException("Creating or supplying spawners is forbidden; naturally generated spawners may remain");
        }
        if (root.equals("locate") && (words.size() != 3 || !Set.of("biome","structure").contains(words.get(1)))) throw new SecurityException("Only locate biome or structure with one identifier or tag permitted");
        if (root.equals("data") && (words.size() < 2 || !words.get(1).equals("get"))) throw new SecurityException("Only data get permitted");
        if (root.equals("tick") && (words.size() < 2 || !Set.of("query", "freeze", "unfreeze", "step", "rate", "sprint").contains(words.get(1)))) throw new SecurityException("Unsupported tick operation");
        if (root.equals("item") && (words.size() < 2 || !words.get(1).equals("replace"))) throw new SecurityException("Only item replace permitted; custom modifiers disabled");
        if (root.equals("kill") && words.size() < 2) throw new SecurityException("kill requires explicit target");
        if (root.equals("execute")) {
            int run = words.indexOf("run");
            if (run < 0) throw new SecurityException("execute requires an explicit run branch");
            if (words.subList(0, run).contains("function")) throw new SecurityException("execute function conditions invoke datapack code");
            if (words.subList(run+1,words.size()).stream().anyMatch(w->w.equals("clone")||w.equals("minecraft:clone"))) throw new SecurityException("Use direct clone with explicit context so its source can be inspected");
            // execute store block can write executable command-block data.
            if (words.subList(0, run).contains("store") && words.subList(0, run).contains("block")) throw new SecurityException("execute store block disabled");
            validateTokens(words.subList(run + 1, words.size()), nesting + 1);
        }
    }
    /** Return the exposed command roots. */
    public static Set<String> allowed() { return ALLOWED; }
    /** Return the public subcommand and payload policy description. */
    public static Map<String,Object> description() {
        return Map.of("data",List.of("get"),"item",List.of("replace"),"locate",List.of("biome","structure"),
            "tick",List.of("query","freeze","unfreeze","step","rate","sprint"),
            "execute","Explicit run required; no function conditions, store block, or nested clone. All run branches are revalidated.",
            "payloads","Executable/custom-code payloads and Unicode-escaped commands are rejected");
    }
    /** Check whether a partial command path belongs to the exposed help tree. */
    public static boolean helpPathAllowed(List<String> path) {
        if(path.isEmpty())return true;
        String root=path.getFirst().replaceFirst("^minecraft:","");
        if(!ALLOWED.contains(root))return false;
        if(path.size()>1) {
            String sub=path.get(1);
            if(root.equals("data")&&!sub.equals("get"))return false;
            if(root.equals("item")&&!sub.equals("replace"))return false;
            if(root.equals("locate")&&!Set.of("biome","structure").contains(sub))return false;
            if(root.equals("tick")&&!Set.of("query","freeze","unfreeze","step","rate","sprint").contains(sub))return false;
        }
        if(root.equals("execute")) {
            int run=path.indexOf("run");List<String> prefix=run<0?path:path.subList(0,run);
            if(prefix.contains("function")||(prefix.contains("store")&&prefix.contains("block")))return false;
            if(run>=0&&run+1<path.size()) {
                List<String> nested=path.subList(run+1,path.size());
                if(nested.getFirst().equals("clone")||nested.getFirst().equals("minecraft:clone"))return false;
                return helpPathAllowed(nested);
            }
        }
        return true;
    }
}
