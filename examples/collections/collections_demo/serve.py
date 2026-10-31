"""Serve the demo agent over the JSONL bridge: ``python -m collections_demo.serve --mode correct``."""

import argparse

from susanoo_sdk import serve

from .agent import MODES, CollectionsAgent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=MODES, default="correct")
    parser.add_argument("--hang-seconds", type=float, default=3600)
    args = parser.parse_args()
    serve(CollectionsAgent(args.mode, hang_seconds=args.hang_seconds))


if __name__ == "__main__":
    main()
