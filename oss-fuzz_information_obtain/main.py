import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import re
import tempfile
import shutil
import os

# 导入timedelta用于时间计算
from datetime import datetime, timedelta

def parse_page_content(page_html):
    """
    一个独立的函数，负责解析单个页面的HTML内容并提取时间和SHA。
    这个函数可以被重复调用，以处理多个页面。

    Args:
        page_html (str): 由Selenium获取的、已完全渲染的页面HTML字符串。

    Returns:
        list: 一个列表，每个元素都是一个元组 (timestamp, sha)。
    """

    page_data = []
    soup = BeautifulSoup(page_html, 'html.parser')

    main_container = soup.find('div', attrs={'data-target': 'react-app.reactRoot'})
    if not main_container:
        print("错误：在当前页面中无法找到根节点 'react-app.reactRoot'。")
        return []

    all_commits = main_container.find_all('li', attrs={'data-commit-link': True})

    if not all_commits:
        print("警告：在当前页面中未能找到任何提交项。")
        return []

    for commit_li in all_commits:
        commit_link = commit_li.get('data-commit-link')
        if not commit_link:
            continue
        sha = commit_link.split('/')[-1]

        relative_time_tag = commit_li.find('relative-time', attrs={'datetime': True})
        if not relative_time_tag:
            continue

        utc_time_str = relative_time_tag['datetime']

        try:
            utc_dt = datetime.strptime(utc_time_str.replace('Z', ''), '%Y-%m-%dT%H:%M:%S.%f')
            gmt8_offset = timedelta(hours=8)
            gmt8_dt = utc_dt + gmt8_offset
            formatted_timestamp = gmt8_dt.strftime('%Y.%m.%d %H:%M')
        except ValueError:
            print(f"警告：无法解析时间戳 '{utc_time_str}'，已跳过SHA: {sha}")
            continue

        page_data.append((formatted_timestamp, sha))

    return page_data

def scrape_github_commits(base_url, pages_to_scrape=10):
    all_commits_data = []

    # 修正后的正确路径
    chrome_driver_path = "/root/reproduce_note_fuzz/oss-fuzz_information_obtain/chromedriver/chromedriver-linux64/chromedriver"
    
    print(f"使用 ChromeDriver 路径: {chrome_driver_path}")
    
    # 检查文件是否存在
    if not os.path.exists(chrome_driver_path):
        print(f"错误：ChromeDriver 文件不存在: {chrome_driver_path}")
        return []

    service = Service(chrome_driver_path)

    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--log-level=3')

    # 解决用户数据目录冲突
    options.add_argument('--disable-gpu')
    options.add_argument('--remote-debugging-port=0')

    # 创建临时用户数据目录
    user_data_dir = tempfile.mkdtemp()
    options.add_argument(f'--user-data-dir={user_data_dir}')

    print("正在启动 Chrome 浏览器...")
    try:
        driver = webdriver.Chrome(service=service, options=options)
        print("Chrome 浏览器启动成功！")
    except Exception as e:
        print(f"启动 Chrome 浏览器失败: {e}")
        # 清理临时目录
        try:
            shutil.rmtree(user_data_dir)
        except:
            pass
        return []

    try:
        # 您的爬虫逻辑保持不变
        print(f"正在从初始网址爬取数据:\n{base_url}\n")
        driver.get(base_url)
        print("正在等待页面所有动态内容加载完成...")
        WebDriverWait(driver, 50).until(
            EC.presence_of_element_located((By.XPATH, "//relative-time[text()]"))
        )
        print("页面完全渲染完成，正在解析初始页面...")

        initial_html = driver.page_source
        initial_data = parse_page_content(initial_html)

        if not initial_data:
            print("错误：未能从初始页面获取任何数据，程序将终止。")
            return []

        all_commits_data.extend(initial_data)
        base_sha = initial_data[0][1]
        print(f"\n获取到用于翻页的基准SHA: {base_sha}")

        for n in range(1, pages_to_scrape + 1):
            number = 35 * n - 1
            next_url = f"{base_url}?after={base_sha}+{number}"

            print(f"\n--- 正在爬取第 {n}/{pages_to_scrape} 个后续页面 ---")
            print(f"URL: {next_url}")

            driver.get(next_url)
            print("正在等待页面所有动态内容加载完成...")
            try:
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.XPATH, "//relative-time[text()]"))
                )
                print("页面完全渲染完成，正在解析...")

                page_html = driver.page_source
                page_data = parse_page_content(page_html)

                if page_data:
                    all_commits_data.extend(page_data)
                    print(f"成功获取 {len(page_data)} 条新数据。")
                else:
                    print("当前页面没有获取到新数据，可能已到达末页。")
                    break

            except Exception as page_e:
                print(f"爬取页面 {next_url} 时发生错误: {page_e}")
                continue

    except Exception as e:
        print(f"\n[严重错误] 发生错误: {e}")
    finally:
        print("\n所有爬取任务完成，正在关闭浏览器...")
        driver.quit()
        # 清理临时目录
        try:
            shutil.rmtree(user_data_dir)
        except:
            pass

    return all_commits_data

def save_data_to_file(data, filename="github_commits.txt"):
    """
    将爬取到的(时间, SHA)数据对保存到文本文件中。
    """
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("GitHub OSS-Fuzz Commits Information (Timestamp-SHA Pairs)\n")
            f.write("=" * 55 + "\n\n")
            if not data:
                f.write("未能获取到任何提交信息。\n")
                return

            for timestamp, sha in data:
                f.write(f"Time: {timestamp}\n")
                f.write(f"  - SHA: {sha}\n")
                f.write("-" * 25 + "\n")
        print(f"\n数据已成功保存到文件: {filename}")
    except IOError as e:
        print(f"错误：无法写入文件 {filename} - {e}")

# --- 主程序入口 ---
if __name__ == "__main__":
    target_url = "https://github.com/google/oss-fuzz/commits/master/"

    # 调用主函数，除了初始URL，还传入了要额外爬取的页面数
    final_data = scrape_github_commits(target_url, pages_to_scrape=15)

    if final_data:
        print(f"\n爬取成功！总共获取到 {len(final_data)} 条数据。结果如下：\n" + "=" * 30)
        for timestamp, sha in final_data:
            print(f"Time: {timestamp}")
            print(f"  - SHA: {sha}")
            print("-" * 20)

        save_data_to_file(final_data)
    else:
        print("\n未能获取到任何提交信息。请根据上面的错误提示进行检查。")
