#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站视频搜索爬虫插件 for AstrBot
按关键词搜索视频，按时间排序，获取播放量和发布时间
支持配置化：SESSDATA、搜索关键词、数量、筛选模式等
支持B站评论功能
"""

import requests
import json
import time
import asyncio
from datetime import datetime
from urllib.parse import quote, unquote
from typing import Optional, List, Dict, Any
from collections import deque
from dataclasses import dataclass

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
import astrbot.api.message_components as Comp


# ============ 安全机制模块 ============

class RateLimiter:
    """限流器 - 防止请求过快"""
    
    def __init__(self, max_requests: int = 10, time_window: int = 60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = deque()
    
    async def acquire(self):
        now = time.time()
        while self.requests and self.requests[0] < now - self.time_window:
            self.requests.popleft()
        
        if len(self.requests) >= self.max_requests:
            sleep_time = self.requests[0] + self.time_window - now
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
                while self.requests and self.requests[0] < time.time() - self.time_window:
                    self.requests.popleft()
        
        self.requests.append(time.time())
    
    def reset(self):
        self.requests.clear()


class CircuitBreaker:
    """熔断器 - 连续失败后暂停"""
    
    def __init__(self, failure_threshold: int = 5, recovery_time: int = 300):
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "closed"
    
    def record_success(self):
        self.failure_count = 0
        self.state = "closed"
    
    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.warning(f"🔌 熔断器打开，连续{self.failure_count}次失败")
    
    async def check(self):
        if self.state == "open":
            if time.time() - self.last_failure_time > self.recovery_time:
                self.state = "half_open"
                logger.info("🔌 熔断器进入半开状态")
                return True
            return False
        return True
    
    def reset(self):
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "closed"


class BilibiliCommentSender:
    """B站评论发送器（带安全机制）"""
    
    def __init__(
        self,
        bili_jct: str = "",
        sessdata: str = "",
        buvid3: str = "F",
        max_daily: int = 100,
        comment_interval: float = 2.0,
    ):
        """
        初始化评论发送器
        
        Args:
            bili_jct: B站 bili_jct Cookie
            sessdata: B站 SESSDATA Cookie
            buvid3: B站 buvid3 Cookie
            max_daily: 每日最大评论数
            comment_interval: 评论间隔（秒）
        """
        # 清理空白字符并存储
        self.clean_sessdata = sessdata.strip() if sessdata else ""
        self.clean_bili_jct = bili_jct.strip() if bili_jct else ""
        
        # 记录配置状态（不记录实际值）
        has_sessdata = bool(self.clean_sessdata and len(self.clean_sessdata) > 10)
        has_bili_jct = bool(self.clean_bili_jct and len(self.clean_bili_jct) > 5)
        logger.info(f"B站评论发送器初始化 | SESSDATA: {'已配置' if has_sessdata else '未配置'} (长度:{len(self.clean_sessdata)}) | bili_jct: {'已配置' if has_bili_jct else '未配置'} (长度:{len(self.clean_bili_jct)})")
        
        # 安全机制
        self.rate_limiter = RateLimiter(max_requests=10, time_window=60)
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_time=300
        )
        
        # 配置
        self.max_daily = max_daily
        self.comment_interval = comment_interval
        
        # 记录今日评论数
        self.daily_comment_count = 0
        self.daily_reset_time = self._get_day_start()
        
        # 上次评论时间
        self.last_comment_time = 0
        
        # 已评论视频记录文件
        self.comment_record_file = "commented_videos.json"
        self.commented_videos = self._load_comment_record()
        
        logger.info(f"B站评论发送器已初始化 | 每日限额: {max_daily} | 间隔: {comment_interval}s | 已记录视频: {len(self.commented_videos)}")
    
    def _get_data_dir(self) -> str:
        """获取数据目录"""
        import os
        # 使用插件目录
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(plugin_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    
    def _load_comment_record(self) -> dict:
        """加载评论记录"""
        import os
        import json
        record_file = os.path.join(self._get_data_dir(), self.comment_record_file)
        if os.path.exists(record_file):
            try:
                with open(record_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}
    
    def _save_comment_record(self):
        """保存评论记录"""
        import os
        import json
        record_file = os.path.join(self._get_data_dir(), self.comment_record_file)
        with open(record_file, "w", encoding="utf-8") as f:
            json.dump(self.commented_videos, f, ensure_ascii=False, indent=2)
    
    def is_commented(self, bvid: str) -> bool:
        """检查视频是否已评论"""
        return bvid in self.commented_videos
    
    def record_comment(self, bvid: str, content: str, title: str = ""):
        """记录评论"""
        self.commented_videos[bvid] = {
            "content": content,
            "title": title,
            "timestamp": time.time()
        }
        self._save_comment_record()
        logger.info(f"已记录评论: {bvid} - {content}")
    
    def _get_day_start(self) -> int:
        """获取今天开始的时间戳"""
        now = time.localtime()
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, 0))
    
    def _reset_daily_count(self):
        """重置每日计数"""
        now = time.time()
        if now >= self.daily_reset_time + 86400:  # 新的一天
            self.daily_comment_count = 0
            self.daily_reset_time = self._get_day_start()
    
    async def _check_and_increment_daily(self) -> bool:
        """检查并增加每日计数"""
        self._reset_daily_count()
        
        if self.daily_comment_count >= self.max_daily:
            logger.warning(f"已达每日最大评论数: {self.max_daily}")
            return False
        
        self.daily_comment_count += 1
        return True
    
    async def _wait_interval(self):
        """等待评论间隔"""
        now = time.time()
        elapsed = now - self.last_comment_time
        if elapsed < self.comment_interval:
            await asyncio.sleep(self.comment_interval - elapsed)
        self.last_comment_time = time.time()
    
    async def send_comment(self, bvid: str, content: str):
        """
        发送根评论（使用requests直接调用API）
        
        Args:
            bvid: 视频BV号
            content: 评论内容
            
        Returns:
            (success: bool, message: str)
        """
        import requests
        import json
        
        # 检查是否已评论（本地记录）
        if self.is_commented(bvid):
            return False, f"视频 {bvid} 已评论过，跳过"
        
        # 安全检查
        if not await self.circuit_breaker.check():
            return False, "熔断器已打开，请稍后再试"
        
        if not await self._check_and_increment_daily():
            return False, "已达每日最大评论数"
        
        # 等待间隔
        await self._wait_interval()
        
        # 获取视频信息
        try:
            await self.rate_limiter.acquire()
            
            # 直接用requests获取视频信息
            view_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.bilibili.com",
            }
            cookies = {
                "SESSDATA": self.clean_sessdata,
                "bili_jct": self.clean_bili_jct,
                "buvid3": "F",
            }
            
            resp = requests.get(view_url, headers=headers, cookies=cookies, timeout=10)
            data = resp.json()
            
            if data.get("code") != 0:
                return False, f"获取视频信息失败: {data.get('message')}"
            
            video_data = data.get("data", {})
            aid = video_data.get("aid")
            title = video_data.get("title", "未知标题")
            
            logger.info(f"📺 视频: {title} (av{aid})")
            
        except Exception as e:
            logger.error(f"获取视频信息失败: {e}")
            self.circuit_breaker.record_failure()
            return False, f"获取视频信息失败: {e}"
        
        # 发送评论
        try:
            await self.rate_limiter.acquire()
            
            # 直接用requests发送评论
            post_url = "https://api.bilibili.com/x/v2/reply/add"
            post_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.bilibili.com",
                "Origin": "https://www.bilibili.com",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }
            post_data = {
                "oid": str(aid),
                "type": "1",
                "message": content,
                "plat": "1",
                "csrf": self.clean_bili_jct,
            }
            
            resp = requests.post(post_url, headers=post_headers, cookies=cookies, data=post_data, timeout=10)
            result = resp.json()
            
            code = result.get("code", -1)
            
            if code == 0:
                data = result.get("data", {})
                rpid = data.get("rpid")
                
                if rpid:
                    self.circuit_breaker.record_success()
                    self.record_comment(bvid, content, title)
                    logger.info(f"✅ 评论发送成功! rpid: {rpid}")
                    return True, f"评论发送成功！\n视频: {title}\n评论: {content}"
                return False, "评论发送成功但未返回ID"
            
            # 错误码处理
            error_msgs = {
                -101: "账号未登录 (SESSDATA无效或已过期)",
                -400: "请求错误",
                -403: "权限不足，可能是Cookie已过期",
                12002: "评论已被删除",
                12051: "评论内容重复",
                12053: "评论审核中",
                12061: "评论已关闭",
            }
            
            msg = error_msgs.get(code, result.get("message", "未知错误"))
            logger.error(f"评论发送失败 [{code}]: {msg}")
            
            # 严重错误熔断
            if code in [-101, -400]:
                self.circuit_breaker.record_failure()
            
            return False, f"接口返回错误代码：{code}，信息：{msg}"
            
            return False, "未知响应格式"
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"评论发送异常: {error_msg}")
            
            if "-401" in error_msg:
                self.circuit_breaker.record_failure()
                return False, "登录已过期，请重新配置SESSDATA"
            
            self.circuit_breaker.record_failure()
            return False, f"发送失败: {error_msg}"
    
    def get_status(self) -> str:
        """获取状态信息"""
        self._reset_daily_count()
        return f"今日已评论: {self.daily_comment_count}/{self.max_daily} | 熔断器: {self.circuit_breaker.state}"


class BilibiliSpider:
    """B站视频搜索爬虫类"""

    def __init__(
        self,
        sessdata: str = "",
        tiered_filter: bool = True,
        tier_hours: list = None,
        tier_thresholds: list = None,
        default_threshold: float = 1500.0,
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
        # 默认分层配置: 5h, 24h, 阈值: 2000, 1500, 1000
        self.tier_hours = tier_hours if tier_hours else [5, 24]
        self.tier_thresholds = tier_thresholds if tier_thresholds else [2000, 1500, 1000]
        self.default_threshold = default_threshold
        
        # 构建日志信息
        tier_str = ", ".join([f"{self.tier_hours[i]}h内>{self.tier_thresholds[i]}/h" for i in range(len(self.tier_hours))])
        tier_str += f", {self.tier_hours[-1]}h+>{self.tier_thresholds[-1]}/h"
        
        self.base_url = "https://api.bilibili.com/x/web-interface/search/type"
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.session.cookies.update(self.cookies)
        
        logger.info(f"B站爬虫初始化 | SESSDATA已配置: {bool(sessdata)} | 分层筛选: {tiered_filter} | 阈值: {tier_str} | 统一阈值: {default_threshold}/h")

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
        for i, hour_limit in enumerate(self.tier_hours):
            if hours_since_publish <= hour_limit:
                return self.tier_thresholds[i]
        # 超过所有时间节点，使用最后一个阈值
        return self.tier_thresholds[-1]

    def check_video_filter(self, video: dict, use_tiered: bool = True) -> bool:
        """检查视频是否满足筛选条件
        
        Args:
            video: 视频信息字典
            use_tiered: 是否使用分层阈值（True使用配置的分层，False使用统一阈值）
        """
        play_per_hour = video.get("play_per_hour", 0)
        
        # 如果使用分层筛选
        if use_tiered and self.tiered_filter:
            hours = video.get("hours_since_publish", 0)
            threshold = self.get_tiered_threshold(hours)
        else:
            # 使用统一阈值
            threshold = self.default_threshold
        
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
        
        # 解析分层时间节点
        tier_hours_str = config.get("tier_hours", "5,24")
        self.tier_hours = [int(x.strip()) for x in tier_hours_str.split(",") if x.strip()]
        
        # 解析分层阈值
        tier_thresholds_str = config.get("tier_thresholds", "2000,1500,1000")
        self.tier_thresholds = [float(x.strip()) for x in tier_thresholds_str.split(",") if x.strip()]
        
        # 统一筛选阈值（用户指定数量时使用）
        self.default_threshold = config.get("default_threshold", 1500.0)
        
        # AI分析配置
        self.analysis_prompt = config.get("analysis_prompt", "请简要总结这些视频的特点和内容，推荐一些高质量的视频，并分析当前的热门趋势")
        self.enable_analysis = config.get("enable_analysis", True)

        # 评论功能配置（通过是否配置bili_jct来判断是否启用）
        self.bili_jct = config.get("bili_jct", "")
        self.max_daily_comments = config.get("max_daily_comments", 100)
        self.comment_interval = config.get("comment_interval", 2.0)
        self.default_comment = config.get("default_comment", "这期神了")

        # 评论发送器
        self.comment_sender = None
        if self.bili_jct:
            self.comment_sender = BilibiliCommentSender(
                bili_jct=self.bili_jct,
                sessdata=self.sessdata,
                max_daily=self.max_daily_comments,
                comment_interval=self.comment_interval
            )

        # 构建日志信息
        tier_str = ", ".join([f"{self.tier_hours[i]}h内>{self.tier_thresholds[i]}/h" for i in range(len(self.tier_hours))])
        tier_str += f", {self.tier_hours[-1]}h+>{self.tier_thresholds[-1]}/h"
        
        logger.info(
            f"B站爬虫插件已加载 | 关键词: {self.keyword} | 排序: {self.order} | 模式: {'集满' if self.use_collect_mode else '普通'} | 分层筛选: {'启用' if self.tiered_filter else '禁用'} | 阈值: {tier_str} | 统一阈值: {self.default_threshold}/h"
        )

    @filter.command("b站搜索")
    async def bilibili_search(self, event: AstrMessageEvent):
        """
        B站视频搜索
        用法: /b站搜索 <关键词> [数量] [总结] [评论 [评论内容]]
        示例: /b站搜索 杀戮尖塔2 10 1000
        示例: /b站搜索 杀戮尖塔2 总结
        示例: /b站搜索 杀戮尖塔2 评论 很棒的视频
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
        enable_comment = False  # 是否启用评论
        comment_content = ""    # 评论内容

        # 检查是否包含评论参数
        comment_idx = -1
        for i, p in enumerate(parts):
            if p == "评论":
                comment_idx = i
                break
        
        if comment_idx >= 0:
            # 提取评论内容（评论后面的所有内容）
            enable_comment = True
            comment_parts = parts[comment_idx + 1:]
            comment_content = " ".join(comment_parts)
            # 去除评论相关内容，获取其他参数
            parts = parts[:comment_idx]
        
        # 过滤掉总结关键词
        filter_keywords = ["总结", "分析", "summary"]
        
        if len(parts) >= 1:
            # 检查是否有总结参数
            if any(p in parts for p in filter_keywords):
                enable_summary = True
                # 去除总结关键词，获取实际关键词
                actual_parts = [p for p in parts if p not in filter_keywords]
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

        logger.info(f"开始搜索 | 关键词: {keyword} | 数量: {count} | 评论: {enable_comment}")

        # 检查评论功能
        if enable_comment:
            if not self.comment_sender:
                yield event.plain_result("❌ 评论功能未启用，请配置bili_jct")
                return
            # 如果没有提供评论内容，使用默认评论
            if not comment_content:
                comment_content = self.default_comment
                yield event.plain_result(f"💬 使用默认评论: {comment_content}")

        # 发送正在搜索的提示
        order_names = {"pubdate": "发布时间", "click": "播放量", "stow": "收藏数"}
        loading_msg = f"🔍 正在搜索「{keyword}」...\n"
        # 判断是否使用用户指定的数量
        user_specified_count = (len(parts) >= 2 and parts[1].isdigit()) if parts else False
        
        # 如果用户指定了数量，使用统一阈值；否则使用分层筛选
        use_tiered = self.tiered_filter and not user_specified_count
        
        # 构建筛选信息
        if use_tiered:
            tier_str = ", ".join([f"{self.tier_hours[i]}h内>{self.tier_thresholds[i]}/h" for i in range(len(self.tier_hours))])
            tier_str += f", {self.tier_hours[-1]}h+>{self.tier_thresholds[-1]}/h"
            filter_info = f"筛选: {tier_str}"
        else:
            filter_info = f"统一阈值: >{self.default_threshold}/h"
        
        loading_msg += f"   📌 数量: {count} | {filter_info} | 排序: {order_names.get(self.order, self.order)}"
        if enable_comment:
            loading_msg += f"\n   💬 将评论: {comment_content}"
        yield event.plain_result(loading_msg)

        # 创建爬虫实例
        spider = BilibiliSpider(
            sessdata=self.sessdata,
            tiered_filter=use_tiered,
            tier_hours=self.tier_hours,
            tier_thresholds=self.tier_thresholds,
            default_threshold=self.default_threshold,
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
                    # 构建分层筛选描述
                    tier_desc = ", ".join([f"{self.tier_hours[i]}h内>{self.tier_thresholds[i]}/h" for i in range(len(self.tier_hours))])
                    tier_desc += f", {self.tier_hours[-1]}h+>{self.tier_thresholds[-1]}/h"
                    search_context = f"以上信息是：B站搜索「{keyword}」，以分层筛选({tier_desc})，排序按{order_cn}，筛选{count}个视频的结果，请你"
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

        # 评论（如果启用）- 在搜索结果发送后评论第一个视频
        if enable_comment and videos and self.comment_sender:
            first_video = videos[0]
            bvid = first_video.get("bvid")
            title = first_video.get("title", "")
            
            yield event.plain_result(f"📝 正在发送评论到第一个视频: {title}")
            
            success, msg = await self.comment_sender.send_comment(bvid, comment_content)
            
            if success:
                yield event.plain_result(f"✅ {msg}")
            else:
                yield event.plain_result(f"❌ 评论失败: {msg}")

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

        # 构建分层筛选信息
        tier_str = ", ".join([f"{self.tier_hours[i]}h内>{self.tier_thresholds[i]}/h" for i in range(len(self.tier_hours))])
        tier_str += f", {self.tier_hours[-1]}h+>{self.tier_thresholds[-1]}/h"
        
        # 发送正在搜索的提示
        order_names = {"pubdate": "发布时间", "click": "播放量", "stow": "收藏数"}
        loading_msg = f"🔥 正在搜索热门「{keyword}」...\n"
        loading_msg += f"   📌 数量: {count} | 筛选: {tier_str} | 排序: {order_names.get(order, order)}"
        if enable_summary:
            loading_msg += " | 🤖 AI总结: 启用"
        yield event.plain_result(loading_msg)

        # b站热门使用默认配置，始终使用分层筛选
        spider = BilibiliSpider(
            sessdata=self.sessdata,
            tiered_filter=self.tiered_filter,
            tier_hours=self.tier_hours,
            tier_thresholds=self.tier_thresholds,
            default_threshold=self.default_threshold,
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
                    # 构建分层筛选描述
                    tier_desc = ", ".join([f"{self.tier_hours[i]}h内>{self.tier_thresholds[i]}/h" for i in range(len(self.tier_hours))])
                    tier_desc += f", {self.tier_hours[-1]}h+>{self.tier_thresholds[-1]}/h"
                    search_context = f"以上信息是：B站搜索「{keyword}」，以分层筛选({tier_desc})，排序按{order_cn}，筛选{count}个视频的结果，请你"
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
        # 构建分层筛选描述
        tier_str = ", ".join([f"{self.tier_hours[i]}h内>{self.tier_thresholds[i]}/h" for i in range(len(self.tier_hours))])
        tier_str += f", {self.tier_hours[-1]}h+>{self.tier_thresholds[-1]}/h"
        
        lines = [
            "⚙️ B站爬虫插件当前配置:",
            "",
            f"📌 默认关键词: {self.keyword}",
            f"📌 排序方式: {order_names.get(self.order, self.order)}",
            f"📌 目标数量: {self.target_count}",
            f"📌 分层筛选: {tier_str}",
            f"📌 统一阈值: >{self.default_threshold}/h (用户指定数量时)",
            f"📌 搜索模式: {'集满模式' if self.use_collect_mode else '普通模式'}",
            f"📌 SESSDATA: {'已配置' if self.sessdata else '未配置'}",
            f"📌 bili_jct: {'已配置' if self.bili_jct else '未配置'}",
            f"📌 默认评论: {self.default_comment}",
            f"📌 已评论视频数: {len(self.comment_sender.commented_videos) if self.comment_sender else 0}",
            "",
            "💡 使用说明:",
            "  /b站搜索 <关键词> [数量] - 搜索视频（默认数量用分层筛选，指定数量用统一阈值）",
            "  /b站搜索 <关键词> 数量 总结 - 搜索并AI总结",
            "  /b站搜索 <关键词> 评论 <内容> - 搜索视频并评论第一个结果",
            "  /b站热门 - 使用默认配置搜索",
            "  /b站热门 总结 - 热门搜索并AI总结",
            "  /b站评论 <BV号> <内容> - 直接发送评论",
            "  /b站评论记录 - 查看已评论的视频列表",
            "  /b站配置 - 查看当前配置",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("b站评论")
    async def bilibili_comment(self, event: AstrMessageEvent):
        """
        B站评论发送
        用法: /b站评论 <BV号> <评论内容>
        示例: /b站评论 BV1xx411c7mD 很棒的视频
        """
        if not self.comment_sender:
            yield event.plain_result("❌ 评论功能未启用，请配置bili_jct")
            return
        
        message = event.message_str.strip()
        parts = message.split()

        # 跳过命令名
        if parts and parts[0] in ["b站评论", "/b站评论"]:
            parts = parts[1:]

        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /b站评论 <BV号> <评论内容>\n示例: /b站评论 BV1xx411c7mD 很棒的视频")
            return

        bvid = parts[0]
        content = " ".join(parts[1:])

        # 验证BV号格式
        if not bvid.startswith("BV"):
            yield event.plain_result("❌ BV号格式错误，应以BV开头")
            return

        # 发送评论
        yield event.plain_result(f"📝 正在发送评论到视频 {bvid}...")
        
        success, msg = await self.comment_sender.send_comment(bvid, content)
        
        if success:
            yield event.plain_result(f"✅ {msg}")
        else:
            yield event.plain_result(f"❌ 评论失败: {msg}")
        
        # 显示状态
        status = self.comment_sender.get_status()
        yield event.plain_result(f"📊 状态: {status}")

    @filter.command("b站评论记录")
    async def bilibili_comment_record(self, event: AstrMessageEvent):
        """查看已评论的视频列表"""
        if not self.comment_sender:
            yield event.plain_result("❌ 评论功能未启用，请配置bili_jct")
            return
        
        commented = self.comment_sender.commented_videos
        if not commented:
            yield event.plain_result("📝 暂无评论记录")
            return
        
        lines = ["📝 已评论视频列表：", ""]
        for bvid, info in commented.items():
            title = info.get("title", "")
            content = info.get("content", "")
            timestamp = info.get("timestamp", 0)
            from datetime import datetime
            time_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
            lines.append(f"• {bvid}")
            lines.append(f"  标题: {title}")
            lines.append(f"  评论: {content}")
            lines.append(f"  时间: {time_str}")
            lines.append("")
        
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="bilibili_search")
    async def bilibili_search_tool(self, event: AstrMessageEvent, keyword: str, count: int = 10, summary: bool = False, comment: bool = False) -> MessageEventResult:
        """搜索B站视频，根据播放量和发布时间筛选高质量视频。
        
        Args:
            keyword(string): 搜索关键词，例如"我的世界"、"杀戮尖塔2"等
            count(number): 返回的视频数量，默认10个，最多25个
            summary(boolean): 是否启用AI总结，默认False
            comment(boolean): 是否评论第一个视频，默认False
        """
        # 限制数量
        count = min(count, 25)
        
        # LLM调用时，使用默认数量时用分层筛选，指定数量时用统一阈值
        use_tiered = self.tiered_filter and count == self.target_count
        
        spider = BilibiliSpider(
            sessdata=self.sessdata,
            tiered_filter=use_tiered,
            tier_hours=self.tier_hours,
            tier_thresholds=self.tier_thresholds,
            default_threshold=self.default_threshold,
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
                    # 构建分层筛选描述
                    tier_desc = ", ".join([f"{self.tier_hours[i]}h内>{self.tier_thresholds[i]}/h" for i in range(len(self.tier_hours))])
                    tier_desc += f", {self.tier_hours[-1]}h+>{self.tier_thresholds[-1]}/h"
                    search_context = f"以上信息是：B站搜索「{keyword}」，以分层筛选({tier_desc})，排序按{order_cn}，筛选{count}个视频的结果，请你"
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
        
        # 评论（如果启用）
        if comment and videos and self.comment_sender:
            first_video = videos[0]
            bvid = first_video.get("bvid")
            title = first_video.get("title", "")
            comment_content = self.default_comment
            
            yield event.plain_result(f"📝 正在发送评论到第一个视频: {title}")
            
            success, msg = await self.comment_sender.send_comment(bvid, comment_content)
            
            if success:
                yield event.plain_result(f"✅ {msg}")
            else:
                yield event.plain_result(f"❌ 评论失败: {msg}")

    async def terminate(self):
        """插件卸载时调用"""
        logger.info("B站爬虫插件已卸载")
