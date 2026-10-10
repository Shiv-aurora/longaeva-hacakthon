"""Export an analyst thesis from a saved monthly run, or verify its replay."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.thesis import ThesisNotes, export_thesis, replay_thesis


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run',type=Path);parser.add_argument('--notes',type=Path);parser.add_argument('--replay',type=str)
    args=parser.parse_args()
    if args.replay:
        print(json.dumps(replay_thesis(args.replay),indent=2));return
    if args.run is None or args.notes is None:parser.error('--run and --notes are required for export')
    result=export_thesis(json.loads(args.run.read_text()),ThesisNotes(**json.loads(args.notes.read_text())))
    print(json.dumps({k:v for k,v in result.items() if k!='thesis'},indent=2))


if __name__=='__main__':main()
