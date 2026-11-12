# -*- coding: utf-8 -*-
"""三个姿态后端：MediaPipe / YOLO-Pose / 纯运动检测。

精度与依赖重量递减：
  mediapipe  骨骼点准、CPU 即可实时，但对 Python 版本挑剔
  yolo       精度最好、支持多人，需要 torch（体积大）
  motion     零额外依赖，只有人体框没有骨骼点，仅作兜底与自检
"""
from __future__ import annotations

import threading

import cv2
import numpy as np

from .base import Person, PoseBackend


MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
             "pose_landmarker_lite/float16/1/pose_landmarker_lite.task")


def _ensure_model(path):
    """模型文件缺失时自动下载（约 5.7MB）。"""
    from pathlib import Path
    import urllib.request

    path = Path(path)
    if path.exists() and path.stat().st_size > 1_000_000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    urllib.request.urlretrieve(MODEL_URL, tmp)
    tmp.replace(path)
    return path


class MediaPipeBackend(PoseBackend):
    """MediaPipe Tasks PoseLandmarker（33 点 BlazePose）。

    注意：mediapipe 1.x 已移除旧的 ``mp.solutions.pose`` API，这里用的是
    Tasks API，需要一个 ``.task`` 模型文件，缺失时自动下载到 models/。
    """

    name = "mediapipe"
    provides_keypoints = True

    # BlazePose 33 点 → 我们需要的命名点
    IDX = {
        "nose": 0, "l_sho": 11, "r_sho": 12, "l_hip": 23, "r_hip": 24,
        "l_knee": 25, "r_knee": 26, "l_ank": 27, "r_ank": 28,
        # 手势识别用：MediaPipe Pose 的 33 点里手腕是 15/16、耳朵是 7/8
        "l_wri": 15, "r_wri": 16, "l_ear": 7, "r_ear": 8,
    }

    def __init__(self, model_path: str | None = None, min_det: float = 0.4,
                 num_poses: int = 1, min_span: float = 0.04):
        from pathlib import Path

        self.min_span = float(min_span)

        import mediapipe as mp                       # 延迟导入
        from mediapipe.tasks import python as mpy
        from mediapipe.tasks.python import vision

        self._mp = mp
        default = Path(__file__).resolve().parent.parent.parent / "models" \
            / "pose_landmarker_lite.task"
        model = _ensure_model(model_path or default)

        self._landmarker = vision.PoseLandmarker.create_from_options(
            vision.PoseLandmarkerOptions(
                base_options=mpy.BaseOptions(model_asset_path=str(model)),
                running_mode=vision.RunningMode.VIDEO,
                num_poses=num_poses,
                min_pose_detection_confidence=min_det,
                # tracking/presence 远低于 detection：前者决定"已经看到的人
                # 要不要继续跟"，后者决定"要不要凭空认出一个人"。地上的人
                # 姿态置信度天然偏低——实测 UR Fall 真人跌倒序列，presence
                # 用 0.5 时人在落地后整段消失，判定直接漏掉整次跌倒。
                # 放低这两项只影响"继续跟一个已存在的人"，不会凭空造人。
                min_tracking_confidence=0.3,
                min_pose_presence_confidence=0.3,
                output_segmentation_masks=False,
            ))
        # detect_for_video 要求时间戳严格递增；用帧序号而非源时钟，
        # 因为视频文件与合成源的时钟可能回绕或重复。
        self._ts_ms = 0

    def infer(self, frame_bgr) -> list[Person]:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB,
                               data=np.ascontiguousarray(rgb))
        self._ts_ms += 33
        result = self._landmarker.detect_for_video(image, self._ts_ms)

        out: list[Person] = []
        for lms in (result.pose_landmarks or []):
            keypoints: dict[str, tuple[float, float, float]] = {}
            for name, idx in self.IDX.items():
                lm = lms[idx]
                keypoints[name] = (float(lm.x), float(lm.y),
                                   float(getattr(lm, "visibility", 1.0)))
            visible = [(lm.x, lm.y) for lm in lms
                       if getattr(lm, "visibility", 1.0) >= 0.3]
            if len(visible) < 4:
                continue
            xs = [p[0] for p in visible]
            ys = [p[1] for p in visible]
            bbox = (max(min(xs), 0.0), max(min(ys), 0.0),
                    min(max(xs), 1.0), min(max(ys), 1.0))
            if not self._plausible(bbox, keypoints):
                continue
            score = float(np.mean([kp[2] for kp in keypoints.values()]))
            out.append(Person(bbox=bbox, score=score, keypoints=keypoints))
        return out

    def _plausible(self, bbox, keypoints) -> bool:
        """只拦截荒谬到不可能是人的输出（默认跨度 <4% 画面）。

        这里曾经有一条 min_span=0.18 的护栏，动机是我观察到模型会返回
        score=0.98 但关键点全部塌缩成小方块的垃圾结果。后来查明：那个现象
        是测试方式造成的——我在同一个 VIDEO 模式实例上先后喂入两张毫不相干
        的图，跟踪器拿上一帧姿态当先验才产生了垃圾。真实连续视频流里不会
        发生。

        而这条护栏在真实跌倒序列上造成了实实在在的漏报：UR Fall 数据集中
        人倒地后 bbox 跨度只有 0.11~0.18（躺着 + 离镜头远 = 画面占比天然小），
        整段被丢弃，一次真跌倒被完全吃掉。

        教训：不要用"看起来小"当作"结果不可信"的代理指标。阈值降到 0.04，
        只过滤明显不可能的输出。
        """
        span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
        return span >= self.min_span

    def close(self) -> None:
        """关闭 landmarker。

        **必须在后台线程里关并设超时。** 实测 mediapipe 的
        `PoseLandmarker.close()` 会卡在 C 层的 serial_dispatcher 里永不返回
        （等一个不会完成的 executor 任务）——它不是慢，是死锁。

        后果很严重：这条路径在 Runtime.close() 里，而 Runtime.close() 在
        运行线程里。一旦卡住，运行线程永远不退出，状态停在 stopping，
        摄像头句柄也永远不释放，下次启动必然"视频源结束"。用户看到的就是
        "点停止没反应、界面卡死、再启动没画面"。

        所以交给守护线程去做，主线程最多等 2 秒。关不掉就算了——进程退出
        时操作系统会回收，比卡死整个应用强得多。
        """
        landmarker = getattr(self, "_landmarker", None)
        self._landmarker = None
        if landmarker is None:
            return

        def _close() -> None:
            try:
                landmarker.close()
            except Exception:
                pass

        t = threading.Thread(target=_close, daemon=True,
                             name="mp-landmarker-close")
        t.start()
        t.join(timeout=2.0)


class YoloPoseBackend(PoseBackend):
    name = "yolo"
    provides_keypoints = True

    # COCO-17 → 命名点
    IDX = {
        "nose": 0, "l_sho": 5, "r_sho": 6, "l_hip": 11, "r_hip": 12,
        "l_knee": 13, "r_knee": 14, "l_ank": 15, "r_ank": 16,
        # 手势识别用：COCO-17 的手腕是 9/10、耳朵是 3/4
        "l_wri": 9, "r_wri": 10, "l_ear": 3, "r_ear": 4,
    }

    def __init__(self, weights: str = "yolo11n-pose.pt", conf: float = 0.4,
                 imgsz: int = 640):
        from ultralytics import YOLO

        self._model = YOLO(weights)
        self._conf = conf
        self._imgsz = imgsz

    def infer(self, frame_bgr) -> list[Person]:
        h, w = frame_bgr.shape[:2]
        results = self._model.predict(frame_bgr, conf=self._conf, imgsz=self._imgsz,
                                      verbose=False)
        out: list[Person] = []
        for res in results:
            if res.boxes is None or res.keypoints is None:
                continue
            boxes = res.boxes.xyxy.cpu().numpy()
            scores = res.boxes.conf.cpu().numpy()
            kps = res.keypoints.data.cpu().numpy()      # (n, 17, 3)
            for box, score, kp in zip(boxes, scores, kps):
                keypoints = {}
                for name, idx in self.IDX.items():
                    x, y, c = kp[idx]
                    keypoints[name] = (float(x) / w, float(y) / h, float(c))
                bbox = (float(box[0]) / w, float(box[1]) / h,
                        float(box[2]) / w, float(box[3]) / h)
                out.append(Person(bbox=bbox, score=float(score), keypoints=keypoints))
        return out


class MotionBackend(PoseBackend):
    """背景差分 + 连通域，只给人体框。

    关键细节：预热若干帧后把学习率设为 0 冻结背景模型。否则 MOG2 会把
    "一直不动的躺倒者"学进背景，人就凭空消失——而这恰好是我们最需要
    检测的状态。冻结背景的前提是摄像头固定不动。
    """

    name = "motion"
    provides_keypoints = False

    def __init__(self, warmup_frames: int = 25, min_area_ratio: float = 0.012,
                 var_threshold: float = 36.0):
        self._sub = cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=var_threshold, detectShadows=False)
        self._warmup = warmup_frames
        self._seen = 0
        self._min_area_ratio = min_area_ratio
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    def infer(self, frame_bgr) -> list[Person]:
        h, w = frame_bgr.shape[:2]
        blur = cv2.GaussianBlur(frame_bgr, (5, 5), 0)
        self._seen += 1
        lr = -1 if self._seen <= self._warmup else 0.0
        mask = self._sub.apply(blur, learningRate=lr)
        if self._seen <= self._warmup:
            return []

        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._kernel, iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        frame_area = float(w * h)
        out: list[Person] = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area / frame_area < self._min_area_ratio:
                continue
            x, y, bw, bh = cv2.boundingRect(cnt)
            fill = area / float(max(bw * bh, 1))
            if fill < 0.25:        # 过于稀疏的连通域多为光照变化
                continue
            bbox = (x / w, y / h, (x + bw) / w, (y + bh) / h)
            score = min(0.45 + fill * 0.5, 0.95)
            out.append(Person(bbox=bbox, score=float(score)))
        out.sort(key=lambda p: p.area, reverse=True)
        return out[:3]
