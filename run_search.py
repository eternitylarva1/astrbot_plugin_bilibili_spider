#!/usr/bin/env python3
"""
独立B站搜索脚本 — 搜索后通过NapCat发送QQ消息（支持合并转发）
用法: python run_search.py [关键词] [-n 数量] [-s] [--forward]
示例: python run_search.py 杀戮尖塔2 -n 10 --forward
"""
import sys
import os
import argparse

# ============ Mock AstrBot 模块 ============
class FakeLogger:
    def info(self, msg): print(f"[INFO] {msg}")
    def warning(self, msg): print(f"[WARN] {msg}")
    def error(self, msg): print(f"[ERRO] {msg}")
    def debug(self, msg): pass

class FakeStar:
    def __init__(self, *args, **kwargs): pass

class FakeModule:
    Star = FakeStar
    logger = FakeLogger()
    def __getattr__(self, name):
        if name == 'Star': return FakeStar
        if name == 'logger': return FakeLogger()
        return FakeModule()
    def __call__(self, *args, **kwargs): return FakeModule()
    def __iter__(self): return iter([])
    def __mro_entries__(self, bases): return (FakeStar,)

for mod in ('astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.star',
            'astrbot.core', 'astrbot.core.utils', 'astrbot.core.utils.astrbot_path',
            'astrbot.api.message_components'):
    sys.modules[mod] = FakeModule()

# ============ 配置 ============
def _load_config():
    """从环境变量或 config.json 加载配置"""
    config = {
        "qq_user_id": int(os.environ.get("QQ_USER_ID", 2674610176)),
        "qq_nickname": os.environ.get("QQ_NICKNAME", "B站爬虫"),
        "napcat_url": os.environ.get("NAPCAT_URL", "http://127.0.0.1:3000"),
        "bilibili_sessdata": os.environ.get("BILIBILI_SESSDATA", ""),
    }
    # 尝试从 config.json 读取
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_search_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            for k in config:
                if k in file_cfg:
                    config[k] = file_cfg[k]
        except Exception as e:
            print(f"⚠️ 读取 run_search_config.json 失败: {e}")
    return config

import json
_CONFIG = _load_config()
QQ_USER_ID = _CONFIG["qq_user_id"]
QQ_NICKNAME = _CONFIG["qq_nickname"]
NAPCAT_BASE = _CONFIG["napcat_url"]
BILIBILI_SESSDATA = _CONFIG["bilibili_sessdata"]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from main import BilibiliSpider
import requests as http_req


def make_node(user_id: str, nickname: str, text: str) -> dict:
    """构造一个合并转发节点"""
    return {
        "type": "node",
        "data": {
            "user_id": user_id,
            "nickname": nickname,
            "content": [{"type": "text", "data": {"text": text}}]
        }
    }


def send_qq_text(text: str):
    """发送普通文本消息"""
    payload = {
        "user_id": QQ_USER_ID,
        "message": [{"type": "text", "data": {"text": text}}]
    }
    try:
        resp = http_req.post(f"{NAPCAT_BASE}/send_private_msg", json=payload, timeout=10)
        result = resp.json()
        if result.get("status") == "ok":
            print(f"✅ 文本已发送 (msg_id: {result['data']['message_id']})")
        else:
            print(f"❌ 发送失败: {result}")
    except Exception as e:
        print(f"❌ 发送异常: {e}")


def send_qq_forward(nodes: list):
    """发送合并转发消息"""
    payload = {
        "user_id": QQ_USER_ID,
        "messages": nodes
    }
    try:
        resp = http_req.post(f"{NAPCAT_BASE}/send_private_forward_msg", json=payload, timeout=15)
        result = resp.json()
        if result.get("status") == "ok":
            print(f"✅ 合并转发已发送 (msg_id: {result['data']['message_id']})")
        else:
            print(f"❌ 发送失败: {result}")
    except Exception as e:
        print(f"❌ 发送异常: {e}")


def send_qq_message(text: str, use_forward: bool = False, chunks: list = None):
    """根据配置发送消息（普通文本或合并转发）"""
    if not use_forward:
        send_qq_text(text)
        return

    # 合并转发模式
    nodes = []
    if chunks:
        # 已经是分好段的列表
        for chunk in chunks:
            nodes.append(make_node(str(QQ_USER_ID), QQ_NICKNAME, chunk))
    else:
        # 单条消息
        MAX_LEN = 1500
        if len(text) > MAX_LEN:
            chunks_split = []
            start = 0
            while start < len(text):
                end = start + MAX_LEN
                if end < len(text):
                    nl = text.rfind("\n", start, end)
                    if nl > start: end = nl
                chunks_split.append(text[start:end])
                start = end
            for chunk in chunks_split:
                nodes.append(make_node(str(QQ_USER_ID), QQ_NICKNAME, chunk))
        else:
            nodes.append(make_node(str(QQ_USER_ID), QQ_NICKNAME, text))

    send_qq_forward(nodes)


def main():
    parser = argparse.ArgumentParser(description="B站视频搜索并发送QQ消息")
    parser.add_argument("keyword", nargs="?", default="杀戮尖塔", help="搜索关键词")
    parser.add_argument("-n", "--count", type=int, default=10, help="目标数量 (默认10)")
    parser.add_argument("-s", "--summary", action="store_true", help="附加AI分析提示")
    parser.add_argument("--order", default="pubdate",
                        choices=["pubdate", "click", "stow"],
                        help="排序方式 (默认pubdate)")
    parser.add_argument("--forward", action="store_true",
                        help="使用合并转发发送（NapCat send_private_forward_msg）")
    parser.add_argument("--user-id", type=int, default=QQ_USER_ID,
                        help=f"接收QQ号 (默认 {QQ_USER_ID})")
    args = parser.parse_args()

    if not BILIBILI_SESSDATA:
        print("⚠️ 未设置 BILIBILI_SESSDATA，将不使用Cookie（可能被反爬）")

    print(f"🔍 正在搜索「{args.keyword}」(目标{args.count}个)...")

    spider = BilibiliSpider(
        sessdata=BILIBILI_SESSDATA,
        play_per_hour_threshold=1500,
        play_count_week_threshold=5000,
        play_count_month_threshold=3000,
    )

    videos = spider.search_videos_until_filtered(
        args.keyword, min_filtered_count=args.count, order=args.order
    )

    if not videos:
        msg = f"未找到关于「{args.keyword}」满足筛选条件的视频"
        print(msg)
        send_qq_message(msg, use_forward=args.forward)
        return

    print(f"✅ 找到 {len(videos)} 个视频")

    result_text = spider.format_videos_message(videos, args.keyword)

    if args.summary:
        result_text += f"\n\n📊 共筛选 {len(videos)} 个视频，请AI根据以上内容进行分析总结。"

    if args.forward:
        # 合并转发模式：每个视频一段
        chunks = []
        for i, v in enumerate(videos, 1):
            play = v.get('play_count', 0)
            play_str = f"{play:,}" if play < 10000 else f"{play/10000:.1f}万"
            chunk = (
                f"{i}. {v.get('title', '')}\n"
                f"   👤 {v.get('author', '')}\n"
                f"   ▶️ 播放: {play_str} | ⏰ {v.get('pubdate_str', '')}\n"
                f"   🚀 {v.get('play_per_hour', 0):,.0f}/小时\n"
                f"   🔗 {v.get('url', '')}"
            )
            chunks.append(chunk)
        print(f"📤 以合并转发发送 {len(chunks)} 个节点...")
        send_qq_message("", use_forward=True, chunks=chunks)
    else:
        send_qq_message(result_text, use_forward=False)


if __name__ == "__main__":
    main()
