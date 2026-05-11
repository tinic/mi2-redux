#!/usr/bin/env python3
"""Side-by-side comparison: PC ground truth | original 1992 Amiga | png2amiga --best.

For each candidate room, also computes PSNR / MSE against the PC ground truth.
"""
import os, subprocess, sys, math
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))



PC_DIR    = f'{REPO_ROOT}/extracted-pc-pngs/IMAGES/backgrounds'
AMIGA_DIR = f'{REPO_ROOT}/preview/amiga-rooms'
BEST_DIR  = f'{REPO_ROOT}/preview/best-from-pc'
OUT_DIR   = f'{REPO_ROOT}/preview/comparisons'
PNG2AMIGA = os.environ['PNG2AMIGA']    # set by build.sh / bootstrap.sh
os.makedirs(BEST_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)


def find_pc_png(room_name):
    """PC PNGs are named NNNN_<roomname>.png."""
    for f in sorted(os.listdir(PC_DIR)):
        if f.endswith('.png') and f.split('_', 1)[-1].rsplit('.', 1)[0] == room_name:
            return os.path.join(PC_DIR, f)
    return None


def find_amiga_png(room_name):
    for f in os.listdir(AMIGA_DIR):
        if f.endswith('.png') and '_' in f and f.split('_', 1)[1].rsplit('.', 1)[0] == room_name:
            return os.path.join(AMIGA_DIR, f)
    return None


SSIM2 = '/opt/homebrew/bin/ssimulacra2'


def s2(ref_path, dist_path):
    """SSIMULACRA2 score (-inf..100). Higher is better. 30=low, 50=med, 70=high, 90=transparent."""
    try:
        out = subprocess.check_output([SSIM2, ref_path, dist_path], stderr=subprocess.STDOUT, text=True)
        return float(out.strip().split()[0])
    except Exception as e:
        return float('nan')


def psnr(a, b):
    """PSNR of two same-size RGB images (kept as a secondary metric)."""
    if a.size != b.size:
        return float('nan')
    d = list(zip(a.tobytes(), b.tobytes()))
    mse = sum((x - y) ** 2 for x, y in d) / len(d)
    if mse == 0:
        return float('inf')
    return 10 * math.log10(255 * 255 / mse)


def run_png2amiga(pc_png, out_png):
    if os.path.exists(out_png):
        return
    # MISE Explorer transparency-sentinel variants (PC VGA → 6-bit DAC + AA edges).
    # Treated as alpha=0 so they don't bias the palette and the comparison
    # matches how the in-game injection sees the source.
    transparent_args = []
    for hexcol in ('FF00FF', 'FC00FC', 'FF57FF', 'FC54FC', 'FF55FF',
                   'A800A8', 'AC00AC'):
        transparent_args.extend(['--transparent-color', hexcol])
    subprocess.run([PNG2AMIGA, '--mode', 'lores', '--depth', '5', '--best',
                    '--dither-strength', '0.8',
                    '--no-scale', *transparent_args, pc_png, '-o', out_png],
                   check=True, capture_output=True)


def compose(pc_png, amiga_png, best_png, out_path, room_name, metrics):
    pc = Image.open(pc_png).convert('RGB')
    amiga = Image.open(amiga_png).convert('RGB')
    best = Image.open(best_png).convert('RGB')
    # All should be same size
    w, h = pc.size
    pad = 20
    label_h = 40
    total_w = w * 3 + pad * 4
    total_h = h + label_h + pad * 2
    canvas = Image.new('RGB', (total_w, total_h), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial Bold.ttf', 18)
    except Exception:
        font = ImageFont.load_default()
    titles = [
        ('PC VGA (256c) — ground truth', pc),
        (f'Amiga 1992 (32c) — S2 {metrics["amiga_s2"]:.2f}', amiga),
        (f'png2amiga --best (32c) — S2 {metrics["best_s2"]:.2f}', best),
    ]
    x = pad
    for title, img in titles:
        if img.size != (w, h):
            img = img.resize((w, h))
        canvas.paste(img, (x, pad))
        draw.text((x, h + pad + 8), title, fill=(220, 220, 220), font=font)
        x += w + pad
    # Big header at top with room name
    draw.text((pad, h + pad + 30), f'Room: {room_name}', fill=(180, 180, 180), font=font)
    canvas.save(out_path)
    return total_w, total_h


def all_room_names_from_pc():
    out = []
    for f in sorted(os.listdir(PC_DIR)):
        if f.endswith('.png') and '_' in f:
            out.append(f.split('_', 1)[1].rsplit('.', 1)[0])
    return out


def main():
    candidates = sys.argv[1:] if len(sys.argv) > 1 else all_room_names_from_pc()
    print(f"{'room':<14}{'size':<10}{'amiga S2':<11}{'best S2':<11}{'delta':<7}")
    print('-' * 56)
    rows = []
    for rn in candidates:
        pc = find_pc_png(rn)
        am = find_amiga_png(rn)
        if not pc or not am:
            continue
        pc_img = Image.open(pc).convert('RGB')
        am_img = Image.open(am).convert('RGB')
        if pc_img.size != am_img.size:
            continue
        best_path = os.path.join(BEST_DIR, f'{rn}.png')
        run_png2amiga(pc, best_path)
        best_img = Image.open(best_path).convert('RGB')
        if best_img.size != pc_img.size:
            # png2amiga writes a 2x preview render; downsample with NEAREST so
            # the canonical 32 hardware colors are preserved (bilinear would mix
            # them and give us ~10k unique RGB values, polluting metrics).
            best_img = best_img.resize(pc_img.size, Image.NEAREST)
            best_img.save(best_path)
        m = {
            'amiga_s2': s2(pc, am),
            'best_s2':  s2(pc, best_path),
        }
        delta = m['best_s2'] - m['amiga_s2']
        print(f"{rn:<14}{str(pc_img.size):<10}{m['amiga_s2']:<11.2f}{m['best_s2']:<11.2f}{delta:+.2f}")
        rows.append((rn, m))
        out = os.path.join(OUT_DIR, f'{rn}.png')
        compose(pc, am, best_path, out, rn, m)
    if rows:
        avg_a = sum(r[1]['amiga_s2'] for r in rows) / len(rows)
        avg_b = sum(r[1]['best_s2']  for r in rows) / len(rows)
        print('-' * 56)
        print(f"{'AVERAGE':<14}{'':<10}{avg_a:<11.2f}{avg_b:<11.2f}{avg_b-avg_a:+.2f}")


if __name__ == '__main__':
    main()
