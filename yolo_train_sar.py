#!/usr/bin/env python3
"""
yolo_train_sar.py — NOCTURNAL Phase 3: fine-tune YOLOv8 on SAR ship data.

The approach:
  1. Start from a COCO-pretrained YOLOv8n checkpoint. Most of COCO's
     semantics (cars, people) will not transfer, but the early-layer
     edge/blob filters in the backbone are generic image features and
     DO transfer — so pretrained weights still beat random init, even
     on single-channel SAR.
  2. Duplicate the 1-channel σ⁰_dB tile into 3 channels at load time
     (Ultralytics expects 3-channel input). Optional variant: stack
     [VV_dB, VH_dB, VV_dB - VH_dB] as a crude colour composite, which
     often improves tiny-object detection.
  3. Freeze the backbone for an initial warm-up phase (head learns the
     new domain first), then unfreeze and train end-to-end.

Dataset layout (YOLO standard):

    root/
      images/train/*.png
      images/val/*.png
      labels/train/*.txt      # one line per box: class cx cy w h
      labels/val/*.txt
      data.yaml               # auto-written by `write_data_yaml()`

Install:
    pip install ultralytics torch pyyaml
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from pathlib import Path
from typing import List, Optional

try:
    import yaml  # type: ignore
except ImportError as e:
    raise SystemExit("yolo_train_sar.py needs: pip install pyyaml") from e


def pick_device() -> str:
    """Return the best training device on this machine."""
    try:
        import torch  # type: ignore
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "0"      # first CUDA GPU (Ultralytics takes '0' / '0,1' / 'cpu' / 'mps')
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def write_data_yaml(root: Path, class_names: List[str],
                    out_name: str = "data.yaml") -> Path:
    """
    Produce a YOLO-compatible dataset YAML at root/out_name.

    class_names is a list indexed by class id; for the LS-SSDD fine-tune
    this is just ["ship"].
    """
    root = Path(root).resolve()
    cfg = {
        "path":  str(root),
        "train": "images/train",
        "val":   "images/val",
        "names": {i: n for i, n in enumerate(class_names)},
    }
    path = root / out_name
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


def train(data_yaml: Path,
          model: str = "yolov8n.pt",
          epochs_warmup: int = 10,
          epochs_full:   int = 40,
          imgsz: int = 800,
          batch: int = 8,
          device: Optional[str] = None,
          project: str = "runs/nocturnal",
          name: str = "yolov8n-sar") -> Path:
    """
    Two-stage fine-tune. Returns path to the best.pt weights file.
    """
    try:
        from ultralytics import YOLO  # type: ignore
    except ImportError as e:
        raise SystemExit(
            "yolo_train_sar.py needs: pip install ultralytics torch\n"
            f"Missing: {getattr(e, 'name', e)}") from e

    device = device or pick_device()
    print(f"[train] device={device}  platform={platform.system()}")
    print(f"[train] data={data_yaml}  model={model}")

    # ── Stage 1: freeze backbone, warm up the head ────────────────
    y = YOLO(model)
    y.train(data=str(data_yaml),
            epochs=epochs_warmup,
            imgsz=imgsz,
            batch=batch,
            device=device,
            project=project,
            name=f"{name}-stage1-warmup",
            freeze=10,          # freeze first 10 layers (backbone)
            lr0=0.005,
            patience=20,
            plots=True)
    stage1_best = Path(project) / f"{name}-stage1-warmup" / "weights" / "best.pt"

    # ── Stage 2: unfreeze, fine-tune end-to-end ───────────────────
    y = YOLO(str(stage1_best))
    y.train(data=str(data_yaml),
            epochs=epochs_full,
            imgsz=imgsz,
            batch=batch,
            device=device,
            project=project,
            name=f"{name}-stage2-finetune",
            lr0=0.001,
            patience=30,
            plots=True)
    stage2_best = Path(project) / f"{name}-stage2-finetune" / "weights" / "best.pt"

    print(f"\n[train] DONE.  best weights: {stage2_best}")
    return stage2_best


# ────────────────────────────── CLI ───────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    y = sub.add_parser("yaml",
        help="write a data.yaml for a YOLO-formatted dataset")
    y.add_argument("root", type=Path,
                   help="dataset root containing images/train|val and labels/train|val")
    y.add_argument("--names", nargs="+", default=["ship"],
                   help="class names in id order. Default: ship")

    t = sub.add_parser("train",
        help="run the two-stage YOLOv8 fine-tune")
    t.add_argument("data", type=Path, help="path to data.yaml")
    t.add_argument("--model", default="yolov8n.pt",
                   help="starting weights. yolov8n.pt (fastest) ... yolov8x.pt")
    t.add_argument("--imgsz", type=int, default=800)
    t.add_argument("--batch", type=int, default=8)
    t.add_argument("--epochs-warmup", type=int, default=10)
    t.add_argument("--epochs-full",   type=int, default=40)
    t.add_argument("--device", default=None,
                   help="cpu | mps | 0 | 0,1 (default: auto)")
    t.add_argument("--project", default="runs/nocturnal")
    t.add_argument("--name",    default="yolov8n-sar")

    args = ap.parse_args(argv)

    if args.cmd == "yaml":
        path = write_data_yaml(args.root, args.names)
        print(f"[ok] wrote {path}")
    elif args.cmd == "train":
        best = train(
            args.data, model=args.model, imgsz=args.imgsz, batch=args.batch,
            epochs_warmup=args.epochs_warmup, epochs_full=args.epochs_full,
            device=args.device, project=args.project, name=args.name)
        print(f"[ok] best weights: {best}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
