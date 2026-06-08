import os
import shutil
import subprocess
import re
import json
import yaml
import time
import signal
import selectors
import unicodedata
from collections import deque
from datetime import datetime
from typing import Dict, List, Any, Tuple, Optional

import os
import shutil
import subprocess
import re
import json
import yaml
import time
import signal
import difflib
import selectors
import unicodedata
from collections import deque
from datetime import datetime
from typing import Dict, List, Any, Tuple, Optional

# 从 agent.py 导入模式常量，确保一致性
try:
    from agent import MODE_UNPROCESSED, MODE_TIMED_OUT
except ImportError:
    # 如果作为独立文件运行或Agent未定义，提供默认值
    MODE_UNPROCESSED = "unprocessed_logs"
    MODE_TIMED_OUT = "timed_out_logs"


# --- 1. 日志管理与基础工具 ---

def get_next_error_log(logs_directory: str, processing_mode: str) -> str:
    """
    根据 'processing_mode' 递归地扫描指定目录及其所有子目录，
    寻找下一个符合条件的错误日志文件。

    Args:
        logs_directory: 存放日志文件的根目录。
        processing_mode: 当前的处理模式，必须是 agent.MODE_UNPROCESSED 或 agent.MODE_TIMED_OUT。

    Returns:
        下一个符合条件的错误日志文件的绝对路径，如果没有找到则返回 "finished"。
    """
    print(f"--- Tool: get_next_error_log called for '{logs_directory}' in mode '{processing_mode}' ---")

    for dirpath, _, filenames in os.walk(logs_directory):
        for filename in sorted(filenames): # 确保按文件名排序，以便每次选取一致
            # 仅处理 .txt 结尾且包含 "error" 的文件，过滤其他文件
            if "error" not in filename:
                continue

            full_log_path = os.path.join(dirpath, filename)

            if processing_mode == MODE_UNPROCESSED:
                # 寻找没有 '+' 前缀的文件
                if not filename.startswith('+'):
                    print(f"--- Found unprocessed log: {full_log_path} ---")
                    return full_log_path
            elif processing_mode == MODE_TIMED_OUT:
                # 寻找以 '++' 或 '+timeout+' 开头的文件
                if filename.startswith('++') or filename.startswith('+timeout+'):
                    print(f"--- Found timed-out log: {full_log_path} ---")
                    return full_log_path
            else:
                print(f"--- Warning: Unknown processing_mode '{processing_mode}'. Skipping. ---")
                return "finished" # 未知模式直接结束

    print(f"--- No more logs found for mode '{processing_mode}'. Returning 'finished'. ---")
    return "finished"

def mark_log_as_processed_by_rename(log_path: str, status: str, processing_mode: str) -> str:
    """
    根据处理模式和最终状态，重命名日志文件以标记其处理进度。

    Args:
        log_path: 待重命名的日志文件完整路径。
        status: 处理尝试的最终结果。可以是 'success_final' (最终成功), 
                'timeout_attempt' (处理过程中超时), 'problem_attempt' (处理过程中出现其他问题)。
        processing_mode: 当前Agent的操作模式，agent.MODE_UNPROCESSED 或 agent.MODE_TIMED_OUT。

    Returns:
        包含重命名结果的消息字符串。
    """
    print(f"--- Tool: mark_log_as_processed_by_rename for '{log_path}', status '{status}', mode '{processing_mode}' ---")

    if not os.path.exists(log_path):
        return f"Error: Log file '{log_path}' not found."
    
    directory = os.path.dirname(log_path)
    filename = os.path.basename(log_path)

    # 鲁棒地提取基础文件名，移除所有已知的处理前缀
    # '++2026_2_10 error.txt' -> '2026_2_10 error.txt'
    # '+timeout+2026_2_10 error.txt' -> '2026_2_10 error.txt'
    # '+2026_2_10 error.txt' -> '2026_2_10 error.txt'
    # '+wrong+2026_2_10 error.txt' -> '2026_2_10 error.txt'
    base_filename = re.sub(r'^\+(timeout|wrong|\+)?\+?', '', filename)
    # 再次检查以防边缘情况，确保 base_filename 不为空
    if not base_filename:
        base_filename = filename.lstrip('+').lstrip('timeout').lstrip('wrong').lstrip('+')
        if not base_filename: # If still empty, it was likely just prefixes or unexpected format
            base_filename = filename # Fallback to original filename

    new_prefix = ""

    if processing_mode == MODE_UNPROCESSED:
        if status == 'success_final':
            new_prefix = "+"
        elif status == 'timeout_attempt':
            new_prefix = "+timeout+"
        elif status == 'problem_attempt': # 根据用户纠正：非超时的“问题尝试”也视为已检查完毕，标记为 '+'
            new_prefix = "+" # <--- 核心修改点：从 '+timeout+' 改为 '+'
        else:
            return f"Error: Invalid status '{status}' for mode '{processing_mode}'."
            
    elif processing_mode == MODE_TIMED_OUT:
        if status == 'success_final':
            new_prefix = "+" # 成功处理，无论之前是什么前缀，都改为 '+'
        elif status in ['timeout_attempt', 'problem_attempt']: # 超时或任何其他问题
            new_prefix = "+wrong+" # 失败，改为 '+wrong+'
        else:
            return f"Error: Invalid status '{status}' for mode '{processing_mode}'."
    else:
        return f"Error: Unknown processing_mode '{processing_mode}'."


    new_filename = f"{new_prefix}{base_filename}"
    new_path = os.path.join(directory, new_filename)

    # 如果目标路径与当前路径相同，说明无需重命名（文件已是所需状态）
    if os.path.abspath(log_path) == os.path.abspath(new_path):
        return f"Log file '{filename}' is already correctly marked as '{new_filename}'."

    try:
        # 尝试普通重命名
        os.rename(log_path, new_path)
        return f"Log file marked as processed. Renamed from '{filename}' to '{new_filename}'."
    except OSError as e:
        # 回退机制：若无权限，将日志所在目录挂载进 Docker 容器，并使用容器内的 root 权限执行 mv
        try:
            abs_log_dir = os.path.abspath(directory)
            cmd = [
                "docker", "run", "--rm",
                "-v", f"{abs_log_dir}:/log_dir",
                "gcr.io/oss-fuzz-base/base-builder",
                "mv", f"/log_dir/{filename}", f"/log_dir/{new_filename}"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                return f"Log file marked as processed. Renamed from '{filename}' to '{new_filename}' (via Docker fallback)."
            else:
                return f"Error renaming file '{filename}' to '{new_filename}': {e} | Docker fallback failed: {res.stderr.strip()}"
        except Exception as docker_e:
            return f"Error renaming file '{filename}' to '{new_filename}': {e} | Docker fallback exception: {docker_e}"

def download_github_repo(project_name: str, target_dir: str, repo_url: Optional[str] = None) -> Dict[str, str]:
    """
    下载第三方项目源代码。
    🔑 强力限制：将项目克隆路由锁定在 process/project/<project_name> 路径下。
    """
    import json
    import time
    import subprocess
    import os
    import shutil

    current_work_dir = os.getcwd()

    if project_name == "oss-fuzz":
        final_target_dir = os.path.abspath(target_dir)
    else:
        safe_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
        final_target_dir = os.path.abspath(os.path.join(current_work_dir, "process", "project", safe_name))

        if os.path.abspath(target_dir) != final_target_dir:
            print(f"--- Path Security Enforcement: Redirecting download from {target_dir} to {final_target_dir} ---")

    print(f"--- Tool: download_github_repo called for '{project_name}' ---")

    if os.path.isdir(final_target_dir) and os.path.exists(os.path.join(final_target_dir, ".git")):
        if project_name == "oss-fuzz":
            print(f"--- oss-fuzz exists, pulling latest... ---")
            try:
                subprocess.run(["git", "pull"], cwd=final_target_dir, check=True, capture_output=True)
                return {'status': 'success', 'path': final_target_dir, 'message': 'oss-fuzz updated.'}
            except:
                return {'status': 'success', 'path': final_target_dir, 'message': 'oss-fuzz update failed, using local.'}
        else:
            print(f"--- Repo '{project_name}' exists and is a valid git repo. Skipping download. ---")
            return {'status': 'success', 'path': final_target_dir, 'message': 'Repository already exists.'}

    if os.path.isdir(final_target_dir):
        shutil.rmtree(final_target_dir)
    os.makedirs(os.path.dirname(final_target_dir), exist_ok=True)

    final_repo_url = repo_url if repo_url and repo_url.strip() else None
    if not final_repo_url:
        if project_name == "oss-fuzz":
            final_repo_url = "https://github.com/google/oss-fuzz.git"
        else:
            try:
                search_cmd = ["gh", "search", "repos", project_name, "--sort", "stars", "--limit", "1", "--json", "fullName"]
                result = subprocess.run(search_cmd, capture_output=True, text=True, check=True, encoding='utf-8')
                parsed = json.loads(result.stdout.strip())
                if parsed:
                    final_repo_url = f"https://github.com/{parsed[0]['fullName']}.git"
                else:
                    return {'status': 'error', 'message': f"Repo not found for {project_name}"}
            except Exception as e:
                return {'status': 'error', 'message': f"Search failed: {e}"}

    subprocess.run(["git", "config", "--global", "http.postBuffer", "524288000"])
    subprocess.run(["git", "config", "--global", "http.lowSpeedLimit", "0"])
    subprocess.run(["git", "config", "--global", "http.lowSpeedTime", "999999"])

    max_retries = 3
    for attempt in range(max_retries):
        print(f"--- Download attempt {attempt + 1}/{max_retries} ---")
        try:
            clone_cmd = ["git", "clone", final_repo_url, final_target_dir]
            result = subprocess.run(clone_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return {'status': 'success', 'path': final_target_dir, 'message': 'Successfully cloned full history repository.'}
            else:
                print(f"--- Attempt {attempt + 1} failed: {result.stderr} ---")
        except Exception as e:
            print(f"--- Attempt {attempt + 1} exception: {e} ---")
        time.sleep(10 * (attempt + 1))

    return {'status': 'error', 'message': f"Failed to download {project_name} after {max_retries} attempts."}

def checkout_project_commit(project_source_path: str, sha: str) -> Dict[str, str]:
    """
    切换本地克隆的第三方项目源码到指定的版本 (Commit SHA)。
    """
    print(f"--- Tool: checkout_project_commit for {project_source_path} to {sha} ---")
    if not os.path.exists(project_source_path):
        return {'status': 'error', 'message': f"Source path {project_source_path} does not exist."}
    cwd = os.getcwd()
    try:
        os.chdir(project_source_path)
        subprocess.run(["git", "reset", "--hard"], capture_output=True)
        res = subprocess.run(["git", "checkout", sha], capture_output=True, text=True, encoding='utf-8')
        if res.returncode == 0:
            return {'status': 'success', 'message': f"Checked out {sha} in {project_source_path}"}
        else:
            # 兼容浅克隆或远程新提交的情况
            subprocess.run(["git", "fetch", "origin"], capture_output=True)
            res2 = subprocess.run(["git", "checkout", sha], capture_output=True, text=True, encoding='utf-8')
            if res2.returncode == 0:
                return {'status': 'success', 'message': f"Checked out {sha} in {project_source_path} (after fetch)"}
            return {'status': 'error', 'message': f"Failed checkout: {res2.stderr}"}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    finally:
        os.chdir(cwd)

def parse_error_log(log_path: str) -> Dict[str, str]:
    print(f"--- Tool: parse_error_log called for {log_path} ---")
    try:
        project_dir = os.path.dirname(log_path)
        project_name = os.path.basename(project_dir)
        filename = os.path.basename(log_path)
        
        # 解析当前日期，支持 YYYY_M_D 格式
        date_str = filename.split(' ')[0]
        # 稳妥起见，清理可能附带的模式前缀，确保日期解析成功
        clean_date_str = re.sub(r'^\+(timeout|wrong|\+)?\+?', '', date_str)
        error_dt = datetime.strptime(clean_date_str, '%Y_%m_%d')
        
        # 寻找紧邻的上次成功 (放宽限制：只要包含 "success" 即可)
        success_files = [f for f in os.listdir(project_dir) if "success" in f]
        last_success = ""
        past_dates = []
        for sf in success_files:
            try:
                # 提取 success 日志的日期部分
                s_date_str = sf.split(' ')[0]
                s_dt = datetime.strptime(s_date_str, '%Y_%m_%d')
                if s_dt < error_dt:
                    past_dates.append(s_dt)
            except: continue
        
        if past_dates:
            last_success = max(past_dates).strftime('%Y-%m-%d')

        return {
            'status': 'success',
            'project_name': project_name,
            'error_time': error_dt.strftime('%Y-%m-%d'),
            'last_success_time': last_success
        }
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

def read_file_content(file_path: str) -> dict:
    """读取文件内容。"""
    if not os.path.isfile(file_path):
        return {"status": "error", "message": "File not found."}
    try:
        with open(file_path, "r", encoding="utf-8", errors='ignore') as f:
            content = f.read()
        MAX_LEN = 50000
        if len(content) > MAX_LEN:
            content = content[:MAX_LEN//2] + "\n...[Truncated]...\n" + content[-MAX_LEN//2:]
        return {"status": "success", "content": content}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# --- 2. 信息提取工具 ---
def extract_build_metadata_from_log(log_path: str) -> Dict:
    """
    强化版元数据提取：严格遵循用户要求的 Step #2 和 Step #3 逻辑
    改进点：
    1. 优化 Engine 提取正则，支持 libFuzzer, AFL++, Honggfuzz 等包含大小写和特殊字符的名称。
    2. 基于 Step #2 - "srcmap" 块的行偏移和回退策略，精准提取主项目和第三方依赖。
    """
    print(f"--- Tool: extract_build_metadata ---")

    def clean_string(text: str) -> str:
        """清理字符串中的非法JSON字符"""
        if not isinstance(text, str):
            return str(text)

        # 移除控制字符和不可打印字符
        text = ''.join(ch for ch in text if unicodedata.category(ch)[0] != "C")

        # 转义JSON特殊字符
        text = text.replace('\\', '\\\\')
        text = text.replace('"', '\\"')
        text = text.replace('\n', '\\n')
        text = text.replace('\r', '\\r')
        text = text.replace('\t', '\\t')

        return text.strip()

    try:
        metadata = {
            'dependencies': [],
            'file_info': {},
            'build_config': {}
        }

        # 1. 流式读取文件，提取 Step #2 对应的所有行，同时缓存前 1000 行用于快速检索其他元数据
        first_chunk = []
        srcmap_lines = []

        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f):
                if i < 1000:
                    first_chunk.append(line)
                if 'Step #2 - "srcmap"' in line:
                    srcmap_lines.append(line)

        content = "".join(first_chunk)

        # 2. UUID & URL 提取
        uuid_match = re.search(r'starting build "([a-f0-9\-]+)"', content)
        if uuid_match:
            metadata['log_uuid'] = clean_string(uuid_match.group(1))
            metadata['log_url'] = f"https://oss-fuzz-build-logs.storage.googleapis.com/log-{uuid_match.group(1)}.txt"

        # 3. Base Image Digest 提取
        digest_match = re.search(r'Digest: sha256:([a-f0-9]{64})', content)
        if digest_match:
            metadata['base_image_digest'] = clean_string(digest_match.group(1))

        # 4. Config (Step #3) - 优化后的正则支持 libFuzzer, AFL++, Honggfuzz 等格式
        for line in first_chunk:
            if 'Starting Step #3 - "compile-' in line:
                # 捕获组 1 和 2 扩展为支持大小写字母、数字、"+" 和 "-"
                m = re.search(r'compile-([a-zA-Z0-9\+\-]+)-([a-zA-Z0-9\+\-]+)-([a-zA-Z0-9_]+)', line)
                if m:
                    metadata['build_config']['engine'] = clean_string(m.group(1))
                    metadata['build_config']['sanitizer'] = clean_string(m.group(2))
                    metadata['build_config']['architecture'] = clean_string(m.group(3))
                break

        # 5. 精准提取主软件仓库与 SHA (Step #2 - "srcmap")
        project_name = os.path.basename(os.path.dirname(log_path))
        success = False
        main_repo_url = ""
        main_repo_sha = ""

        # 尝试方法 A：寻找包含 "/src/<project_name>" 的最后一行并向后取 4 行数据
        target_str = f'/src/{project_name}'
        last_match_idx = -1
        for idx, line in enumerate(srcmap_lines):
            if target_str in line:
                last_match_idx = idx

        if last_match_idx != -1:
            four_lines = srcmap_lines[last_match_idx: last_match_idx + 4]
            url_val, rev_val = None, None
            for l in four_lines:
                u_m = re.search(r'"url":\s*["\']([^"\']+)["\']', l)
                r_m = re.search(r'"rev":\s*["\']([^"\']+)["\']', l)
                if u_m:
                    url_val = u_m.group(1)
                if r_m:
                    rev_val = r_m.group(1)
            if url_val and rev_val:
                main_repo_url = url_val
                main_repo_sha = rev_val
                success = True

        # 尝试方法 B（匹配失败或项目名不符时的兜底）：取最后 7 行数据搜寻
        if not success and len(srcmap_lines) >= 7:
            last_seven_lines = srcmap_lines[-7:]
            url_val, rev_val = None, None
            for l in last_seven_lines:
                u_m = re.search(r'"url":\s*["\']([^"\']+)["\']', l)
                r_m = re.search(r'"rev":\s*["\']([^"\']+)["\']', l)
                if u_m:
                    url_val = u_m.group(1)
                if r_m:
                    rev_val = r_m.group(1)
            if url_val and rev_val:
                main_repo_url = url_val
                main_repo_sha = rev_val
                success = True

        # 保存提取的主项目仓库信息
        if success:
            metadata['software_repo_url'] = clean_string(main_repo_url)
            metadata['software_sha'] = clean_string(main_repo_sha)

        # 6. 获取所有的第三方源码库依赖并排除主软件仓库
        all_deps = []
        for k in range(len(srcmap_lines)):
            u_m = re.search(r'"url":\s*["\']([^"\']+)["\']', srcmap_lines[k])
            if u_m:
                dep_url = u_m.group(1)
                dep_rev = None
                # 在 url 匹配行的前后 3 行范围内搜寻对应的 rev
                for offset in range(-3, 4):
                    idx = k + offset
                    if 0 <= idx < len(srcmap_lines):
                        r_m = re.search(r'"rev":\s*["\']([^"\']+)["\']', srcmap_lines[idx])
                        if r_m:
                            dep_rev = r_m.group(1)
                            break
                if dep_url and dep_rev:
                    dep_item = {
                        'url': clean_string(dep_url),
                        'rev': clean_string(dep_rev)
                    }
                    if dep_item not in all_deps:
                        all_deps.append(dep_item)

        # 如果方法 A 或 B 提取成功，从 dependencies 列表中剔除主项目，以防混淆
        if success:
            aux_deps = [d for d in all_deps if d['url'] != metadata['software_repo_url']]
            metadata['dependencies'] = aux_deps[:50]
        else:
            metadata['dependencies'] = all_deps[:50]

        # 7. 补全日志文件物理信息
        file_stats = os.stat(log_path)
        metadata['file_info'] = {
            'path': log_path,
            'size_bytes': file_stats.st_size,
            'size_human': f"{file_stats.st_size / 1024:.1f} KB"
        }

        return {
            'status': 'success',
            'metadata': metadata,
            'metadata_size': len(json.dumps(metadata))
        }

    except Exception as e:
        return {
            'status': 'error',
            'message': clean_string(str(e)),
            'error_type': type(e).__name__
        }

def get_project_yaml_info(project_name: str, oss_fuzz_path: str) -> Dict[str, str]:
    """读取 project.yaml 获取语言信息。"""
    yaml_path = os.path.join(oss_fuzz_path, "projects", project_name, "project.yaml")
    if not os.path.exists(yaml_path):
        return {'status': 'error', 'message': "project.yaml not found"}
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            return {'status': 'success', 'language': data.get('language', 'unknown')}
    except Exception as e:
        return {'status': 'error', 'message': f"YAML parse error: {e}"}


def find_sha_for_timestamp(commits_file_path: str, target_date_str: str) -> Dict[str, str]:
    """在 commits 文件中查找 SHA。"""
    print(f"--- Tool: find_sha_for_timestamp called for: {target_date_str} ---")

    try:
        # 1. 预处理：统一将连字符和下划线替换为点
        normalized_date = target_date_str.replace('-', '.').replace('_', '.')

        # 2. 如果包含时间（如 2025.10.03T00:00:00），只取 T 或空格前的日期部分
        normalized_date = normalized_date.split('T')[0].split(' ')[0]

        # 3. 尝试解析 YYYY.MM.DD
        # 处理可能的 2025.10.3 -> 2025.10.03 补零问题
        parts = normalized_date.split('.')
        if len(parts) == 3:
            normalized_date = f"{parts[0]}.{int(parts[1]):02d}.{int(parts[2]):02d}"

        target_date = datetime.strptime(normalized_date, '%Y.%m.%d').date()
    except Exception as e:
        return {'status': 'error', 'message': f"Invalid date format: {target_date_str}. Error: {str(e)}"}

    past_commits = []

    try:
        with open(commits_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if line.startswith("Time: ") and i + 1 < len(lines) and lines[i + 1].strip().startswith("- SHA: "):
                    try:
                        timestamp_str = line.replace("Time: ", "")
                        commit_datetime = datetime.strptime(timestamp_str, '%Y.%m.%d %H:%M')
                        sha = lines[i + 1].strip().replace("- SHA: ", "")

                        # 严格只匹配目标日期之前的提交
                        if commit_datetime.date() < target_date:
                            past_commits.append((commit_datetime, sha))
                        i += 2
                    except ValueError:
                        pass
                i += 1
    except FileNotFoundError:
        return {'status': 'error', 'message': f"Commits file not found: {commits_file_path}"}

    if past_commits:
        return {'status': 'success', 'sha': max(past_commits)[1]}
    else:
        return {'status': 'error', 'message': f"No suitable SHA found strictly before {target_date_str}"}


def find_fix_date(log_path: str) -> Dict[str, str]:
    """查找下一个成功构建的日期。"""
    try:
        project_dir = os.path.dirname(log_path)
        error_filename = os.path.basename(log_path)
        
        # 清理可能附带的模式前缀
        date_str = error_filename.split(' ')[0]
        clean_date_str = re.sub(r'^\+(timeout|wrong|\+)?\+?', '', date_str)
        error_date = datetime.strptime(clean_date_str, '%Y_%m_%d')
        
        success_dates = []
        for filename in sorted(os.listdir(project_dir)):
            # 放宽限制：只要包含 "success" 即可
            if "success" in filename:
                try:
                    s_date_str = filename.split(' ')[0]
                    s_date = datetime.strptime(s_date_str, '%Y_%m_%d')
                    if s_date > error_date:
                        success_dates.append(s_date)
                except: continue
        if success_dates:
            return {'status': 'success', 'fix_date': min(success_dates).strftime('%Y-%m-%d')}
        return {'status': 'not_found', 'fix_date': ''}
    except Exception as e:
        return {'status': 'error', 'message': str(e), 'fix_date': ''}


def checkout_oss_fuzz_commit(oss_fuzz_path: str, sha: str) -> Dict[str, str]:
    """切换 OSS-Fuzz 仓库到指定 Commit。"""
    print(f"--- Tool: checkout_oss_fuzz_commit SHA: {sha} ---")
    cwd = os.getcwd()
    try:
        os.chdir(oss_fuzz_path)
        subprocess.run(["git", "checkout", "master"], capture_output=True, check=False) 
        res = subprocess.run(["git", "checkout", sha], capture_output=True, text=True, encoding='utf-8')
        if res.returncode == 0:
            return {'status': 'success', 'message': f"Checked out {sha}"}
        return {'status': 'error', 'message': res.stderr}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    finally:
        os.chdir(cwd)

def patch_project_dockerfile(
    project_name: str, 
    oss_fuzz_path: str, 
    base_image_digest: str, 
    dependencies: List[Dict]
) -> Dict[str, str]:
    """修改 Dockerfile 锁定 Base Image 和 Git 版本。"""
    print(f"--- Tool: patch_project_dockerfile for {project_name} ---")
    dockerfile_path = os.path.join(oss_fuzz_path, "projects", project_name, "Dockerfile")
    backup_path = dockerfile_path + ".bak"

    if not os.path.exists(dockerfile_path):
        return {'status': 'error', 'message': "Dockerfile not found."}

    if os.path.exists(backup_path):
        shutil.copy2(backup_path, dockerfile_path)
    else:
        shutil.copy2(dockerfile_path, backup_path)

    try:
        with open(dockerfile_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        new_lines = []
        for line in lines:
            stripped = line.strip()
            
            # Patch Base Image
            if stripped.startswith("FROM") and "oss-fuzz-base" in stripped and base_image_digest:
                base_image = stripped.split()[1].split(':')[0].split('@')[0]
                line = f"FROM {base_image}@sha256:{base_image_digest}\n"
                print(f"--- Patched Base Image ---")
            
            # Patch Git Clone
            if stripped.startswith("RUN") and "git clone" in stripped:
                for dep in dependencies:
                    url = dep.get('url', '')
                    sha = dep.get('rev', '')
                    simple_url = url.replace("https://", "").replace("http://", "").replace(".git", "")
                    
                    if simple_url in line:
                        line = line.replace("--depth 1", "").replace("--depth=1", "")
                        parts = line.split()
                        dir_name = parts[-1]
                        if dir_name.startswith("http"): 
                            dir_name = url.split('/')[-1].replace('.git', '')
                        
                        clean_line = line.rstrip()
                        if clean_line.endswith("\\"): clean_line = clean_line[:-1].strip()
                        patch_cmd = f" && cd {dir_name} && git checkout {sha} && cd -"
                        
                        if line.strip().endswith("\\"):
                             line = f"{clean_line}{patch_cmd} \\\n"
                        else:
                             line = f"{clean_line}{patch_cmd}\n"
                        print(f"--- Patched Git: {dir_name} -> {sha} ---")
                        break 
            new_lines.append(line)

        with open(dockerfile_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        return {'status': 'success', 'message': "Dockerfile patched."}

    except Exception as e:
        if os.path.exists(backup_path):
            shutil.copy2(backup_path, dockerfile_path)
        return {'status': 'error', 'message': f"Patch failed: {e}"}


def reset_project_environment(project_name: str, oss_fuzz_path: str) -> Dict[str, str]:
    """
    深度清理复现环境，确保下一次复现从干净的状态开始。
    🔑 升级：增加了自动删除本地克隆的第三方项目源码仓库的机制，防止残留。
    """
    print(f"--- Tool: reset_project_environment (Deep Clean) for {project_name} ---")
    clean_results = []

    # 1. 还原 Dockerfile
    dockerfile_path = os.path.join(oss_fuzz_path, "projects", project_name, "Dockerfile")
    backup_path = dockerfile_path + ".bak"
    if os.path.exists(backup_path):
        try:
            shutil.move(backup_path, dockerfile_path)
            clean_results.append("Dockerfile restored")
        except Exception as e:
            clean_results.append(f"Dockerfile restore failed: {e}")

    # 2. 清理编译产物目录
    for folder in ['out', 'work']:
        artifact_path = os.path.join(oss_fuzz_path, "build", folder, project_name)
        if os.path.exists(artifact_path):
            try:
                shutil.rmtree(artifact_path)
                clean_results.append(f"Cleaned build/{folder}/{project_name}")
            except Exception as e:
                try:
                    build_dir_abs = os.path.abspath(os.path.join(oss_fuzz_path, "build"))
                    cmd = [
                        "docker", "run", "--rm",
                        "-v", f"{build_dir_abs}:/build_dir",
                        "gcr.io/oss-fuzz-base/base-builder",
                        "rm", "-rf", f"/build_dir/{folder}/{project_name}"
                    ]
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode == 0:
                        clean_results.append(f"Cleaned build/{folder}/{project_name} (via Docker fallback)")
                    else:
                        clean_results.append(f"Folder cleanup failed ({folder}): {e} | Docker fallback failed: {res.stderr.strip()}")
                except Exception as docker_e:
                    clean_results.append(f"Folder cleanup failed ({folder}): {e} | Docker fallback exception: {docker_e}")

    # 3. 删除生成的 Docker 镜像
    image_name = f"gcr.io/oss-fuzz/{project_name}"
    try:
        check_img = subprocess.run(["docker", "images", "-q", image_name], capture_output=True, text=True)
        if check_img.stdout.strip():
            subprocess.run(["docker", "rmi", "-f", image_name], capture_output=True)
            clean_results.append(f"Docker image {image_name} removed")
    except Exception as e:
        clean_results.append(f"Docker rmi failed: {e}")

    # 4. Git 仓库状态重置
    cwd = os.getcwd()
    try:
        os.chdir(oss_fuzz_path)
        subprocess.run(["git", "reset", "--hard"], capture_output=True)
        subprocess.run(["git", "clean", "-fd"], capture_output=True)
        clean_results.append("Git repository hard reset")
    except Exception as e:
        clean_results.append(f"Git reset failed: {e}")
    finally:
        os.chdir(cwd)

    # 🔑 5. 自动销毁本地挂载模式拉取的第三方源码仓库
    safe_name = "".join(c for c in project_name if c.isalnum() or c in ('_', '-')).rstrip()
    local_repo_dir = os.path.abspath(os.path.join(cwd, "process", "project", safe_name))
    if os.path.exists(local_repo_dir):
        try:
            shutil.rmtree(local_repo_dir)
            clean_results.append(f"Cleaned local project repo: {local_repo_dir}")
        except Exception as e:
            # 如果因为权限原因无法删除，回退到容器内使用 root 权限执行 rm
            try:
                cmd = [
                    "docker", "run", "--rm",
                    "-v", f"{os.path.dirname(local_repo_dir)}:/parent_dir",
                    "gcr.io/oss-fuzz-base/base-builder",
                    "rm", "-rf", f"/parent_dir/{safe_name}"
                ]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode == 0:
                    clean_results.append(f"Cleaned local project repo (via Docker fallback): {local_repo_dir}")
                else:
                    clean_results.append(f"Local repo cleanup failed: {e} | Docker fallback failed: {res.stderr.strip()}")
            except Exception as docker_e:
                clean_results.append(f"Local repo cleanup failed: {e} | Docker fallback exception: {docker_e}")

    msg = " | ".join(clean_results) if clean_results else "Environment was already clean."
    print(f"--- Cleanup Summary: {msg} ---")
    return {'status': 'success', 'message': msg}

def run_fuzz_build_streaming(
        project_name: str,
        oss_fuzz_path: str,
        sanitizer: str,
        engine: str,
        architecture: str,
        original_log_path: str,
        mount_path: Optional[str] = None,  # 🔑 新增：接受本地挂载路径参数
        timeout: int = 7200
) -> dict:
    """
    支持本地挂载模式的模糊构建流程。
    """
    print(f"--- Tool: run_fuzz_build_streaming {project_name} (Mount: {mount_path}) ---")
    LOG_DIR = "build_logs"
    os.makedirs(LOG_DIR, exist_ok=True)
    LOG_FILE_PATH = os.path.join(LOG_DIR, f"{project_name}_reproduce_log.txt")

    start_time = time.time()
    helper_path = os.path.join(oss_fuzz_path, "infra/helper.py")

    def execute_with_timeout(cmd_list, step_name):
        print(f"--- Starting {step_name} for '{project_name}' ---")
        process = subprocess.Popen(
            cmd_list,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=oss_fuzz_path,
            bufsize=1,
            encoding='utf-8',
            errors='ignore',
            start_new_session=True
        )

        sel = selectors.DefaultSelector()
        sel.register(process.stdout, selectors.EVENT_READ)

        try:
            while True:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    print(f"\n!!! [TIMEOUT] {step_name} for '{project_name}' exceeded {timeout}s.")
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except:
                        pass
                    return "timeout"

                events = sel.select(timeout=1)
                if events:
                    line = process.stdout.readline()
                    if line:
                        print(f"[{project_name}][{step_name}] {line}", end='')
                        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
                            f.write(line)

                if process.poll() is not None:
                    for line in process.stdout:
                        print(f"[{project_name}][{step_name}] {line}", end='')
                        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
                            f.write(line)
                    break

            return "success" if process.returncode == 0 else "error"
        finally:
            sel.close()

    # Step 1: Build Image
    res1 = execute_with_timeout(
        ["python3", helper_path, "build_image", "--no-pull", project_name],
        "Step 1: Build Image"
    )
    if res1 == "timeout":
        return {"status": "timeout", "message": "Image build timed out.", "new_build_log_path": LOG_FILE_PATH}

    # Step 2: Build Fuzzers (根据是否配置 mount_path 组装指令)
    rem = timeout - (time.time() - start_time)
    if rem <= 0:
        return {"status": "timeout", "message": "No time left for Fuzzer build.", "new_build_log_path": LOG_FILE_PATH}

    # 🔑 核心挂载命令组装：build_fuzzers <project_name> [mount_path] --sanitizer ...
    build_cmd = ["python3", helper_path, "build_fuzzers"]
    if mount_path:
        # 本地挂载模式，在项目名后插入绝对路径
        build_cmd.extend([project_name, os.path.abspath(mount_path)])
    else:
        # 常规构建模式
        build_cmd.append(project_name)

    build_cmd.extend(["--sanitizer", sanitizer, "--engine", engine, "--architecture", architecture])

    res2 = execute_with_timeout(
        build_cmd,
        "Step 2: Build Fuzzers"
    )

    if res2 == "timeout":
        return {"status": "timeout", "message": "Fuzzer build timed out.", "new_build_log_path": LOG_FILE_PATH}

    return {"status": res2, "new_build_log_path": LOG_FILE_PATH}


from collections import deque


def compare_error_logs(original_log_path: str, new_log_path: str) -> Dict:
    """
    提取原始下载的报错日志的最后30行与复现后的报错日志的最后30行，返回给 Agent 进行分析。
    """
    print(f"--- Tool: compare_error_logs (Tail 30 Lines Extraction) ---")

    def get_last_n_lines(file_path: str, n: int = 30) -> str:
        if not os.path.exists(file_path):
            return f"Error: File '{file_path}' not found."
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                # 使用 deque 高效获取文件末尾 N 行，避免一次性加载大文件
                last_lines = deque(f, maxlen=n)
            return "".join(last_lines)
        except Exception as e:
            return f"Error reading file '{file_path}': {e}"

    original_tail = get_last_n_lines(original_log_path, 30)
    new_tail = get_last_n_lines(new_log_path, 30)

    return {
        'status': 'success',
        'original_log_tail': original_tail,
        'new_log_tail': new_tail
    }

def append_to_reproduce_report(data: Dict) -> Dict[str, str]:
    """写入 YAML 报告。"""
    print(f"--- Tool: append_to_reproduce_report ---")
    project = data.get("project")
    if not project or project == "null" or project is None:
        print("!!! Warning: Attempted to write a null project report. Blocked.")
        return {'status': 'error', 'message': 'Invalid data: project name is missing.'}
    file_path = "reproduce_report.yaml"
    entry = [{
        "project": data.get("project"),
        "language": data.get("language"),
        "error_time": data.get("error_time"),
        "last_success_time": data.get("last_success_time"),
        "oss-fuzz_sha": data.get("oss-fuzz_sha"),
        "fuzzing_build_error_log": data.get("fuzzing_build_error_log"),
        "software_repo_url": data.get("software_repo_url"),
        "software_sha": data.get("software_sha"),
        "engine": data.get("engine"),
        "sanitizer": data.get("sanitizer"),
        "architecture": data.get("architecture"),
        "base_image_digest": data.get("base_image_digest"),
        "error_category": data.get("error_category"),
        "fixed_state": "no"
    }]

    try:
        mode = 'a' if os.path.exists(file_path) else 'w'
        with open(file_path, mode, encoding='utf-8') as f:
            if mode == 'a' and os.path.getsize(file_path) > 0:
                f.write("\n")
            yaml.dump(entry, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return {'status': 'success', 'message': "Report appended."}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
