import os
from PIL import Image

src_path = "/opt/bmas/mission-control/public/ant-head.png"
public_dir = "/opt/bmas/mission-control/public"
app_dir = "/opt/bmas/mission-control/src/app"

# Open the source image
img = Image.open(src_path)

# 1. Save favicon.ico (multi-size ICO)
img.save(
    os.path.join(public_dir, "favicon.ico"),
    format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48)],
)
img.save(
    os.path.join(app_dir, "favicon.ico"),
    format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48)],
)
print("Generated favicon.ico (multi-size)")

# 2. Resized PNGs
sizes_map = {
    os.path.join(public_dir, "favicon-16x16.png"): (16, 16),
    os.path.join(public_dir, "favicon-32x32.png"): (32, 32),
    os.path.join(public_dir, "apple-touch-icon.png"): (180, 180),
    os.path.join(public_dir, "android-chrome-192x192.png"): (192, 192),
    os.path.join(public_dir, "android-chrome-512x512.png"): (512, 512),
    os.path.join(app_dir, "icon.png"): (32, 32),
    os.path.join(app_dir, "apple-icon.png"): (180, 180),
}

for path, size in sizes_map.items():
    resized = img.resize(size, Image.Resampling.LANCZOS)
    resized.save(path, format="PNG")
    print(f"Generated: {path} ({size[0]}x{size[1]})")
