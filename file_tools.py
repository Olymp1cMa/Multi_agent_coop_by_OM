"""本地文件工具：读取文本/PDF，向桌面输出目录生成新文件。"""

import fcntl
import json
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


OUTPUT_DIR = Path.home() / "Desktop" / "agent_output"
FILE_TOOL_NAMES = ["list_files", "read_file", "read_pdf", "write_file", "create_pdf"]
MAX_READ_CHARS = 8000
MAX_WRITE_CHARS = 200000


# 复用本次runs目录的时间戳，保证一轮共用目录、不同轮次分开存放。
def output_dir_for(memory_path):
    return OUTPUT_DIR / Path(memory_path).parent.name


class FileTools:
    # 绑定项目路径、输出目录和本成员的操作记录；初始化不会创建输出文件。
    def __init__(self, project_dir, output_dir, log_path, author):
        self.project_dir = Path(project_dir).resolve()
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.log_path = Path(log_path)
        self.author = author

    # 输入路径支持绝对路径、~ 和相对于项目目录的路径。
    def _input_path(self, path):
        result = Path(path).expanduser()
        return (result if result.is_absolute() else self.project_dir / result).resolve()

    # 输出只能位于指定桌面目录内，拒绝 ../ 和指向目录外的符号链接。
    def _output_path(self, filename):
        if not filename.strip():
            raise ValueError("文件名不能为空。")
        path = Path(filename).expanduser()
        path = (path if path.is_absolute() else self.output_dir / path).resolve()
        if path == self.output_dir or not path.is_relative_to(self.output_dir):
            raise ValueError(f"只能在 {self.output_dir} 内生成文件。")
        return path

    # 记录成功操作的路径和大小，不把整份读取内容再复制到操作日志中。
    def _record(self, tool, result):
        entry = {"time": datetime.now().astimezone().isoformat(), "by": self.author,
                 "tool": tool, **{k: v for k, v in result.items() if k not in {"text", "entries"}}}
        try:
            with self.log_path.open("a", encoding="utf-8") as file:
                fcntl.flock(file, fcntl.LOCK_EX)
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as error:
            result["warning"] = f"操作已完成，但操作日志保存失败：{error}"
        return result

    # 先完成内容生成，再独占创建目标文件；同名文件和并发写入都不会被覆盖。
    def _save(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as file:
                try:
                    file.write(data)
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise
        except FileExistsError as error:
            raise ValueError("文件已存在，请选择新文件名，不会覆盖原文件。") from error
        return {"ok": True, "path": str(path), "bytes": len(data)}

    # 列出指定文件夹的一层内容，按偏移分页，避免把整个目录树交给模型。
    def list_files(self, path: str, offset: int = 0, limit: int = 50) -> dict:
        """列出指定本地目录，不递归；支持~、绝对路径和项目相对路径。offset从0开始，limit为1-100。"""
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset须非负，limit须为1-100。")
        folder = self._input_path(path)
        entries = sorted(folder.iterdir(), key=lambda item: item.name.casefold())
        selected = entries[offset:offset + limit]
        result = {"ok": True, "path": str(folder), "total": len(entries),
                  "entries": [{"name": p.name, "is_dir": p.is_dir()} for p in selected],
                  "next_offset": offset + len(selected) if offset + len(selected) < len(entries) else None}
        return self._record("list_files", result)

    # 分段读取UTF-8文本或代码，返回下一段字符位置，避免丢失长行内容。
    def read_file(self, path: str, offset: int = 0, max_chars: int = 4000) -> dict:
        """读取UTF-8文本/代码，最多2MB；offset为字符偏移，max_chars为1-8000，PDF请用read_pdf。"""
        if offset < 0 or not 1 <= max_chars <= MAX_READ_CHARS:
            raise ValueError("offset须非负，max_chars须为1-8000。")
        source = self._input_path(path)
        if not source.is_file() or source.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("需要一个不超过2MB的普通文本文件。")
        if source.suffix.lower() == ".pdf":
            raise ValueError("读取PDF请使用read_pdf。")
        text = source.read_text(encoding="utf-8")
        if "\x00" in text:
            raise ValueError("不是可读取的文本文件。")
        end = min(offset + max_chars, len(text))
        return self._record("read_file", {"ok": True, "path": str(source), "text": text[offset:end],
                            "total_chars": len(text), "next_offset": end if end < len(text) else None})

    # 按页、按字符分段提取PDF文本；扫描图片不做OCR。
    def read_pdf(self, path: str, page: int = 1, offset: int = 0, max_chars: int = 4000) -> dict:
        """提取不超过20MB的PDF文字；page从1开始，offset为本页字符位置，max_chars为1-8000。不支持OCR。"""
        if page < 1 or offset < 0 or not 1 <= max_chars <= MAX_READ_CHARS:
            raise ValueError("page须为正整数，offset须非负，max_chars须为1-8000。")
        source = self._input_path(path)
        if not source.is_file() or source.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("需要一个不超过20MB的PDF文件。")
        with source.open("rb") as file:
            reader = PdfReader(file)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("暂不支持需要密码的PDF。")
            total = len(reader.pages)
            if page > total:
                raise ValueError(f"页码超出范围，文件共{total}页。")
            text = reader.pages[page - 1].extract_text() or ""
        end = min(offset + max_chars, len(text))
        result = {"ok": True, "path": str(source), "page": page, "total_pages": total,
                  "text": text[offset:end], "next_offset": end if end < len(text) else None,
                  "next_page": page + 1 if end == len(text) and page < total else None}
        if not text.strip():
            result["warning"] = "本页没有可提取文字，可能是扫描图片；当前不支持OCR。"
        return self._record("read_pdf", result)

    # 将文本保存为一个新文件，只保存不运行，拒绝伪装为PDF的纯文本。
    def write_file(self, filename: str, content: str) -> dict:
        """在本轮带时间戳的桌面目录中创建UTF-8代码/文本文件，不覆盖。filename请用相对文件名，可含子目录；正文最多20万字符。"""
        if len(content) > MAX_WRITE_CHARS:
            raise ValueError("正文最多20万字符，请分成多个文件。")
        path = self._output_path(filename)
        if path.suffix.lower() == ".pdf":
            raise ValueError("生成PDF请使用create_pdf，不能把文本直接命名为PDF。")
        return self._record("write_file", self._save(path, content.encode("utf-8")))

    # 把标题和纯文本排成中文PDF，先在内存中完成排版，再保存为新文件。
    def create_pdf(self, filename: str, title: str, content: str) -> dict:
        """在本轮带时间戳的桌面目录中创建中文PDF，不覆盖。filename请用相对文件名并以.pdf结尾；标题最多200字符，正文最多20万字符。"""
        if not title.strip() or len(title) > 200 or not content.strip() or len(content) > MAX_WRITE_CHARS:
            raise ValueError("标题须为1-200字符，正文须为1-20万字符。")
        path = self._output_path(filename)
        if path.suffix.lower() != ".pdf":
            raise ValueError("PDF文件名必须以.pdf结尾。")
        font = "STSong-Light"
        pdfmetrics.registerFont(UnicodeCIDFont(font))
        title_style = ParagraphStyle("Title", fontName=font, fontSize=18, leading=26,
                                     spaceAfter=18, textColor=colors.HexColor("#17324d"), wordWrap="CJK")
        body_style = ParagraphStyle("Body", fontName=font, fontSize=11, leading=18,
                                    spaceAfter=9, wordWrap="CJK")
        story = [Paragraph(escape(title), title_style)]
        for block in content.replace("\r\n", "\n").split("\n\n"):
            if block.strip():
                story.append(Paragraph(escape(block.expandtabs(4)).replace("\n", "<br/>"), body_style))
            else:
                story.append(Spacer(1, 9))

        # 每页右下角输出页码，不把页码混入正文排版。
        def footer(canvas, document):
            canvas.setFont(font, 9)
            canvas.drawRightString(A4[0] - 48, 28, str(document.page))

        buffer = BytesIO()
        document = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=48, leftMargin=48,
                                     topMargin=48, bottomMargin=48, title=title)
        document.build(story, onFirstPage=footer, onLaterPages=footer)
        return self._record("create_pdf", self._save(path, buffer.getvalue()))
