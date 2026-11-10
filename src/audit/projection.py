"""Deterministic evidence projection. This module never invokes a model."""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import shutil
import time
import uuid
from .recorder import save_json, utc_now


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def infer_regions(calls):
    """Coordinates are evidence of a requested footprint, never proof of a build.

    Explicit submissions and absolute setblock/fill/clone destinations only.
    Relative commands and arbitrary execute chains are deliberately not guessed.
    """
    regions={};gaps=[]
    def add(dimension,low,high,source):
        if not isinstance(dimension,str) or not isinstance(low,list) or not isinstance(high,list):return
        if len(low)!=3 or len(high)!=3 or any(type(x)!=int for x in low+high):return
        if any(a>b for a,b in zip(low,high)):return
        region=regions.setdefault(dimension,{'dimension':dimension,'min':list(low),'max':list(high),'evidence_ids':[]})
        region['min']=[min(a,b) for a,b in zip(region['min'],low)]
        region['max']=[max(a,b) for a,b in zip(region['max'],high)]
        region['evidence_ids'].append(source)
    for call in calls:
        request=call['request'];params=request.get('params',{});cid=call['call_id']
        if not isinstance(params,dict):
            gaps.append({'call_id':cid,'reason':'Invalid request parameters'});continue
        submission=params.get('submission',{})
        if not isinstance(submission,dict):submission={}
        if 'bounds' in submission:
            bounds=submission['bounds']
            if isinstance(bounds,dict):add(submission.get('dimension'),bounds.get('min'),bounds.get('max'),cid)
            else:gaps.append({'call_id':cid,'reason':'Malformed submission bounds'})
        for key in ('regions','review_volumes'):
            declared=submission.get(key,[])
            if not isinstance(declared,list):
                gaps.append({'call_id':cid,'reason':'Malformed '+key});continue
            for index,part in enumerate(declared):
                if not isinstance(part,dict) or not isinstance(part.get('bounds'),dict):
                    gaps.append({'call_id':cid,'reason':'Malformed '+key+' entry'});continue
                bounds=part['bounds']
                add(part.get('dimension'),bounds.get('min'),bounds.get('max'),cid+'#'+key+'/'+str(index))
        action=params.get('action',{})
        if not isinstance(action,dict):continue
        p=action.get('params',{});method=action.get('method','')
        if not isinstance(p,dict):continue
        if method=='command.run':
            text=p.get('text');context=p.get('context',{})
            if not isinstance(text,str) or not isinstance(context,dict):
                gaps.append({'call_id':cid,'reason':'Malformed command request'});continue
            words=text.lstrip('/').split()
            dimension=context.get('dimension','minecraft:overworld')
            if context.get('executor') and 'dimension' not in context:
                gaps.append({'call_id':cid,'reason':'Executor dimension is unknown'});continue
            try:
                if words[0]=='setblock':
                    pos=list(map(int,words[1:4]));add(dimension,pos,pos,cid)
                elif words[0]=='fill':
                    a=list(map(int,words[1:4]));b=list(map(int,words[4:7]))
                    add(dimension,[min(x,y) for x,y in zip(a,b)],[max(x,y) for x,y in zip(a,b)],cid)
                elif words[0]=='clone':
                    a=list(map(int,words[1:4]));b=list(map(int,words[4:7]));target=list(map(int,words[7:10]))
                    add(dimension,target,[t+abs(x-y) for t,x,y in zip(target,a,b)],cid)
                else:gaps.append({'call_id':cid,'reason':'Command footprint is not statically resolved'})
            except (ValueError,IndexError):gaps.append({'call_id':cid,'reason':'Relative or unsupported command coordinates'})
        elif method in ('action.interact','action.break') and all(type(p.get(k))==int for k in ('x','y','z')):
            # Normal placement may affect adjacent blocks; capture a one-block margin.
            if 'dimension' in p:
                pos=[p[k] for k in ('x','y','z')];add(p['dimension'],[v-1 for v in pos],[v+1 for v in pos],cid)
            else:gaps.append({'call_id':cid,'reason':'Client interaction dimension is not recorded'})
    return list(regions.values()),gaps


def tiles(region):
    for x in range(region['min'][0],region['max'][0]+1,32):
        for y in range(region['min'][1],region['max'][1]+1,32):
            for z in range(region['min'][2],region['max'][2]+1,32):
                yield {'dimension':region['dimension'],'min':[x,y,z],
                       'max':[min(v+31,limit) for v,limit in zip((x,y,z),region['max'])]}


def activity_evidence(calls):
    """Classify explicit activity, never hidden intent or functional success."""
    groups={key:[] for key in ('task_binding','knowledge_requests','observation_requests',
                              'world_action_requests','submission_validation','evaluation_requests','other')}
    for call in calls:
        request=call['request'];op=request.get('op');params=request.get('params',{})
        action=params.get('action',{}) if isinstance(params,dict) else {}
        method=action.get('method','') if isinstance(action,dict) else ''
        if not isinstance(method,str):method=''
        if op=='task.start':key='task_binding'
        elif op=='submission.validate':key='submission_validation'
        elif op in ('evaluation.run','evaluation.get'):key='evaluation_requests'
        elif method.startswith(('wiki.','source.')):key='knowledge_requests'
        elif method.startswith(('observe.','debug.')) or method in ('clock.query','experiment.conditions'):key='observation_requests'
        elif op=='action.execute':key='world_action_requests'
        else:key='other'
        groups[key].append({'call_id':call['call_id'],'op':op,'method':method or None,
                            'feedback_status':call['feedback'].get('status')})
    return groups


def operational_evidence(calls):
    """Keep actor sites separate from machine bounds, including off-machine AFK.

    Uses declared coordinates and recorded lifecycle telemetry only. It never
    creates actors, loads chunks, or supplies advice to a workflow.
    """
    latest={};lifecycle=[]
    for call in calls:
        params=call['request'].get('params',{})
        submission=params.get('submission') if isinstance(params,dict) else None
        if isinstance(submission,dict):
            op=submission.get('operation',{});pos=op.get('afk_position') if isinstance(op,dict) else None
            dim=submission.get('dimension');iid=submission.get('task_instance_id')
            if not isinstance(iid,str):iid=None
            if isinstance(dim,str) and isinstance(pos,list) and len(pos)==3 and all(type(v) in (int,float) and math.isfinite(v) for v in pos):
                latest[iid or call['call_id']]={'task_instance_id':iid,'dimension':dim,'position':pos,'call_id':call['call_id']}
        feedback=call.get('feedback',{});result=feedback.get('result')
        metrics=result.get('metrics') if isinstance(result,dict) else None
        actor=metrics.get('evaluator_actor') if isinstance(metrics,dict) else None
        if isinstance(actor,dict) and call['request'].get('op') in ('evaluation.run','evaluation.get'):
            lifecycle.append({'call_id':call['call_id'],'evaluation_id':result.get('evaluation_id'),'actor':actor})
    return list(latest.values()),lifecycle


class ProgressAuditor:
    """Read-only side channel. No agent context, no task tokens, no world edits.

    Fixed regions supplied by a comparison harness take precedence over inference.
    A schematic is a client-cache export; authoritative block samples are server-side.
    Missing chunks/dimensions/footprints remain explicitly incomplete.
    """
    def __init__(self,folder,server=None,client=None,regions=None,max_volume=262144,schematics=True):
        self.folder=Path(folder);self.server=server;self.client=client
        self.source_folder=self.folder
        self.regions=regions;self.max_volume=max_volume;self.schematics=schematics
        if max_volume<1:raise ValueError('Audit volume must be positive')
        if regions is not None:
            if not isinstance(regions,list):raise ValueError('Audit regions must be an array')
            for region in regions:
                if (not isinstance(region,dict) or not isinstance(region.get('dimension'),str)
                    or any(not isinstance(region.get(k),list) or len(region[k])!=3 for k in ('min','max'))
                    or any(type(v)!=int for k in ('min','max') for v in region[k])
                    or any(a>b for a,b in zip(region['min'],region['max']))):
                    raise ValueError('Invalid audit region')

    @property
    def configuration(self):
        return {'algorithm':'deterministic-evidence-projection-v3',
                'algorithm_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'max_volume':self.max_volume,'schematics':self.schematics,
                'live_server':self.server is not None,'live_client':self.client is not None,
                'fixed_regions':self.regions}

    def capture(self,round_record,calls,instances):
        started=time.perf_counter();number=round_record['round']
        directory=self.folder/f'audit/round-{number:04d}';directory.mkdir(parents=True,exist_ok=True)
        inferred,gaps=infer_regions(calls)
        regions=self.regions if self.regions is not None else inferred
        sites,lifecycle=operational_evidence(calls)
        projection={'audit_version':'3.0','created_at':utc_now(),'round':number,
                    'configuration':self.configuration,
                    'algorithm':'deterministic-evidence-projection-v3','model_calls':0,'task_tokens':0,
                    'basis':'recorded requests, raw feedback, bound task snapshots, optional read-only world sampling',
                    'limits':['No access to internal model reasoning','No inferred completion percentage',
                              'Snapshots do not prove sustained production or renewal',
                              'World keeps ticking; audit is not an atomic multi-region snapshot'],
                    'coverage_gaps':gaps,'region_source':'fixed_harness' if self.regions is not None else 'observed_footprints',
                    'regions':regions,'tasks':{},'observations':[],'schematics':[],'client_schematics':[],
                    'primary_structure_source':'recorded_server_block_snapshot',
                    'operational_sites':[],'evaluator_actor_lifecycle':lifecycle,
                    'activity_evidence':activity_evidence(calls),
                    'request_counts':dict(Counter(c['request'].get('op','invalid') for c in calls)),
                    'evidence_digest':digest(calls),'round_summary':{k:v for k,v in round_record.items()
                                                                  if k not in ('steps','env_calls','model_calls')}}
        for iid,instance in instances.items():
            outcomes=[o for r in self._prior_rounds(round_record) for o in r.get('task_outcomes',[]) if o.get('task_instance_id')==iid]
            submissions=[];validations=[]
            for call in calls:
                params=call['request'].get('params',{})
                submission=params.get('submission',{}) if isinstance(params,dict) else {}
                if not isinstance(submission,dict):continue
                if submission.get('task_instance_id')!=iid:continue
                submissions.append({'call_id':call['call_id'],'submission':submission})
                if call['request'].get('op')=='submission.validate':
                    validations.append({'call_id':call['call_id'],'feedback':call['feedback']})
            projection['tasks'][iid]={'task':instance['task'],'outcomes':outcomes,
                                      'submissions':submissions,'validation_evidence':validations,
                                      'ever_passed':any(o.get('verdict')=='pass' for o in outcomes)}
        operational_regions=[]
        if len(sites)>32:projection['coverage_gaps'].append({'reason':'Only the latest 32 task AFK sites fit the operational audit cap'})
        seen=set()
        for site in sites[-32:]:
            site={**site,'observation':self._read(self.server,'observe.afk_site',{'position':site['position'],'dimension':site['dimension']})}
            projection['operational_sites'].append(site)
            key=(site['dimension'],*site['position'])
            if key in seen:continue
            seen.add(key);x,y,z=map(math.floor,site['position'])
            operational_regions.append({'dimension':site['dimension'],'min':[x-1,y-1,z-1],'max':[x+1,y+2,z+1],
                                        'purpose':'operational_afk_site','evidence_ids':[site['call_id']]})
        projection['operational_regions']=operational_regions
        consumed=0
        for region in [*regions,*operational_regions]:
            volume=1
            for a,b in zip(region['min'],region['max']):volume*=b-a+1
            if volume<1 or consumed+volume>self.max_volume:
                projection['observations'].append({'region':region,'status':'unavailable','reason':'Audit volume limit'});continue
            consumed+=volume
            for index,tile in enumerate(tiles(region)):
                label=f"region-{len(projection['observations']):04d}"
                sample=self._read(self.server,'observe.blocks',tile)
                save_json(directory/(label+'.blocks.json'),sample)
                value=sample.get('feedback',{}).get('result',{})
                blocks=value.get('blocks',[])
                projection['observations'].append({'region':tile,'file':label+'.blocks.json',
                    'purpose':region.get('purpose','machine_structure'),
                    'status':sample['status'],'tick':value.get('tick'),
                    'complete':len(blocks)==self._volume(tile) and all(b.get('loaded') for b in blocks),
                    'block_state_counts':dict(Counter(b['state'] for b in blocks if b.get('loaded')))})
                if self.schematics:
                    from .schematic import write_snapshot
                    try:structure=write_snapshot(directory/(label+'.server.litematic'),sample,tile)
                    except Exception as exc:structure={'status':'unavailable','source':'recorded_server_block_snapshot','error':str(exc)}
                    projection['schematics'].append(structure)
                    projection['client_schematics'].append(self._schematic(tile,directory,label,sample))
        if not regions:projection['coverage_gaps'].append({'reason':'No known build region; configure fixed audit regions for full coverage'})
        projection['wall_seconds']=time.perf_counter()-started
        save_json(directory/'projection.json',projection)
        return {'file':str((directory/'projection.json').relative_to(self.folder)),
                'wall_seconds':projection['wall_seconds'],'model_calls':0,'task_tokens':0}

    def _prior_rounds(self,current):
        values=[]
        for path in sorted((self.source_folder/'rounds').glob('*.json')):
            value=json.loads(path.read_text(encoding='utf-8'))
            if value['round']<current['round']:values.append(value)
        return values+[current]

    @staticmethod
    def _volume(region):
        volume=1
        for a,b in zip(region['min'],region['max']):volume*=b-a+1
        return volume

    @staticmethod
    def _read(bridge,method,params):
        started=time.perf_counter();result={'method':method,'params':params,'started_at':utc_now()}
        if bridge is None:result.update(status='unavailable',reason='Live audit bridge not configured')
        else:
            try:result.update(status='completed',feedback=bridge.call(method,params))
            except Exception as exc:result.update(status='unavailable',error={'type':type(exc).__name__,'message':str(exc)})
        result['wall_seconds']=time.perf_counter()-started
        return result

    def _schematic(self,region,directory,label,expected=None):
        if self.client is None:return {'status':'unavailable','reason':'Client audit bridge not configured'}
        player=self._read(self.client,'observe.player',{})
        if player['status']!='completed':
            return {'status':'unavailable','reason':'Client observation unavailable','player':player}
        dimension=player.get('feedback',{}).get('result',{}).get('dimension')
        if dimension!=region['dimension']:
            return {'status':'unavailable','reason':'Client dimension does not match audit region','player':player}
        filename='audit-'+str(uuid.uuid4())+'.litematic'
        sample=self._read(self.client,'schematic.save',{'file':filename,'min':region['min'],'max':region['max']})
        if sample['status']=='completed':
            source=Path(sample['feedback']['result']['file'])
            try:
                destination=directory/(label+'.litematic');shutil.copyfile(source,destination)
                sample['artifact']=destination.name
                sample['sha256']=hashlib.sha256(destination.read_bytes()).hexdigest()
                if expected is not None:
                    from .schematic import validate_snapshot
                    try:sample['server_state_validation']={'status':'matched',**validate_snapshot(destination,expected,region)}
                    except Exception as exc:sample['server_state_validation']={'status':'mismatch_or_incomplete','reason':str(exc)}
                # The bridge owns its export location; retain source rather than delete external files.
                sample['source_retained']=str(source)
            except Exception as exc:sample.update(status='unavailable',copy_error=str(exc))
        return sample


def replay(folder):
    """Rebuild JSON evidence projections offline; cannot recreate historical worlds."""
    folder=Path(folder);calls=[];instances={};auditor=ProgressAuditor(folder,schematics=False)
    results=[]
    for path in sorted((folder/'rounds').glob('*.json')):
        record=json.loads(path.read_text(encoding='utf-8'));calls.extend(record['env_calls'])
        for call in record['env_calls']:
            if call['request'].get('op')=='task.start' and call['feedback'].get('status')=='completed':
                value=call['feedback']['result'];instances[value['id']]=value
        # Replay uses another destination so it cannot overwrite live world evidence.
        auditor.folder=folder/'audit-replay'
        results.append(auditor.capture(record,calls,instances))
    return results


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('run_folder');args=parser.parse_args()
    print(json.dumps(replay(args.run_folder),indent=2))
