#!/usr/bin/env python3
"""TikTok雑学動画を無料ツールだけで作る（クラウドの定期実行で使う）。

使い方: python3 make_video.py script.json out.mp4
script.json:
  {"seed": 123, "lines": [{"text": "セリフ", "prompts": ["画像の指示(英語)", ...]}, ...]}

- 音声: edge-tts（Microsoftの無料音声、ja-JP-KeitaNeural）
- 画像: Pollinations（無料の画像生成、登録不要）。右下の透かしは下端を切って消す
- 組み立て: ffmpeg。各セリフの音声の長さに合わせて画像をゆっくりパンし、字幕を焼き込む
出力は 1080x1920 / 30fps の mp4 1本。
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import edge_tts

W, H, FPS = 1080, 1920, 30
GAP = 0.25          # セリフ間の無音
WRAP = 13           # 字幕1行あたりの最大文字数
FONT = "IPAGothic"
VOICE = "ja-JP-KeitaNeural"
STYLE = ("cinematic hand-painted 2D animation, storybook fairytale illustration, "
         "soft painterly rendering, warm volumetric light, deep atmospheric shadows, "
         "muted jewel-tone palette of deep burgundy, forest green, dusk blue and candle gold, "
         "delicate linework, dramatic chiaroscuro, dreamy enchanted mood, non-photorealistic, "
         "no text, no letters, no words, no numbers")
WATERMARK_CROP = 0.07   # 画像の下端7%を切り捨てる（透かし対策）
IMAGE_INTERVAL = 47     # Pollinationsの無料枠は「同じIPから45秒に1枚」まで
_last_image_at = 0.0


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", path])
    return float(out)


def tts(text, path):
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        asyncio.run(edge_tts.Communicate(text, VOICE).save(path))
    return path


def image(prompt, seed, path, tries=5):
    global _last_image_at
    if os.path.exists(path) and os.path.getsize(path) > 10_000:
        return path
    q = urllib.parse.quote(f"{prompt}, {STYLE}")
    url = (f"https://image.pollinations.ai/prompt/{q}"
           f"?width=1080&height=1920&model=flux&nologo=true&seed={seed}")
    for attempt in range(tries):
        wait = IMAGE_INTERVAL - (time.time() - _last_image_at)
        if wait > 0:
            time.sleep(wait)
        _last_image_at = time.time()
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                data = r.read()
            if len(data) > 10_000:
                with open(path, "wb") as f:
                    f.write(data)
                return path
        except Exception as e:  # 無料枠の制限(402)や混雑(503)は、間隔をあけて再試行
            print(f"image retry {attempt + 1}: {e}", file=sys.stderr)
    raise RuntimeError(f"画像を取得できませんでした: {prompt[:40]}")


def wrap(text):
    # 句読点の直後で優先的に改行し、それでも長ければ WRAP 文字で折る
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= WRAP or (ch in "、。！？" and len(cur) >= WRAP * 0.6):
            lines.append(cur)
            cur = ""
    if cur:
        if lines and len(cur) <= 2:
            lines[-1] += cur
        else:
            lines.append(cur)
    return r"\N".join(lines)


def ass_time(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def main(script_path, out_path):
    spec = json.load(open(script_path, encoding="utf-8"))
    seed = int(spec.get("seed", 1))
    lines = spec["lines"]
    os.makedirs("work", exist_ok=True)
    clips, audios, subs = [], [], []
    t = 0.0
    for i, line in enumerate(lines, 1):
        a = tts(line["text"], f"work/a{i:02d}.mp3")
        seg = duration(a) + GAP
        padded = f"work/a{i:02d}.wav"
        run(["ffmpeg", "-y", "-i", a, "-af", f"apad=whole_dur={seg}",
             "-ar", "44100", "-ac", "2", padded])
        audios.append(padded)
        subs.append((t, t + seg, line["text"]))
        prompts = line["prompts"]
        per = seg / len(prompts)
        for j, prompt in enumerate(prompts, 1):
            src = image(prompt, seed + i * 10 + j, f"work/i{i:02d}_{j}.jpg")
            clip = f"work/c{i:02d}_{j}.mp4"
            # 透かしを切り、少し拡大してから左右交互にゆっくりパン（静止画に動きを出す）
            sw, sh = int(W * 1.12) // 2 * 2, int(H * 1.12) // 2 * 2
            direction = "(iw-ow)*t/{d}" if (i + j) % 2 else "(iw-ow)*(1-t/{d})"
            x = direction.format(d=per)
            vf = (f"crop=iw:ih*{1 - WATERMARK_CROP}:0:0,"
                  f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
                  f"crop={sw}:{sh},crop={W}:{H}:x='{x}':y='(ih-oh)/2',"
                  f"fps={FPS},format=yuv420p")
            run(["ffmpeg", "-y", "-loop", "1", "-t", f"{per:.3f}", "-i", src,
                 "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", clip])
            clips.append(clip)
        t += seg

    with open("work/v.txt", "w") as f:
        f.writelines(f"file '{os.path.basename(c)}'\n" for c in clips)
    with open("work/a.txt", "w") as f:
        f.writelines(f"file '{os.path.basename(a)}'\n" for a in audios)
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "work/v.txt", "-c", "copy", "work/video.mp4"])
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "work/a.txt", "work/audio.wav"])

    with open("work/subs.ass", "w", encoding="utf-8") as f:
        f.write(f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT},78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,2,2,60,60,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""")
        for s, e, text in subs:
            f.write(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Default,,0,0,0,,{wrap(text)}\n")

    run(["ffmpeg", "-y", "-i", "work/video.mp4", "-i", "work/audio.wav",
         "-vf", "subtitles=work/subs.ass", "-c:v", "libx264", "-preset", "medium",
         "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-shortest",
         "-movflags", "+faststart", out_path])
    print(json.dumps({"out": out_path, "seconds": round(duration(out_path), 2),
                      "frames": len(clips), "lines": len(lines)}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
