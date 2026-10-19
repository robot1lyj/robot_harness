"""Run with the separately installed, trusted NVIDIA FFS environment.

No upstream code/weights are vendored. Contract: original-resolution disparity in pixels.
Public API reference: NVlabs/Fast-FoundationStereo/scripts/run_demo.py.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--valid-iters", type=int, default=8)
    parser.add_argument("--max-disp", type=int, default=192)
    parser.add_argument("--scale", type=float, default=1)
    args = parser.parse_args()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            sys.path.insert(0, args.repository)
            import numpy as np
            import torch
            from core.utils.utils import InputPadder
            from PIL import Image

            if not torch.cuda.is_available():
                raise RuntimeError("CUDA unavailable in the configured FFS environment")
            # Official serialized checkpoints contain Python model objects. Host-selected trusted files only.
            model = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
            model.args.valid_iters = args.valid_iters
            model.args.max_disp = args.max_disp
            model.cuda().eval()
            sha = hashlib.sha256()
            with Path(args.checkpoint).open("rb") as f:
                while block := f.read(1024 * 1024):
                    sha.update(block)
            revision = subprocess.run(
                ["git", "-C", args.repository, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            identity = {
                "checkpoint_sha256": sha.hexdigest(),
                "source_revision": revision.stdout.strip() or "unknown",
                "torch": torch.__version__,
                "device": torch.cuda.get_device_name(),
            }
        print(json.dumps({"ready": True, "model": identity}), flush=True)
    except Exception as exc:
        print(json.dumps({"ready": False, "error": f"{type(exc).__name__}: {exc}"}), flush=True)
        return
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr), torch.inference_mode():
                images = [Image.open(request[key]).convert("RGB") for key in ("left", "right")]
                width, height = images[0].size
                if images[1].size != (width, height):
                    raise ValueError("Stereo dimensions differ")
                sw, sh = max(32, round(width * args.scale)), max(32, round(height * args.scale))
                tensors = [
                    torch.from_numpy(np.asarray(image.resize((sw, sh))).copy())
                    .cuda()
                    .float()
                    .permute(2, 0, 1)[None]
                    for image in images
                ]
                padder = InputPadder(tensors[0].shape, divis_by=32, force_square=False)
                left, right = padder.pad(*tensors)
                torch.cuda.synchronize()
                start = time.monotonic()
                with torch.amp.autocast("cuda", dtype=torch.float16):
                    disparity = model.forward(
                        left,
                        right,
                        iters=args.valid_iters,
                        test_mode=True,
                        optimize_build_volume="pytorch1",
                    )
                disparity = padder.unpad(disparity.float()).reshape(1, 1, sh, sw)
                # Resizing a disparity field must also change its horizontal pixel units.
                disparity = torch.nn.functional.interpolate(
                    disparity, size=(height, width), mode="bilinear", align_corners=False
                ) / (sw / width)
                array = disparity[0, 0].cpu().numpy()
                torch.cuda.synchronize()
                elapsed = time.monotonic() - start
                np.save(request["output"], array, allow_pickle=False)
            print(json.dumps({"ok": True, "inference_s": elapsed}), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), flush=True)


if __name__ == "__main__":
    main()
