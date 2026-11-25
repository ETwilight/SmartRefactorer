package research.agent;

import java.util.*;

/**
 * Sample tick timings and measure wall-clock TPS between calls for each run.
 *
 * Each run retains a tick/time anchor and the endpoint tick-mode flags. A sample
 * at least one second later reports tick delta divided by elapsed monotonic time.
 * Mode changes or a backwards counter reset the anchor. Recent completed tick
 * durations supply MSPT percentiles independently of the wall-clock measurement.
 *
 * <pre>{@code
 * PerformanceTelemetry telemetry = new PerformanceTelemetry();
 * Map<String, Object> sample = telemetry.read(run, tick, nanos, target,
 *     frozen, stepping, sprinting, averageNanos, tickNanos);
 * }</pre>
 * The first sample establishes the run anchor; later calls can report observed TPS.
 */
public final class PerformanceTelemetry {
    /** Retain a server tick, monotonic timestamp, and endpoint mode signature. */
    private record Point(long tick,long nanos,String mode) {}
    private final Map<String,Point> starts=new LinkedHashMap<>() {
        /** Bound retained run anchors to the most recently inserted 256 entries. */
        protected boolean removeEldestEntry(Map.Entry<String,Point> entry){return size()>256;}
    };
    /** Read tick-mode state, recent MSPT samples, and the run's wall-clock TPS window. */
    public Map<String,Object> read(String run,long tick,long nanos,float target,boolean frozen,
                                 boolean stepping,boolean sprinting,double averageNanos,long[] tickNanos) {
        var result=new LinkedHashMap<String,Object>();
        result.put("source","server");result.put("server_tick",tick);result.put("target_tps",target);
        result.put("frozen",frozen);result.put("stepping",stepping);result.put("sprinting",sprinting);
        var samples=Arrays.stream(tickNanos).filter(n->n>0).sorted().toArray();
        var mspt=new LinkedHashMap<String,Object>();
        mspt.put("mean",samples.length==0?null:averageNanos/1_000_000.0);
        mspt.put("p50",percentile(samples,.50));mspt.put("p95",percentile(samples,.95));
        mspt.put("p99",percentile(samples,.99));mspt.put("samples",samples.length);
        result.put("mspt",mspt);
        String mode=target+":"+frozen+":"+stepping+":"+sprinting;
        Point previous=starts.get(run);
        // observed stays null until a same-mode interval spans at least one second.
        Double observed=null;double seconds=previous==null?0:(nanos-previous.nanos)/1_000_000_000.0;
        String status="warming_up";
        if(previous==null||!previous.mode.equals(mode)||tick<previous.tick||seconds<0) {
            if(previous!=null)status="mode_changed";
            starts.put(run,new Point(tick,nanos,mode));seconds=0;
        } else if(seconds<1)status="insufficient_interval";
        else {
            observed=(tick-previous.tick)/seconds;status="measured";
            starts.put(run,new Point(tick,nanos,mode));
        }
        result.put("observed_tps",observed);result.put("tps_status",status);
        result.put("window_seconds",seconds);
        result.put("sampling","Wall TPS between calls at least 1s apart; mode flags are endpoint observations. MSPT uses recent completed ticks, including paused periods' preceding samples.");
        return result;
    }
    /** Select a nearest-rank duration from sorted nanoseconds and convert to milliseconds. */
    private static Double percentile(long[] values,double fraction) {
        return values.length==0?null:values[Math.max(0,(int)Math.ceil(values.length*fraction)-1)]/1_000_000.0;
    }
    /** Release the run's retained measurement anchor. */
    public void remove(String run){starts.remove(run);}
}
