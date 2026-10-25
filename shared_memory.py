"""紧凑共享工作笔记：进程锁防覆盖，条目版本防止过期修改。"""

import fcntl
import json
from datetime import datetime


MAX_NOTES = 10
MAX_NOTE_CHARS = 120
MAX_TOTAL_CHARS = 800
PREFIX = "# shared\n```json\n"
SUFFIX = "\n```\n"


# 把当前状态原子替换进 .md；读取者只会看到完整的旧版或新版。
def _save(path, state):
    text = PREFIX + json.dumps(state, ensure_ascii=False, separators=(",", ":")) + SUFFIX
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


# 解析由程序维护的 .md 格式；格式异常时停止写入，保留原文件。
def _load(path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith(PREFIX) or not text.endswith(SUFFIX):
        raise ValueError("共享笔记格式异常，请勿直接改变文件结构。")
    state = json.loads(text[len(PREFIX):-len(SUFFIX)])
    if not isinstance(state, dict) or set(state) != {"rev", "next_id", "notes"} or not isinstance(state["notes"], list):
        raise ValueError("共享笔记状态字段异常。")
    return state


# 每次组建团队创建新文档，保留旧团队的记录。
def create_memory(project_dir):
    folder = project_dir / "runs" / datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True)
    path = folder / "shared.md"
    _save(path, {"rev": 0, "next_id": 1, "notes": []})
    return path


# 立即新增、修订或删除一条笔记；作者由工具进程绑定，不由模型填写。
def update_note(path, author, note_id, expected_version, text):
    if not isinstance(note_id, str) or not isinstance(text, str) or type(expected_version) is not int or expected_version < 0:
        return {"ok": False, "error": "id/text 必须为文本，expected_version 必须为非负整数。"}
    text = text.strip()
    if len(text) > MAX_NOTE_CHARS:
        return {"ok": False, "error": f"每条最多 {MAX_NOTE_CHARS} 字符，请精简后再提交，不能截断关键条件。"}
    # 各智能体的 MCP 工具是独立进程，必须使用跨进程锁。
    with path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _load(path)
        old = next((note for note in state["notes"] if note["id"] == note_id), None)
        if not note_id:
            if expected_version != 0 or not text:
                return {"ok": False, "error": "新增笔记使用空 id、expected_version=0 和非空 text。"}
            if len(state["notes"]) >= MAX_NOTES:
                return {"ok": False, "error": f"最多 {MAX_NOTES} 条，请先合并或移除过时条目。"}
            note_id = f"N{state['next_id']}"
            state["next_id"] += 1
        elif old is None:
            return {"ok": False, "error": "条目不存在或已删除；不要用旧内容重新覆盖。"}
        elif old["v"] != expected_version:
            return {"ok": False, "error": "版本冲突；请核对 current，再决定是否重新提交。", "current": old}

        new = {"id": note_id, "v": old["v"] + 1 if old else 1, "by": author, "text": text} if text else None
        state["notes"] = [note for note in state["notes"] if note["id"] != note_id]
        if new:
            state["notes"].append(new)
        if sum(len(note["text"]) for note in state["notes"]) > MAX_TOTAL_CHARS:
            return {"ok": False, "error": f"笔记正文总计最多 {MAX_TOTAL_CHARS} 字符，请先精简已有条目。"}
        state["rev"] += 1
        _save(path, state)
        result = {"ok": True, "id": note_id, "v": new["v"] if new else None, "rev": state["rev"]}
        # 历史单独存盘，不发送给模型；日志失败也不能谎称已经完成的写入没有发生。
        try:
            event = {"time": datetime.now().astimezone().isoformat(), "by": author, "before": old, "after": new, "rev": state["rev"]}
            with (path.parent / "memory_history.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as error:
            result["warning"] = f"笔记已保存，但历史日志保存失败：{error}"
        return result
