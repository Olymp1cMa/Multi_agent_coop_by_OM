# Multi_agent_coop

一个简单的多智能体协作终端。  
A simple terminal application for multi-agent collaboration.

## 功能 / Features

- 智能体数量可输入任意正整数；共用一次选择的模型，各自拥有独立会话。  
  Choose any positive agent count; agents share one model selection and keep separate conversations.
- 自动由成员1分工，全员并行执行，最后统一审查汇总。  
  Agent 1 assigns roles and tasks, everyone works in parallel, and agent 1 reviews and summarizes.
- 共享简短工作笔记，保存运行记录。  
  Share compact working notes and save run logs.
- 读取本地文本、代码和文字型PDF，在桌面生成代码、文本或中文PDF。  
  Read local text, code, and text-based PDFs; create code, text, or Chinese PDFs on the desktop.

## 用法 / Usage

需要Mac或Linux、Python 3.10以上，以及本机已登录的Codex ChatGPT账号。终端和默认提示词目前为中文。  
Requires macOS or Linux, Python 3.10+, and a local Codex login using your ChatGPT account. The terminal and default prompts are currently in Chinese.

**Mac：双击 `start.command`，首次运行自动安装依赖，需要联网。**  
**macOS: double-click `start.command`. The first launch installs dependencies and requires internet access.**

也可在项目目录运行 / Or run these commands in the project directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

选择人数 → 输入任务 → 选择共用模型。每次运行完成一轮协作后退出。  
Choose the agent count → enter a task → select the shared model. Each run completes one collaboration round, then exits.

示例任务 / Example task:

> 读取 examples/file_input.txt，生成求和脚本 my_sum.py 和中文报告 my_sum_report.pdf。

> Read examples/file_input.txt and create a sum script named my_sum.py and a Chinese report named my_sum_report.pdf.

每次运行的生成文件独立保存在 `~/Desktop/agent_output/时间戳/`，例如 `20261025_223000_123456/`；同一轮共用目录，同名文件不覆盖，代码不自动运行。日志位于 `runs/`。  
Each run saves files in its own `~/Desktop/agent_output/<timestamp>/` folder, such as `20261025_223000_123456/`. Agents in the same run share that folder. Existing files are not overwritten, and generated code is not executed. Logs go to `runs/`.

输入 `exit` 或按 Ctrl+C 退出。运行会消耗当前账号对应的Codex额度。  
Enter `exit` or press Ctrl+C to stop. Model calls use the Codex allowance associated with your current account.

实际可同时运行的人数仍受电脑资源及账号并发和额度限制。  
Practical concurrency is still limited by your computer resources and account limits.
