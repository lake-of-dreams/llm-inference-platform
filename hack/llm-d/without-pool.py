#!/usr/bin/env python3
"""Drops the InferencePool from the chart's output, read from standard input.

The chart makes a pool named after the release. The operator already makes and
owns that pool for the InferenceService (ADR-0007), and two owners of one
object would overwrite each other. The picker is told the pool's name and reads
the operator's.
"""
import re
import sys

docs = re.split(r"(?m)^---\s*$", sys.stdin.read())
kept = [d for d in docs if d.strip() and not re.search(r"(?m)^kind:\s*InferencePool\s*$", d)]
if len(kept) == len([d for d in docs if d.strip()]):
    sys.exit("expected the chart to render an InferencePool; its layout has changed")
sys.stdout.write("---\n" + "\n---\n".join(d.strip("\n") for d in kept) + "\n")
