import json
import os
import sys
import asyncio
import logging
from datetime import datetime
from typing import AsyncGenerator

# 引入 ADK 核心组件
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.tool_context import ToolContext
from google.adk.agents import LlmAgent, SequentialAgent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from dotenv import load_dotenv

# --- 导入 agent_tools.py 中的工具 ---
from agent_tools import (
    get_next_error_log,
    mark_log_as_processed_by_rename,
    parse_error_log,
    find_sha_for_timestamp,
    find_fix_date,
    checkout_oss_fuzz_commit,
    extract_build_metadata_from_log,
    get_project_yaml_info,
    patch_project_dockerfile,
    reset_project_environment,
    run_fuzz_build_streaming,
    compare_error_logs,
    download_github_repo,
    checkout_project_commit,
    append_to_reproduce_report
)


# --- Logger 实现 (保留目前代码中优秀的日志记录机制) ---
class AgentLogger:
    def init(self, log_directory: str = "agent_logs"):
        self.log_directory = log_directory
        self.logger = None
        self.file_handler_setup = False
        self.log_buffer = []
        self.project_name = "orchestrator"
        os.makedirs(self.log_directory, exist_ok=True)

    def set_project_context(self, project_name: str):
        if self.logger:
            for handler in self.logger.handlers[:]:
                handler.close()
                self.logger.removeHandler(handler)
        self.project_name = project_name
        self.file_handler_setup = False
        self.setup_file_handler()

    def setup_file_handler(self):
        if self.file_handler_setup:
            return
        safe_project_name = "".join(c for c in self.project_name if c.isalnum() or c in ('_', '-')).rstrip()
        timestamp = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
        log_filename = f"{safe_project_name}_run_{timestamp}.log"
        log_filepath = os.path.join(self.log_directory, log_filename)

        self.logger = logging.getLogger(f"AgentLogger_{safe_project_name}_{timestamp}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        file_handler = logging.FileHandler(log_filepath, encoding='utf-8')
        formatter = logging.Formatter('%(message)s')
        file_handler.setFormatter(formatter)

        if not self.logger.handlers:
            self.logger.addHandler(file_handler)

        print(f"✅ Log file created: {log_filepath}")

        for log_entry in self.log_buffer:
            self.logger.info(log_entry)
        self.log_buffer = []
        self.file_handler_setup = True

    def log_event(self, event: Event):
        log_message = self._format_message(event)
        if log_message:
            print(log_message)
            if self.file_handler_setup:
                self.logger.info(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {log_message}")
            else:
                self.log_buffer.append(f"INFO - {log_message}")

    def _format_message(self, event: Event) -> str:
        author = event.author
        log_parts = [f"EVENT from author: '{author}'"]
        if hasattr(event, 'get_function_calls') and (func_calls := event.get_function_calls()):
            for call in func_calls:
                log_parts.append(f"  - TOOL_CALL: {call.name}({json.dumps(call.args, ensure_ascii=False)})")
        if hasattr(event, 'get_function_responses') and (func_resps := event.get_function_responses()):
            for resp in func_resps:
                response_str = str(resp.response)
                response_str = response_str[:500] + "..." if len(response_str) > 500 else response_str
                log_parts.append(f"  - TOOL_RESPONSE for '{resp.name}': {response_str}")
        if (actions := event.actions):
            if actions.state_delta:
                log_parts.append(f"  - STATE_UPDATE: {actions.state_delta}")
            if actions.escalate:
                log_parts.append("  - ACTION: Escalate (Agent Finish)")
        return "\n".join(log_parts)


class LoggingWrapperAgent(BaseAgent):
    name: str = "LoggingWrapperAgent"
    subject_agent: BaseAgent

    async def _run_async_impl(self, context: InvocationContext) -> AsyncGenerator[Event, None]:
        try:
            async for event in self.subject_agent.run_async(context):
                GLOBAL_LOGGER.log_event(event)
                yield event
        except (Exception, KeyboardInterrupt) as e:
            print(f"\n--- Interruption or error detected: {type(e).__name__} ---")
            raise e
        finally:
            if not GLOBAL_LOGGER.file_handler_setup:
                GLOBAL_LOGGER.setup_file_handler()


GLOBAL_LOGGER = AgentLogger()

# --- 全局处理模式控制 ---
MODE_UNPROCESSED = "unprocessed_logs"
MODE_TIMED_OUT = "timed_out_logs"
CURRENT_PROCESSING_MODE = MODE_UNPROCESSED

# 获取当前脚本所在的目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv()
DPSEEK_API_KEY = os.getenv("DPSEEK_API_KEY")
MODEL = "deepseek/deepseek-coder"
COMMITS_FILE_PATH = os.path.join(BASE_DIR, "commits_obtain_oss_fuzz", "github_commits.txt")
APP_NAME = "oss_fuzz_reproduce_app"

# 设置子目录路径
OSS_FUZZ_PATH = os.path.join(BASE_DIR, "oss-fuzz")
LOGS_DIRECTORY = os.path.join(BASE_DIR, "sample")

# --- Agent 定义 ---

# 1. Build Fuzzer Agent (移除了 exit_loop 与 get_next_error_log，专职接收指定路径)
build_fuzzer_agent = LlmAgent(
    name="build_fuzzer_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=4096),
    instruction=f"""
    [SYSTEM CONFIGURATION]
    OSS_FUZZ_PATH = "{OSS_FUZZ_PATH}"
    LOGS_DIRECTORY = "{LOGS_DIRECTORY}"
    CURRENT_PROCESSING_MODE = "{CURRENT_PROCESSING_MODE}"

    你的任务是使用**本地挂载模式**精确复现指定的 OSS-Fuzz 项目构建错误。

    1.  **确认并解析日志**: 仔细确认用户在初始消息中指定的待复现日志绝对路径 `log_path`。你不需要调用任何工具获取日志，直接基于该路径开始下一步。
    2.  **提取元数据**: 调用 `extract_build_metadata_from_log` 处理该日志文件，获得主项目仓库 `software_repo_url` 与 `software_sha` 等。
    3.  **解析日志路径**: 调用 `parse_error_log` 获取项目名、错误日期和上次成功日期。
    4.  **获取OSS-Fuzz版本**: 调用 `find_sha_for_timestamp`。**必须使用文件路径 `{COMMITS_FILE_PATH}`** 和步骤3中的日期。
    5.  **获取项目语言**: 调用 `get_project_yaml_info`，传入项目名。
    6.  **切换OSS-Fuzz环境**: 调用 `checkout_oss_fuzz_commit`，传入步骤4获取的 SHA。
    7.  **克隆第三方源码到本地**: 调用 `download_github_repo` 工具：
        - `project_name` 传入步骤3解析出的项目名。
        - `target_dir` (强制规范) 必须传入相对路径形式的 `"./process/project/"` 后面跟项目名（例如 `"./process/project/mosquitto"`）。
        - `repo_url` 传入步骤2得到的 `software_repo_url` [1]。
    8.  **对齐本地源码版本**: 调用 `checkout_project_commit` 工具：
        - `project_source_path` 传入步骤7克隆的本地路径。
        - `sha` 传入步骤2得到的 `software_sha`。
    9.  **锁定复现环境 (Patch)**: 调用 `patch_project_dockerfile`。传入步骤2提取的 `base_image_digest` 和 `dependencies`。
    10. **执行本地挂载构建**: 调用 `run_fuzz_build_streaming`。
        - 必须使用步骤2提取的 Engine, Sanitizer, Architecture。
        - **关键：必须传入 `mount_path` 参数，值为步骤7中你克隆第三方源码的目标本地路径。**
        - **关键：必须将本轮任务指定的 `log_path` 作为 `original_log_path` 参数传入。**
        - 设置 `timeout` 为 7200。
    11. **重置环境并自动清理第三方源码**: 无论构建成功、失败还是超时，必须调用 `reset_project_environment`。该步骤会自动清退本地克隆的第三方项目源码仓库。

    **绝对禁令（IMPORTANT）：**
    - 每轮对话你**只能处理指定的这一个**日志文件。
    - 如果构建工具返回 "status": "timeout"，你仍然需要执行重置环境，并输出 JSON，但可以在 JSON 中注明超时。
    - 完成步骤 11 后，必须立即输出 JSON 字符串并结束回合。


    **输出规范**:
    将收集到的信息整合为一个 JSON 字符串输出，必须包含以下字段供后续 Agent 使用：
    - `log_path`: 原始错误日志的绝对路径
    - `project_name`: 项目名
    - `error_date`: 报错日期
    - `last_success_time`: 步骤3解析出的上次成功日期
    - `oss_fuzz_sha`: 步骤4获取的 SHA
    - `language`: 项目语言
    - `metadata`: 步骤2提取的所有元数据 (含 log_url, software_repo_url, software_sha, engine, sanitizer, architecture, base_image_digest, dependencies)
    - `new_build_log_path`: 步骤10生成的复现构建日志路径
    """.replace("{COMMITS_FILE_PATH}", COMMITS_FILE_PATH),
    tools=[
        parse_error_log,
        extract_build_metadata_from_log,
        find_sha_for_timestamp,
        get_project_yaml_info,
        checkout_oss_fuzz_commit,
        download_github_repo,
        checkout_project_commit,
        patch_project_dockerfile,
        run_fuzz_build_streaming,
        reset_project_environment
    ],
    output_key="build_context",
)

# 2. Classify & Report Agent (强化了分类逻辑和报告过滤)
classify_agent = LlmAgent(
    name="classify_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=8192),
    instruction="""
    [SYSTEM CONFIGURATION]
    CURRENT_PROCESSING_MODE = "{CURRENT_PROCESSING_MODE}" # <-- 注入当前处理模式

    你是一个严格的 OSS-Fuzz 归档专家。

    **第一步：检查状态**
    - 如果 `build_context` 显示构建状态为 "timeout"：
        - 你不需要调用 `compare_error_logs` 或 `append_to_reproduce_report`。
        - **必须调用** `mark_log_as_processed_by_rename`，传入 `log_path=build_context['log_path']`, `status='timeout_attempt'`, 和 `processing_mode=CURRENT_PROCESSING_MODE` [1]。
        - 这样做是为了正式关闭当前任务。
        - 回复：“项目构建超时，已跳过”即可。

    - 如果状态不是超时，则继续：
        1. 调用 `compare_error_logs`，传入 `original_log_path=build_context['log_path']` 和 `new_log_path=build_context['new_build_log_path']`。该工具会返回：
           - `original_log_tail`: 原始下载报错日志的最后 30 行。
           - `new_log_tail`: 编译复现日志的最后 30 行。
        2. **分析与决定**：对比这两个日志末尾片段，评估其核心编译错误、链接错误、断言失败、崩溃栈或未定义符号等是否具有实质上的一致性。
           - 请忽略时间戳、Makefile 行号微调、临时路径、内存地址差异等干扰性细节。
           - 独立做出是否为相同报错的判定。
        3. **如果判定一致（即相同的报错）**：
            - 调用 `append_to_reproduce_report`，传入所有必要的 `build_context` 和 `error_category`。
            - 最终调用 `mark_log_as_processed_by_rename`，传入 `log_path=build_context['log_path']`, `status='success_final'`, 和 `processing_mode=CURRENT_PROCESSING_MODE` [1]。
        4. **如果判定不一致（即不同的报错）**：
            - 最终调用 `mark_log_as_processed_by_rename`，传入 `log_path=build_context['log_path']`, `status='problem_attempt'`, 和 `processing_mode=CURRENT_PROCESSING_MODE` [1]。

    - project: build_context['project_name']
    - language: build_context['language']
    - error_time: build_context['error_time']
    - last_success_time: build_context['last_success_time'] (如果是空字符串，请填入空值)
    - oss-fuzz_sha: build_context['oss_fuzz_sha']
    - fuzzing_build_error_log: build_context['metadata']['log_url']
    - software_repo_url: build_context['metadata']['software_repo_url']
    - software_sha: build_context['metadata']['software_sha']
    - engine: build_context['metadata']['engine']
    - sanitizer: build_context['sanitizer']
    - architecture: build_context['architecture']
    - base_image_digest: build_context['base_image_digest']
    - error_category: [从 RC1-RC25 中根据新日志内容选择]
    - fixed_state: 'no'

    **注意：**
    - 在判定不一致的情况下，严禁写入报告 (`append_to_reproduce_report`)。
    - `log_path` 在 `mark_log_as_processed_by_rename` 中总是原始日志的路径 [1]。
    - `error_category` 字段必须从提供的分类列表 (RC1-RC25) 中选择。

        **分类列表 (RC1-RC25)**:
        RC1: Compiler issues (crash), RC2: Coverage file/dir issues, RC3: Project env/Memory issue, RC4: Network/Server error, RC5: Hardware issues, RC6: Permission issues, RC7: Corpus issues, RC8: External resource download issues, RC9: Dependency issues, RC10: Config and build file issues, RC11: Coverage build config issues, RC12: Fuzzer build script issues, RC13: Source code compilation errors, RC14: Missing source files, RC15: Command/Argument issues, RC16: Runtime issues while fuzzing, RC17: Not enough information, RC18: Sanitizer errors, RC19: Broken fuzz target, RC20: Missing fuzz target, RC21: Input causes unusual crashes, RC22: Failing test cases, RC23: Missing OSS-Fuzz scripts, RC24: Unusual crash from target binary, RC25: Regression causes build crash.

    """,
    tools=[
        compare_error_logs,
        find_fix_date,
        append_to_reproduce_report,
        mark_log_as_processed_by_rename
    ],
)

# 串联起单次工作的流程
single_iteration_agent = SequentialAgent(
    name="single_iteration_agent",
    sub_agents=[build_fuzzer_agent, classify_agent]
)


# --- 启动逻辑 ---
async def main():
    print(">>> 正在启动全自动复现 Agent...")
    if not os.path.exists(OSS_FUZZ_PATH) or not os.path.exists(LOGS_DIRECTORY):
        print("!!! 错误: 预设路径不存在。")
        return

    # --- 动态注入 Agent 指令以反映 CURRENT_PROCESSING_MODE ---
    # build_fuzzer_agent 指令更新
    build_fuzzer_agent.instruction = f"""
    [SYSTEM CONFIGURATION]
    OSS_FUZZ_PATH = "{OSS_FUZZ_PATH}"
    LOGS_DIRECTORY = "{LOGS_DIRECTORY}"
    CURRENT_PROCESSING_MODE = "{CURRENT_PROCESSING_MODE}"

    你的任务是使用**本地挂载模式**精确复现指定的 OSS-Fuzz 项目构建错误。

    1.  **确认并解析日志**: 仔细确认用户在初始消息中指定的待复现日志绝对路径 `log_path`。你不需要调用任何工具获取日志，直接基于该路径开始下一步。
    2.  **提取元数据**: 调用 `extract_build_metadata_from_log` 处理该日志文件，获得主项目仓库 `software_repo_url` 与 `software_sha` 等。
    3.  **解析日志路径**: 调用 `parse_error_log` 获取项目名、错误日期和上次成功日期。
    4.  **获取OSS-Fuzz版本**: 调用 `find_sha_for_timestamp`。**必须使用文件路径 `{COMMITS_FILE_PATH}`** 和步骤3中的日期。
    5.  **获取项目语言**: 调用 `get_project_yaml_info`，传入项目名。
    6.  **切换OSS-Fuzz环境**: 调用 `checkout_oss_fuzz_commit`，传入步骤4获取的 SHA。
    7.  **克隆第三方源码到本地**: 调用 `download_github_repo` 工具：
        - `project_name` 传入步骤3解析出的项目名。
        - `target_dir` (强制规范) 必须传入相对路径形式的 `"./process/project/"` 后面跟项目名（例如 `"./process/project/mosquitto"`）。
        - `repo_url` 传入步骤2得到的 `software_repo_url` [1]。
    8.  **对齐本地源码版本**: 调用 `checkout_project_commit` 工具：
        - `project_source_path` 传入步骤7克隆的本地路径。
        - `sha` 传入步骤2得到的 `software_sha`。
    9.  **锁定复现环境 (Patch)**: 调用 `patch_project_dockerfile`。传入步骤2提取的 `base_image_digest` 和 `dependencies`。
    10. **执行本地挂载构建**: 调用 `run_fuzz_build_streaming`。
        - 必须使用步骤2提取的 Engine, Sanitizer, Architecture。
        - **关键：必须传入 `mount_path` 参数，值为步骤7中你克隆第三方源码的目标本地路径。**
        - **关键：必须将本轮任务指定的 `log_path` 作为 `original_log_path` 参数传入。**
        - 设置 `timeout` 为 7200。
    11. **重置环境并自动清理第三方源码**: 无论构建成功、失败还是超时，必须调用 `reset_project_environment`。该步骤会自动清退本地克隆的第三方项目源码仓库。

    **绝对禁令（IMPORTANT）：**
    - 每轮对话你**只能处理指定的这一个**日志文件。
    - 如果构建工具返回 "status": "timeout"，你仍然需要执行重置环境，并输出 JSON，但可以在 JSON 中注明超时。
    - 完成步骤 11 后，必须立即输出 JSON 字符串并结束回合。


    **输出规范**:
    将收集到的信息整合为一个 JSON 字符串输出，必须包含以下字段供后续 Agent 使用：
    - `log_path`: 原始错误日志的绝对路径
    - `project_name`: 项目名
    - `error_date`: 报错日期
    - `last_success_time`: 步骤3解析出的上次成功日期
    - `oss_fuzz_sha`: 步骤4获取的 SHA
    - `language`: 项目语言
    - `metadata`: 步骤2提取的所有元数据 (含 log_url, software_repo_url, software_sha, engine, sanitizer, architecture, base_image_digest, dependencies)
    - `new_build_log_path`: 步骤10生成的复现构建日志路径
    """.replace("{COMMITS_FILE_PATH}", COMMITS_FILE_PATH)

    # classify_agent 指令更新
    classify_agent.instruction = f"""
    [SYSTEM CONFIGURATION]
    CURRENT_PROCESSING_MODE = "{CURRENT_PROCESSING_MODE}" # <-- 注入当前处理模式

    你是一个严格的 OSS-Fuzz 归档专家。

    **第一步：检查状态**
    - 如果 `build_context` 显示构建状态为 "timeout"：
        - 你不需要调用 `compare_error_logs` 或 `append_to_reproduce_report`。
        - **必须调用** `mark_log_as_processed_by_rename`，传入 `log_path=build_context['log_path']`, `status='timeout_attempt'`, 和 `processing_mode=CURRENT_PROCESSING_MODE` [1]。
        - 这样做是为了正式关闭当前任务。
        - 回复：“项目构建超时，已跳过”即可。

    - 如果状态不是超时，则继续：
        1. 调用 `compare_error_logs`，传入 `original_log_path=build_context['log_path']` 和 `new_log_path=build_context['new_build_log_path']`。该工具会返回：
           - `original_log_tail`: 原始下载报错日志的最后 30 行。
           - `new_log_tail`: 编译复现日志的最后 30 行。
        2. **分析与决定**：对比这两个日志末尾片段，评估其核心编译错误、链接错误、断言失败、崩溃栈或未定义符号等是否具有实质上的一致性。
           - 请忽略时间戳、Makefile 行号微调、临时路径、内存地址差异等干扰性细节。
           - 独立做出是否为相同报错的判定。
        3. **如果判定一致（即相同的报错）**：
            - 调用 `append_to_reproduce_report`，传入所有必要的 `build_context` 和 `error_category`。
            - 最终调用 `mark_log_as_processed_by_rename`，传入 `log_path=build_context['log_path']`, `status='success_final'`, 和 `processing_mode=CURRENT_PROCESSING_MODE` [1]。
        4. **如果判定不一致（即不同的报错）**：
            - 最终调用 `mark_log_as_processed_by_rename`，传入 `log_path=build_context['log_path']`, `status='problem_attempt'`, 和 `processing_mode=CURRENT_PROCESSING_MODE` [1]。

    - project: build_context['project_name']
    - language: build_context['language']
    - error_time: build_context['error_time']
    - last_success_time: build_context['last_success_time'] (如果是空字符串，请填入空值)
    - oss-fuzz_sha: build_context['oss_fuzz_sha']
    - fuzzing_build_error_log: build_context['metadata']['log_url']
    - software_repo_url: build_context['metadata']['software_repo_url']
    - software_sha: build_context['metadata']['software_sha']
    - engine: build_context['metadata']['engine']
    - sanitizer: build_context['sanitizer']
    - architecture: build_context['architecture']
    - base_image_digest: build_context['base_image_digest']
    - error_category: [从 RC1-RC25 中根据新日志内容选择]
    - fixed_state: 'no'

    **注意：**
    - 在判定不一致的情况下，严禁写入报告 (`append_to_reproduce_report`)。
    - `log_path` 在 `mark_log_as_processed_by_rename` 中总是原始日志的路径 [1]。
    - `error_category` 字段必须从提供的分类列表 (RC1-RC25) 中选择。

        **分类列表 (RC1-RC25)**:
        RC1: Compiler issues (crash), RC2: Coverage file/dir issues, RC3: Project env/Memory issue, RC4: Network/Server error, RC5: Hardware issues, RC6: Permission issues, RC7: Corpus issues, RC8: External resource download issues, RC9: Dependency issues, RC10: Config and build file issues, RC11: Coverage build config issues, RC12: Fuzzer build script issues, RC13: Source code compilation errors, RC14: Missing source files, RC15: Command/Argument issues, RC16: Runtime issues while fuzzing, RC17: Not enough information, RC18: Sanitizer errors, RC19: Broken fuzz target, RC20: Missing fuzz target, RC21: Input causes unusual crashes, RC22: Failing test cases, RC23: Missing OSS-Fuzz scripts, RC24: Unusual crash from target binary, RC25: Regression causes build crash.

    """

    # 注入路径
    base_instruction = build_fuzzer_agent.instruction
    injected_instruction = f"""
    [SYSTEM CONFIGURATION]
    OSS_FUZZ_PATH = "{OSS_FUZZ_PATH}"
    LOGS_DIRECTORY = "{LOGS_DIRECTORY}"
    请在调用工具时务必使用上述路径。
    {base_instruction}
    """
    build_fuzzer_agent.instruction = injected_instruction

    GLOBAL_LOGGER.init()
    GLOBAL_LOGGER.set_project_context("reproduce_workflow")

    session_service = InMemorySessionService()
    # 💡 替换：直接包装单次迭代 Agent
    root_agent_with_logging = LoggingWrapperAgent(subject_agent=single_iteration_agent)
    runner = Runner(agent=root_agent_with_logging, app_name=APP_NAME, session_service=session_service)

    print(">>> 开始执行全自动复现任务...")

    iteration_count = 0
    max_iterations = 1000

    while iteration_count < max_iterations:
        # 🔑 1. 使用 Python 在最外层获取下一个待复现日志
        next_log_path = get_next_error_log(LOGS_DIRECTORY, CURRENT_PROCESSING_MODE)
        if next_log_path == "finished":
            print("\n>>> 所有待复现日志均已处理完毕。任务结束。")
            break

        project_name = os.path.basename(os.path.dirname(next_log_path))
        print(f"\n>>> [轮次 {iteration_count + 1}] 正在启动项目 '{project_name}' 的复现流程...")

        # 🔑 2. 为该项目生成全新且隔离的 Session ID，从源头上斩断上下文污染
        timestamp = datetime.now().strftime('%m%d_%H%M%S')
        session_id = f"session_{project_name}_{timestamp}"
        await session_service.create_session(app_name=APP_NAME, user_id="user", session_id=session_id)

        # 🔑 3. 直接通过初始消息向 Agent 传递需要复现的日志绝对路径
        initial_message = types.Content(
            parts=[types.Part(text=f"请启动本地挂载模式，开始处理以下日志文件：{next_log_path}")],
            role='user'
        )

        try:
            # 运行单次迭代
            async for event in runner.run_async(user_id="user", session_id=session_id, new_message=initial_message):
                if event.actions and event.actions.escalate:
                    print("\n>>> 接收到内部中断信号。")
                    break
        except Exception as e:
            print(f"执行项目 '{project_name}' 时发生错误: {e}")
            import traceback
            traceback.print_exc()

        iteration_count += 1
        # 强制小睡以释放资源并确保文件系统句柄刷新
        await asyncio.sleep(1.0)

    print(">>> 全自动复现工作流全部执行完毕。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("User interrupted.")