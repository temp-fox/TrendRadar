# coding=utf-8
"""
导出匹配新闻并触发 ai-news-writer

从当天 SQLite 数据库读取匹配频率词的新闻，
按大类（养老金/养生美食/民生）分组去重，
通过 GitHub API 发送 repository_dispatch 到 ai-news-writer 仓库。
"""

import json
import os
import sys
from datetime import datetime

import urllib.request
import urllib.error

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trendradar.core.frequency import load_frequency_words, _word_matches
from trendradar.core.data import read_all_today_titles_from_storage
from trendradar.storage.local import LocalStorageBackend


# 词组 display_name → 大类 的映射
CATEGORY_MAP = {
    # 养老金
    "养老金政策": "养老金",
    "养老生活": "养老金",
    # 养生美食
    "养生总论": "养生美食",
    "健脾养胃": "养生美食",
    "润肺止咳": "养生美食",
    "补气补血": "养生美食",
    "护肝解毒": "养生美食",
    "养肾固本": "养生美食",
    "祛湿排毒": "养生美食",
    "清热降火": "养生美食",
    "明目护眼": "养生美食",
    "安神助眠": "养生美食",
    "驱寒暖身": "养生美食",
    "四季节气养生": "养生美食",
    "滋补食材": "养生美食",
    "养生食材": "养生美食",
    "养生食谱": "养生美食",
    "茶饮养生": "养生美食",
    # 民生
    "社会保障": "民生",
    "住房民生": "民生",
    "教育民生": "民生",
    "生活成本": "民生",
    "就业民生": "民生",
    "医疗健康": "民生",
    "安全与灾害": "民生",
    "人口与家庭": "民生",
}


def match_title_to_category(title, word_groups, filter_words, global_filters):
    """
    检查标题匹配哪个大类，返回类别名称或 None。
    与 matches_word_groups() 逻辑一致，但额外返回匹配的词组信息。
    """
    if not isinstance(title, str) or not title.strip():
        return None

    title_lower = title.lower()

    # 全局过滤
    if global_filters:
        if any(gw.lower() in title_lower for gw in global_filters):
            return None

    # 过滤词检查
    for filter_item in filter_words:
        if _word_matches(filter_item, title_lower):
            return None

    # 词组匹配 — 找到第一个匹配的词组，返回对应大类
    for group in word_groups:
        required_words = group["required"]
        normal_words = group["normal"]

        if required_words:
            if not all(_word_matches(req, title_lower) for req in required_words):
                continue

        if normal_words:
            if not any(_word_matches(nw, title_lower) for nw in normal_words):
                continue

        display_name = group.get("display_name", "")
        return CATEGORY_MAP.get(display_name)

    return None


def export_matched_news():
    """主流程：读取数据 → 匹配分类 → 去重合并 → 发送 dispatch"""

    # 1. 初始化存储后端
    storage = LocalStorageBackend(data_dir="output", timezone="Asia/Shanghai")

    # 2. 加载频率词
    word_groups, filter_words, global_filters = load_frequency_words()

    # 3. 读取当天所有标题
    all_results, id_to_name, _ = read_all_today_titles_from_storage(storage)

    if not all_results:
        print("[export] 今天没有抓取到任何数据，跳过")
        return

    # 4. 按大类分组 + 跨平台去重
    # title_map: {category: {title: {"sources": [...], "url": "..."}}}
    title_map = {}

    for source_id, titles in all_results.items():
        source_name = id_to_name.get(source_id, source_id)
        for title, data in titles.items():
            category = match_title_to_category(
                title, word_groups, filter_words, global_filters
            )
            if not category:
                continue

            if category not in title_map:
                title_map[category] = {}

            if title in title_map[category]:
                # 已有相同标题 — 合并来源
                if source_name not in title_map[category][title]["sources"]:
                    title_map[category][title]["sources"].append(source_name)
            else:
                title_map[category][title] = {
                    "sources": [source_name],
                    "url": data.get("url", ""),
                }

    if not title_map:
        print("[export] 今天没有匹配的新闻，跳过 dispatch")
        return

    # 5. 构建 payload
    categories_payload = {}
    total_count = 0
    for category, titles in title_map.items():
        items = []
        for title, info in titles.items():
            items.append({
                "title": title,
                "sources": info["sources"],
                "url": info["url"],
            })
        categories_payload[category] = items
        total_count += len(items)

    now = datetime.now()
    payload = {
        "event_type": "new_articles",
        "client_payload": {
            "date": now.strftime("%Y-%m-%d"),
            "crawl_time": now.strftime("%H:%M"),
            "categories": categories_payload,
        },
    }

    # 打印统计
    print(f"[export] 匹配新闻统计:")
    for cat, items in categories_payload.items():
        print(f"  {cat}: {len(items)} 条")
    print(f"  总计: {total_count} 条")

    # 6. 发送 repository_dispatch
    pat_token = os.environ.get("PAT_TOKEN", "")
    target_repo = os.environ.get("AI_NEWS_WRITER_REPO", "")

    if not pat_token or not target_repo:
        print("[export] 未配置 PAT_TOKEN 或 AI_NEWS_WRITER_REPO，跳过 dispatch")
        print("[export] 输出 payload 供调试:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    dispatch_url = f"https://api.github.com/repos/{target_repo}/dispatches"
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # 检查 payload 大小
    payload_size_kb = len(payload_bytes) / 1024
    print(f"[export] Payload 大小: {payload_size_kb:.1f} KB")
    if payload_size_kb > 60:
        print(f"[export] 警告: payload 接近 65KB 限制 ({payload_size_kb:.1f} KB)")

    req = urllib.request.Request(
        dispatch_url,
        data=payload_bytes,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {pat_token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            print(f"[export] dispatch 成功 (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        print(f"[export] dispatch 失败: HTTP {e.code} - {e.read().decode()}")
    except urllib.error.URLError as e:
        print(f"[export] dispatch 请求异常: {e.reason}")


if __name__ == "__main__":
    try:
        export_matched_news()
    except Exception as e:
        # dispatch 失败不影响主流程
        print(f"[export] 导出脚本异常: {e}")
        import traceback
        traceback.print_exc()
