# agent_tools.py
# 这是一个为 OSS-Fuzz 错误复现 Agent 提供核心功能的工具箱。

import os
import shutil
import subprocess
import tempfile
import time
from collections import deque
from datetime import datetime
from typing import Dict, List, Tuple
import openpyxl
from openpyxl import Workbook

# Selenium 用于浏览器自动化，与腾讯文档交互
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def get_next_error_log(logs_directory: str) -> Dict[str, str]:
    """
    【新策略】从指定的日志目录中找到下一个尚未处理的错误日志文件。
    通过检查文件名是否以 '+' 开头来判断日志是否已被处理。
    """
    if not os.path.isdir(logs_directory):
        return {'status': 'error', 'message': f"Logs directory not found: {logs_directory}"}

    try:
        project_dirs = sorted([d for d in os.listdir(logs_directory) if os.path.isdir(os.path.join(logs_directory, d))])
    except OSError as e:
        return {'status': 'error', 'message': f"Error reading logs directory: {e}"}

    for project_name in project_dirs:
        project_path = os.path.join(logs_directory, project_name)
        try:
            log_files = sorted(os.listdir(project_path))
        except OSError:
            continue

        for log_filename in log_files:
            # 【核心逻辑】: 跳过以 '+' 开头的文件
            if log_filename.startswith('+'):
                continue
            
            full_log_path = os.path.join(project_path, log_filename)
            if "error" in log_filename and os.path.isfile(full_log_path):
                print(f"--- Tool: Found next log to process: {full_log_path} ---")
                return {'status': 'success', 'log_path': full_log_path}

    return {'status': 'finished', 'message': 'All logs have been processed.'}


def mark_log_as_processed_by_rename(log_path: str) -> Dict[str, str]:
    """
    【新策略】通过在文件名前添加 '+' 来将日志文件标记为已处理。
    """
    if not os.path.isfile(log_path):
        return {'status': 'error', 'message': f"Log file not found at path: {log_path}"}
    
    try:
        directory = os.path.dirname(log_path)
        filename = os.path.basename(log_path)
        
        # 检查是否已经标记过，避免重复添加 '+'
        if filename.startswith('+'):
            message = f"Log file '{log_path}' is already marked as processed."
            print(f"--- Tool: {message} ---")
            return {'status': 'success', 'message': message}
            
        new_filename = '+' + filename
        new_log_path = os.path.join(directory, new_filename)
        
        os.rename(log_path, new_log_path)
        
        message = f"Successfully marked log by renaming to '{new_log_path}'."
        print(f"--- Tool: {message} ---")
        return {'status': 'success', 'message': message, 'new_path': new_log_path}
    except Exception as e:
        message = f"Failed to mark log by renaming: {e}"
        print(f"--- Tool ERROR: {message} ---")
        return {'status': 'error', 'message': message}


def read_file_content(file_path: str) -> dict:
    """
    读取指定文本文件的内容并返回。
    """
    print(f"--- Tool: read_file_content called for path: {file_path} ---")
    if not os.path.isfile(file_path):
        message = f"错误：路径 '{file_path}' 不是一个有效的文件。"
        return {"status": "error", "message": message}
    try:
        with open(file_path, "r", encoding="utf-8", errors='ignore') as f:
            content = f.read()
        # 限制返回给LLM的内容长度，避免超出上下文限制
        MAX_LEN = 32000
        if len(content) > MAX_LEN:
            content = content[-MAX_LEN:]
            message = f"文件 '{file_path}' 内容已成功读取（为避免超长，已截断为最后 {MAX_LEN} 字符）。"
        else:
            message = f"文件 '{file_path}' 的内容已成功读取。"
        
        return {"status": "success", "message": message, "content": content}
    except Exception as e:
        message = f"读取文件 '{file_path}' 时发生错误: {str(e)}"
        return {"status": "error", "message": message}


def parse_error_log(log_path: str) -> Dict[str, str]:
    """
    从给定的错误日志文件路径中解析出项目名称和报错日期。
    """
    print(f"--- Tool: parse_error_log called for path: {log_path} ---")
    try:
        project_name = os.path.basename(os.path.dirname(log_path))
        filename = os.path.basename(log_path)
        
        # 从文件名 "YYYY_M_D error.txt" 或 "YYYY_M_D error" 中提取日期部分
        date_string = filename.split(' ')[0]
        
        parts = date_string.split('_')
        if len(parts) != 3:
            raise ValueError(f"Date part '{date_string}' is not in 'YYYY_M_D' format.")
        
        year, month, day = parts
        error_timestamp = f"{year}.{month}.{day}"
        
        return {
            'status': 'success',
            'project_name': project_name,
            'error_date': error_timestamp # 使用 'error_date' 键名更清晰
        }
    except Exception as e:
        return {'status': 'error', 'message': f"Failed to parse log path '{log_path}': {e}"}


def find_sha_for_timestamp(commits_file_path: str, target_date_str: str) -> Dict[str, str]:
    """
    在 commits 文件中，为给定的日期找到当天最早的 commit SHA。
    """
    print(f"--- Tool: find_sha_for_timestamp called for date: {target_date_str} ---")
    try:
        target_date = datetime.strptime(target_date_str, '%Y.%m.%d').date()
    except ValueError:
        return {'status': 'error', 'message': f"Invalid target date format: '{target_date_str}'. Expected 'YYYY.MM.DD'."}

    daily_commits: List[Tuple[datetime, str]] = []
    try:
        with open(commits_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if line.startswith("Time: ") and i + 1 < len(lines) and lines[i+1].strip().startswith("- SHA: "):
                    try:
                        timestamp_str = line.replace("Time: ", "")
                        commit_datetime = datetime.strptime(timestamp_str, '%Y.%m.%d %H:%M')
                        if commit_datetime.date() == target_date:
                            sha = lines[i+1].strip().replace("- SHA: ", "")
                            daily_commits.append((commit_datetime, sha))
                        i += 2 # Move to the SHA line
                    except (ValueError, IndexError):
                        pass
                i += 1
    except FileNotFoundError:
        return {'status': 'error', 'message': f"Commits file not found at: {commits_file_path}"}

    if not daily_commits:
        return {'status': 'error', 'message': f"No suitable SHA found for the date {target_date_str}."}

    earliest_commit_datetime, earliest_sha = min(daily_commits)
    return {'status': 'success', 'sha': earliest_sha}


def checkout_oss_fuzz_commit(oss_fuzz_path: str, sha: str) -> Dict[str, str]:
    """
    在指定的 oss-fuzz 目录下，执行 git checkout 命令。
    """
    print(f"--- Tool: checkout_oss_fuzz_commit called for SHA: {sha} ---")
    if not os.path.isdir(os.path.join(oss_fuzz_path, ".git")):
        return {'status': 'error', 'message': f"The directory '{oss_fuzz_path}' is not a git repository."}

    original_path = os.getcwd()
    try:
        os.chdir(oss_fuzz_path)
        # 先切换到一个已知分支（如master），避免在detached HEAD状态下再次checkout导致的问题
        subprocess.run(["git", "switch", "master"], capture_output=True, text=True)
        command = ["git", "checkout", sha]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        if result.returncode == 0:
            return {'status': 'success', 'message': f"Successfully checked out SHA {sha}."}
        else:
            return {'status': 'error', 'message': f"Git command failed: {result.stderr.strip()}"}
    except Exception as e:
        return {'status': 'error', 'message': f"An unexpected error occurred during checkout: {e}"}
    finally:
        os.chdir(original_path)


def run_fuzz_build_streaming(project_name: str, oss_fuzz_path: str, sanitizer: str = "address", engine: str = "libfuzzer", architecture: str = "x86_64") -> dict:
    """
    执行 fuzzing 构建命令，并将结果流式传输到日志文件。
    """
    print(f"--- Tool: run_fuzz_build_streaming called for project: {project_name} ---")
    LOG_DIR = "build_logs"
    LOG_FILE_PATH = os.path.join(LOG_DIR, f"{project_name}_build_log.txt")
    os.makedirs(LOG_DIR, exist_ok=True)

    try:
        helper_script_path = os.path.join(oss_fuzz_path, "infra/helper.py")
        command = ["python3.10", helper_script_path, "build_fuzzers", "--sanitizer", sanitizer, "--engine", engine, "--architecture", architecture, project_name]
        
        print(f"--- Executing command: {' '.join(command)} ---")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=oss_fuzz_path, encoding='utf-8', errors='ignore')

        log_buffer = deque(maxlen=280)
        for line in process.stdout:
            print(line, end='', flush=True)
            log_buffer.append(line)
        process.wait()

        if process.returncode == 0:
            content_to_write = "success"
            status = "success"
        else:
            content_to_write = "".join(log_buffer)
            status = "error"

        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(content_to_write)
        
        message = f"Build process finished for {project_name}. Status: {status}. Log saved to '{LOG_FILE_PATH}'."
        return {"status": status, "message": message, "new_build_log_path": LOG_FILE_PATH}
    except Exception as e:
        message = f"An exception occurred during build: {str(e)}"
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(message)
        return {"status": "error", "message": message, "new_build_log_path": LOG_FILE_PATH}


def update_reproduce_table(project_name: str, date: str, category: str, failure_reason: str, is_consistent: str) -> Dict[str, str]:
    """
    【新策略】将一行新的复现结果数据写入本地的 Excel (.xlsx) 文件中。
    如果文件不存在，则会自动创建并写入表头。
    """
    print(f"--- Tool: update_reproduce_table (Local Excel) called for project: {project_name} ---")
    
    # 定义输出文件名和表头
    EXCEL_FILE_PATH = "reproduce_report.xlsx"
    HEADER = ["项目名称", "日期", "归类", "build失败原因", "报错是否一致"]

    try:
        # 1. 检查文件是否存在，如果不存在则创建并写入表头
        if not os.path.exists(EXCEL_FILE_PATH):
            print(f"--- Tool: Excel file not found. Creating '{EXCEL_FILE_PATH}' with headers. ---")
            # 创建一个新的工作簿
            workbook = Workbook()
            # 获取活动工作表
            sheet = workbook.active
            # 写入表头
            sheet.append(HEADER)
            # 保存文件
            workbook.save(EXCEL_FILE_PATH)

        # 2. 加载工作簿并追加新数据
        workbook = openpyxl.load_workbook(EXCEL_FILE_PATH)
        sheet = workbook.active
        
        # 准备要写入的数据行
        data_row = [project_name, date, category, failure_reason, is_consistent]
        
        # 追加新行
        sheet.append(data_row)
        
        # 保存更改
        workbook.save(EXCEL_FILE_PATH)
        
        message = f"Successfully wrote data to local Excel file: {EXCEL_FILE_PATH}"
        print(f"--- Tool: {message} ---")
        return {'status': 'success', 'message': message}

    except Exception as e:
        message = f"Failed to write to local Excel file: {str(e)}"
        print(f"--- Tool ERROR: {message} ---")
        return {'status': 'error', 'message': message}
