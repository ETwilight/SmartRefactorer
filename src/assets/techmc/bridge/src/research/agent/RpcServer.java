package research.agent;

import com.google.gson.*;
import com.sun.net.httpserver.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.channels.FileChannel;
import java.nio.ByteBuffer;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;
import java.util.function.*;

/** Loopback only, authenticated, durable request IDs. Timed out requests are never blindly replayed. */
public final class RpcServer implements AutoCloseable {
    public static final Gson JSON = new GsonBuilder().serializeNulls().create();
    private final HttpServer server;
    private final ExecutorService workers = Executors.newFixedThreadPool(2);
    // Only offsets stay resident. Region/NBT responses remain in the durable journal.
    private final Map<String, Long> ledger = new HashMap<>();
    private final Path journal;
    private final byte[] token;
    private final Consumer<Runnable> queue;
    private final Function<JsonObject, JsonElement> handler;
    private final String side;

    public RpcServer(int port, String side, Consumer<Runnable> queue, Function<JsonObject, JsonElement> handler) throws Exception {
        this.side = side; this.queue = queue; this.handler = handler;
        Path root = Path.of(System.getProperty("minecraft.agent.root", ".." )).toAbsolutePath().normalize();
        Path secret = Path.of(System.getProperty("minecraft.agent.token_file", root.resolve("runtime/bridge.token").toString())).toAbsolutePath().normalize();
        Files.createDirectories(secret.getParent());
        if (!Files.exists(secret)) {
            try { Files.writeString(secret, UUID.randomUUID() + "-" + UUID.randomUUID(), StandardOpenOption.CREATE_NEW); }
            catch (FileAlreadyExistsException ignored) {}
        }
        token = ("Bearer " + Files.readString(secret).trim()).getBytes(StandardCharsets.UTF_8);
        boolean defaultPort = side.equals("server") ? port == 8766 : port == 8765;
        String instance=System.getProperty("minecraft.agent.instance","");
        if(!instance.matches("[a-zA-Z0-9_-]*"))throw new IllegalArgumentException("Invalid instance journal ID");
        journal = root.resolve("runs/bridge-" + side + (defaultPort ? "" : "-" + port) + (instance.isEmpty()?"":"-"+instance) + ".jsonl");
        Files.createDirectories(journal.getParent());
        if (Files.exists(journal)) {
            long offset=0, complete=0;
            try(var reader=Files.newBufferedReader(journal,StandardCharsets.UTF_8)) {
                String line;
                long size=Files.size(journal);
                while((line=reader.readLine())!=null) {
                    long next=offset+line.getBytes(StandardCharsets.UTF_8).length+1;
                    if(next>size)break; // Never replay a torn final record.
                    if(!line.isBlank())ledger.put(JsonParser.parseString(line).getAsJsonObject().get("key").getAsString(),offset);
                    complete=next;offset=next;
                }
            }
            if(complete<Files.size(journal)) {
                Files.copy(journal,journal.resolveSibling(journal.getFileName()+".interrupted-"+System.currentTimeMillis()));
                try(var file=FileChannel.open(journal,StandardOpenOption.WRITE)){file.truncate(complete);file.force(true);}
            }
        }
        server = HttpServer.create(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 8);
        server.createContext("/rpc", this::receive);
        server.setExecutor(workers);
        server.start();
    }
    private synchronized void persist(String key, JsonObject record) throws Exception {
        record.addProperty("key", key);
        long offset;
        try(var file=FileChannel.open(journal,StandardOpenOption.CREATE,StandardOpenOption.WRITE,StandardOpenOption.APPEND)) {
            offset=file.size();
            ByteBuffer bytes=StandardCharsets.UTF_8.encode(JSON.toJson(record)+"\n");
            while(bytes.hasRemaining())file.write(bytes);
            file.force(true);
        }
        ledger.put(key, offset);
    }
    private JsonObject previous(String key) throws Exception {
        Long offset=ledger.get(key);
        if(offset==null)return null;
        try(var file=new java.io.RandomAccessFile(journal.toFile(),"r")) {
            file.seek(offset);
            String line=file.readLine();
            return JsonParser.parseString(new String(line.getBytes(StandardCharsets.ISO_8859_1),StandardCharsets.UTF_8)).getAsJsonObject();
        }
    }
    private void receive(HttpExchange exchange) {
        JsonObject response;
        int status = 200;
        try {
            if (!exchange.getRequestMethod().equals("POST") || !exchange.getRequestURI().getPath().equals("/rpc")) throw new IllegalArgumentException("POST /rpc required");
            String supplied = exchange.getRequestHeaders().getFirst("Authorization");
            if (supplied == null || !MessageDigest.isEqual(token, supplied.getBytes(StandardCharsets.UTF_8))) {
                status = 401; throw new SecurityException("Authentication required");
            }
            byte[] body = exchange.getRequestBody().readNBytes(1024 * 1024 + 1);
            if (body.length > 1024 * 1024) throw new IllegalArgumentException("Request too large");
            JsonObject request = JsonParser.parseString(new String(body, StandardCharsets.UTF_8)).getAsJsonObject();
            for (String field : List.of("run_id", "actor_id", "request_id", "method")) {
                String value = request.get(field).getAsString();
                if (!value.matches("[a-zA-Z0-9_.:-]{1,128}")) throw new IllegalArgumentException("Invalid " + field);
            }
            String key = JSON.toJson(List.of(request.get("run_id").getAsString(),request.get("actor_id").getAsString(),request.get("request_id").getAsString()));
            String fingerprint = JSON.toJson(request);
            synchronized (this) {
                JsonObject previous = previous(key);
                if (previous != null) {
                    if (!previous.get("fingerprint").getAsString().equals(fingerprint)) throw new IllegalArgumentException("request_id reused with different payload");
                    send(exchange, 200, previous); return;
                }
                JsonObject pending = new JsonObject();
                pending.addProperty("fingerprint", fingerprint);
                pending.addProperty("status", "pending_or_unknown");
                persist(key, pending);
            }
            CompletableFuture<JsonObject> future = new CompletableFuture<>();
            queue.accept(() -> {
                JsonObject result = envelope(request);
                try {
                    result.add("result", handler.apply(request)); result.addProperty("status", "ok");
                } catch (Throwable error) {
                    result.addProperty("status", "error"); result.addProperty("error", error.toString());
                }
                result.addProperty("fingerprint", fingerprint);
                try { persist(key, result); } catch (Exception e) { result.addProperty("persistence_error", e.toString()); }
                future.complete(result);
            });
            try { response = future.get(10, TimeUnit.SECONDS); }
            catch (TimeoutException e) { response = envelope(request); response.addProperty("status", "pending_or_unknown"); }
        } catch (Throwable error) {
            response = new JsonObject(); response.addProperty("status", "error"); response.addProperty("error", error.toString());
        }
        try { send(exchange, status, response); } catch (Exception ignored) { exchange.close(); }
    }
    private JsonObject envelope(JsonObject request) {
        JsonObject result = new JsonObject();
        for (String field : List.of("run_id", "actor_id", "request_id", "method")) result.add(field, request.get(field));
        result.addProperty("source", side);
        result.addProperty("sample_wall_ms", System.currentTimeMillis());
        return result;
    }
    private static void send(HttpExchange exchange, int status, JsonObject response) throws Exception {
        byte[] bytes = JSON.toJson(response).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (var stream = exchange.getResponseBody()) { stream.write(bytes); }
    }
    public void close() { server.stop(0); workers.shutdownNow(); }
    public static JsonObject params(JsonObject request) { return request.has("params") ? request.getAsJsonObject("params") : new JsonObject(); }
    public static String str(JsonObject p, String key) { return p.get(key).getAsString(); }
    public static int integer(JsonObject p, String key) { return p.get(key).getAsInt(); }
    public static JsonElement json(Object obj) { return JSON.toJsonTree(obj); }
}
