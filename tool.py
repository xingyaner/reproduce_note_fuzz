import os
import re
from datetime import datetime

# 配置根目录，修改为你的实际路径
ROOT_DIR = "build_error_log_of_projects"
# 文件名日期正则：匹配 年_月_日
DATE_PATTERN = re.compile(r"(\d{4})_(\d{1,2})_(\d{1,2})")


def parse_file_name(file_name: str):
    """
    解析文件名：返回 (datetime对象, 状态success/error, 原始文件名)
    """
    match = DATE_PATTERN.search(file_name)
    if not match:
        return None, None, file_name
    year, month, day = map(int, match.groups())
    try:
        dt = datetime(year, month, day)
    except ValueError:
        return None, None, file_name

    # 提取状态
    status = "success" if "success" in file_name.lower() else "error"
    return dt, status, file_name


def process_project_dir(project_path: str):
    """处理单个项目文件夹"""
    if not os.path.isdir(project_path):
        return

    file_list = []
    # 遍历目录下所有文件（忽略子目录）
    for f in os.listdir(project_path):
        f_path = os.path.join(project_path, f)
        if os.path.isfile(f_path):
            dt, status, name = parse_file_name(f)
            if dt is None:
                continue
            file_list.append((dt, status, name, f_path))

    # 按时间全局排序
    file_list.sort(key=lambda x: x[0])
    if not file_list:
        return

    # 拆分 success / error 列表
    success_list = [item for item in file_list if item[1] == "success"]
    error_list = [item for item in file_list if item[1] == "error"]

    if not success_list:
        # 无success：按需求可自行处理，这里保留全部error
        return

    # 最终需要保留的文件集合
    keep_names = set()
    # 先保留所有 success
    for s in success_list:
        keep_names.add(s[2])

    # 遍历相邻两个 success 区间，筛选 error
    for i in range(len(success_list)):
        curr_succ_dt = success_list[i][0]
        # 下一个 success 时间（最后一个success 无后续区间）
        next_succ_dt = success_list[i+1][0] if (i + 1) < len(success_list) else None

        # 筛选当前区间内的 error: curr_succ < error < next_succ
        candidates = []
        for err in error_list:
            err_dt = err[0]
            if err_dt <= curr_succ_dt:
                continue
            if next_succ_dt is not None and err_dt >= next_succ_dt:
                continue
            candidates.append(err)

        if not candidates:
            continue

        # 规则：选【距离当前success时间最近】的 error
        candidates.sort(key=lambda x: abs(x[0] - curr_succ_dt))
        best_err = candidates[0]
        keep_names.add(best_err[2])

    # 遍历当前目录文件，删除不在保留列表中的 error
    for f in os.listdir(project_path):
        f_path = os.path.join(project_path, f)
        if os.path.isdir(f_path):
            continue
        # success 全部保留，只清理多余 error
        if "error" in f.lower() and f not in keep_names:
            print(f"[DELETE] {f_path}")
            os.remove(f_path)


def main():
    if not os.path.isdir(ROOT_DIR):
        print(f"根目录不存在: {ROOT_DIR}")
        return

    # 遍历所有项目子文件夹
    for project_name in os.listdir(ROOT_DIR):
        project_dir = os.path.join(ROOT_DIR, project_name)
        if os.path.isdir(project_dir):
            print(f"\n===== 开始处理项目: {project_name} =====")
            process_project_dir(project_dir)

    print("\n✅ 所有项目处理完成！")


if __name__ == "__main__":
    main()

