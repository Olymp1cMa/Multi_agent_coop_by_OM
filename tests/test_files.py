"""验证本地文件读写、PDF生成及真实MCP接入，不连接模型。"""

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from file_tools import FileTools, output_dir_for
from memory_tool import create_server
from model_runtime import PROJECT_DIR
from shared_memory import create_memory


class FileTests(unittest.TestCase):
    # 所有单元测试读写临时目录，不碰用户已有文件。
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.output = self.root / "output"
        self.files = FileTools(self.root, self.output, self.root / "file_history.jsonl", 2)

    # 中文文本按字符偏移连续读取，长行不会被丢弃或误截为全文。
    def test_text_reading_and_pagination(self):
        text = "中文UTF-8文本与Python代码print(123)"
        (self.root / "input.py").write_text(text, encoding="utf-8")
        first = self.files.read_file("input.py", max_chars=5)
        second = self.files.read_file(str(self.root / "input.py"), offset=first["next_offset"])
        self.assertEqual(first["text"] + second["text"], text)
        self.assertIsNone(second["next_offset"])
        self.assertEqual(first["total_chars"], len(text))

    # 列目录只列一层，分页可以继续，不读取子目录中的私人内容。
    def test_directory_pagination(self):
        (self.root / "a.txt").write_text("a")
        (self.root / "b").mkdir()
        first = self.files.list_files(str(self.root), limit=1)
        self.assertEqual(first["entries"], [{"name": "a.txt", "is_dir": False}])
        second = self.files.list_files(str(self.root), offset=first["next_offset"])
        self.assertIn({"name": "b", "is_dir": True}, second["entries"])
        with self.assertRaises(ValueError):
            self.files.list_files(str(self.root), limit=0)

    # 错误路径、二进制和过大文件明确失败，不假称已读取。
    def test_invalid_inputs_and_size_limits(self):
        (self.root / "binary").write_bytes(b"\x00\x00")
        (self.root / "invalid").write_bytes(b"\xff")
        (self.root / "large").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        for name in ("missing", "binary", "invalid", "large"):
            with self.subTest(name=name), self.assertRaises((ValueError, UnicodeError)):
                self.files.read_file(name)
        with self.assertRaises(ValueError):
            self.files.read_file("binary", max_chars=8001)

    # 保存代码不会执行，操作记录只包含路径与大小，不重复保存文件正文。
    def test_write_code_and_audit(self):
        content = 'raise RuntimeError("只生成，不运行")\n'
        result = self.files.write_file("demo/code.py", content)
        self.assertEqual(Path(result["path"]).read_text(), content)
        history = [json.loads(line) for line in self.files.log_path.read_text().splitlines()]
        self.assertEqual(history[0]["by"], 2)
        self.assertEqual(history[0]["tool"], "write_file")
        self.assertNotIn("content", history[0])
        self.assertNotIn("text", history[0])

    # 普通写入、PDF写入和重复任务都不能覆盖已有文件。
    def test_existing_file_is_preserved(self):
        result = self.files.write_file("keep.txt", "原文")
        with self.assertRaises(ValueError):
            self.files.write_file("keep.txt", "新文")
        self.assertEqual(Path(result["path"]).read_text(), "原文")
        self.files.create_pdf("keep.pdf", "原文", "保留内容")
        before = (self.output / "keep.pdf").read_bytes()
        with self.assertRaises(ValueError):
            self.files.create_pdf("keep.pdf", "新文", "新内容")
        self.assertEqual((self.output / "keep.pdf").read_bytes(), before)

    # 两次运行使用独立时间戳目录，可以生成同名代码和PDF而不会互相覆盖。
    def test_separate_runs_can_use_same_filenames(self):
        with patch("file_tools.OUTPUT_DIR", self.output):
            first_memory = create_memory(self.root)
            second_memory = create_memory(self.root)
            paths = [output_dir_for(memory) for memory in (first_memory, second_memory)]
            self.assertNotEqual(paths[0], paths[1])
            self.assertFalse(self.output.exists())
            for memory, path, text in zip((first_memory, second_memory), paths, ("第一轮", "第二轮")):
                files = FileTools(self.root, path, memory.parent / "file_history.jsonl", 1)
                self.assertFalse(path.exists())
                files.write_file("result.py", f"# {text}\n")
                files.create_pdf("report.pdf", "本轮报告", text)
            self.assertEqual((paths[0] / "result.py").read_text(), "# 第一轮\n")
            self.assertEqual((paths[1] / "result.py").read_text(), "# 第二轮\n")
            self.assertEqual(paths[0].name, first_memory.parent.name)
            self.assertEqual(paths[1].name, second_memory.parent.name)
            self.assertFalse((self.output / "result.py").exists())

    # 相对路径逃逸、绝对目录外路径和符号链接都不能绕过输出目录。
    def test_output_stays_in_selected_folder(self):
        self.output.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.output / "link").symlink_to(outside, target_is_directory=True)
        for name in ("../escape.py", str(outside / "escape.py"), "link/escape.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.files.write_file(name, "不可越界")
        self.assertEqual(list(outside.iterdir()), [])

    # 两个成员同时生成同名文件时只能一方成功，不混合或覆盖内容。
    def test_concurrent_same_filename(self):
        # 模拟模型遇到同名冲突时收到明确失败。
        def save(text):
            try:
                self.files.write_file("same.txt", text)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ("成员A", "成员B")))
        self.assertEqual(sum(results), 1)
        self.assertIn((self.output / "same.txt").read_text(), ("成员A", "成员B"))

    # 生成的中文PDF能被真实解析，标题、转义符号及字符分页均正确。
    def test_chinese_pdf_round_trip(self):
        result = self.files.create_pdf("中文.pdf", "中文测试报告", "第一段：代码与共享记忆。\n条件：A < B & C > D。")
        self.assertTrue(Path(result["path"]).read_bytes().startswith(b"%PDF-"))
        first = self.files.read_pdf(result["path"], max_chars=4)
        rest = self.files.read_pdf(result["path"], offset=first["next_offset"])
        text = first["text"] + rest["text"]
        self.assertIn("中文测试报告", text)
        self.assertIn("A < B & C > D", text)
        with self.assertRaises(ValueError):
            self.files.read_pdf(result["path"], page=999)

    # 较长正文自动分页，工具返回正确的后续页码，不把第一页当作全文。
    def test_pdf_multiple_pages(self):
        content = "\n\n".join(f"第{i}段：中文排版与分页检查。" for i in range(100))
        result = self.files.create_pdf("pages.pdf", "多页测试", content)
        first = self.files.read_pdf(result["path"], max_chars=8000)
        self.assertGreater(first["total_pages"], 1)
        self.assertEqual(first["next_page"], 2)

    # PDF必须经排版生成，不接受伪PDF或无效正文。
    def test_pdf_arguments_are_checked(self):
        for name, title, content in (("wrong.txt", "标题", "正文"), ("empty.pdf", "", "正文"), ("empty.pdf", "标题", "")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.files.create_pdf(name, title, content)
        with self.assertRaises(ValueError):
            self.files.write_file("fake.pdf", "这不是PDF")


class FileMcpTests(unittest.IsolatedAsyncioTestCase):
    # 未传覆盖参数时，MCP服务也必须绑定当前运行的时间戳目录。
    async def test_default_server_uses_timestamped_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            memory = create_memory(root)
            with patch("file_tools.OUTPUT_DIR", root / "desktop"):
                server = create_server(memory, 2)
                expected = output_dir_for(memory) / "same.txt"
                await server.call_tool("write_file", {"filename": "same.txt", "content": "本轮内容"})
                self.assertEqual(expected.read_text(), "本轮内容")
                self.assertFalse((root / "desktop" / "same.txt").exists())

    # 通过真实本地MCP进程生成并读回代码/PDF，所有输出仍在临时目录。
    async def test_real_mcp_file_tools(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryFile(mode="w+") as errors:
            root = Path(folder)
            memory = create_memory(root)
            output = root / "output"
            params = StdioServerParameters(command=sys.executable, args=[str(PROJECT_DIR / "memory_tool.py"), str(memory), "3", "--output-dir", str(output)])
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    saved = await session.call_tool("write_file", {"filename": "hello.py", "content": "print('你好')\n"})
                    self.assertFalse(saved.isError)
                    result = await session.call_tool("read_file", {"path": str(output / "hello.py")})
                    self.assertFalse(result.isError)
                    self.assertIn("你好", json.loads(result.content[0].text)["text"])
                    pdf = await session.call_tool("create_pdf", {"filename": "hello.pdf", "title": "工具实测", "content": "中文PDF已生成。"})
                    self.assertFalse(pdf.isError)
                    reread = await session.call_tool("read_pdf", {"path": str(output / "hello.pdf")})
                    self.assertFalse(reread.isError)
                    self.assertIn("中文PDF已生成", json.loads(reread.content[0].text)["text"])
                    conflict = await session.call_tool("write_file", {"filename": "hello.py", "content": "覆盖"})
                    self.assertTrue(conflict.isError)
            history = [json.loads(line) for line in (memory.parent / "file_history.jsonl").read_text().splitlines()]
            self.assertTrue(all(row["by"] == 3 for row in history))


if __name__ == "__main__":
    unittest.main()
