import json
import subprocess

video = "input.mp4"

#ffmpeg -i input.mp4 -ss 00:00:00 -t 00:20:00 output.mp4

with open("highlights_La_French_Tacos_passe_ENF.json") as f:
    data = json.load(f)

for i, m in enumerate(data["moments"], start=1):
    start = m["start"]
    duration = m["duration"]
    title = m["title"].replace(" ", "_")

    output = f"{title}.mp4"

    cmd = [
        "ffmpeg",
        "-i", video,
        "-ss", str(start),
        "-t", str(duration),
        "-c", "copy",
        output
    ]

    subprocess.run(cmd)