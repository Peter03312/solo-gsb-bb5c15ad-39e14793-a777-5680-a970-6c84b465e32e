"""遮盖范围比较的纯几何计算（整数像素坐标）。

输入两组轴对齐整数矩形（当前标注 / 基准发布时冻结的标注），输出：
- ``added`` ：仅被当前标注覆盖（基准未覆盖）的像素数；
- ``lost``  ：曾被基准覆盖、当前不再覆盖的像素数；
- ``segments``：按水平边界段（band）列出的"失去遮盖" x 区间。

约定：
- 矩形/区间一律采用**半开**整数坐标 ``[x, x+w)``、``[y, y+h)``，
  与渲染涂实（``draw.rectangle([x, y, x+w-1, y+h-1])``）的像素集合一致；
  因此边界相接（``a_right == b_left``）的两个矩形**区间不相交**，
  重叠像素只计一次；
- x 区间先排序、再合并真正交叠（``start < end`` 才算交叠）的相邻项，
  相接（``start == end``）的区间保持分离；
- 水平段以两组矩形 y 边的并集切分：同一段内"生效矩形集合"不变，
  故各段的 x 区间差集为常数；相邻段的失去遮盖区间**完全相同**时合并
  为一段输出。无失去遮盖的条带不输出并作为断点：上下两块失去遮盖区域
  即使区间相同，也不会越过中间的无失去条带被拼成一段。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

# 归一化后的矩形：(left, top, right, bottom)，半开整数坐标
_NormRect = tuple[int, int, int, int]
# 半开整数区间 [start, end)
_Interval = tuple[int, int]


def _norm(rect: Mapping[str, Any]) -> _NormRect:
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    return (x, y, x + w, y + h)


def merge_intervals(intervals: Iterable[_Interval]) -> list[_Interval]:
    """把半开区间的可迭代对象合并为有序、互不交叠的并集区间。

    仅在严格交叠（``start < end``）时合并；端点相接（``start == end``）
    不合并——边界相接不算重叠，像素不重复也不被吞掉。
    """
    ordered = sorted((iv for iv in intervals if iv[0] < iv[1]))
    merged: list[_Interval] = []
    for start, end in ordered:
        if not merged or start >= merged[-1][1]:
            # 与上一段分离或仅端点相接（start == prev_end）：不合并
            merged.append((start, end))
        else:  # start < prev_end：严格交叠
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
    return merged


def subtract_intervals(
    base: Sequence[_Interval], cover: Sequence[_Interval]
) -> list[_Interval]:
    """有序半开区间差集 ``base - cover``（两者均已合并、有序）。

    端点相接时不产生截断（``cut == lo``/``cut == hi`` 时区间长度为 0），
    天然满足"边界相接不算重叠"。
    """
    out: list[_Interval] = []
    j = 0
    for lo, hi in base:
        # 跳过完全位于本区间左侧的 cover
        while j < len(cover) and cover[j][1] <= lo:
            j += 1
        k = j
        cur = lo
        while k < len(cover) and cover[k][0] < hi:
            cs, ce = cover[k]
            if cs > cur:
                out.append((cur, min(cs, hi)))
            if ce > cur:
                cur = ce
            k += 1
        if cur < hi:
            out.append((cur, hi))
    return [(s, e) for s, e in out if s < e]


def _active_intervals(
    rects: Sequence[_NormRect], top: int, bottom: int
) -> list[_Interval]:
    """在水平半开条带 [top, bottom) 内覆盖该条带的矩形 x 投影并集。

    判定为严格交叠：``top < rb and bottom > rt``。条带来自全部矩形的
    y 边并集，因此条带内不会发生矩形进出。
    """
    return merge_intervals(
        (rl, rr)
        for rl, rt, rr, rb in rects
        if top < rb and bottom > rt
    )


def coverage_diff(
    current_rects: Iterable[Mapping[str, Any]],
    baseline_rects: Iterable[Mapping[str, Any]],
) -> dict:
    """比较两组标注矩形的像素遮盖范围。

    返回 ``{"added_pixels", "lost_pixels", "segments"}``，其中 segments
    仅包含失去遮盖像素的水平段（自上而下列出）：
    ``{"y", "height", "intervals": [{"x", "width"}, ...]}``。
    """
    cur = [_norm(r) for r in current_rects]
    base = [_norm(r) for r in baseline_rects]

    edges = sorted(
        {edge for rl, rt, rr, rb in (*cur, *base) for edge in (rt, rb)}
    )
    added = 0
    lost = 0
    raw_bands: list[tuple[int, int, list[_Interval]]] = []
    for top, bottom in zip(edges, edges[1:]):
        band_h = bottom - top
        cur_iv = _active_intervals(cur, top, bottom)
        base_iv = _active_intervals(base, top, bottom)
        gained_iv = subtract_intervals(cur_iv, base_iv)
        lost_iv = subtract_intervals(base_iv, cur_iv)
        added += band_h * sum(e - s for s, e in gained_iv)
        lost += band_h * sum(e - s for s, e in lost_iv)
        raw_bands.append((top, band_h, lost_iv))

    # 合并相邻且失去遮盖区间完全相同的条带；无失去遮盖的条带（空区间）
    # 不输出，并作为"断点"：上下两块失去遮盖区域即使区间相同，也不会越过
    # 中间的无失去条带被拼成一段。
    segments: list[dict] = []

    def _emit(top: int, height: int, iv: list[_Interval]) -> None:
        # 参数显式传入：不按引用闭包读取循环变量
        if iv:
            segments.append(
                {
                    "y": top,
                    "height": height,
                    "intervals": [{"x": s, "width": e - s} for s, e in iv],
                }
            )

    run_top = 0
    run_h = 0
    run_iv: list[_Interval] = []
    for top, band_h, lost_iv in raw_bands:
        if not lost_iv:
            # 无失去条带：结束当前 run 并作为断点（不并入任何段）
            _emit(run_top, run_h, run_iv)
            run_top, run_h, run_iv = top, 0, []
        elif run_iv and lost_iv == run_iv:
            run_h += band_h
        else:
            _emit(run_top, run_h, run_iv)
            run_top, run_h, run_iv = top, band_h, lost_iv
    _emit(run_top, run_h, run_iv)
    return {"added_pixels": added, "lost_pixels": lost, "segments": segments}
