#!/usr/bin/env bash
# 遮盖范围比较端到端示例（重发影像前，只读）：
#   上传原图 -> 标注 v1 -> 发布（冻结基准）-> 修改标注到 rev2
#   -> 比较"当前标注 rev2"与"已发布基准 v1"：
#      新增/失去遮盖像素数、占旋正后图像比例、按水平边界段的失去遮盖区间
#      （重叠矩形只计一次、边界相接不算重叠；相邻段区间完全相同已合并）
#   -> 演示拒绝路径：修订号过期 409、未知版本 404、未到公开时间 404、
#      已撤回 410（均不泄露基准遮盖详情）
# 比较为只读：不发布新版本、不改任何记录。
# 依赖：curl、python3（仅用于解析 JSON、生成示例图）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (60, 40), (40, 80, 140)).save("/tmp/cov.png")
EOF

DOC=$(curl -sS -F "file=@/tmp/cov.png" "$BASE/documents" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])")
echo "DOC=$DOC（旋正后 60x40，总像素 2400）"

echo
echo "== 1. 标注 rev1 并发布 v1（v1 冻结 rev1 作为基准） =="
curl -sS -X PUT "$BASE/documents/$DOC/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[
    {"x":0,"y":0,"w":20,"h":10,"reason":"身份证号"},
    {"x":10,"y":5,"w":20,"h":10,"reason":"人脸"}
  ]}' >/dev/null
curl -sS -X POST "$BASE/documents/$DOC/publish" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"published v{d['version']} @ annotation rev {d['annotation_revision']}\")"
# rev1 两块矩形重叠 10x5=50px，并集 = 20*10 + 20*10 - 50 = 350px

echo
echo "== 2. 修改标注到 rev2（不发布）：左移/新增/保留部分遮盖 =="
curl -sS -X PUT "$BASE/documents/$DOC/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":1,"rects":[
    {"x":0,"y":0,"w":10,"h":10,"reason":"身份证号"},
    {"x":10,"y":5,"w":20,"h":10,"reason":"人脸"},
    {"x":40,"y":30,"w":15,"h":10,"reason":"新增：工号"}
  ]}' >/dev/null

echo
echo "== 3. 比较当前 rev2 与基准 v1（同一读取快照，只读） =="
curl -sS -X POST "$BASE/documents/$DOC/coverage-comparison" \
  -H 'Content-Type: application/json' \
  -d '{"current_revision":2,"baseline_version":1}' \
  | python3 -m json.tool
# 期望：
#   added_pixels：身份证号右移让出 0 之外新增部分 + 工号块 150px；
#   lost_pixels ：身份证号块右侧 [10,20)x[0,5) 等基准独有像素；
#   lost_segments：按水平边界段给出失去遮盖的 x 区间，
#                  相邻段区间完全相同时合并为一段。

echo
echo "== 4. 当前标注与基准完全相同 -> 新增/失去均为 0 =="
curl -sS -X PUT "$BASE/documents/$DOC/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":2,"rects":[]}' >/dev/null                 # rev3：清空
curl -sS -X PUT "$BASE/documents/$DOC/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":3,"rects":[
    {"x":0,"y":0,"w":20,"h":10,"reason":"身份证号"},
    {"x":10,"y":5,"w":20,"h":10,"reason":"人脸"}
  ]}' >/dev/null                                             # rev4：恢复成 rev1
curl -sS -X POST "$BASE/documents/$DOC/coverage-comparison" \
  -H 'Content-Type: application/json' \
  -d '{"current_revision":4,"baseline_version":1}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"added={d['added_pixels']} lost={d['lost_pixels']} segments={d['lost_segments']}\")"

echo
echo "== 5. 修订号过期 -> 409 stale_revision（附 current_revision，不读基准遮盖） =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/documents/$DOC/coverage-comparison" \
  -H 'Content-Type: application/json' \
  -d '{"current_revision":1,"baseline_version":1}'

echo
echo "== 6. 基准版本不存在 -> 404 unknown_version =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/documents/$DOC/coverage-comparison" \
  -H 'Content-Type: application/json' \
  -d '{"current_revision":4,"baseline_version":99}'

echo
echo "== 7. 基准未到公开时间 -> 404 publication_not_yet_public（不泄露遮盖详情） =="
curl -sS -X POST "$BASE/documents/$DOC/publish" -H 'Content-Type: application/json' \
  -d '{"publish_at":"2026-10-20T08:00:00Z"}' >/dev/null     # v2 定时公开
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/documents/$DOC/coverage-comparison" \
  -H 'Content-Type: application/json' \
  -d '{"current_revision":4,"baseline_version":2}'

echo
echo "== 8. 基准已撤回 -> 410 publication_withdrawn（撤回优先，不泄露遮盖详情） =="
curl -sS -X POST "$BASE/documents/$DOC/publications/1/withdraw" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}' >/dev/null
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/documents/$DOC/coverage-comparison" \
  -H 'Content-Type: application/json' \
  -d '{"current_revision":4,"baseline_version":1}'

echo
echo "比较全程只读：可用 GET $BASE/documents/$DOC 核对版本/修订号均未变化。"
