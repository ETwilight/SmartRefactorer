"""Rebuild structural artifacts from historical evidence without contacting Minecraft."""
import json
from pathlib import Path
import sys
if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from audit.schematic import repair_run

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('run_folder');args=parser.parse_args()
    print(json.dumps(repair_run(args.run_folder),indent=2))
