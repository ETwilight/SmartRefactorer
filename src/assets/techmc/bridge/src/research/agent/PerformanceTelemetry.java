package research.agent;

import java.util.*;

/** Read-only, nonblocking observations. Wall TPS is measured, never inferred from MSPT. */
public final class PerformanceTelemetry {
    private record Point(long tick,long nanos,String mode) {}
    private final Map<String,Point> starts=new LinkedHashMap<>() {
        protected boolean removeEldestEntry(Map.Entry<String,Point> entry){return size()>256;}
    };
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
    private static Double percentile(long[] values,double fraction) {
        return values.length==0?null:values[Math.max(0,(int)Math.ceil(values.length*fraction)-1)]/1_000_000.0;
    }
    public void remove(String run){starts.remove(run);}
}
