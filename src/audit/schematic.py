"""Litematic structural export from recorded authoritative block snapshots.

No world interaction or model calls. Container contents, block entities and
entities are not present in observe.blocks and are explicitly not reconstructed.
Packing follows the installed Litematica LitematicaBitArray continuous bit layout.
"""
import hashlib
import json
from pathlib import Path
import re
import time
import zipfile
import nbtlib as nbt

AIR={'minecraft:air','minecraft:cave_air','minecraft:void_air'}
MASK=(1<<64)-1


def data_version():
    jar=Path(__file__).resolve().parents[1]/'assets/techmc/knowledge/minecraft/26.1/jars/client.jar'
    with zipfile.ZipFile(jar) as archive:return int(json.loads(archive.read('version.json'))['world_version'])


def parse_state(text):
    match=re.fullmatch(r'Block\{(minecraft:[a-z0-9_/]+)\}(?:\[([^\]]*)\])?',text)
    if match is None:raise ValueError('Unrecognized recorded block state: '+str(text))
    name,properties=match.groups();result={'Name':name}
    if properties:
        result['Properties']={}
        for pair in properties.split(','):
            key,value=pair.split('=',1);result['Properties'][key]=value
    return result


def block_index(position,region):
    dx,dy,dz=[b-a+1 for a,b in zip(region['min'],region['max'])]
    x,y,z=[p-a for p,a in zip(position,region['min'])]
    if not (0<=x<dx and 0<=y<dy and 0<=z<dz):raise ValueError('Block position is outside snapshot region')
    return (y*dz+z)*dx+x


def prepare_blocks(sample,region):
    sizes=[b-a+1 for a,b in zip(region['min'],region['max'])];volume=sizes[0]*sizes[1]*sizes[2]
    if volume<1 or volume>32768:raise ValueError('Structural export requires 1..32768 blocks')
    if sample.get('status')!='completed':raise ValueError('Snapshot read was not completed')
    value=sample.get('feedback',{}).get('result',{});blocks=value.get('blocks',[])
    if len(blocks)!=volume or any(not b.get('loaded') for b in blocks):raise ValueError('Incomplete or unloaded block snapshot')
    if value.get('dimension',region['dimension'])!=region['dimension']:raise ValueError('Snapshot dimension mismatch')
    states=[None]*volume
    for block in blocks:
        index=block_index(block['position'],region)
        if states[index] is not None:raise ValueError('Duplicate snapshot position')
        states[index]=parse_state(block['state'])
    if any(s is None for s in states):raise ValueError('Snapshot has missing positions')
    return states,sizes


def write_snapshot(path,sample,region,minecraft_data_version=None):
    path=Path(path);states,sizes=prepare_blocks(sample,region)
    palette=[{'Name':'minecraft:air'}];lookup={json.dumps(palette[0],sort_keys=True):0};indices=[]
    for state in states:
        key=json.dumps(state,sort_keys=True)
        if key not in lookup:lookup[key]=len(palette);palette.append(state)
        indices.append(lookup[key])
    bits=max(2,(len(palette)-1).bit_length());words=[0]*((len(indices)*bits+63)//64)
    for i,index in enumerate(indices):
        offset=i*bits;word,shift=divmod(offset,64)
        words[word]|=(index<<shift)&MASK
        if shift+bits>64:words[word+1]|=index>>(64-shift)
    signed=[w if w<(1<<63) else w-(1<<64) for w in words]
    pos=lambda values:nbt.Compound(dict(zip(('x','y','z'),map(nbt.Int,values))))
    tags=[]
    for state in palette:
        tag=nbt.Compound({'Name':nbt.String(state['Name'])})
        if 'Properties' in state:tag['Properties']=nbt.Compound({k:nbt.String(v) for k,v in state['Properties'].items()})
        tags.append(tag)
    total=sum(s['Name'] not in AIR for s in states);now=int(time.time()*1000)
    region_tag=nbt.Compound({'BlockStatePalette':nbt.List[nbt.Compound](tags),'BlockStates':nbt.LongArray(signed),
        'TileEntities':nbt.List[nbt.Compound](),'Entities':nbt.List[nbt.Compound](),
        'PendingBlockTicks':nbt.List[nbt.Compound](),'PendingFluidTicks':nbt.List[nbt.Compound](),
        'Position':pos([0,0,0]),'Size':pos(sizes)})
    root=nbt.Compound({'MinecraftDataVersion':nbt.Int(minecraft_data_version or data_version()),
        'Version':nbt.Int(7),'SubVersion':nbt.Int(1),
        'Metadata':nbt.Compound({'Name':nbt.String(path.stem),'Author':nbt.String('Independent server snapshot audit'),
            'Description':nbt.String('Recorded server block states only. No block-entity inventory or entity data.'),
            'RegionCount':nbt.Int(1),'TotalVolume':nbt.Int(len(states)),'TotalBlocks':nbt.Int(total),
            'TimeCreated':nbt.Long(now),'TimeModified':nbt.Long(now),'EnclosingSize':pos(sizes)}),
        'Regions':nbt.Compound({'main':region_tag})})
    nbt.File(root,gzipped=True).save(path)
    checked=validate_snapshot(path,sample,region)
    return {'status':'completed','artifact':path.name,'source':'recorded_server_block_snapshot',
            'origin':region['min'],'dimension':region['dimension'],'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'non_air_blocks':total,'validation':checked,
            'limitations':['No block entity NBT or inventories','No entities or scheduled ticks','Structural projection, not a complete world backup']}


def validate_snapshot(path,sample,region):
    """Decode every position from the written file and compare to original evidence."""
    expected,sizes=prepare_blocks(sample,region);root=nbt.load(path)
    tag=root['Regions']['main'];palette=[s.unpack() for s in tag['BlockStatePalette']]
    actual_sizes=[int(tag['Size'][k]) for k in ('x','y','z')]
    if actual_sizes!=sizes:raise ValueError('Exported region dimensions do not match snapshot')
    bits=max(2,(len(palette)-1).bit_length());words=[int(w)&MASK for w in tag['BlockStates']];mask=(1<<bits)-1
    non_air=0
    for i,state in enumerate(expected):
        offset=i*bits;word,shift=divmod(offset,64)
        value=words[word]>>shift
        if shift+bits>64:value|=words[word+1]<<(64-shift)
        index=value&mask
        if index>=len(palette) or palette[index]!=state:raise ValueError('Exported block state does not match evidence at index '+str(i))
        non_air+=state['Name'] not in AIR
    if int(root['Metadata']['TotalBlocks'])!=non_air:raise ValueError('Exported block count metadata mismatch')
    return {'matched_blocks':len(expected),'non_air_blocks':non_air,'properties_verified':True}


def repair_run(folder):
    """Add corrected artifacts from historical evidence, without recapturing the world."""
    from .recorder import save_json,utc_now
    folder=Path(folder);summary=[]
    for projection_path in sorted((folder/'audit').glob('round-*/projection.json')):
        projection=json.loads(projection_path.read_text(encoding='utf-8'));result=[];started=time.perf_counter()
        for sample in projection['observations']:
            evidence_path=projection_path.parent/sample['file']
            evidence=json.loads(evidence_path.read_text(encoding='utf-8'))
            output=evidence_path.with_name(evidence_path.name.replace('.blocks.json','.server.litematic'))
            result.append(write_snapshot(output,evidence,sample['region']))
        patch={'created_at':utc_now(),'source':'original recorded server snapshots; no new world reads',
               'algorithm_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'task_tokens':0,'model_calls':0,'wall_seconds':time.perf_counter()-started,
               'server_schematics':result,'original_client_exports':'retained; not authoritative'}
        save_json(projection_path.parent/'structure-repair.json',patch)
        projection['structure_repair']={'file':'structure-repair.json',**patch}
        projection['primary_structure_source']='recorded_server_block_snapshot'
        client_structures=projection.get('client_schematics')
        if client_structures is None:client_structures=[s for s in projection.get('schematics',[]) if s.get('source')!='recorded_server_block_snapshot']
        for observation,client_structure in zip(projection['observations'],client_structures):
            if client_structure.get('artifact'):
                original=projection_path.parent/client_structure['artifact']
                evidence=json.loads((projection_path.parent/observation['file']).read_text(encoding='utf-8'))
                try:validation={'status':'matched',**validate_snapshot(original,evidence,observation['region'])}
                except Exception as exc:validation={'status':'mismatch_or_incomplete','reason':str(exc)}
                client_structure['server_state_validation']=validation
        projection['client_schematics']=client_structures
        projection['schematics']=result
        save_json(projection_path,projection)
        summary.append({'round':projection['round'],'files':len(result),'non_air_blocks':sum(x['non_air_blocks'] for x in result)})
    return summary


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('run_folder');args=parser.parse_args()
    print(json.dumps(repair_run(args.run_folder),indent=2))
