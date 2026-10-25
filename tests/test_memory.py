"""共享笔记与真实本地 MCP 进程测试；不调用模型。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import model_runtime
from shared_memory import MAX_NOTE_CHARS, MAX_NOTES, MAX_TOTAL_CHARS, _load, create_memory, update_note
from file_tools import FILE_TOOL_NAMES


class MemoryTests(unittest.TestCase):
    # 每项测试使用独立的临时团队目录。
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = create_memory(Path(self.folder.name))

    # 误运行内部工具时应提示正确入口；这些命令不会连接模型。
    def test_tool_cli_explains_correct_entry(self):
        script = model_runtime.PROJECT_DIR / "memory_tool.py"
        for arguments, code in (([], 2), (["--help"], 0), ([str(self.path), "abc"], 2)):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(script), *arguments], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, code)
                self.assertIn("python main.py", result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    # 新团队生成新文件，不覆盖旧团队的工作笔记。
    def test_new_team_keeps_old_memory(self):
        result = update_note(self.path, 1, "", 0, "[待验证] 原始结论")
        second = create_memory(Path(self.folder.name))
        self.assertNotEqual(self.path, second)
        self.assertEqual(_load(second)["notes"], [])
        self.assertEqual(_load(self.path)["notes"][0]["id"], result["id"])

    # 不同成员可修订同一条目，旧版本修改被拒绝，历史独立保留。
    def test_revision_conflict_and_history(self):
        first = update_note(self.path, 1, "", 0, "[待验证] 初稿")
        second = update_note(self.path, 2, first["id"], first["v"], "[有分歧] 需要检查边界")
        stale = update_note(self.path, 1, first["id"], first["v"], "不该覆盖新版")
        self.assertTrue(second["ok"])
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["current"]["v"], 2)
        self.assertEqual(_load(self.path)["notes"][0]["by"], 2)
        history = [json.loads(line) for line in (self.path.parent / "memory_history.jsonl").read_text().splitlines()]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["before"]["text"], "[待验证] 初稿")
        self.assertNotIn("初稿", self.path.read_text())

    # 合并后可删除冗余条目；已删除编号不复用，也不能被旧请求恢复。
    def test_merge_delete_and_no_id_reuse(self):
        first = update_note(self.path, 1, "", 0, "条件甲")
        second = update_note(self.path, 2, "", 0, "条件乙")
        self.assertTrue(update_note(self.path, 1, first["id"], 1, "条件甲；条件乙")["ok"])
        self.assertTrue(update_note(self.path, 2, second["id"], 1, "")["ok"])
        self.assertFalse(update_note(self.path, 2, second["id"], 1, "旧内容")["ok"])
        third = update_note(self.path, 3, "", 0, "新问题")
        self.assertNotEqual(third["id"], second["id"])
        self.assertEqual(len(_load(self.path)["notes"]), 2)

    # 超长、空新增或无效版本均不能悄悄改变文件。
    def test_invalid_updates_do_not_change_memory(self):
        before = self.path.read_text()
        cases = [("", 0, "x" * (MAX_NOTE_CHARS + 1)), ("", 0, " "), ("", -1, "内容"), ("", True, "内容")]
        for note_id, version, text in cases:
            with self.subTest(version=version, text=text):
                self.assertFalse(update_note(self.path, 1, note_id, version, text)["ok"])
                self.assertEqual(self.path.read_text(), before)

    # 条目数与正文长度超限时拒绝写入，保留原有内容，允许删减后继续。
    def test_size_budgets(self):
        for index in range(MAX_NOTES):
            self.assertTrue(update_note(self.path, 1, "", 0, f"结论{index}")["ok"])
        self.assertFalse(update_note(self.path, 1, "", 0, "第十一条")["ok"])
        for note in list(_load(self.path)["notes"]):
            self.assertTrue(update_note(self.path, 1, note["id"], note["v"], "")["ok"])
        for _ in range(MAX_TOTAL_CHARS // MAX_NOTE_CHARS):
            self.assertTrue(update_note(self.path, 1, "", 0, "x" * MAX_NOTE_CHARS)["ok"])
        remaining = MAX_TOTAL_CHARS % MAX_NOTE_CHARS
        self.assertTrue(update_note(self.path, 1, "", 0, "x" * remaining)["ok"])
        self.assertFalse(update_note(self.path, 1, "", 0, "超限")["ok"])
        self.assertEqual(sum(len(note["text"]) for note in _load(self.path)["notes"]), MAX_TOTAL_CHARS)

    # 启动真实 Python 子进程并行提交修改，仅运行本地文件函数。
    def parallel_updates(self, arguments):
        script = "import json,sys; from pathlib import Path; from shared_memory import update_note; print(json.dumps(update_note(Path(sys.argv[1]), **json.loads(sys.argv[2]))))"
        processes = [subprocess.Popen(
            [sys.executable, "-c", script, str(self.path), json.dumps(item)],
            cwd=model_runtime.PROJECT_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for item in arguments]
        try:
            results = []
            for process in processes:
                output, errors = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, errors)
                results.append(json.loads(output))
            return results
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    # 十个独立进程同时新增，十条记录全部保留且编号唯一。
    def test_ten_processes_do_not_lose_updates(self):
        results = self.parallel_updates([
            {"author": number, "note_id": "", "expected_version": 0, "text": f"成员{number}的结论"}
            for number in range(1, 11)
        ])
        self.assertTrue(all(result["ok"] for result in results))
        state = _load(self.path)
        self.assertEqual(state["rev"], 10)
        self.assertEqual(len({note["id"] for note in state["notes"]}), 10)
        self.assertEqual({note["by"] for note in state["notes"]}, set(range(1, 11)))

    # 两进程拿着同一旧版本修改时，只能一个成功，另一个明确收到冲突。
    def test_competing_revisions_do_not_overwrite(self):
        first = update_note(self.path, 1, "", 0, "初始内容")
        results = self.parallel_updates([
            {"author": number, "note_id": first["id"], "expected_version": 1, "text": f"成员{number}修改"}
            for number in (1, 2)
        ])
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.assertEqual(_load(self.path)["notes"][0]["v"], 2)

    # 原子替换前失败时保留原文件，避免截断共享笔记。
    def test_failed_replace_keeps_previous_memory(self):
        before = self.path.read_text()
        with patch.object(Path, "replace", side_effect=OSError("模拟写盘失败")):
            with self.assertRaises(OSError):
                update_note(self.path, 1, "", 0, "新内容")
        self.assertEqual(self.path.read_text(), before)


class McpTests(unittest.IsolatedAsyncioTestCase):
    # 使用真实 MCP stdio 进程初始化、发现工具并写文件；没有模型参与。
    async def test_real_mcp_process_writes_and_binds_author(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryFile(mode="w+") as errors:
            path = create_memory(Path(folder))
            parameters = StdioServerParameters(command=sys.executable, args=[str(model_runtime.PROJECT_DIR / "memory_tool.py"), str(path), "2"])
            async with stdio_client(parameters, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({tool.name for tool in tools.tools}, {"update_note", *FILE_TOOL_NAMES})
                    result = await session.call_tool("update_note", {"note_id": "", "expected_version": 0, "text": "[待验证] MCP 中途写入"})
                    self.assertFalse(result.isError)
                    self.assertEqual(_load(path)["notes"][0]["by"], 2)
                    self.assertIn("MCP 中途写入", path.read_text())



if __name__ == "__main__":
    unittest.main()
