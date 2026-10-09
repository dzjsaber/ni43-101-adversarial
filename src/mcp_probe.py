"""
mcp_probe.py — MCP 客户端探针(零成本, 不需要 mcp 包)

用真实的 MCP stdio 握手去验证 mineral_mcp.py 这个"服务器"到底能不能被宿主
(Cherry Studio / Claude Desktop / 任意 MCP 客户端)正常调用, 顺便验证费用护栏。

用法: python src/mcp_probe.py
退出码 0 = 全部通过; 1 = 有失败项。
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src" / "mineral_mcp.py"


class Server:
    def __init__(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        self.p = subprocess.Popen([sys.executable, str(SERVER), "--stdio"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, cwd=str(ROOT),
                                  text=True, encoding="utf-8", bufsize=1, env=env)
        self.q, self._buf = [], ""
        threading.Thread(target=self._reader, daemon=True).start()
        self.err = []
        threading.Thread(target=self._read_err, daemon=True).start()

    def _reader(self):
        for line in self.p.stdout:
            line = line.strip()
            if line:
                self.q.append(line)

    def _read_err(self):
        for line in self.p.stderr:
            if line.strip():
                self.err.append(line.strip())

    def send(self, obj):
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def call(self, mid, method, params=None, timeout=20):
        self.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.q:
                return json.loads(self.q.pop(0))
            time.sleep(0.05)
        return {"error": f"timeout waiting for {method}"}

    def notify(self, method, params=None):
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:                                        # noqa: BLE001
            self.p.kill()


FAILED = []


def check(ok, msg):
    print(("  [PASS] " if ok else "  [FAIL] ") + msg)
    if not ok:
        FAILED.append(msg)


def text_of(resp):
    try:
        return resp["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return json.dumps(resp, ensure_ascii=False)[:400]


def main():
    print(f"启动 MCP server: python src/mineral_mcp.py --stdio")
    s = Server()
    try:
        print("1. MCP 握手")
        r = s.call(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "mcp_probe", "version": "1.0"}})
        info = r.get("result", {})
        check(info.get("serverInfo", {}).get("name") == "mineral-resources",
              f"initialize -> {info.get('serverInfo')} protocol={info.get('protocolVersion')}")
        check("tools" in info.get("capabilities", {}), "声明了 tools 能力")
        s.notify("notifications/initialized")

        print("2. tools/list")
        r = s.call(2, "tools/list")
        tools = [t["name"] for t in r.get("result", {}).get("tools", [])]
        print(f"   暴露工具: {tools}")
        need = {"list_reports", "deliverable_summary", "needs_human_review", "top_deposits",
                "records_for_page", "run_pipeline", "evaluate_gt", "run_fault_drill",
                "list_report_configs", "spec_results"}
        check(need <= set(tools) and len(tools) == 10, f"工具数量与名称正确({len(tools)} 个)")

        print("3. 只读工具(离线, 免费)")
        r = s.call(3, "tools/call", {"name": "deliverable_summary", "arguments": {}})
        summary = text_of(r)
        check('"records"' in summary, f"deliverable_summary -> {summary[:120]}...")
        r = s.call(4, "tools/call", {"name": "top_deposits",
                                     "arguments": {"category": "M&I", "n": 2, "basis": "Barrick"}})
        t = text_of(r)
        check("Moz" in t or "没有匹配" in t, f"top_deposits -> {t[:120]}")
        r = s.call(5, "tools/call", {"name": "records_for_page", "arguments": {"page": 17}})
        t = text_of(r)
        check('"source_page": 17' in t, f"records_for_page(17) -> {t[:90]}...")
        r = s.call(6, "tools/call", {"name": "needs_human_review", "arguments": {}})
        check(len(text_of(r)) > 0, f"needs_human_review -> {text_of(r)[:90]}")

        print("4. 费用护栏: 没有 confirm 必须拒绝")
        r = s.call(7, "tools/call", {"name": "run_pipeline",
                                     "arguments": {"pdf": "data/pdfs/barrick.pdf"}})
        t = text_of(r)
        refused = "refused" in t and "confirm" in t
        check(refused and r["result"].get("isError") is False,
              f"run_pipeline(无 confirm) -> {t[:160]}")

        print("5. 路径白名单: 目录外路径必须拒绝")
        r = s.call(8, "tools/call", {"name": "run_pipeline",
                                     "arguments": {"pdf": "C:/Windows/System32/drivers/etc/hosts",
                                                   "confirm": True, "dry_run": True}})
        t = text_of(r)
        check(r["result"].get("isError") is True and "护栏拒绝" in t,
              f"白名单外路径 -> {t[:120]}")

        print("6. confirm + dry_run: 只返回计划, 不烧钱")
        r = s.call(9, "tools/call", {"name": "run_pipeline",
                                     "arguments": {"pdf": "data/pdfs/barrick.pdf",
                                                   "confirm": True, "dry_run": True}})
        t = text_of(r)
        check("dry_run" in t and "api_calls_estimate" in t, f"计划 -> {t[:160]}")

        print("7. 未知工具/未知方法必须报错而不是崩")
        r = s.call(10, "tools/call", {"name": "drop_database", "arguments": {}})
        check("未知工具" in text_of(r), "未知工具被拒绝")
        r = s.call(11, "no/such/method")
        check(r.get("error", {}).get("code") == -32601, "未知方法返回 -32601")
        r = s.call(16, "resources/list")
        check(r.get("result", {}).get("resources") == [], "resources/list 返回空列表(宿主探测友好)")
        r = s.call(17, "prompts/list")
        check(r.get("result", {}).get("prompts") == [], "prompts/list 返回空列表")

        print("8. GT 对账工具(离线免费)与演习护栏")
        r = s.call(12, "tools/call", {"name": "evaluate_gt", "arguments": {"gt": "p192"}})
        t = text_of(r)
        check("42/42" in t and "100.0%" in t, f"evaluate_gt(p192) -> {t.replace(chr(10), ' | ')[:170]}")
        r = s.call(13, "tools/call", {"name": "evaluate_gt", "arguments": {"gt": "p17"}})
        t = text_of(r)
        check("7/7" in t, f"evaluate_gt(p17) -> {t.replace(chr(10), ' | ')[:140]}")
        r = s.call(14, "tools/call", {"name": "run_fault_drill", "arguments": {"always": True}})
        t = text_of(r)
        check("refused" in t and "confirm" in t, "run_fault_drill 无 confirm 被拒绝")
        r = s.call(15, "tools/call", {"name": "spec_results", "arguments": {}})
        t = text_of(r)
        check("indicated" in t and "counts" in t, f"spec_results -> {t.replace(chr(10), ' ')[:110]}")
    finally:
        err = " | ".join(s.err)          # 由后台线程收集, 不能直接 read() 否则会被阻塞
        s.close()
        if err:
            print(f"   (server stderr: {err[:300]})")

    print()
    print("FAILED:", FAILED if FAILED else "无 —— MCP 服务端可被宿主正常调用")
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:                                   # 父进程用管道抓输出时会退回 GBK, 这里强制 UTF-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
