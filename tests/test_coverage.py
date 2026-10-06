"""遮盖范围比较端点与纯几何的测试。"""
import io
import threading

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.coverage import (
    coverage_diff,
    merge_intervals,
    subtract_intervals,
)
from app.main import create_app


def png_bytes(size=(40, 30), color=(200, 10, 10)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def upload(client, size=(40, 30)):
    return client.post(
        "/documents", files={"file": ("img.png", png_bytes(size))}
    ).json()["doc_id"]


def put_annotations(client, doc_id, revision, rects):
    return client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )


def publish(client, doc_id, **kw):
    return client.post(f"/documents/{doc_id}/publish", json=kw or None)


def compare(client, doc_id, current_revision, baseline_version):
    return client.post(
        f"/documents/{doc_id}/coverage-comparison",
        json={
            "current_revision": current_revision,
            "baseline_version": baseline_version,
        },
    )


# ---------------- 纯几何：区间 ----------------

def test_merge_intervals_overlap_vs_touch():
    # 真正交叠 -> 合并；端点相接 -> 保持分离
    assert merge_intervals([(0, 10), (5, 15)]) == [(0, 15)]
    assert merge_intervals([(0, 10), (10, 20)]) == [(0, 10), (10, 20)]
    # 乱序输入
    assert merge_intervals([(30, 35), (0, 5), (4, 10)]) == [(0, 10), (30, 35)]


def test_subtract_intervals_touch_does_not_cover():
    # cover 右缘相接 -> base 不被减去
    assert subtract_intervals([(0, 20)], [(20, 30)]) == [(0, 20)]
    # cover 左缘相接
    assert subtract_intervals([(10, 30)], [(0, 10)]) == [(10, 30)]
    # 真正交叠切中间
    assert subtract_intervals([(0, 20)], [(5, 15)]) == [(0, 5), (15, 20)]
    # 完全覆盖
    assert subtract_intervals([(5, 6)], [(0, 20)]) == []


# ---------------- 纯几何：覆盖差 ----------------

def test_coverage_diff_union_counts_overlap_once():
    # 两个互相重叠的矩形，并集面积不是面积之和
    base = [{"x": 0, "y": 0, "w": 10, "h": 10},
            {"x": 5, "y": 5, "w": 10, "h": 10}]  # 并集 175
    r = coverage_diff(base, [])
    assert r["added_pixels"] == 175
    assert r["lost_pixels"] == 0
    assert r["segments"] == []

    r2 = coverage_diff([], base)
    assert r2["lost_pixels"] == 175
    assert r2["added_pixels"] == 0


def test_coverage_diff_edge_touching_not_overlap():
    # 左右相接：并集 = 面积之和
    assert coverage_diff(
        [{"x": 0, "y": 0, "w": 10, "h": 10},
         {"x": 10, "y": 0, "w": 10, "h": 10}], []
    )["added_pixels"] == 200
    # 当前矩形仅与基准右缘相接 -> 基准全部失去
    r = coverage_diff(
        [{"x": 10, "y": 0, "w": 5, "h": 10}],
        [{"x": 0, "y": 0, "w": 10, "h": 10}],
    )
    assert r["lost_pixels"] == 100 and r["added_pixels"] == 50


def test_coverage_diff_identical_and_empty():
    rects = [{"x": 0, "y": 0, "w": 10, "h": 10}]
    assert coverage_diff(rects, rects) == {
        "added_pixels": 0, "lost_pixels": 0, "segments": []}
    assert coverage_diff([], []) == {
        "added_pixels": 0, "lost_pixels": 0, "segments": []}


def test_coverage_diff_segments_basic():
    base = [{"x": 0, "y": 0, "w": 10, "h": 10}]
    cur = [{"x": 0, "y": 0, "w": 10, "h": 4}]   # 只遮上 4 行
    r = coverage_diff(cur, base)
    assert r["added_pixels"] == 0
    assert r["lost_pixels"] == 60
    assert r["segments"] == [
        {"y": 4, "height": 6, "intervals": [{"x": 0, "width": 10}]}
    ]


def test_coverage_diff_segments_merged_when_identical():
    # 基准整列 [0,10)x[0,30)；当前在中间 y=10..20 右移 -> 上下两块失去
    # 遮盖区间完全相同，且中间夹着的条带"失去区间为空"：
    # 不允许越过中间空条带把上下拼成一段。
    base = [{"x": 0, "y": 0, "w": 10, "h": 30}]
    cur = [{"x": 0, "y": 0, "w": 10, "h": 10},
           {"x": 5, "y": 10, "w": 5, "h": 10},
           {"x": 0, "y": 20, "w": 10, "h": 10}]
    r = coverage_diff(cur, base)
    # 上、下两条带被当前矩形完全覆盖（无失去）；中间条带失去 [0,5) 共 50px
    assert r["lost_pixels"] == 50
    # 上下两块本就无失去；输出只有中间一段
    assert r["segments"] == [
        {"y": 10, "height": 10, "intervals": [{"x": 0, "width": 5}]},
    ]


def test_coverage_diff_segments_do_not_cross_empty_gap():
    # 当前在中间保留、上下失去：上下两块失去区间相同，中间条带失去为空。
    # 空条带必须作为断点，上下两段不得越过中间拼成一段。
    base = [{"x": 0, "y": 0, "w": 10, "h": 30}]
    cur = [{"x": 0, "y": 10, "w": 10, "h": 10}]  # 只遮中间 10 行
    r = coverage_diff(cur, base)
    assert r["lost_pixels"] == 200
    assert r["segments"] == [
        {"y": 0, "height": 10, "intervals": [{"x": 0, "width": 10}]},
        {"y": 20, "height": 10, "intervals": [{"x": 0, "width": 10}]},
    ]


def test_coverage_diff_adjacent_bands_with_same_loss_merge():
    # 当前矩形在基准内部把 y 轴切成若干相邻条带；没有失去遮盖的条带为空，
    # 有失去遮盖的相邻条带区间相同 -> 合并成一段。
    base = [{"x": 0, "y": 0, "w": 20, "h": 20}]
    cur = [{"x": 10, "y": 5, "w": 10, "h": 5}]
    # 边：0,5,10,20；各条带失去：[0,20)、[0,10)、[0,20)
    # 首末两段区间相同但不相邻（中间隔着 [0,10)），保持两段不拼接。
    r = coverage_diff(cur, base)
    assert r["lost_pixels"] == 100 + 50 + 200
    assert r["segments"] == [
        {"y": 0, "height": 5, "intervals": [{"x": 0, "width": 20}]},
        {"y": 5, "height": 5, "intervals": [{"x": 0, "width": 10}]},
        {"y": 10, "height": 10, "intervals": [{"x": 0, "width": 20}]},
    ]

    # 真正相邻且相同：当前矩形只覆盖右缘相接的一列（不算重叠），
    # y=0..20 所有相邻条带失去区间同为 [0,20) -> 合并成一段。
    cur2 = [{"x": 20, "y": 0, "w": 5, "h": 20}]
    r2 = coverage_diff(cur2, base)
    assert r2["lost_pixels"] == 400
    assert r2["segments"] == [
        {"y": 0, "height": 20, "intervals": [{"x": 0, "width": 20}]}
    ]

    # 当前矩形完全落在基准内部（无新增），只在中间留下一圈失去区：
    # 上、下相邻条带区间相同 -> 合并；中间条带另算。
    cur3 = [{"x": 5, "y": 5, "w": 5, "h": 10}]
    r3 = coverage_diff(cur3, base)
    # 边：0,5,15,20；y0..5 与 y15..20 失去 [0,20)，中间失去 [0,5)+[10,20)
    assert r3["lost_pixels"] == 100 + 150 + 100
    assert r3["segments"] == [
        {"y": 0, "height": 5, "intervals": [{"x": 0, "width": 20}]},
        {"y": 5, "height": 10,
         "intervals": [{"x": 0, "width": 5}, {"x": 10, "width": 10}]},
        {"y": 15, "height": 5, "intervals": [{"x": 0, "width": 20}]},
    ]


# ---------------- 端点：成功路径 ----------------

def test_compare_identical_annotations(client):
    doc_id = upload(client)
    put_annotations(client, doc_id, 0,
                    [{"x": 0, "y": 0, "w": 10, "h": 10, "reason": "人脸"}])
    publish(client, doc_id)
    r = compare(client, doc_id, 1, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_id"] == doc_id
    assert body["image_width"] == 40 and body["image_height"] == 30
    assert body["total_pixels"] == 1200
    assert body["current_revision"] == 1
    assert body["baseline_version"] == 1
    assert body["baseline_annotation_revision"] == 1
    assert body["added_pixels"] == 0 and body["lost_pixels"] == 0
    assert body["added_ratio"] == 0 and body["lost_ratio"] == 0
    assert body["lost_segments"] == []


def test_compare_added_and_lost_counts_and_ratios(client):
    doc_id = upload(client)
    put_annotations(client, doc_id, 0,
                    [{"x": 0, "y": 0, "w": 10, "h": 10, "reason": "a"}])
    publish(client, doc_id)  # v1 冻结 rev1
    put_annotations(client, doc_id, 1,
                    [{"x": 20, "y": 20, "w": 10, "h": 10, "reason": "b"}])
    # 不发布 rev2；基准仍是 v1 冻结的 rev1
    r = compare(client, doc_id, 2, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added_pixels"] == 100 and body["lost_pixels"] == 100
    assert body["lost_segments"] == [
        {"y": 0, "height": 10, "intervals": [{"x": 0, "width": 10}]}
    ]
    assert body["added_ratio"] == pytest.approx(100 / 1200)
    assert body["lost_ratio"] == pytest.approx(100 / 1200)
    assert body["baseline_annotation_revision"] == 1
    assert body["current_revision"] == 2


def test_compare_overlapping_rects_counted_once(client):
    doc_id = upload(client)
    # 基准：两个重叠矩形，并集 175
    put_annotations(client, doc_id, 0, [
        {"x": 0, "y": 0, "w": 10, "h": 10, "reason": "a"},
        {"x": 5, "y": 5, "w": 10, "h": 10, "reason": "b"},
    ])
    publish(client, doc_id)
    put_annotations(client, doc_id, 1, [])
    r = compare(client, doc_id, 2, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lost_pixels"] == 175  # 不是 200
    assert body["added_pixels"] == 0


def test_compare_empty_annotations_both_sides(client):
    doc_id = upload(client)
    publish(client, doc_id)  # rev0 无矩形
    r = compare(client, doc_id, 0, 1)
    assert r.status_code == 200
    body = r.json()
    assert body["added_pixels"] == 0 and body["lost_pixels"] == 0
    assert body["lost_segments"] == []


def test_compare_baseline_is_frozen_snapshot_of_annotation(client):
    """基准取发布时冻结的修订：发布后再改标注，v1 基准矩形不变。"""
    doc_id = upload(client)
    put_annotations(client, doc_id, 0,
                    [{"x": 0, "y": 0, "w": 10, "h": 10, "reason": "a"}])
    publish(client, doc_id)  # v1 = rev1
    put_annotations(client, doc_id, 1,
                    [{"x": 0, "y": 0, "w": 10, "h": 10, "reason": "a"},
                     {"x": 30, "y": 0, "w": 10, "h": 10, "reason": "c"}])
    publish(client, doc_id)  # v2 = rev2
    # 当前 rev3（清空），与 v1 比较：失去 v1 的 100px（与 v2 无关）
    put_annotations(client, doc_id, 2, [])
    r1 = compare(client, doc_id, 3, 1)
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["lost_pixels"] == 100
    assert b1["baseline_annotation_revision"] == 1
    # 与 v2 比较：失去 rev2 并集 200（两块不相交）
    r2 = compare(client, doc_id, 3, 2)
    assert r2.status_code == 200
    assert r2.json()["lost_pixels"] == 200
    assert r2.json()["baseline_annotation_revision"] == 2


# ---------------- 端点：拒绝路径（不泄露基准遮盖详情） ----------------

def test_compare_unknown_document(client):
    r = compare(client, "nope", 0, 1)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"
    assert "segments" not in r.text


def test_compare_unknown_version(client):
    doc_id = upload(client)
    publish(client, doc_id)
    r = compare(client, doc_id, 0, 99)
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "unknown_version"
    assert d["version"] == 99
    # 不携带任何遮盖详情
    assert "segment" not in r.text and "pixel" not in r.text


def test_compare_stale_revision(client):
    doc_id = upload(client)
    put_annotations(client, doc_id, 0,
                    [{"x": 0, "y": 0, "w": 5, "h": 5, "reason": "a"}])
    publish(client, doc_id)  # 当前 rev1
    r = compare(client, doc_id, 0, 1)  # 提交过期的 rev0
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "stale_revision"
    assert d["current_revision"] == 1
    assert d["supplied_revision"] == 0
    assert "segment" not in r.text


def test_compare_not_yet_public_rejected(client, monkeypatch):
    doc_id = upload(client)
    put_annotations(client, doc_id, 0,
                    [{"x": 0, "y": 0, "w": 5, "h": 5, "reason": "a"}])
    publish(client, doc_id, publish_at="2026-10-20T08:00:00Z")  # v1 未到点
    # 即使修订号正确也拒绝，且不泄露遮盖详情
    r = compare(client, doc_id, 1, 1)
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "publication_not_yet_public"
    assert "segment" not in r.text and "pixel" not in r.text


def test_compare_withdrawn_rejected(client):
    doc_id = upload(client)
    put_annotations(client, doc_id, 0,
                    [{"x": 0, "y": 0, "w": 5, "h": 5, "reason": "a"}])
    publish(client, doc_id)
    cw = client.post(
        f"/documents/{doc_id}/publications/1/withdraw",
        json={"reason": "遮盖遗漏"},
    )
    assert cw.status_code == 201
    r = compare(client, doc_id, 1, 1)
    assert r.status_code == 410
    d = r.json()["detail"]
    assert d["code"] == "publication_withdrawn"
    assert d["doc_id"] == doc_id and d["version"] == 1
    assert "segment" not in r.text and "pixel" not in r.text


def test_compare_withdrawal_takes_precedence_over_stale_revision(client):
    doc_id = upload(client)
    publish(client, doc_id)
    client.post(f"/documents/{doc_id}/publications/1/withdraw",
                json={"reason": "x"})
    # 修订号也过期：撤回（410）优先，不返回 stale_revision
    put_annotations(client, doc_id, 0,
                    [{"x": 0, "y": 0, "w": 5, "h": 5, "reason": "a"}])
    r = compare(client, doc_id, 0, 1)
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "publication_withdrawn"


def test_compare_not_yet_public_takes_precedence_over_stale(client, monkeypatch):
    doc_id = upload(client)
    put_annotations(client, doc_id, 0,
                    [{"x": 0, "y": 0, "w": 5, "h": 5, "reason": "a"}])
    publish(client, doc_id, publish_at="2026-10-20T08:00:00Z")
    # 当前 rev1；提交 rev0（过期）+ 未到点基准 -> 404 不泄露
    r = compare(client, doc_id, 0, 1)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "publication_not_yet_public"


def test_compare_invalid_body_422(client):
    doc_id = upload(client)
    r = client.post(
        f"/documents/{doc_id}/coverage-comparison",
        json={"current_revision": -1, "baseline_version": 0},
    )
    assert r.status_code == 422


# ---------------- 只读性 ----------------

def test_compare_is_read_only(client):
    import sqlite3
    doc_id = upload(client)
    put_annotations(client, doc_id, 0,
                    [{"x": 0, "y": 0, "w": 5, "h": 5, "reason": "a"}])
    publish(client, doc_id)

    settings = client.app.state.settings
    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    before = {
        "rev": db.execute(
            "SELECT annotation_revision FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0],
        "pubs": db.execute(
            "SELECT COUNT(*) c FROM publications WHERE doc_id=?", (doc_id,)
        ).fetchone()["c"],
        "rect_revs": [r[0] for r in db.execute(
            "SELECT DISTINCT revision FROM annotation_rects WHERE doc_id=?"
            " ORDER BY revision", (doc_id,))],
    }
    db.close()

    assert compare(client, doc_id, 1, 1).status_code == 200
    # 再比一次、再用错误参数比一次
    assert compare(client, doc_id, 1, 1).status_code == 200
    assert compare(client, doc_id, 1, 99).status_code == 404

    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    after = {
        "rev": db.execute(
            "SELECT annotation_revision FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0],
        "pubs": db.execute(
            "SELECT COUNT(*) c FROM publications WHERE doc_id=?", (doc_id,)
        ).fetchone()["c"],
        "rect_revs": [r[0] for r in db.execute(
            "SELECT DISTINCT revision FROM annotation_rects WHERE doc_id=?"
            " ORDER BY revision", (doc_id,))],
    }
    db.close()
    assert before == after


# ---------------- 同一读取快照 ----------------

def test_compare_reads_everything_in_one_snapshot(client):
    """资格校验通过后、读取矩形前，另一连接撤回基准版本：
    本次比较仍按快照完成（200）；之后新发起的比较才被拒绝（410）。"""
    doc_id = upload(client)
    put_annotations(client, doc_id, 0,
                    [{"x": 0, "y": 0, "w": 10, "h": 10, "reason": "a"}])
    publish(client, doc_id)
    gate = threading.Event()
    proceed = threading.Event()
    outcomes: dict = {}

    def hook():
        gate.set()
        assert proceed.wait(timeout=10)

    def withdrawer():
        assert gate.wait(timeout=10)
        outcomes["withdraw"] = client.post(
            f"/documents/{doc_id}/publications/1/withdraw",
            json={"reason": "快照测试中撤回"},
        )
        proceed.set()

    main_module.COVERAGE_COMPARISON_HOOK = hook
    t = threading.Thread(target=withdrawer)
    t.start()
    try:
        r = compare(client, doc_id, 1, 1)
    finally:
        main_module.COVERAGE_COMPARISON_HOOK = None
        t.join(timeout=10)

    assert outcomes["withdraw"].status_code == 201
    # 快照内的比较照常完成
    assert r.status_code == 200, r.text
    assert r.json()["lost_pixels"] == 0
    # 事务结束后的新请求看到撤回
    assert compare(client, doc_id, 1, 1).status_code == 410
