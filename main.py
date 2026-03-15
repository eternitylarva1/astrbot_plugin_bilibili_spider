#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站视频搜索爬虫插件 for AstrBot
按关键词搜索视频，按时间排序，获取播放量和发布时间
支持配置化：SESSDATA、搜索关键词、数量、筛选模式等
"""

import requests
import json
import time
from datetime import datetime
from urllib.parse import quote, unquote
from typing import Optional, List, Dict, Any

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
import astrbot.api.message_components as Comp


class BilibiliSpider:
    """B站视频搜索爬虫类"""

    def __init__(
        self,
        sessdata: str = "",
        tiered_filter: bool = True,
        tier_5h: float = 2000.0,
        tier_24h: float = 1500.0,
        tier_old: float = 1000.0,
    ):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        # 基础 cookies
        self.cookies = {
            "buvid3": "F",
            "buvid4": "F",
        }
        if sessdata:
            # 解析 SESSDATA，可能包含 URL 编码
            try:
                # 尝试 URL 解码
                decoded_sessdata = unquote(sessdata)
                self.cookies["SESSDATA"] = decoded_sessdata
            except Exception:
                self.cookies["SESSDATA"] = sessdata

        # 分层筛选配置
        self.tiered_filter = tiered_filter
        self.tier_5h = tier_5h
        self.tier_24h = tier_24h
        self.tier_old = tier_old
        
        self.base_url = "https://api.bilibili.com/x/web-interface/search/type"
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.session.cookies.update(self.cookies)
        
        logger.info(f"B站爬虫初始化 | SESSDATA已配置: {bool(sessdata)} | 分层筛选: {tiered_filter} | 阈值: 5h内>{tier_5h}/h, 5-24h>{tier_24h}/h, 24h+>{tier_old}/h")

    def search_videos(
        self, keyword: str, page: int = 1, page_size: int = 30, order: str = "pubdate"
    ) -> Optional[dict]:
        """搜索视频
        
        Args:
            keyword: 搜索关键词
            page: 页码
            page_size: 每页数量
            order: 排序方式 (pubdate/click/stow) - 发布时间/播放量/收藏数
        """
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "order": order,
        }

        try:
            response = self.session.get(self.base_url, params=params, timeout=10)
            # 记录响应状态和部分内容用于调试
            logger.info(f"B站API响应: status={response.status_code}, url={response.url}")
            if response.status_code != 200:
                logger.warning(f"B站API非200响应: {response.text[:200]}")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"B站搜索请求失败: {e}")
            return None

    def search_videos_until_filtered(
        self, keyword: str, min_filtered_count: int, order: str = "pubdate", min_play_per_hour: float = 0
    ) -> List[dict]:
        """持续搜索直到满足筛选条件的视频达到指定数量
        
        Args:
            keyword: 搜索关键词
            min_filtered_count: 需要达成的筛选后最小数量
            order: 排序方式 (pubdate/click/stow)
            min_play_per_hour: 筛选条件（已废弃，使用分层筛选）
        """
        videos = []
        page_size = 30
        page = 1
        filtered_count = 0

        while filtered_count < min_filtered_count:
            result = self.search_videos(keyword, page=page, page_size=page_size, order=order)

            if not result or result.get("code") != 0:
                break

            data = result.get("data", {})
            result_list = data.get("result", [])

            if not result_list:
                break

            for item in result_list:
                video_info = self._parse_video_item(item)

                # 检查是否满足分层筛选条件
                # 5小时内: >2000/小时, 5-24小时: >1500/小时, 24小时以上: >1000/小时
                if self.check_video_filter(video_info, use_tiered=True):
                    videos.append(video_info)
                    filtered_count += 1

                    if filtered_count >= min_filtered_count:
                        break

            if filtered_count >= min_filtered_count:
                break

            if len(result_list) < page_size:
                break

            page += 1
            time.sleep(0.5)

        return videos

    def search_videos_normal(
        self,
        keyword: str,
        max_count: int,
        enable_filter: bool,
        order: str = "pubdate",
        min_play_per_hour: float = 0,
    ) -> List[dict]:
        """普通模式：获取固定数量的视频，然后筛选
        
        Args:
            keyword: 搜索关键词
            max_count: 最大获取数量
            enable_filter: 是否启用筛选
            order: 排序方式 (pubdate/click/stow)
            min_play_per_hour: 播放量/小时阈值（已废弃，使用分层筛选）
        """
        videos = []
        page_size = 30
        page = 1

        while len(videos) < max_count:
            remaining = max_count - len(videos)
            current_page_size = min(page_size, remaining)

            result = self.search_videos(keyword, page=page, page_size=current_page_size, order=order)

            if not result or result.get("code") != 0:
                break

            data = result.get("data", {})
            result_list = data.get("result", [])

            if not result_list:
                break

            for item in result_list:
                if len(videos) >= max_count:
                    break

                video_info = self._parse_video_item(item)
                videos.append(video_info)

            if len(result_list) < current_page_size:
                break

            page += 1
            time.sleep(0.5)

        # 筛选（使用分层阈值）
        if enable_filter:
            videos = [
                v for v in videos if self.check_video_filter(v, use_tiered=True)
            ]

        return videos

    def _parse_video_item(self, item: dict) -> dict:
        """解析视频项"""
        pubdate_timestamp = item.get("pubdate", 0) or 0
        pubdate = datetime.fromtimestamp(pubdate_timestamp)
        hours_since_publish = max(1, (datetime.now() - pubdate).total_seconds() / 3600)
        play_count = int(item.get("play") or 0)
        play_per_hour = play_count / hours_since_publish

        return {
            "title": item.get("title", "")
            .replace('<em class="keyword">', "")
            .replace("</em>", ""),
            "bvid": item.get("bvid", ""),
            "author": item.get("author", ""),
            "play_count": play_count,
            "video_review": int(item.get("video_review") or 0),
            "pubdate": pubdate_timestamp,
            "pubdate_str": pubdate.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": item.get("duration", ""),
            "url": f"https://www.bilibili.com/video/{item.get('bvid', '')}",
            "play_per_hour": round(play_per_hour, 2),
            "hours_since_publish": round(hours_since_publish, 2),
        }

    def get_tiered_threshold(self, hours_since_publish: float) -> float:
        """根据视频发布时间计算分层阈值"""
        if hours_since_publish <= 5:
            return self.tier_5h
        elif hours_since_publish <= 24:
            return self.tier_24h
        else:
            return self.tier_old

    def check_video_filter(self, video: dict, use_tiered: bool = True) -> bool:
        """检查视频是否满足筛选条件
        
        Args:
            video: 视频信息字典
            use_tiered: 是否使用分层阈值
        """
        play_per_hour = video.get("play_per_hour", 0)
        
        # 如果未启用分层筛选，使用默认阈值
        if not self.tiered_filter:
            threshold = 1000
        else:
            hours = video.get("hours_since_publish", 0)
            threshold = self.get_tiered_threshold(hours)
        
        return play_per_hour > threshold

    def format_videos_message(self, videos: List[dict], keyword: str) -> str:
        """格式化视频列表为消息"""
        if not videos:
            return f"未找到关于「{keyword}」的视频"

        lines = [f"🔍 关键词: {keyword}", f"📊 共找到 {len(videos)} 个视频\n"]

        for i, video in enumerate(videos, 1):
            lines.append(f"{i}. {video['title']}")
            lines.append(f"   👤 作者: {video['author']}")
            lines.append(
                f"   ▶️ 播放: {video['play_count']:,} | ⏰ {video['pubdate_str']}"
            )
            if video.get("play_per_hour"):
                lines.append(f"   🚀 每小时: {video['play_per_hour']:,}/小时")
            lines.append(f"   🔗 {video['url']}")
            lines.append("")

        return "\n".join(lines)

    def format_video_chunk(self, videos: List[dict], keyword: str, chunk_idx: int, total_chunks: int, start_idx: int) -> str:
        """格式化视频片段（分页发送用）"""
        if not videos:
            return f"未找到关于「{keyword}」的视频"

        lines = [f"🔍 关键词: {keyword}", f"📊 第{chunk_idx}/{total_chunks}页 (共{len(videos)}个)\n"]

        for i, video in enumerate(videos, start_idx):
            lines.append(f"{i}. {video['title']}")
            lines.append(f"   👤 作者: {video['author']}")
            lines.append(
                f"   ▶️ 播放: {video['play_count']:,} | ⏰ {video['pubdate_str']}"
            )
            if video.get("play_per_hour"):
                lines.append(f"   🚀 每小时: {video['play_per_hour']:,}/小时")
            lines.append(f"   🔗 {video['url']}")
            lines.append("")

        return "\n".join(lines)


class BilibiliPlugin(Star):
    """B站视频搜索插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 获取配置参数
        self.sessdata = config.get("sessdata", "")
        self.keyword = config.get("default_keyword", "杀戮尖塔2")
        self.order = config.get("order", "pubdate")
        self.target_count = config.get("target_count", 10)
        self.use_collect_mode = config.get("use_collect_mode", True)
        self.default_max_count = config.get("default_max_count", 50)
        self.export_json = config.get("export_json", False)
        
        # 分层筛选配置
        self.tiered_filter = config.get("tiered_filter", True)
        self.tier_5h = config.get("tier_5h_threshold", 2000.0)
        self.tier_24h = config.get("tier_24h_threshold", 1500.0)
        self.tier_old = config.get("tier_old_threshold", 1000.0)
        
        # AI分析配置
        self.analysis_prompt = config.get("analysis_prompt", "请简要总结这些视频的特点和内容，推荐一些高质量的视频，并分析当前的热门趋势")
        self.enable_analysis = config.get("enable_analysis", True)

        logger.info(
            f"B站爬虫插件已加载 | 关键词: {self.keyword} | 排序: {self.order} | 模式: {'集满' if self.use_collect_mode else '普通'} | 分层筛选: {'启用' if self.tiered_filter else '禁用'} | 阈值: 5h内>{self.tier_5h}/h, 5-24h>{self.tier_24h}/h, 24h+>{self.tier_old}/h"
        )

    @filter.command("b站搜索")
    async def bilibili_search(self, event: AstrMessageEvent):
        """
        B站视频搜索
        用法: /b站搜索 <关键词> [数量] [筛选阈值] [总结]
        示例: /b站搜索 杀戮尖塔2 10 1000
        示例: /b站搜索 杀戮尖塔2 总结
        """
        message = event.message_str.strip()
        parts = message.split()

        # 跳过命令名 "b站搜索" 如果它被包含在消息中
        if parts and parts[0] in ["b站搜索", "/b站搜索"]:
            parts = parts[1:]

        # 解析参数
        keyword = self.keyword
        count = self.target_count
        use_collect = self.use_collect_mode
        enable_summary = False  # 是否启用AI总结

        # 检查是否包含总结参数
        param_keywords = [p for p in parts if p not in ["总结", "分析", "summary", "分析"]]
        
        if len(parts) >= 1:
            # 检查是否有总结参数
            if any(p in parts for p in ["总结", "分析", "summary"]):
                enable_summary = True
                # 去除总结关键词，获取实际关键词
                actual_parts = [p for p in parts if p not in ["总结", "分析", "summary"]]
                if actual_parts:
                    try:
                        # 尝试解析数量
                        keyword = actual_parts[0]
                        if len(actual_parts) >= 2:
                            count = int(actual_parts[1])
                    except ValueError:
                        keyword = actual_parts[0]
                else:
                    keyword = self.keyword
            else:
                # 原有逻辑
                if len(parts) >= 1:
                    keyword = parts[0]
                if len(parts) >= 2:
                    try:
                        count = int(parts[1])
                    except ValueError:
                        pass

        logger.info(f"开始搜索 | 关键词: {keyword} | 数量: {count}")

        # 发送正在搜索的提示
        order_names = {"pubdate": "发布时间", "click": "播放量", "stow": "收藏数"}
        loading_msg = f"🔍 正在搜索「{keyword}」...\n"
        loading_msg += f"   📌 数量: {count} | 筛选: 5h内>{self.tier_5h}/h, 5-24h>{self.tier_24h}/h, 24h+>{self.tier_old}/h | 排序: {order_names.get(self.order, self.order)}"
        yield event.plain_result(loading_msg)

        # 创建爬虫实例
        spider = BilibiliSpider(
            sessdata=self.sessdata,
            tiered_filter=self.tiered_filter,
            tier_5h=self.tier_5h,
            tier_24h=self.tier_24h,
            tier_old=self.tier_old,
        )

        # 根据模式搜索
        if use_collect:
            videos = spider.search_videos_until_filtered(
                keyword, min_filtered_count=count, order=self.order
            )
        else:
            videos = spider.search_videos_normal(
                keyword, max_count=count, enable_filter=True, order=self.order
            )

        # 检查是否有错误
        if videos is None:
            yield event.plain_result("❌ 搜索失败：B站访问过于频繁，请稍后再试或检查SESSDATA是否过期")
            return

        # 构建转发消息内容
        nodes = []
        
        # 分页结果（每页10个）
        CHUNK_SIZE = 10
        total_videos = len(videos)
        total_chunks = (total_videos + CHUNK_SIZE - 1) // CHUNK_SIZE

        for chunk_idx in range(total_chunks):
            start_idx = chunk_idx * CHUNK_SIZE
            end_idx = min(start_idx + CHUNK_SIZE, total_videos)
            chunk_videos = videos[start_idx:end_idx]

            chunk_msg = spider.format_video_chunk(
                chunk_videos, keyword, chunk_idx + 1, total_chunks, start_idx + 1
            )
            nodes.append(Comp.Node(content=[Comp.Plain(chunk_msg)]))

        # AI 总结（如果启用）
        if enable_summary and videos and self.enable_analysis and self.analysis_prompt:
            try:
                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                if provider_id:
                    # 构建搜索条件描述
                    order_cn = order_names.get(self.order, self.order)
                    search_context = f"以上信息是：B站搜索「{keyword}」，以分层筛选(5h内>{self.tier_5h}/h, 5-24h>{self.tier_24h}/h, 24h+>{self.tier_old}/h)，排序按{order_cn}，筛选{count}个视频的结果，请你"
                    summary_prompt = f"{search_context}\n\n{self.analysis_prompt}\n\n以下是B站视频搜索结果（共{total_videos}个视频）：\n{spider.format_videos_message(videos, keyword)}"
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=summary_prompt,
                    )
                    summary_result = llm_resp.completion_text
                    nodes.append(Comp.Node(content=[Comp.Plain(f"📝 AI总结：\n{summary_result}")]))
            except Exception as e:
                logger.error(f"AI总结失败: {e}")

        # 发送合并转发消息
        yield event.chain_result(nodes)

    @filter.command("b站热门")
    async def bilibili_hot(self, event: AstrMessageEvent):
        """
        B站热门视频搜索（使用默认配置）
        用法: /b站热门 [总结]
        示例: /b站热门 总结
        """
        message = event.message_str.strip()
        parts = message.split()
        
        # 检查是否需要总结
        enable_summary = any(p in parts for p in ["总结", "分析", "summary"]) if parts else False
        
        keyword = self.keyword
        count = self.target_count
        order = self.order

        # 发送正在搜索的提示
        order_names = {"pubdate": "发布时间", "click": "播放量", "stow": "收藏数"}
        loading_msg = f"🔥 正在搜索热门「{keyword}」...\n"
        loading_msg += f"   📌 数量: {count} | 筛选: 5h内>{self.tier_5h}/h, 5-24h>{self.tier_24h}/h, 24h+>{self.tier_old}/h | 排序: {order_names.get(order, order)}"
        if enable_summary:
            loading_msg += " | 🤖 AI总结: 启用"
        yield event.plain_result(loading_msg)

        spider = BilibiliSpider(
            sessdata=self.sessdata,
            tiered_filter=self.tiered_filter,
            tier_5h=self.tier_5h,
            tier_24h=self.tier_24h,
            tier_old=self.tier_old,
        )

        if self.use_collect_mode:
            videos = spider.search_videos_until_filtered(
                keyword, min_filtered_count=count, order=order
            )
        else:
            videos = spider.search_videos_normal(
                keyword,
                max_count=self.default_max_count,
                enable_filter=True,
                order=order,
            )

        # 构建转发消息内容
        nodes = []
        
        # 分页结果（每页10个）
        CHUNK_SIZE = 10
        total_videos = len(videos)
        total_chunks = (total_videos + CHUNK_SIZE - 1) // CHUNK_SIZE

        for chunk_idx in range(total_chunks):
            start_idx = chunk_idx * CHUNK_SIZE
            end_idx = min(start_idx + CHUNK_SIZE, total_videos)
            chunk_videos = videos[start_idx:end_idx]

            chunk_msg = spider.format_video_chunk(
                chunk_videos, keyword, chunk_idx + 1, total_chunks, start_idx + 1
            )
            nodes.append(Comp.Node(content=[Comp.Plain(chunk_msg)]))

        # AI 总结（如果启用）
        if enable_summary and videos and self.enable_analysis and self.analysis_prompt:
            try:
                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                if provider_id:
                    # 构建搜索条件描述
                    order_cn = order_names.get(order, order)
                    search_context = f"以上信息是：B站搜索「{keyword}」，以分层筛选(5h内>{self.tier_5h}/h, 5-24h>{self.tier_24h}/h, 24h+>{self.tier_old}/h)，排序按{order_cn}，筛选{count}个视频的结果，请你"
                    summary_prompt = f"{search_context}\n\n{self.analysis_prompt}\n\n以下是B站视频搜索结果（共{total_videos}个视频）：\n{spider.format_videos_message(videos, keyword)}"
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=summary_prompt,
                    )
                    summary_result = llm_resp.completion_text
                    nodes.append(Comp.Node(content=[Comp.Plain(f"📝 AI总结：\n{summary_result}")]))
            except Exception as e:
                logger.error(f"AI总结失败: {e}")

        # 发送合并转发消息
        yield event.chain_result(nodes)

    @filter.command("b站配置")
    async def bilibili_config(self, event: AstrMessageEvent):
        """查看当前B站爬虫插件配置"""
        order_names = {
            "pubdate": "按发布时间",
            "click": "按播放量",
            "stow": "按收藏数"
        }
        lines = [
            "⚙️ B站爬虫插件当前配置:",
            "",
            f"📌 默认关键词: {self.keyword}",
            f"📌 排序方式: {order_names.get(self.order, self.order)}",
            f"📌 目标数量: {self.target_count}",
            f"📌 分层筛选: 5h内>2000/h, 5-24h>1500/h, 24h+>1000/h",
            f"📌 搜索模式: {'集满模式' if self.use_collect_mode else '普通模式'}",
            f"📌 SESSDATA: {'已配置' if self.sessdata else '未配置'}",
            "",
            "💡 使用说明:",
            "  /b站搜索 <关键词> [数量] - 搜索视频",
            "  /b站搜索 <关键词> 数量 总结 - 搜索并AI总结",
            "  /b站热门 - 使用默认配置搜索",
            "  /b站热门 总结 - 热门搜索并AI总结",
            "  /b站配置 - 查看当前配置",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="bilibili_search")
    async def bilibili_search_tool(self, event: AstrMessageEvent, keyword: str, count: int = 10, summary: bool = False) -> MessageEventResult:
        """搜索B站视频，根据播放量和发布时间筛选高质量视频。
        
        Args:
            keyword(string): 搜索关键词，例如"我的世界"、"杀戮尖塔2"等
            count(number): 返回的视频数量，默认10个，最多25个
            summary(boolean): 是否启用AI总结，默认False
        """
        # 限制数量
        count = min(count, 25)
        
        spider = BilibiliSpider(
            sessdata=self.sessdata,
            tiered_filter=self.tiered_filter,
            tier_5h=self.tier_5h,
            tier_24h=self.tier_24h,
            tier_old=self.tier_old,
        )
        
        # 搜索视频
        if self.use_collect_mode:
            videos = spider.search_videos_until_filtered(
                keyword, min_filtered_count=count, order=self.order
            )
        else:
            videos = spider.search_videos_normal(
                keyword, max_count=count, enable_filter=True, order=self.order
            )
        
        if videos is None:
            yield event.plain_result("搜索失败：B站访问过于频繁，请稍后再试")
            return
        
        # 构建转发消息
        nodes = []
        
        # 分页结果（每页10个）
        CHUNK_SIZE = 10
        total_videos = len(videos)
        total_chunks = (total_videos + CHUNK_SIZE - 1) // CHUNK_SIZE
        
        order_names = {"pubdate": "发布时间", "click": "播放量", "stow": "收藏数"}
        
        for chunk_idx in range(total_chunks):
            start_idx = chunk_idx * CHUNK_SIZE
            end_idx = min(start_idx + CHUNK_SIZE, total_videos)
            chunk_videos = videos[start_idx:end_idx]
            
            chunk_msg = spider.format_video_chunk(
                chunk_videos, keyword, chunk_idx + 1, total_chunks, start_idx + 1
            )
            nodes.append(Comp.Node(content=[Comp.Plain(chunk_msg)]))
        
        # AI 总结（如果启用）
        if summary and videos and self.enable_analysis and self.analysis_prompt:
            try:
                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                if provider_id:
                    order_cn = order_names.get(self.order, self.order)
                    search_context = f"以上信息是：B站搜索「{keyword}」，以分层筛选(5h内>{self.tier_5h}/h, 5-24h>{self.tier_24h}/h, 24h+>{self.tier_old}/h)，排序按{order_cn}，筛选{count}个视频的结果，请你"
                    summary_prompt = f"{search_context}\n\n{self.analysis_prompt}\n\n以下是B站视频搜索结果（共{total_videos}个视频）：\n{spider.format_videos_message(videos, keyword)}"
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=summary_prompt,
                    )
                    summary_result = llm_resp.completion_text
                    nodes.append(Comp.Node(content=[Comp.Plain(f"📝 AI总结：\n{summary_result}")]))
            except Exception as e:
                logger.error(f"AI总结失败: {e}")
        
        # 发送合并转发消息
        yield event.chain_result(nodes)

    async def terminate(self):
        """插件卸载时调用"""
        logger.info("B站爬虫插件已卸载")
