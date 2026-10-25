"""模型配置与程序入口测试，不连接真实模型。"""

import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai_codex.generated.v2_all import Model, ModelListResponse, ReasoningEffort, ReasoningEffortOption

import main
import model_runtime
from file_tools import FILE_TOOL_NAMES, output_dir_for


# 构造本地模型目录，测试时不查询真实服务。
def model(name, effort=ReasoningEffort.low):
    return Model(
        id=name, model=name, display_name=name, description="test",
        hidden=False, is_default=False, default_reasoning_effort=effort,
        supported_reasoning_efforts=[ReasoningEffortOption(reasoning_effort=effort, description="test")],
    )


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    # 模型只选择一次，所有成员共用；修改一个配置不影响其他成员。
    async def test_select_shared_model(self):
        entries = [model("model-a"), model("model-b")]
        codex = SimpleNamespace(models=AsyncMock(return_value=ModelListResponse(data=entries)))
        with patch("builtins.input", side_effect=["0", "abc", "2"]) as inputs, contextlib.redirect_stdout(io.StringIO()):
            selected = await model_runtime.configure_agents(codex, 25)
        self.assertEqual([entry["model"].model for entry in selected], ["model-b"] * 25)
        self.assertEqual([entry["role"] for entry in selected], [""] * 25)
        self.assertEqual(inputs.call_count, 3)
        selected[0]["role"] = "统筹要求"
        self.assertTrue(all(entry["role"] == "" for entry in selected[1:]))

    # 同一个模型仍创建独立会话，每个工具绑定正确的文件和成员编号。
    async def test_create_independent_threads_and_supported_effort(self):
        first, second = object(), object()
        codex = SimpleNamespace(thread_start=AsyncMock(side_effect=[first, second]))
        entry = model("same-model", ReasoningEffort.medium)
        configurations = [{"model": entry, "role": "提出方案"}, {"model": entry, "role": "检查漏洞"}]
        items = await model_runtime.create_agents(codex, configurations, "通用要求", Path("/tmp/example_shared.md"))
        self.assertIs(items[0]["thread"], first)
        self.assertIs(items[1]["thread"], second)
        self.assertEqual([item["effort"] for item in items], ["medium", "medium"])
        self.assertEqual(codex.thread_start.await_count, 2)
        calls = codex.thread_start.call_args_list
        self.assertTrue(all(call.kwargs["model"] == "same-model" for call in calls))
        self.assertTrue(all(call.kwargs["config"]["agents.enabled"] is False for call in calls))
        self.assertEqual([call.kwargs["developer_instructions"] for call in calls],
                         ["通用要求\n\n本会话的角色要求：\n提出方案", "通用要求\n\n本会话的角色要求：\n检查漏洞"])
        tools = [call.kwargs["config"]["mcp_servers.shared_notes"] for call in calls]
        self.assertEqual([config["args"][2] for config in tools], ["1", "2"])
        self.assertTrue(all(config["args"][1] == "/tmp/example_shared.md" for config in tools))
        self.assertTrue(all(config["args"][3:] == ["--output-dir", str(output_dir_for(Path("/tmp/example_shared.md")))] for config in tools))
        self.assertTrue(all(config["enabled_tools"] == ["update_note", *FILE_TOOL_NAMES] and config["required"] for config in tools))

    # 退出数量选择时，不启动协作模块。
    async def test_exit_does_not_open_sdk(self):
        with patch.object(main, "run_collaboration") as runner, patch("builtins.input", return_value="exit"):
            main.main()
        runner.assert_not_called()

    # 主入口只校验人数，再将数量交给协作模块。
    async def test_main_passes_selected_count(self):
        for choice in map(str, (1, 2, 10, 11, 25, 100, 10000)):
            with self.subTest(choice=choice), patch.object(main, "run_collaboration") as runner:
                with patch("builtins.input", side_effect=["abc", "0", "-1", "1.5", choice]), contextlib.redirect_stdout(io.StringIO()):
                    main.main()
                runner.assert_called_once_with(int(choice))

    # 模型目录为空或用户退出时，不创建会话。
    async def test_empty_catalog_and_cancel_selection(self):
        codex = SimpleNamespace(models=AsyncMock(return_value=ModelListResponse(data=[])))
        with self.assertRaisesRegex(RuntimeError, "模型"):
            await model_runtime.configure_agents(codex, 1)
        codex.models.return_value = ModelListResponse(data=[model("model-a")])
        with patch("builtins.input", return_value="exit"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await model_runtime.configure_agents(codex, 2), [])


if __name__ == "__main__":
    unittest.main()
