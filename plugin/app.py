#!/usr/bin/env python3
"""
SageAir air quality classifier plugin for Sage edge nodes.

Reads a camera image + temperature/humidity/pressure from the node's sensors,
runs the trained multimodal model, and publishes a binary air quality prediction
(good/bad) via pywaggle.

The model was trained on Sage node data (5 Chicago WSN nodes, 2 weeks).
PM2.5 was used only as a training label — at inference the model uses
image + meteorology only.

Usage (inside Sage container):
  python3 app.py
  python3 app.py --camera "file:///images/test.jpg"  # test with local image
"""
import argparse
import os
import sys
import time
import pickle
import hashlib
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torchvision import transforms, models
from PIL import Image

# pywaggle imports
from waggle.plugin import Plugin
from waggle.data.vision import Camera


# ── Model definition (must match training) ──────────────────────────────
class AirQualityModel(nn.Module):
    def __init__(self, meteo_dim=3, dropout=0.3):
        super().__init__()
        # CLIP ViT-B/32 visual encoder
        import open_clip
        clip_model, _, _ = open_clip.create_model_and_transforms(
            'ViT-B-32', pretrained='openai'
        )
        self.clip_visual = clip_model.visual
        clip_dim = 512

        self.meteo_encoder = nn.Sequential(
            nn.Linear(meteo_dim, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
        )
        self.head = nn.Sequential(
            nn.Linear(clip_dim + 32, 256),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(256, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, 2),
        )

    def forward(self, img, met):
        feats = self.clip_visual(img)
        feats = feats.float()
        met_feats = self.meteo_encoder(met)
        combined = torch.cat([feats, met_feats], dim=1)
        return self.head(combined)


# ── Image transform (eval mode) ─────────────────────────────────────────
IMG_SIZE = 224
IMG_MEAN = [0.48145466, 0.4578275, 0.40821073]
IMG_STD = [0.26862954, 0.26130258, 0.27577711]
PM25_THRESHOLD = 151.0  # PurpleAir PM2.5 threshold used for v5 training labels

eval_transform = transforms.Compose([
    transforms.Resize((IMG_SIZE, IMG_SIZE)),
    transforms.ToTensor(),
    transforms.Normalize(mean=IMG_MEAN, std=IMG_STD),
])


def load_model(model_path, device):
    """Load the trained model from a checkpoint."""
    checkpoint = torch.load(model_path, weights_only=True, map_location=device)
    model = AirQualityModel(meteo_dim=3, dropout=0.3)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()
    model.to(device)
    return model


def load_scaler(scaler_path):
    """Load the StandardScaler used during training."""
    with open(scaler_path, "rb") as f:
        return pickle.load(f)


def predict(model, image, meteo_values, scaler, device):
    """Run inference on a single image + meteo vector.
    
    Args:
        model: loaded AirQualityModel
        image: PIL Image (RGB)
        meteo_values: [temperature, humidity, pressure] raw values
        scaler: fitted StandardScaler for meteo normalization
        device: torch device
    
    Returns:
        dict with prediction, prob_good, prob_bad, confidence
    """
    # Transform image
    img_tensor = eval_transform(image).unsqueeze(0).to(device)

    # Normalize meteo
    meteo_raw = np.array([meteo_values])
    meteo_scaled = scaler.transform(meteo_raw)
    met_tensor = torch.tensor(meteo_scaled, dtype=torch.float32).to(device)

    # Predict
    with torch.no_grad():
        outputs = model(img_tensor, met_tensor)
        probs = torch.softmax(outputs, dim=1)
        pred = probs.argmax(dim=1).item()
        prob_good = probs[0][0].item()
        prob_bad = probs[0][1].item()

    return {
        "prediction": "good" if pred == 0 else "bad",
        "prediction_code": pred,
        "prob_good": prob_good,
        "prob_bad": prob_bad,
        "confidence": max(prob_good, prob_bad),
    }


def capture_image(camera_url):
    """Capture an image from camera URL or file.
    
    Args:
        camera_url: either a file:// URL or a pywaggle camera device string
    
    Returns:
        PIL Image (RGB)
    """
    if camera_url.startswith("file://"):
        path = camera_url[7:]
        return Image.open(path).convert("RGB")
    else:
        # Use pywaggle Camera to grab a snapshot
        cam = Camera(camera_url)
        sample = cam.snapshot()
        # pywaggle Camera.snapshot returns a dict with 'data' key containing JPG bytes
        if isinstance(sample, dict) and "data" in sample:
            import io
            return Image.open(io.BytesIO(sample["data"])).convert("RGB")
        elif hasattr(sample, "data"):
            import io
            return Image.open(io.BytesIO(sample.data)).convert("RGB")
        elif isinstance(sample, bytes):
            import io
            return Image.open(io.BytesIO(sample)).convert("RGB")
        else:
            raise ValueError(f"Unexpected camera snapshot type: {type(sample)}")


def get_sensor_values(plugin, timeout=30):
    """Get temperature, humidity, pressure from the node's sensors.
    
    Subscribes to the standard Sage environmental sensor topics and
    waits for the latest readings.
    """
    # Sage sensor topics for WSN nodes
    sensor_topics = {
        "temperature": "env.temperature",
        "humidity": "env.humidity",
        "pressure": "env.pressure",
    }

    for topic in sensor_topics.values():
        plugin.subscribe(topic)

    # Wait for messages
    values = {}
    deadline = time.time() + timeout
    while time.time() < deadline and len(values) < 3:
        msg = plugin.get(timeout=min(5, deadline - time.time()))
        if msg is None:
            continue
        for name, topic in sensor_topics.items():
            if msg.name == topic and name not in values:
                values[name] = float(msg.value)
                print(f"  {name}: {msg.value}")

    # Fill missing with defaults
    defaults = {"temperature": 20.0, "humidity": 50.0, "pressure": 1013.0}
    for name, default in defaults.items():
        if name not in values:
            print(f"WARNING: no {name} reading received, using default {default}")
            values[name] = default

    return values["temperature"], values["humidity"], values["pressure"]


def main():
    parser = argparse.ArgumentParser(
        description="SageAir air quality classifier plugin"
    )
    parser.add_argument(
        "--camera", type=str, default="camera.top",
        help="Camera device name (e.g. camera.top, camera.bottom) or file://path"
    )
    parser.add_argument(
        "--model", type=str,
        default="/app/models/best_model.pt",
        help="Path to model checkpoint"
    )
    parser.add_argument(
        "--scaler", type=str,
        default="/app/models/meteo_scaler.pkl",
        help="Path to fitted StandardScaler"
    )
    parser.add_argument(
        "--continuous", type=str, default="Y",
        help="Y for continuous mode (loop), N for single capture"
    )
    parser.add_argument(
        "--interval", type=int, default=600,
        help="Seconds between predictions in continuous mode (default 600 = 10 min)"
    )
    parser.add_argument(
        "--timeout", type=int, default=30,
        help="Seconds to wait for sensor readings"
    )
    # Test overrides (skip sensor subscription, use fixed values)
    parser.add_argument(
        "--temp", type=float, default=None,
        help="Override temperature (testing, skips sensor subscription)"
    )
    parser.add_argument(
        "--humidity", type=float, default=None,
        help="Override humidity (testing, skips sensor subscription)"
    )
    parser.add_argument(
        "--pressure", type=float, default=None,
        help="Override pressure (testing, skips sensor subscription)"
    )
    args = parser.parse_args()

    # Device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    # Load model + scaler
    model = load_model(args.model, device)
    scaler = load_scaler(args.scaler)
    print("Model and scaler loaded.")
    print(f"PM2.5 threshold for 'bad': {PM25_THRESHOLD} µg/m³ (EPA 24-hr)")

    # Initialize pywaggle plugin
    with Plugin() as plugin:
        print(f"Plugin initialized. Camera: {args.camera}")

        while True:
            # 1. Capture image
            try:
                image = capture_image(args.camera)
                print(f"Image captured: {image.size}")
            except Exception as e:
                print(f"ERROR capturing image: {e}")
                if args.continuous != "Y":
                    sys.exit(1)
                time.sleep(args.interval)
                continue

            # 2. Get sensor readings (or use overrides)
            if args.temp is not None and args.humidity is not None and args.pressure is not None:
                temp, humidity, pressure = args.temp, args.humidity, args.pressure
                print(f"Sensors (override): temp={temp:.1f}, humidity={humidity:.1f}, pressure={pressure:.1f}")
            else:
                try:
                    temp, humidity, pressure = get_sensor_values(plugin, args.timeout)
                    print(f"Sensors: temp={temp:.1f}, humidity={humidity:.1f}, pressure={pressure:.1f}")
                except Exception as e:
                    print(f"ERROR reading sensors: {e}")
                    temp, humidity, pressure = 20.0, 50.0, 1013.0

            # 3. Run inference
            try:
                result = predict(model, image, [temp, humidity, pressure], scaler, device)
                print(f"Prediction: {result['prediction'].upper()} "
                      f"(good={result['prob_good']:.3f}, bad={result['prob_bad']:.3f}, "
                      f"confidence={result['confidence']*100:.1f}%)")
            except Exception as e:
                print(f"ERROR during inference: {e}")
                if args.continuous != "Y":
                    sys.exit(1)
                time.sleep(args.interval)
                continue

            # 4. Publish results via pywaggle
            # Publish binary prediction (0=good, 1=bad)
            plugin.publish(
                "airquality.prediction",
                result["prediction_code"],
                meta={
                    "label": result["prediction"],
                    "confidence": str(result["confidence"]),
                    "prob_good": str(result["prob_good"]),
                    "prob_bad": str(result["prob_bad"]),
                    "temperature": str(temp),
                    "humidity": str(humidity),
                    "pressure": str(pressure),
                    "pm25_threshold": str(PM25_THRESHOLD),
                },
            )

            # Also publish probabilities as separate topics
            plugin.publish("airquality.prob_good", result["prob_good"])
            plugin.publish("airquality.prob_bad", result["prob_bad"])

            print("Published airquality.prediction")

            # 5. Exit or continue
            if args.continuous != "Y":
                break

            print(f"Sleeping {args.interval}s...")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
