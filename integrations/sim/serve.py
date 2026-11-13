import argparse
from susanoo_sdk.jsonl import serve
from sim_agent import SimAgent

parser = argparse.ArgumentParser()
parser.add_argument("--mode", default="correct", choices=["correct", "false_completion", "wrong_amount"])
parser.add_argument("--binding-pin")
args = parser.parse_args()
serve(SimAgent(args.mode, args.binding_pin))
