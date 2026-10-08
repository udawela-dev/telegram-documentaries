# Face detection model — attribution

Our key-free local vision fallback uses **YuNet**, a compact deep face
detector from the official OpenCV zoo:

- **Model file**: `face_detection_yunet_2023mar.onnx` (232 KB)
- **Source**: OpenCV Zoo — https://github.com/opencv/opencv_zoo
  (path: `models/face_detection_yunet/face_detection_yunet_2023mar.onnx`)
- **License**: Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0)
- **Paper**: "YuNet: A Tiny Millisecond-level Face Detector" — Linzaer et al.

The model runs entirely in-process via `cv2.FaceDetectorYN`; no network
requests, no API key, no data leaves the machine.