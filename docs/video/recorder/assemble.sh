#!/usr/bin/env bash
# Turns recorded frame folders into segment clips, joins them and lays the narration under them.
# Usage: assemble.sh <recDir> <audio.mp3> <out.mp4>
set -euo pipefail
REC=$1; AUDIO=$2; OUT=$3
: > "$REC/segments.txt"
for s in s1 s2 s3 s4 s5 s6 s7 s8; do
  python3 - "$REC/$s" <<'PY'
import json, sys, os
d = sys.argv[1]; j = json.load(open(os.path.join(d, "frames.json")))
fr = [f for f in j["frames"] if f["t"] < j["dur"]]
with open(os.path.join(d, "list.txt"), "w") as o:
    o.write("ffconcat version 1.0\n")
    for i, f in enumerate(fr):
        end = fr[i + 1]["t"] if i + 1 < len(fr) else j["dur"]
        o.write(f"file '{f['file']}'\nduration {max(end - f['t'], 0.001):.4f}\n")
    o.write(f"file '{fr[-1]['file']}'\n")
PY
  dur=$(python3 -c "import json;print(json.load(open('$REC/$s/frames.json'))['dur'])")
  ffmpeg -hide_banner -loglevel error -y -reinit_filter 0 -f concat -safe 0 -i "$REC/$s/list.txt" \
    -vf "scale=1920:1080:flags=lanczos,fps=30,tpad=stop_mode=clone:stop=-1,format=yuv420p" -t "$dur" \
    -c:v libx264 -preset slow -crf 16 -r 30 "$REC/$s.mp4"
  echo "file '$s.mp4'" >> "$REC/segments.txt"
done
ffmpeg -hide_banner -loglevel error -y -f concat -safe 0 -i "$REC/segments.txt" -i "$AUDIO" \
  -map 0:v -map 1:a -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p -r 30 -profile:v high -movflags +faststart \
  -c:a aac -b:a 192k -ar 48000 -shortest "$OUT"
ffprobe -hide_banner -v error -show_entries format=duration:stream=codec_name,width,height -of compact "$OUT"
