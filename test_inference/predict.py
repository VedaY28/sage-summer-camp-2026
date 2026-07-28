#!/usr/bin/env python3
"""
SageAir inference script — predict air quality from a single image + meteo data.

Usage:
  python3 predict.py --image <path_to_jpg> --temp 25.3 --humidity 60.2 --pressure 1013.5

All three meteo values are required (temperature, humidity, pressure).
"""
import argparse
import pickle
import sys
from pathlib import Path

import torch
import numpy as np
from PIL import Image
from torchvision import transforms, models
import torch.nn as nn

# ── Paths ──────────────────────────────────────────────────────────────
MODEL_PATH = Path("/home/veday28/SageAir/models/best_model.pt")
SCALER_PATH = Path("/home/veday28/SageAir/models/meteo_scaler.pkl")

# ── Model definition (must match training) ─────────────────────────────
class AirQualityModel(nn.Module):
    def __init__(self, meteo_dim=3, dropout=0.3):
        super().__init__()
        resnet = models.resnet50(weights=None)
        self.image_encoder = nn.Sequential(*list(resnet.children())[:-1])
        self.img_proj = nn.Linear(2048, 512)
        self.meteo_encoder = nn.Sequential(
            nn.Linear(meteo_dim, 16),
            nn.ReLU(),
            nn.Dropout(dropout),
        )
        self.head = nn.Sequential(
            nn.Linear(512 + 16, 256),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(256, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, 2),
        )

    def forward(self, img, met):
        feats = self.image_encoder(img)
        feats = feats.flatten(1)
        feats = self.img_proj(feats)
        met_feats = self.meteo_encoder(met)
        combined = torch.cat([feats, met_feats], dim=1)
        return self.head(combined)

# ── Image transform (eval mode — no augmentation) ──────────────────────
transform = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

def main():
    parser = argparse.ArgumentParser(description="Predict air quality from image + meteorology")
    parser.add_argument("--image", required=True, type=str, help="Path to JPEG image")
    parser.add_argument("--temp", required=True, type=float, help="Temperature")
    parser.add_argument("--humidity", required=True, type=float, help="Humidity")
    parser.add_argument("--pressure", required=True, type=float, help="Pressure")
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Load model
    checkpoint = torch.load(MODEL_PATH, weights_only=True, map_location=device)
    model = AirQualityModel(meteo_dim=3, dropout=0.3).to(device)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()
    print(f"Loaded model from epoch {checkpoint['epoch']} "
          f"(val F1={checkpoint['val_f1']:.4f}, val acc={checkpoint['val_acc']:.4f})")

    # Load scaler
    with open(SCALER_PATH, "rb") as f:
        scaler = pickle.load(f)

    # Load and transform image
    img_path = Path(args.image)
    if not img_path.exists():
        print(f"ERROR: Image not found: {img_path}")
        sys.exit(1)
    img = Image.open(img_path).convert("RGB")
    img_tensor = transform(img).unsqueeze(0).to(device)

    # Normalize meteo
    meteo_raw = np.array([[args.temp, args.humidity, args.pressure]])
    meteo_scaled = scaler.transform(meteo_raw)
    met_tensor = torch.tensor(meteo_scaled, dtype=torch.float32).to(device)

    # Predict
    with torch.no_grad():
        outputs = model(img_tensor, met_tensor)
        probs = torch.softmax(outputs, dim=1)
        pred = probs.argmax(dim=1).item()
        prob_good = probs[0][0].item()
        prob_bad = probs[0][1].item()

    # Output
    print(f"\n{'='*50}")
    print(f"  Input image:  {img_path.name}")
    print(f"  Temperature:  {args.temp}")
    print(f"  Humidity:     {args.humidity}")
    print(f"  Pressure:     {args.pressure}")
    print(f"{'='*50}")
    print(f"  Prediction:   {'GOOD air' if pred == 0 else 'BAD air'}")
    print(f"  P(good):      {prob_good:.4f} ({prob_good*100:.1f}%)")
    print(f"  P(bad):       {prob_bad:.4f} ({prob_bad*100:.1f}%)")
    print(f"  Confidence:  {max(prob_good, prob_bad)*100:.1f}%")
    print(f"{'='*50}")
    print(f"\n  Threshold: raw PM2.5 > 35 = bad (EPA 24-hr)")

if __name__ == "__main__":
    main()
