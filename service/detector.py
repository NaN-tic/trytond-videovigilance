"""CPU inference for official YOLOX ONNX exports.

BGR 0..255 preprocessing, top-left letterboxing with 114 padding,
grid decoding, and NMS. Nano uses 416x416; S uses 640x640.
Reference: https://github.com/Megvii-BaseDetection/YOLOX/tree/main/demo/ONNXRuntime
"""

import time

import cv2
import numpy as np
import onnxruntime as ort


class Detector:
    def __init__(self, model, threads=2, confidence=0.35, nms=0.45):
        if threads < 1 or not 0 < confidence <= 1 or not 0 < nms <= 1:
            raise ValueError('Invalid detector thresholds or thread count')
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(str(model), options, providers=['CPUExecutionProvider'])
        input_spec = self.session.get_inputs()[0]
        shape = input_spec.shape
        if (len(shape) != 4 or shape[:2] != [1, 3]
                or not all(isinstance(n, int) and n > 0 and n % 32 == 0 for n in shape[2:])):
            raise ValueError('Expected a fixed-shape YOLOX NCHW input')
        self.height, self.width = shape[2:]
        self.input_name = input_spec.name
        self.confidence, self.nms = confidence, nms
        grids, strides = [], []
        for stride in [8, 16, 32]:
            x, y = np.meshgrid(np.arange(self.width // stride), np.arange(self.height // stride))
            grid = np.stack((x, y), axis=2).reshape(-1, 2)
            grids.append(grid)
            strides.append(np.full((len(grid), 1), stride))
        self.grid = np.concatenate(grids)
        self.strides = np.concatenate(strides)

    @property
    def input_size(self):
        return [self.width, self.height]

    def detect(self, frame):
        ratio = min(self.height / frame.shape[0], self.width / frame.shape[1])
        resized = cv2.resize(frame, (int(frame.shape[1] * ratio), int(frame.shape[0] * ratio)))
        padded = np.full((self.height, self.width, 3), 114, dtype=np.uint8)
        padded[:resized.shape[0], :resized.shape[1]] = resized
        tensor = np.ascontiguousarray(padded.transpose(2, 0, 1), dtype=np.float32)[None]
        started = time.perf_counter()
        prediction = self.session.run(None, {self.input_name: tensor})[0][0]
        model_ms = (time.perf_counter() - started) * 1000
        if prediction.ndim != 2 or prediction.shape[0] != len(self.grid) or prediction.shape[1] < 6:
            raise ValueError('Expected raw YOLOX output (anchors, 5 + classes) before grid decoding')
        prediction[:, :2] = (prediction[:, :2] + self.grid) * self.strides
        prediction[:, 2:4] = np.exp(prediction[:, 2:4]) * self.strides
        scores = prediction[:, 4] * prediction[:, 5]
        selected = (prediction[:, 5:].argmax(axis=1) == 0) & (scores >= self.confidence)
        boxes, scores = prediction[selected, :4], scores[selected]
        xywh = np.empty_like(boxes)
        xywh[:, :2] = (boxes[:, :2] - boxes[:, 2:4] / 2) / ratio
        xywh[:, 2:4] = boxes[:, 2:4] / ratio
        kept = cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), self.confidence, self.nms)
        return [
            (xywh[index], float(scores[index]))
            for index in np.asarray(kept, dtype=int).reshape(-1)
            ], model_ms
