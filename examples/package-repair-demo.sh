#!/usr/bin/env bash
# 安装包核验与修复端到端演示（只读开放 + API_TOKEN 写入规则）。
#
# 流程：发布两个共享同一安装包内容的版本 -> 核验完好 -> 模拟长期保存后
# 文件损坏 -> verify=corrupt、下载 404（X-SHA256/X-Revoked 头仍在）->
# 提交同摘要安装包原子修复 -> 两个版本同时恢复下载 -> 摘要不符 422、
# 未知版本 404、完好包再修返回 intact。
#
# 用法：
#   BASE=http://localhost:8080 bash examples/package-repair-demo.sh
#
# 模拟“磁盘上的文件损坏”需要触碰服务器的内容寻址目录 PACKAGE_DIR：
#   * 默认按 docker compose 部署演示：脚本通过 `docker compose exec api`
#     在 api 容器内改写/删除包文件（容器内 PACKAGE_DIR=/data/packages）；
#   * 服务本机直跑时，请设置 SERVER_PKG_DIR 指向服务器 PACKAGE_DIR
#     （脚本须对该目录有写权限）；
#   * 如无法触碰服务器存储，可仅观察前半段（发布+核验），损坏步骤会被跳过。
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
API="$BASE/api/v1"
AUTH=()
if [[ -n "${API_TOKEN:-}" ]]; then
  AUTH=(-H "Authorization: Bearer ${API_TOKEN}")
fi

# 容器内包目录（compose 的 api 服务）；本机直跑用 SERVER_PKG_DIR 覆盖
CONTAINER_PKG_DIR="${CONTAINER_PKG_DIR:-/data/packages}"

sha256_file() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# 在服务器存储上执行一条 shell：优先 docker compose exec，其次本机目录
# 传入参数即完整的 shell 命令（可用 $PKG_DIR 引用包目录）
remote_sh() {
  local cmd="$1"
  if [[ -n "${SERVER_PKG_DIR:-}" ]]; then
    PKG_DIR="$SERVER_PKG_DIR" bash -c "$cmd"
  elif command -v docker >/dev/null 2>&1 && docker compose ps >/dev/null 2>&1; then
    docker compose exec -T -e PKG_DIR="$CONTAINER_PKG_DIR" api sh -c "$cmd"
  else
    return 2
  fi
}

can_touch_storage() { remote_sh 'exit 0' >/dev/null 2>&1; }

pkg_path_for() {  # $1=sha256 -> 内容寻址路径
  echo "$CONTAINER_PKG_DIR/${1:0:2}/$1"
}

echo ">>> 1) 发布两个共享同一内容的版本 fix-a==1.0.0 / fix-b==1.0.0"
printf 'repair-demo-payload' > "$tmpdir/pkg.bin"
SHA="$(sha256_file "$tmpdir/pkg.bin")"
for p in fix-a fix-b; do
  curl -fsS -X POST "${AUTH[@]}" \
    -F "metadata={\"plugin\":\"$p\",\"version\":\"1.0.0\",\"sha256\":\"$SHA\"};type=application/json" \
    -F "package=@$tmpdir/pkg.bin;filename=$p.bin" \
    "$API/plugins/$p/versions" >/dev/null
done

echo ">>> 2) 核验 fix-a==1.0.0（期望 state=intact，shared_version_count=2）"
curl -fsS "$API/plugins/fix-a/versions/1.0.0/verify" | python3 -m json.tool

if can_touch_storage; then
  echo ">>> 3) 模拟介质损坏：改写内容寻址文件 ${SHA:0:2}/$SHA"
  remote_sh "printf 'CORRUPTED-CONTENT' > \"\$PKG_DIR/${SHA:0:2}/$SHA\""

  echo ">>> 4) 再次核验（期望 state=corrupt）"
  curl -fsS "$API/plugins/fix-a/versions/1.0.0/verify" | python3 -m json.tool

  echo ">>> 5) 下载损坏包必须失败（404 package_corrupt，响应头保留 X-SHA256/X-Revoked）"
  curl -sS -D "$tmpdir/hdr" -o "$tmpdir/body" \
    "$API/plugins/fix-a/versions/1.0.0/download" || true
  grep -Ei 'HTTP|x-sha256|x-revoked' "$tmpdir/hdr"
  cat "$tmpdir/body" | python3 -m json.tool

  echo ">>> 6) 全库巡检：只看损坏项"
  curl -fsS "$API/packages?status=corrupt" \
    | python3 -c 'import json,sys;d=json.load(sys.stdin);
print("corrupt=",d["corrupt"],"items=",[ (i["plugin"],i["version"]) for i in d["items"]])'

  echo ">>> 7) 原子修复（同摘要才替换；fix-a 修复，fix-b 共享文件随之恢复）"
  curl -fsS -X POST "${AUTH[@]}" \
    -F "package=@$tmpdir/pkg.bin;filename=replacement.bin" \
    "$API/plugins/fix-a/versions/1.0.0/repair" | python3 -m json.tool

  echo ">>> 8) 两个版本恢复下载，内容与摘要一致"
  for p in fix-a fix-b; do
    curl -fsS -o "$tmpdir/back.bin" \
      "$API/plugins/$p/versions/1.0.0/download"
    test "$(sha256_file "$tmpdir/back.bin")" = "$SHA"
    echo "  $p 下载恢复，SHA-256 一致"
  done

  echo ">>> 9) 完好包再次修复 -> result=intact（不改写）"
  curl -fsS -X POST "${AUTH[@]}" \
    -F "package=@$tmpdir/pkg.bin;filename=replacement.bin" \
    "$API/plugins/fix-a/versions/1.0.0/repair" \
    | python3 -c 'import json,sys;d=json.load(sys.stdin);
print("result=",d["result"],"previous_state=",d["previous_state"],
      "affected=",d["affected_version_count"])'
else
  echo ">>> 3-9) 无法触碰服务器存储（非 compose 部署且未设置 SERVER_PKG_DIR），"
  echo "         跳过损坏/修复演练；核验接口仍可用于只读巡检："
  curl -fsS "$API/packages" | python3 -m json.tool
fi

echo ">>> 10) 摘要不符的替换文件 -> 422 sha256_mismatch（原文件保留）"
printf 'wrong-content' > "$tmpdir/bad.bin"
curl -sS -X POST "${AUTH[@]}" \
  -F "package=@$tmpdir/bad.bin;filename=bad.bin" \
  "$API/plugins/fix-a/versions/1.0.0/repair" | python3 -m json.tool || true

echo ">>> 11) 未知版本：核验与修复都 -> 404"
curl -sS -o /dev/null -w 'verify  unknown -> HTTP %{http_code}\n' \
  "$API/plugins/no-such/versions/9.9.9/verify"
printf 'x' > "$tmpdir/x.bin"
curl -sS -o /dev/null -w 'repair  unknown -> HTTP %{http_code}\n' \
  -X POST "${AUTH[@]}" -F "package=@$tmpdir/x.bin" \
  "$API/plugins/no-such/versions/9.9.9/repair"

echo "修复演示完成。"
