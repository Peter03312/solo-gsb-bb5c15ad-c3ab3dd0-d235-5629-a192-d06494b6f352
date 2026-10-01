#!/usr/bin/env bash
# 端到端调用示例：发布一组插件 -> 求解安装方案 -> 重复请求命中缓存
# -> 撤销版本 -> 历史方案仍可查且标记 revoked。
#
# 用法：
#   BASE=http://localhost:8080 bash examples/api-examples.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
API="$BASE/api/v1"
AUTH=()
if [[ -n "${API_TOKEN:-}" ]]; then
  AUTH=(-H "Authorization: Bearer ${API_TOKEN}")
fi

sha256_file() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

publish() {
  local plugin="$1" version="$2" meta="$3" content="$4"
  local pkg="$tmpdir/${plugin}-${version}.bin"
  printf '%s' "$content" > "$pkg"
  local sha; sha="$(sha256_file "$pkg")"
  local metafile="$tmpdir/meta-${plugin}-${version}.json"
  SHA="$sha" python3 -c '
import json,os,sys
m=json.loads(sys.argv[1]); m["sha256"]=os.environ["SHA"]
open(sys.argv[2],"w").write(json.dumps(m, ensure_ascii=False))
' "$meta" "$metafile"
  echo ">>> 发布 ${plugin}==${version}"
  curl -fsS -X POST "${AUTH[@]}" \
    -F "metadata=<$metafile;type=application/json" \
    -F "package=@${pkg};filename=${plugin}-${version}.bin" \
    "$API/plugins/$plugin/versions" | python3 -m json.tool
}

# 1) 发布：numpy 底座 + data-toolkit 两个版本（新版需要 numpy>=2）
publish numpy 2.1.0 '{"plugin":"numpy","version":"2.1.0",
  "runtimes":[{"name":"python","spec":">=3.10"}]}' "numpy-2.1.0-payload"
publish numpy 1.26.4 '{"plugin":"numpy","version":"1.26.4",
  "runtimes":[{"name":"python","spec":">=3.9"}]}' "numpy-1.26.4-payload"
publish data-toolkit 3.0.0 '{"plugin":"data-toolkit","version":"3.0.0",
  "runtimes":[{"name":"python","spec":">=3.10"}],
  "dependencies":[{"plugin":"numpy","spec":">=2,<3"}]}' "dtk-3.0.0"
publish data-toolkit 2.4.1 '{"plugin":"data-toolkit","version":"2.4.1",
  "runtimes":[{"name":"python","spec":">=3.9"}],
  "dependencies":[{"plugin":"numpy","spec":">=1.20,<2"}]}' "dtk-2.4.1"

# 2) 求解：Python 3.11 + 最新 data-toolkit -> 应选 data-toolkit 3.0.0 / numpy 2.1.0
echo ">>> 求解（python 3.11，目标 data-toolkit 最新）"
curl -fsS -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"runtimes":[{"name":"python","spec":"3.11.*"}],
       "targets":{"data-toolkit":">=2"}}' \
  "$API/solutions" | tee "$tmpdir/sol1.json" | python3 -m json.tool

# 3) 同样请求再来一次：cached=true，方案完全一致
echo ">>> 重复请求（应 cached=true）"
curl -fsS -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"runtimes":[{"name":"python","spec":"3.11.*"}],
       "targets":{"data-toolkit":">=2"}}' \
  "$API/solutions" | python3 -m json.tool

# 4) Python 3.9：data-toolkit 3.0.0 不兼容，求解器自动选 2.4.1 / numpy 1.26.4
echo ">>> 求解（python 3.9，应回退旧版）"
curl -fsS -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"runtimes":[{"name":"python","spec":"3.9.*"}],
       "targets":{"data-toolkit":""}}' \
  "$API/solutions" | python3 -m json.tool

# 5) 无解示例：强行要求 data-toolkit>=3 但 Python 3.8（HTTP 409 + 冲突明细）
echo ">>> 无解示例（409）"
curl -sS -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d '{"runtimes":[{"name":"python","spec":"3.8.*"}],
       "targets":{"data-toolkit":">=3"}}' \
  "$API/solutions" | python3 -m json.tool || true

# 6) 查看目录修订
echo ">>> 目录修订列表"
curl -fsS "$API/revisions" | python3 -m json.tool

SOL_ID="$(python3 -c 'import json;print(json.load(open("'$tmpdir'/sol1.json"))["solution_id"])')"

# 7) 撤销 numpy 2.1.0（不影响已保存方案）
echo ">>> 撤销 numpy==2.1.0"
curl -fsS -X POST "${AUTH[@]}" \
  -H 'Content-Type: application/json' -d '{"reason":"发现严重缺陷"}' \
  "$API/plugins/numpy/versions/2.1.0/revoke" | python3 -m json.tool

# 8) 历史方案仍可查，numpy 2.1.0 被标记 revoked=true
echo ">>> 历史方案 $SOL_ID（numpy 应显示 revoked=true）"
curl -fsS "$API/solutions/$SOL_ID" | python3 -m json.tool

# 9) 指定旧修订重新求解：撤销前的修订仍能解出原方案
OLD_REV="$(python3 -c 'import json;print(json.load(open("'$tmpdir'/sol1.json"))["revision"])')"
echo ">>> 在修订 $OLD_REV 上重放请求（撤销不影响旧修订）"
curl -fsS -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"revision\":$OLD_REV,
       \"runtimes\":[{\"name\":\"python\",\"spec\":\"3.11.*\"}],
       \"targets\":{\"data-toolkit\":\">=2\"}}" \
  "$API/solutions" | python3 -m json.tool

# 10) 下载安装包（响应头带 X-SHA256）
echo ">>> 下载 data-toolkit 3.0.0 安装包"
curl -fsS -D - -o "$tmpdir/dtk.bin" \
  "$API/plugins/data-toolkit/versions/3.0.0/download" | grep -Ei 'HTTP|x-sha256|content-disposition'
echo "本地校验：$(sha256_file "$tmpdir/dtk.bin")"

# 11) 方案预演（dry-run）：对已保存方案 #SOL_ID 做不保存的“如果……会怎样”
# 再发布 numpy 2.2.0 与 data-toolkit 3.1.0（3.1 依赖 numpy>=2.2）
publish numpy 2.2.0 '{"plugin":"numpy","version":"2.2.0",
  "runtimes":[{"name":"python","spec":">=3.10"}]}' "numpy-2.2.0-payload"
publish data-toolkit 3.1.0 '{"plugin":"data-toolkit","version":"3.1.0",
  "runtimes":[{"name":"python","spec":">=3.10"}],
  "dependencies":[{"plugin":"numpy","spec":">=2.2"}]}' "dtk-3.1.0"
LATEST_REV="$(curl -fsS "$API/revisions" | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["revision"])')"

# 注意：第 7 步撤销过 numpy 2.1.0；原方案在其原修订上重放仍全部 keep
echo ">>> 预演：原修订 $OLD_REV 上重放（应 0 变动）"
curl -fsS -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"revision\":$OLD_REV}" \
  "$API/solutions/$SOL_ID/preview" | python3 -m json.tool

# 在最新修订上覆盖目标为 data-toolkit>=3.1：最小变动 = 两处换版
echo ">>> 预演：修订 $LATEST_REV 上升级到 data-toolkit 3.1（应 2 次换版）"
curl -fsS -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"revision\":$LATEST_REV,
       \"runtimes\":[{\"name\":\"python\",\"spec\":\"3.11.*\"}],
       \"targets\":{\"data-toolkit\":\">=3.1\"}}" \
  "$API/solutions/$SOL_ID/preview" | tee "$tmpdir/preview.json" | python3 -m json.tool

echo ">>> 预演结果不落库：方案 $SOL_ID 原样可查"
curl -fsS "$API/solutions/$SOL_ID" \
  | python3 -c 'import json,sys;s=json.load(sys.stdin);print("saved selection:",
        {k:v["version"] for k,v in s["selection"].items()})'

# 无解预演：409 + 结构化冲突，不保存任何部分方案
echo ">>> 预演无解（data-toolkit>=3.1 但 python 3.9）"
curl -sS -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"revision\":$LATEST_REV,
       \"runtimes\":[{\"name\":\"python\",\"spec\":\"3.9.*\"}],
       \"targets\":{\"data-toolkit\":\">=3.1\"}}" \
  "$API/solutions/$SOL_ID/preview" | python3 -m json.tool || true

# 12) 离线批次交付：相互依赖的多个版本一次提交，只产生一个目录修订
echo ">>> 批次发布 batch-2026-10-01：pack-a + pack-b（b 依赖 a）同时可见"
batch_dir="$tmpdir/batch"
mkdir -p "$batch_dir"
batch_member() {
  local idx="$1" plugin="$2" version="$3" meta="$4" content="$5"
  local pkg="$batch_dir/pkg-$idx.bin"
  printf '%s' "$content" > "$pkg"
  local sha; sha="$(sha256_file "$pkg")"
  SHA="$sha" python3 -c '
import json,os,sys
m=json.loads(sys.argv[1]); m["sha256"]=os.environ["SHA"]
open(sys.argv[2],"w").write(json.dumps(m, ensure_ascii=False))
' "$meta" "$batch_dir/meta-$idx.json"
  echo "$pkg|$plugin-$version.bin"
}
mapfile -t PARTS < <(
  batch_member 1 pack-a 1.0.0 \
    '{"plugin":"pack-a","version":"1.0.0",
      "runtimes":[{"name":"python","spec":">=3.10"}]}' \
    "pack-a-1.0.0-payload"
  batch_member 2 pack-b 1.0.0 \
    '{"plugin":"pack-b","version":"1.0.0",
      "runtimes":[{"name":"python","spec":">=3.10"}],
      "dependencies":[{"plugin":"pack-a","spec":">=1"}]}' \
    "pack-b-1.0.0-payload"
)
curl -fsS -X POST "${AUTH[@]}" \
  -F "batch_id=batch-2026-10-01" \
  -F "metadata=<$batch_dir/meta-1.json;type=application/json" \
  -F "package=@${PARTS[0]%%|*};filename=${PARTS[0]##*|}" \
  -F "metadata=<$batch_dir/meta-2.json;type=application/json" \
  -F "package=@${PARTS[1]%%|*};filename=${PARTS[1]##*|}" \
  "$API/batches" | tee "$tmpdir/batch.json" | python3 -m json.tool

BATCH_REV="$(python3 -c 'import json;print(json.load(open("'$tmpdir'/batch.json"))["revision"])')"
echo ">>> 批次只产生一个修订 $BATCH_REV，两个成员在该修订可一起求解"
curl -fsS -X POST "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"revision\":$BATCH_REV,\"targets\":{\"pack-b\":\"\"}}" \
  "$API/solutions" | python3 -m json.tool

# 13) 相同批次标识 + 相同内容重试 -> 200 cached=true，返回原修订号
echo ">>> 批次重试（应 cached=true，修订号不变）"
curl -fsS -X POST "${AUTH[@]}" \
  -F "batch_id=batch-2026-10-01" \
  -F "metadata=<$batch_dir/meta-2.json;type=application/json" \
  -F "package=@${PARTS[1]%%|*};filename=${PARTS[1]##*|}" \
  -F "metadata=<$batch_dir/meta-1.json;type=application/json" \
  -F "package=@${PARTS[0]%%|*};filename=${PARTS[0]##*|}" \
  "$API/batches" | python3 -c 'import json,sys;b=json.load(sys.stdin);
print("revision:",b["revision"],"cached:",b["cached"])'

# 14) 按批次标识查询原结果
echo ">>> 查询批次 batch-2026-10-01"
curl -fsS "$API/batches/batch-2026-10-01" \
  | python3 -c 'import json,sys;b=json.load(sys.stdin);
print("members:",[(m["plugin"],m["version"]) for m in b["members"]])'

# 15) 相同批次标识但内容不同 -> 409 batch_conflict
echo ">>> 同号不同内容（应 409）"
curl -sS -X POST "${AUTH[@]}" \
  -F "batch_id=batch-2026-10-01" \
  -F "metadata=<$batch_dir/meta-1.json;type=application/json" \
  -F "package=@${PARTS[0]%%|*};filename=pack-a-1.0.0.bin" \
  -F "metadata=<$batch_dir/meta-2.json;type=application/json" \
  -F "package=@${PARTS[0]%%|*};filename=pack-b-1.0.0.bin" \
  "$API/batches" | python3 -m json.tool || true

echo "全部示例执行完成。"
