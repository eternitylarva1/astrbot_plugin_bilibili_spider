# Changelog

本文档记录 `astrbot_plugin_bilibili_spider` 的功能更新。

---

## [1.2.0] - 2026-06-30

### Changed
- **两段式筛选替代旧的三级分层**：24h 内视频按播放速率（播放量/小时）筛选，24h~1周视频按总播放量筛选，1周以上视频按较低总播放量筛选
- 新增配置项：`play_per_hour_threshold`（默认 1500）、`play_count_week_threshold`（默认 5000）、`play_count_month_threshold`（默认 3000）
- 移除旧配置项：`tier_hours`、`tier_thresholds`

### Fixed
- **WBI 签名支持**：B站搜索 API 端点更新为 `/x/web-interface/wbi/search/type`，自动获取 WBI 密钥并签名（`_fetch_wbi_keys` / `_sign_wbi`）
- **JSON 解析异常捕获**：当 B站返回非 JSON 响应时不再崩溃，改为记录 Content-Type 和原始内容并安全退出
- **buvid3/buvid4 动态生成**：从硬编码 `"F"` 改为 `uuid.uuid4()` 生成，降低被反爬识别风险
- User-Agent 更新：Chrome 120 → Chrome 132

---

## [1.1.0] - 2026-06-24

### Added
- **SQLite 视频历史数据库**（`VideoDB` 类）：以 BV 号为主键存储搜索记录，支持 `upsert` 批量写入
- **`/b站日志` 命令**：查看最近的 B站爬虫相关日志，支持指定行数
- **LLM 智能评论选择**（`llm_select_comment`）：根据视频标题自动从词库匹配最合适的评论

### Changed
- 评论词库改为两个独立列表：`comment_list`（评论内容）+ `condition_list`（适用条件），支持 UI 分别编辑
- 搜索后自动评论改为评论**所有**通过筛选的视频（原为仅评论第一个）
- 移除 `bilibili-api-python` 依赖，改用 `requests` 直接调用 B站 API

### Fixed
- 分层筛选逻辑错误修复（`check_video_filter` 阈值匹配）
- Cookie URL 编码处理（SESSDATA 可能包含 `%2C` 等编码字符）
- 评论词库 schema 格式兼容性修复（`list` / `object` / `array`）

---

## [1.0.0] - 2026-06-22

### Added
- **B站视频搜索插件首发**：`BilibiliSpider` 类，通过 B站搜索 API 按关键词获取视频
- **命令**：`/b站搜索`、`/b站热门`、`/b站配置`、`/b站评论`、`/b站评论记录`
- **LLM 工具**：`bilibili_search` 供 AI 自动调用
- **分层筛选机制**：按视频发布时间动态调整播放速率阈值（5h / 24h / 24h+）
- **集满模式 / 普通模式**：持续搜索直到满足条件 vs 获取固定数量后筛选
- **B站评论功能**（`BilibiliCommentSender`）：带限流（`RateLimiter`）、熔断（`CircuitBreaker`）、每日上限保护
- **AI 总结**：通过 LLM 分析搜索结果并输出推荐
- **安全机制**：评论本地记录（`commented_videos.json`）+ 每日 100 条上限
- **WebUI 配置**：SESSDATA、bili_jct、搜索关键词、排序方式、阈值等均可在后台配置
