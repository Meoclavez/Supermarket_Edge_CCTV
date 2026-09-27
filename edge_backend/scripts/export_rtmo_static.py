#!/usr/bin/env python3
"""Build a static-shape, NMS-free RTMO-s ONNX model from the released mmpose/mmdeploy SDK zip.

Source: the OpenMMLab RTMO-s body7 640x640 ONNX SDK (end2end.onnx, opset 11, dynamic batch,
in-graph TopK + NonMaxSuppression + final TopK(50)).

Surgery (no retraining, no torch/mmpose needed):
  * input fixed to [1, 3, 640, 640] (BGR, 0-255 float, no mean/std; the graph has none);
  * the in-graph NMS block is cut out: the final-candidate index tensor ``y.7`` that fed the
    ``dets`` gather and the per-candidate keypoint (DCC/SimCC) decoder is replaced by a
    constant-k ``TopK(max_scores, k=K)`` over all 2000 anchors (strides 16 + 32 at 640);
  * the now-dead NMS subgraph is pruned and all Shape/Range/ConstantOfShape arithmetic is
    constant-folded so every tensor has a static shape;
  * outputs: ``dets`` [1, K, 5] (x1, y1, x2, y2, score; 640-input pixels, sorted by score desc)
    and ``keypoints`` [1, K, 17, 3] (x, y, visibility score; 640-input pixels).

The host must reproduce the removed post-processing (values read from the original graph):
    keep score > 0.15; class-agnostic greedy NMS, suppress IoU > 0.65; max 200 kept;
    then top 50 by score.

Usage:
    python export_rtmo_static.py [--src ZIP_PATH_OR_URL] [--out FILE] [--topk K]

Requires: onnx, onnxruntime, numpy, onnx-graphsurgeon.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import sys
import urllib.request
import zipfile

import numpy as np
import onnx
import onnx_graphsurgeon as gs
from onnx import TensorProto, helper, numpy_helper

SRC_URL = ("https://download.openmmlab.com/mmpose/v1/projects/rtmo/onnx_sdk/"
           "rtmo-s_8xb32-600e_body7-640x640-dac2bf74_20231211.zip")
ZIP_SHA256 = "3da7ad88b209f9da8be87ba5c325610639af04de3b5c8a96649b98cd9e2848a2"
ONNX_MEMBER = "end2end.onnx"
ONNX_SHA256 = "d0703d40d19f3921da51ae725402d5fdae4d2478c7442072d3101bd396f370d8"
CHECKPOINT = "rtmo-s_8xb32-600e_body7-640x640-dac2bf74_20231211.pth"

# Tensor names in the released graph (verified against ONNX_SHA256).
SCORES = "max_scores"      # [B, 2000] max class score per anchor (sigmoid), pre-NMS
FINAL_IDX = "y.7"          # [B, N] anchor indices after NMS + TopK(50); feeds dets + keypoints
NMS_SCORE_THR, NMS_IOU_THR, NMS_MAX_BOXES, KEEP_TOP_K = 0.15, 0.65, 200, 50
NUM_ANCHORS = 2000
FORBIDDEN = {"NonMaxSuppression", "NonZero", "Loop", "If", "Scan", "Range", "Shape",
             "ConstantOfShape", "Size"}


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_source(src: str) -> bytes:
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src) as r:
            data = r.read()
    else:
        with open(src, "rb") as f:
            data = f.read()
    if sha256(data) != ZIP_SHA256:
        sys.exit(f"zip sha256 mismatch: {sha256(data)}")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        onnx_bytes = z.read(ONNX_MEMBER)
    if sha256(onnx_bytes) != ONNX_SHA256:
        sys.exit(f"{ONNX_MEMBER} sha256 mismatch: {sha256(onnx_bytes)}")
    return onnx_bytes


def get_const(g: onnx.GraphProto, name: str):
    for i in g.initializer:
        if i.name == name:
            return numpy_helper.to_array(i)
    for n in g.node:
        if n.op_type == "Constant" and n.output[0] == name:
            return numpy_helper.to_array(n.attribute[0].t)
    return None


def check_nms_params(g: onnx.GraphProto) -> None:
    nms = [n for n in g.node if n.op_type == "NonMaxSuppression"]
    assert len(nms) == 1, "expected exactly one NonMaxSuppression"
    n = nms[0]
    got = (int(get_const(g, n.input[2])[0]), float(get_const(g, n.input[3])[0]),
           float(get_const(g, n.input[4])[0]))
    want = (NMS_MAX_BOXES, NMS_IOU_THR, NMS_SCORE_THR)
    assert np.allclose(got, want), f"NMS params {got} != {want}"
    assert all(a.name != "center_point_box" or a.i == 0 for a in n.attribute)


def prune(g: onnx.GraphProto) -> None:
    prod = {o: n for n in g.node for o in n.output}
    need, stack, keep = set(), [o.name for o in g.output], set()
    while stack:
        t = stack.pop()
        if t in need:
            continue
        need.add(t)
        n = prod.get(t)
        if n is not None and id(n) not in keep:
            keep.add(id(n))
            stack.extend(i for i in n.input if i)
    nodes = [n for n in g.node if id(n) in keep]
    del g.node[:]
    g.node.extend(nodes)
    inits = [i for i in g.initializer if i.name in need]
    del g.initializer[:]
    g.initializer.extend(inits)


def build(onnx_bytes: bytes, k: int) -> onnx.ModelProto:
    assert 1 <= k <= NUM_ANCHORS
    m = onnx.load_from_string(onnx_bytes)
    g = m.graph
    opset, ir = [(o.domain, o.version) for o in m.opset_import], m.ir_version
    check_nms_params(g)

    # 1. static batch
    g.input[0].type.tensor_type.shape.dim[0].Clear()
    g.input[0].type.tensor_type.shape.dim[0].dim_value = 1

    # 2. replace post-NMS indices with a constant-k TopK over all anchors
    g.initializer.append(numpy_helper.from_array(np.array([k], np.int64), "rtmo_static/topk_k"))
    topk = helper.make_node("TopK", [SCORES, "rtmo_static/topk_k"],
                            ["rtmo_static/cand_scores", "rtmo_static/cand_idx"],
                            name="rtmo_static/TopK", axis=1, largest=1, sorted=1)
    for n in g.node:
        for j, t in enumerate(n.input):
            if t == FINAL_IDX:
                n.input[j] = "rtmo_static/cand_idx"
    # insert right after the producer of max_scores to keep topological order
    pos = next(i for i, n in enumerate(g.node) if SCORES in n.output) + 1
    g.node.insert(pos, topk)

    # 3. drop the NMS subgraph and stale shape annotations
    prune(g)
    del g.value_info[:]
    for o in g.output:
        o.type.tensor_type.shape.Clear()

    # 4. fold every shape computation now that all dims are static
    for _ in range(6):
        n_before = len(m.graph.node)
        m = onnx.shape_inference.infer_shapes(m, data_prop=True)
        graph = gs.import_onnx(m)
        graph.fold_constants(fold_shapes=True, partitioning=None, error_ok=False)
        graph.cleanup(remove_unused_graph_inputs=True).toposort()
        m = gs.export_onnx(graph, do_type_check=True)
        del m.graph.value_info[:]
        if len(m.graph.node) == n_before:
            break

    # 5. restore original opset/IR, fix output shapes, re-infer
    del m.opset_import[:]
    m.opset_import.extend([helper.make_opsetid(d, v) for d, v in opset])
    m.ir_version = ir
    del m.graph.value_info[:]
    shapes = {"dets": [1, k, 5], "keypoints": [1, k, 17, 3]}
    del m.graph.output[:]
    m.graph.output.extend(helper.make_tensor_value_info(n, TensorProto.FLOAT, s)
                          for n, s in shapes.items())
    m = onnx.shape_inference.infer_shapes(m, check_type=True, strict_mode=True)

    # 6. deterministic metadata
    m.producer_name, m.producer_version = "export_rtmo_static.py", "1"
    m.doc_string = ("RTMO-s body7 640x640, static shape, NMS removed. Host post-process: "
                    f"score>{NMS_SCORE_THR}, NMS IoU>{NMS_IOU_THR} suppress, max "
                    f"{NMS_MAX_BOXES}, keep top {KEEP_TOP_K}.")
    meta = {
        "model_family": "rtmo",
        "input_format": "bgr_0_255",
        "input_layout": "NCHW",
        "imgsz": "[640, 640]",
        "letterbox": "centred, scale=min(640/w,640/h), cv2.INTER_LINEAR",
        "pad_value": "114",
        "normalization": "none (mean 0, std 1)",
        "kpt_shape": "[17, 3]",
        "names": "{0: 'person'}",
        "weights": CHECKPOINT,
        "source_url": SRC_URL,
        "source_sha256": ONNX_SHA256,
        "topk": str(k),
        "score_thr": str(NMS_SCORE_THR),
        "nms_iou": str(NMS_IOU_THR),
        "nms_max_boxes": str(NMS_MAX_BOXES),
        "max_det": str(KEEP_TOP_K),
        "outputs": "dets[1,K,5]=x1,y1,x2,y2,score; keypoints[1,K,17,3]=x,y,vis; "
                   "640-input pixels, sorted by score desc",
    }
    del m.metadata_props[:]
    for key in sorted(meta):
        m.metadata_props.add(key=key, value=meta[key])
    onnx.checker.check_model(m, full_check=True)
    verify_static(m, k)
    return m


def verify_static(m: onnx.ModelProto, k: int) -> None:
    ops = collections.Counter(n.op_type for n in m.graph.node)
    bad = FORBIDDEN & set(ops)
    assert not bad, f"dynamic-shape ops remain: {bad}"
    inits = {i.name for i in m.graph.initializer}
    for n in m.graph.node:
        if n.op_type == "TopK":
            assert n.input[1] in inits, "TopK k must be a constant initializer"
        if n.op_type in ("Reshape", "Expand", "Tile", "Slice", "Resize", "Unsqueeze"):
            for t in n.input[1:]:
                assert not t or t in inits, f"{n.op_type} {n.name} has non-constant {t}"
    for vi in list(m.graph.input) + list(m.graph.output) + list(m.graph.value_info):
        dims = vi.type.tensor_type.shape.dim
        assert all(d.HasField("dim_value") for d in dims), f"non-static shape: {vi.name}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=SRC_URL, help="SDK zip path or URL")
    ap.add_argument("--out", default="rtmo-s-body7-640x640-static.onnx")
    ap.add_argument("--topk", type=int, default=300, help="static candidate count K (<= 2000)")
    a = ap.parse_args()
    m = build(load_source(a.src), a.topk)
    data = m.SerializeToString(deterministic=True)
    with open(a.out, "wb") as f:
        f.write(data)
    ops = collections.Counter(n.op_type for n in m.graph.node)
    print(f"wrote {a.out} ({len(data)} bytes) sha256={sha256(data)}")
    print("ops:", dict(sorted(ops.items())))
    for vi in list(m.graph.input) + list(m.graph.output):
        t = vi.type.tensor_type
        print(f"  {vi.name}: {TensorProto.DataType.Name(t.elem_type)} "
              f"{[d.dim_value for d in t.shape.dim]}")


if __name__ == "__main__":
    main()
