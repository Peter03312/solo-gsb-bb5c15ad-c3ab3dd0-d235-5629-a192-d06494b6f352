#!/usr/bin/env python3
"""纯标准库调用示例（离线环境无需 pip install requests）。

运行：
    python3 examples/api_client.py --base http://localhost:8080

展示：发布（含 SHA-256 校验）、求解、缓存命中、无解冲突明细、
撤销、历史方案与撤销标记、按旧修订重放。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import urllib.error
import urllib.request
import uuid


class Client:
    def __init__(self, base: str, token: str | None = None):
        self.base = base.rstrip("/") + "/api/v1"
        self.token = token

    def _request(self, method: str, path: str, data=None, headers=None):
        url = self.base + path
        body = None
        hdrs = dict(headers or {})
        if data is not None and not isinstance(data, (bytes, bytearray)):
            body = json.dumps(data).encode()
            hdrs.setdefault("Content-Type", "application/json")
        else:
            body = data
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, dict(resp.headers), (
                    json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw.decode("utf-8", "replace")
            return e.code, dict(e.headers), payload

    # ------------------------------------------------------------ API
    def publish(self, meta: dict, payload: bytes):
        boundary = "----lab" + uuid.uuid4().hex
        buf = io.BytesIO()
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            b'Content-Disposition: form-data; name="metadata"\r\n'
            b"Content-Type: application/json\r\n\r\n")
        buf.write(json.dumps(meta, ensure_ascii=False).encode())
        filename = f"{meta['plugin']}-{meta['version']}.bin"
        buf.write(f"\r\n--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="package"; filename="{filename}"\r\n'
            .encode())
        buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
        buf.write(payload)
        buf.write(f"\r\n--{boundary}--\r\n".encode())
        return self._request(
            "POST", f"/plugins/{meta['plugin']}/versions",
            data=buf.getvalue(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def publish_batch(self, batch_id: str, members: list[tuple[dict, bytes]]):
        """离线批次交付：members 为 [(metadata, payload), ...]，顺序即提交顺序。"""
        boundary = "----labbatch" + uuid.uuid4().hex
        buf = io.BytesIO()

        def write_field(name, value: bytes, content_type="application/json",
                       filename=None):
            disp = f'Content-Disposition: form-data; name="{name}"'
            if filename:
                disp += f'; filename="{filename}"'
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(disp.encode() + b"\r\n")
            buf.write(f"Content-Type: {content_type}\r\n\r\n".encode())
            buf.write(value if isinstance(value, bytes) else value.encode())
            buf.write(b"\r\n")

        write_field("batch_id", batch_id, content_type="text/plain")
        for meta, payload in members:
            write_field("metadata", json.dumps(meta, ensure_ascii=False))
            filename = f"{meta['plugin']}-{meta['version']}.bin"
            write_field("package", payload,
                        content_type="application/octet-stream",
                        filename=filename)
        buf.write(f"--{boundary}--\r\n".encode())
        return self._request(
            "POST", "/batches", data=buf.getvalue(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def batch(self, batch_id: str):
        return self._request("GET", f"/batches/{batch_id}")

    def solve(self, req: dict):
        return self._request("POST", "/solutions", data=req)

    def preview(self, solution_id: int, req: dict):
        return self._request(
            "POST", f"/solutions/{solution_id}/preview", data=req)

    def solution(self, sid: int):
        return self._request("GET", f"/solutions/{sid}")

    def revoke(self, plugin: str, version: str, reason: str):
        return self._request(
            "POST", f"/plugins/{plugin}/versions/{version}/revoke",
            data={"reason": reason})

    def revisions(self):
        return self._request("GET", "/revisions")


def show(title, status, payload):
    print(f"\n=== {title} [HTTP {status}] ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--token", default=None)
    args = ap.parse_args()
    c = Client(args.base, args.token)

    def pub(plugin, version, runtimes, deps=None, conflicts=None, content=None):
        content = (content or f"{plugin}-{version}").encode()
        meta = {
            "plugin": plugin,
            "version": version,
            "sha256": hashlib.sha256(content).hexdigest(),
            "runtimes": runtimes,
            "dependencies": deps or [],
            "conflicts": conflicts or [],
        }
        st, _, body = c.publish(meta, content)
        show(f"发布 {plugin}=={version}", st, body if st < 400 else body)
        return st

    # 准备数据：A、B 两个插件，B 依赖 A
    pub("alpha", "2.0.0", [{"name": "python", "spec": ">=3.10"}],
        content="alpha2")
    pub("alpha", "1.5.0", [{"name": "python", "spec": ">=3.8,<3.10"}],
        content="alpha1")
    pub("beta", "1.0.0",
        [{"name": "python", "spec": ">=3.10"}],
        deps=[{"plugin": "alpha", "spec": ">=2,<3"}], content="beta1")

    # 求解：Python 3.11
    req = {"runtimes": [{"name": "python", "spec": "3.11.*"}],
           "targets": {"beta": ""}}
    st, _, sol = c.solve(req)
    show("求解 beta（Python 3.11）", st, sol)
    assert st == 200 and sol["selection"]["alpha"]["version"] == "2.0.0"
    assert sol["install_order"] == ["alpha", "beta"]
    sid = sol["solution_id"]

    # 同请求再发一次 -> 缓存
    st, _, sol2 = c.solve(req)
    show("重复请求（cached 应为 true）", st, {"cached": sol2["cached"]})
    assert sol2["cached"] is True
    assert sol2["selection"] == sol["selection"]

    # Python 3.9 -> beta 1.0.0 不兼容，整体无解
    st, _, err = c.solve({
        "runtimes": [{"name": "python", "spec": "3.9.*"}],
        "targets": {"beta": ""}})
    show("无解：beta 不支持 3.9（409 + 冲突）", st, err)
    assert st == 409 and err["detail"]["code"] == "unsatisfiable"

    # 撤销 alpha 2.0.0
    st, _, rev = c.revoke("alpha", "2.0.0", "回归缺陷")
    show("撤销 alpha==2.0.0", st, {"revoked_revision": rev["revoked_revision"]})

    # 历史方案仍可查，且 alpha 被标 revoked
    st, _, hist = c.solution(sid)
    show(f"历史方案 {sid}（alpha 应 revoked=true）", st, {
        "alpha_revoked": hist["selection"]["alpha"]["revoked"],
        "selection": hist["selection"],
    })
    assert hist["selection"]["alpha"]["revoked"] is True

    # 按旧修订重放：撤销后旧修订仍可得到原方案
    old_rev = sol["revision"]
    st, _, replay = c.solve({"revision": old_rev, **req})
    show(f"在旧修订 {old_rev} 重放", st,
         {"revision": replay["revision"], "alpha": replay["selection"]["alpha"]["version"]})
    assert replay["selection"]["alpha"]["version"] == "2.0.0"

    # ------------------------------------------------------------ 方案预演
    # 再发布 alpha 2.1.0 与 beta 2.0.0（beta2 依赖 alpha>=2.1）
    pub("alpha", "2.1.0", [{"name": "python", "spec": ">=3.10"}],
        content="alpha21")
    pub("beta", "2.0.0",
        [{"name": "python", "spec": ">=3.10"}],
        deps=[{"plugin": "alpha", "spec": ">=2.1"}], content="beta2")
    latest_rev = c.revisions()[2][0]["revision"]

    # 预演 1：同输入在原修订上 → 全部 keep，变动 0，不保存
    st, _, pv = c.preview(sid, {"revision": old_rev})
    show(f"预演：原修订 {old_rev} 重放（应 0 变动、saved=false）", st, {
        "saved": pv["saved"], "change_counts": pv["change_counts"],
        "changes": pv["changes"]})
    assert st == 200 and pv["change_counts"]["changed"] == 0
    assert pv["saved"] is False

    # 预演 2：在最新修订上覆盖目标为 beta 最新
    # 最小变动：alpha 2.0->2.1、beta 1.0->2.0，各计一次换版
    st, _, pv2 = c.preview(sid, {
        "revision": latest_rev,
        "runtimes": [{"name": "python", "spec": "3.11.*"}],
        "targets": {"beta": ">=2"}})
    show(f"预演：修订 {latest_rev} 升级到 beta2（应 2 次换版）", st, {
        "selection": {k: v["version"] for k, v in pv2["selection"].items()},
        "install_order": pv2["install_order"],
        "change_counts": pv2["change_counts"],
        "changes": pv2["changes"]})
    assert st == 200
    assert pv2["selection"]["alpha"]["version"] == "2.1.0"
    assert pv2["selection"]["beta"]["version"] == "2.0.0"
    assert pv2["install_order"] == ["alpha", "beta"]
    assert pv2["change_counts"]["change"] == 2

    # 预演 3：覆盖为不兼容运行环境 python 3.9 → 409 结构化原因，不保存
    st, _, err = c.preview(sid, {
        "revision": latest_rev,
        "runtimes": [{"name": "python", "spec": "3.9.*"}],
        "targets": {"beta": ">=2"}})
    show("预演无解：beta2 不支持 python 3.9（409，不保存部分方案）", st, err)
    assert st == 409 and err["detail"]["code"] == "unsatisfiable"

    # 预演 4：非法范围 / 不存在方案 / 不存在修订 → 明确拒绝
    st, _, bad = c.preview(sid, {"revision": latest_rev,
                                 "targets": {"beta": "~=1"}})
    show("预演拒绝：非法覆盖约束（400）", st, bad)
    assert st == 400
    st, _, bad = c.preview(999999, {"revision": latest_rev})
    show("预演拒绝：方案不存在（404）", st, bad)
    assert st == 404
    st, _, bad = c.preview(sid, {"revision": 999999})
    show("预演拒绝：修订不存在（400）", st, bad)
    assert st == 400

    # 摘要不符必须拒绝
    bad = {"plugin": "alpha", "version": "9.9.9", "sha256": "0" * 64,
           "runtimes": [], "dependencies": [], "conflicts": []}
    st, _, body = c.publish(bad, b"different-content")
    show("摘要不符（应 422 拒绝）", st, body)
    assert st == 422

    # ------------------------------------------------------------ 批次发布
    # 相互依赖的两个版本一次交付：只产生一个目录修订，成员同时可见
    b_lib = ({"plugin": "batch-lib", "version": "1.0.0",
              "sha256": hashlib.sha256(b"batchlib").hexdigest(),
              "runtimes": [], "dependencies": [], "conflicts": []}, b"batchlib")
    b_app = ({"plugin": "batch-app", "version": "1.0.0",
              "sha256": hashlib.sha256(b"batchapp").hexdigest(),
              "runtimes": [],
              "dependencies": [{"plugin": "batch-lib", "spec": ">=1"}],
              "conflicts": []}, b"batchapp")

    st, _, b1 = c.publish_batch("batch-demo-001", [b_lib, b_app])
    show("批次发布（201，单修订、两成员）", st, {
        "revision": b1["revision"], "cached": b1["cached"],
        "members": [(m["plugin"], m["version"]) for m in b1["members"]]})
    assert st == 201 and len(b1["members"]) == 2
    batch_rev = b1["revision"]

    # 在批次修订上求解，两个相互依赖成员都应可见
    st, _, bs = c.solve({"revision": batch_rev, "targets": {"batch-app": ""}})
    show("批次修订上求解", st, {"install_order": bs["install_order"]})
    assert st == 200 and bs["install_order"] == ["batch-lib", "batch-app"]

    # 相同标识 + 相同内容重试（顺序调换也一样）-> 200 cached=true
    st, _, b2 = c.publish_batch("batch-demo-001", [b_app, b_lib])
    show("批次重试（200 cached=true，修订号不变）", st, {
        "revision": b2["revision"], "cached": b2["cached"]})
    assert st == 200 and b2["cached"] is True
    assert b2["revision"] == batch_rev

    # 按标识查询原结果
    st, _, b3 = c.batch("batch-demo-001")
    show("按批次标识查询", st, {"revision": b3["revision"],
                               "count": b3["member_count"]})
    assert st == 200 and b3["revision"] == batch_rev

    # 相同标识 + 不同内容 -> 409
    b_app_conflict = dict(b_app[0]), b"totally-different-payload"
    b_app_conflict[0]["sha256"] = hashlib.sha256(
        b"totally-different-payload").hexdigest()
    st, _, berr = c.publish_batch(
        "batch-demo-001", [b_lib, b_app_conflict])
    show("批次同号不同内容（409 batch_conflict）", st, berr)
    assert st == 409 and berr["detail"]["code"] == "batch_conflict"

    # 批次内 (插件,版本) 重复 -> 409
    st, _, berr = c.publish_batch("batch-demo-dup", [b_lib, b_lib])
    show("批次内重复成员（409）", st, berr)
    assert st == 409

    print("\n全部示例断言通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
