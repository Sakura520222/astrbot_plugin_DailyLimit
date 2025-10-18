import json
import redis.asyncio as redis
import datetime
from datetime import timedelta
import astrbot.api.star as star
from astrbot.api.event import (filter,
                               AstrMessageEvent,
                               MessageEventResult,
                               MessageChain,
                               EventResultType)
from astrbot.api.platform import MessageType
from astrbot.api.event.filter import PermissionType
from astrbot.api import AstrBotConfig
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger


@star.register(
    name="DailyLimit",
    desc="限制用户调用大模型的次数",
    author="Sakura520222",
    version="v2.3",
    repo="https://github.com/Sakura520222/astrbot_plugin_DailyLimit"
)
class DailyLimitPlugin(star.Star):
    """限制群组成员每日调用大模型的次数"""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        self.group_limits = {}  # 群组特定限制 {"group_id": limit_count}
        self.user_limits = {}  # 用户特定限制 {"user_id": limit_count}
        self.group_modes = {}  # 群组模式配置 {"group_id": "shared"或"individual"}
        self.usage_records = {}  # 使用记录 {"user_id": {"date": count}}
        self.redis = None  # 初始化redis属性为None

        # 加载群组和用户特定限制
        self._load_limits_from_config()

    async def start(self):
        """插件启动时的初始化工作"""
        await self._init_redis()

    def _load_limits_from_config(self):
        """从配置文件加载群组和用户特定限制"""
        # 加载群组特定限制
        for group_limit in self.config["limits"]["group_limits"]:
            group_id = group_limit.get("group_id")
            limit = group_limit.get("limit")
            if group_id and limit is not None:
                self.group_limits[str(group_id)] = limit

        # 加载用户特定限制
        for user_limit in self.config["limits"]["user_limits"]:
            user_id = user_limit.get("user_id")
            limit = user_limit.get("limit")
            if user_id and limit is not None:
                self.user_limits[str(user_id)] = limit

        # 加载群组模式配置
        for group_mode in self.config["limits"]["group_mode_settings"]:
            group_id = group_mode.get("group_id")
            mode = group_mode.get("mode")
            if group_id and mode in ["shared", "individual"]:
                self.group_modes[str(group_id)] = mode

        logger.info(f"已加载 {len(self.group_limits)} 个群组限制、{len(self.user_limits)} 个用户限制和 {len(self.group_modes)} 个群组模式配置")

    def _save_group_limit(self, group_id, limit):
        """保存群组特定限制到配置文件"""
        group_id = str(group_id)

        # 检查是否已存在该群组的限制
        group_limits = self.config["limits"]["group_limits"]
        for i, group_limit in enumerate(group_limits):
            if str(group_limit.get("group_id")) == group_id:
                # 更新现有限制
                group_limits[i]["limit"] = limit
                self.config.save_config()
                return

        # 添加新的群组限制
        group_limits.append({"group_id": group_id, "limit": limit})
        self.config.save_config()

    def _save_user_limit(self, user_id, limit):
        """保存用户特定限制到配置文件"""
        user_id = str(user_id)

        # 检查是否已存在该用户的限制
        user_limits = self.config["limits"]["user_limits"]
        for i, user_limit in enumerate(user_limits):
            if str(user_limit.get("user_id")) == user_id:
                # 更新现有限制
                user_limits[i]["limit"] = limit
                self.config.save_config()
                return

        # 添加新的用户限制
        user_limits.append({"user_id": user_id, "limit": limit})
        self.config.save_config()

    def _save_group_mode(self, group_id, mode):
        """保存群组模式配置到配置文件"""
        group_id = str(group_id)

        # 检查是否已存在该群组的模式配置
        group_modes = self.config["limits"]["group_mode_settings"]
        for i, group_mode in enumerate(group_modes):
            if str(group_mode.get("group_id")) == group_id:
                # 更新现有模式
                group_modes[i]["mode"] = mode
                self.config.save_config()
                return

        # 添加新的群组模式配置
        group_modes.append({"group_id": group_id, "mode": mode})
        self.config.save_config()

    async def _init_redis(self):
        """初始化Redis连接"""
        try:
            self.redis = redis.Redis(
                host=self.config["redis"]["host"],
                port=self.config["redis"]["port"],
                db=self.config["redis"]["db"],
                password=self.config["redis"]["password"],
                decode_responses=True  # 自动将响应解码为字符串
            )
            # 测试连接
            await self.redis.ping()
            logger.info("Redis连接成功")
        except Exception as e:
            logger.error(f"Redis连接失败: {str(e)}")
            self.redis = None

    @staticmethod
    def _get_today_key():
        """获取今天的日期键"""
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        return f"astrbot:daily_limit:{today}"

    def _get_user_key(self, user_id, group_id=None):
        """获取用户在特定群组的Redis键"""
        if group_id is None:
            group_id = "private_chat"
        
        return f"{self._get_today_key()}:{group_id}:{user_id}"

    def _get_group_key(self, group_id):
        """获取群组共享的Redis键"""
        return f"{self._get_today_key()}:group:{group_id}"

    def _parse_redis_key(self, key: str) -> dict:
        """解析Redis键，返回结构化信息"""
        parts = key.split(":")
        
        # 基本验证
        if len(parts) < 4 or parts[0] != "astrbot" or parts[1] != "daily_limit":
            return None
            
        result = {
            "date": parts[2],
            "type": None,
            "group_id": None,
            "user_id": None
        }
        
        # 解析键类型
        if "group" in parts:
            try:
                group_index = parts.index("group")
                if group_index + 1 < len(parts):
                    result["type"] = "group"
                    result["group_id"] = parts[group_index + 1]
            except ValueError:
                pass
        elif "private_chat" in parts:
            try:
                private_index = parts.index("private_chat")
                if private_index + 1 < len(parts):
                    result["type"] = "user"
                    result["user_id"] = parts[private_index + 1]
            except ValueError:
                pass
        elif len(parts) >= 5:
            # 格式: astrbot:daily_limit:2025-01-23:群组ID:用户ID
            if parts[-2].isdigit() and parts[-1].isdigit():
                result["type"] = "user"
                result["group_id"] = parts[-2]
                result["user_id"] = parts[-1]
        
        return result if result["type"] else None

    def _get_usage_record_key(self, user_id, group_id=None, date_str=None):
        """获取使用记录Redis键"""
        if date_str is None:
            date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        
        if group_id is None:
            group_id = "private_chat"
        
        return f"astrbot:usage_record:{date_str}:{group_id}:{user_id}"

    def _get_usage_stats_key(self, date_str=None):
        """获取使用统计Redis键"""
        if date_str is None:
            date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        
        return f"astrbot:usage_stats:{date_str}"

    def _get_group_mode(self, group_id):
        """获取群组的模式配置"""
        if not group_id:
            return "individual"  # 私聊默认为独立模式
        
        # 检查是否有特定群组模式配置
        if str(group_id) in self.group_modes:
            return self.group_modes[str(group_id)]
        
        # 默认使用共享模式（保持向后兼容性）
        return "shared"

    def _get_current_time_period_limit(self):
        """获取当前时间段适用的限制次数"""
        current_time = datetime.datetime.now()
        current_hour_minute = current_time.strftime("%H:%M")
        
        # 检查是否是周末
        is_weekend = current_time.weekday() >= 5  # 5=周六, 6=周日
        
        # 遍历所有时间段限制配置
        for time_period in self.config["limits"].get("time_period_limits", []):
            if not time_period.get("enabled", True):
                continue
                
            start_time = time_period.get("start_time", "00:00")
            end_time = time_period.get("end_time", "23:59")
            limit = time_period.get("limit", self.config["limits"]["default_daily_limit"])
            
            # 检查时间段名称是否包含"周末"，如果是则只在周末生效
            period_name = time_period.get("name", "").lower()
            if "周末" in period_name and not is_weekend:
                continue
            elif "周末" not in period_name and is_weekend:
                # 如果是周末，但时间段名称不包含"周末"，则跳过非周末时间段
                continue
            
            # 处理跨天的时间段（如22:00-06:00）
            if start_time > end_time:
                # 跨天时间段：当前时间在开始时间之后或结束时间之前
                if current_hour_minute >= start_time or current_hour_minute <= end_time:
                    return limit
            else:
                # 正常时间段：当前时间在开始时间和结束时间之间
                if start_time <= current_hour_minute <= end_time:
                    return limit
        
        # 如果没有匹配的时间段，返回默认限制
        return None

    def _get_user_limit(self, user_id, group_id=None):
        """获取用户的调用限制次数"""
        # 检查用户是否豁免
        if str(user_id) in self.config["limits"]["exempt_users"]:
            return float('inf')  # 无限制

        # 首先检查时间段限制
        time_period_limit = self._get_current_time_period_limit()
        if time_period_limit is not None:
            return time_period_limit

        # 检查用户特定限制
        if str(user_id) in self.user_limits:
            return self.user_limits[str(user_id)]

        # 检查群组特定限制
        if group_id and str(group_id) in self.group_limits:
            return self.group_limits[str(group_id)]

        # 返回默认限制
        return self.config["limits"]["default_daily_limit"]

    async def _get_user_usage(self, user_id, group_id=None):
        """获取用户已使用次数（兼容旧版本）"""
        if not self.redis:
            return 0

        key = self._get_user_key(user_id, group_id)
        usage = await self.redis.get(key)
        return int(usage) if usage else 0

    async def _get_group_usage(self, group_id):
        """获取群组共享使用次数"""
        if not self.redis:
            return 0

        key = self._get_group_key(group_id)
        usage = await self.redis.get(key)
        return int(usage) if usage else 0

    async def _increment_user_usage(self, user_id, group_id=None):
        """增加用户使用次数（兼容旧版本）"""
        if not self.redis:
            return False

        key = self._get_user_key(user_id, group_id)
        # 增加计数并设置过期时间
        pipe = self.redis.pipeline()
        pipe.incr(key)

        # 设置过期时间到明天凌晨
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_tomorrow = int((tomorrow - datetime.datetime.now()).total_seconds())
        pipe.expire(key, seconds_until_tomorrow)

        await pipe.execute()
        return True

    async def _increment_group_usage(self, group_id):
        """增加群组共享使用次数"""
        if not self.redis:
            return False

        key = self._get_group_key(group_id)
        # 增加计数并设置过期时间
        pipe = self.redis.pipeline()
        pipe.incr(key)

        # 设置过期时间到明天凌晨
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_tomorrow = int((tomorrow - datetime.datetime.now()).total_seconds())
        pipe.expire(key, seconds_until_tomorrow)

        await pipe.execute()
        return True

    async def _record_usage(self, user_id, group_id=None, usage_type="llm_request"):
        """记录使用记录"""
        if not self.redis:
            return False
            
        timestamp = datetime.datetime.now().isoformat()
        record_key = self._get_usage_record_key(user_id, group_id)
        
        # 记录详细使用信息
        record_data = {
            "timestamp": timestamp,
            "user_id": user_id,
            "group_id": group_id,
            "usage_type": usage_type,
            "date": datetime.datetime.now().strftime("%Y-%m-%d")
        }
        
        # 使用Redis列表存储使用记录
        await self.redis.rpush(record_key, json.dumps(record_data))
        
        # 设置过期时间到明天凌晨
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_tomorrow = int((tomorrow - datetime.datetime.now()).total_seconds())
        await self.redis.expire(record_key, seconds_until_tomorrow)
        
        # 更新统计信息
        await self._update_usage_stats(user_id, group_id)
        
        return True

    async def _update_usage_stats(self, user_id, group_id=None):
        """更新使用统计信息"""
        if not self.redis:
            return False
            
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        stats_key = self._get_usage_stats_key(date_str)
        
        # 更新用户统计
        user_stats_key = f"{stats_key}:user:{user_id}"
        await self.redis.hincrby(user_stats_key, "total_usage", 1)
        
        # 更新群组统计（如果有群组）
        if group_id:
            group_stats_key = f"{stats_key}:group:{group_id}"
            await self.redis.hincrby(group_stats_key, "total_usage", 1)
            
            # 更新群组用户统计
            group_user_stats_key = f"{stats_key}:group:{group_id}:user:{user_id}"
            await self.redis.hincrby(group_user_stats_key, "usage_count", 1)
        
        # 更新全局统计
        global_stats_key = f"{stats_key}:global"
        await self.redis.hincrby(global_stats_key, "total_requests", 1)
        
        # 设置过期时间到明天凌晨
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_tomorrow = int((tomorrow - datetime.datetime.now()).total_seconds())
        
        # 为所有统计键设置过期时间
        for key in [user_stats_key, group_stats_key, group_user_stats_key, global_stats_key]:
            if await self.redis.exists(key):
                await self.redis.expire(key, seconds_until_tomorrow)
        
        return True

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """处理LLM请求事件"""
        if not self.redis:
            logger.error("Redis未连接，阻止处理LLM请求")
            event.stop_event()
            return False
        if not req.prompt.strip() or event.message_str.startswith("@所有人"):
            event.stop_event()
            return False

        user_id = event.get_sender_id()

        if str(user_id) in self.config["limits"]["exempt_users"]:
            return True  # 豁免用户，允许继续处理

        group_id = None
        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            # 获取群组ID和用户ID
            group_id = event.get_group_id()

        # 检查限制
        limit = self._get_user_limit(user_id, group_id)
        
        # 根据群组模式决定使用哪种计数方式
        if group_id is not None:
            group_mode = self._get_group_mode(group_id)
            if group_mode == "shared":
                # 共享模式：使用群组共享使用次数
                key = self._get_group_key(group_id)
                usage_type = "群组共享"
            else:
                # 独立模式：使用用户个人使用次数
                key = self._get_user_key(user_id, group_id)
                usage_type = "个人独立"
        else:
            # 私聊消息：使用个人使用次数
            key = self._get_user_key(user_id, group_id)
            usage_type = "个人"

        # 使用原子操作检查和增加使用次数
        new_usage = await self.redis.incr(key)
        
        # 如果是第一次使用，设置过期时间
        if new_usage == 1:
            tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
            tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
            seconds_until_tomorrow = int((tomorrow - datetime.datetime.now()).total_seconds())
            await self.redis.expire(key, seconds_until_tomorrow)

        # 检查是否超过限制
        if new_usage > limit:
            logger.info(f"用户 {user_id} 在群 {group_id} 中已达到调用限制 {limit}")
            # 撤销本次增加
            await self.redis.decr(key)
            
            if group_id is not None:
                user_name = event.get_sender_name()
                if self._get_group_mode(group_id) == "shared":
                    await event.send(
                        MessageChain().at(user_name, user_id).message(f"本群组AI访问次数已达上限（{limit}/{limit}），"
                                                                      f"请稍后再试或联系管理员提升限额。")
                    )
                else:
                    await event.send(
                        MessageChain().at(user_name, user_id).message(f"您在本群组的AI访问次数已达上限（{limit}/{limit}），"
                                                                      f"请稍后再试或联系管理员提升限额。")
                    )
            else:
                await event.send(
                    MessageChain().message(f"您的AI访问次数已达上限（{limit}/{limit}），"
                                           f"请稍后再试或联系管理员提升限额。")
                )
            event.stop_event()  # 终止事件传播
            return False

        # 检查是否需要提醒剩余次数（当剩余次数为1、3、5时提醒）
        remaining = limit - new_usage
        if remaining in [1, 3, 5]:
            if group_id is not None:
                user_name = event.get_sender_name()
                if self._get_group_mode(group_id) == "shared":
                    reminder_msg = f"💡 提醒：本群组剩余AI调用次数为 {remaining} 次"
                else:
                    reminder_msg = f"💡 提醒：您在本群组剩余AI调用次数为 {remaining} 次"
                await event.send(
                    MessageChain().at(user_name, user_id).message(reminder_msg)
                )
            else:
                reminder_msg = f"💡 提醒：您剩余AI调用次数为 {remaining} 次"
                await event.send(
                    MessageChain().message(reminder_msg)
                )
        
        # 记录使用记录
        await self._record_usage(user_id, group_id, "llm_request")
        
        return True  # 允许继续处理

    @filter.command("limit_status")
    async def limit_status(self, event: AstrMessageEvent):
        """用户查看当前使用状态"""
        user_id = event.get_sender_id()
        group_id = None
        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            group_id = event.get_group_id()

        # 检查Redis连接状态
        if not self.redis:
            event.set_result(MessageEventResult().message("❌ 插件未正确初始化，请检查Redis连接配置"))
            return

        # 检查使用状态
        limit = self._get_user_limit(user_id, group_id)
        
        # 根据群组模式显示正确的状态信息
        if group_id is not None:
            group_mode = self._get_group_mode(group_id)
            if group_mode == "shared":
                # 共享模式：显示群组共享状态
                usage = await self._get_group_usage(group_id)
                # 首先检查是否被豁免（无限制）
                if limit == float('inf'):
                    # 群组被豁免（无限制）
                    status_msg = "本群组没有调用次数限制（共享模式）"
                # 然后检查群组是否设置了特定限制
                elif str(group_id) in self.group_limits:
                    # 群组有特定限制
                    remaining = limit - usage
                    status_msg = f"本群组今日已使用 {usage}/{limit} 次（共享模式），剩余 {remaining} 次"
                else:
                    # 群组使用默认限制
                    remaining = limit - usage
                    status_msg = f"本群组今日已使用 {usage}/{limit} 次（默认限制，共享模式），剩余 {remaining} 次"
            else:
                # 独立模式：显示用户个人状态
                usage = await self._get_user_usage(user_id, group_id)
                # 首先检查是否被豁免（无限制）
                if limit == float('inf'):
                    # 用户被豁免（无限制）
                    status_msg = "您在本群组没有调用次数限制（独立模式）"
                # 然后检查用户是否设置了特定限制
                elif str(user_id) in self.user_limits:
                    # 用户有特定限制
                    remaining = limit - usage
                    status_msg = f"您在本群组今日已使用 {usage}/{limit} 次（独立模式），剩余 {remaining} 次"
                # 检查群组是否设置了特定限制
                elif str(group_id) in self.group_limits:
                    # 群组有特定限制
                    remaining = limit - usage
                    status_msg = f"您在本群组今日已使用 {usage}/{limit} 次（群组限制，独立模式），剩余 {remaining} 次"
                else:
                    # 使用默认限制
                    remaining = limit - usage
                    status_msg = f"您在本群组今日已使用 {usage}/{limit} 次（默认限制，独立模式），剩余 {remaining} 次"
        else:
            # 私聊消息：显示个人状态
            usage = await self._get_user_usage(user_id, group_id)
            if limit == float('inf'):
                status_msg = "您没有调用次数限制"
            else:
                remaining = limit - usage
                status_msg = f"您今日已使用 {usage}/{limit} 次，剩余 {remaining} 次"

        event.set_result(MessageEventResult().message(status_msg))

    @filter.command("限制帮助")
    async def limit_help_all(self, event: AstrMessageEvent):
        """显示本插件所有指令及其帮助信息"""
        help_msg = (
            "📋 日调用限制插件 - 指令帮助\n\n"
            "👤 用户指令：\n"
            "• /limit_status - 查看当前使用状态\n"
            "• /限制帮助 - 显示本帮助信息\n\n"
            "👨‍💼 管理员指令：\n"
            "• /limit help - 显示详细帮助信息\n"
            "• /limit set <用户ID> <次数> - 设置特定用户的限制\n"
            "• /limit setgroup <次数> - 设置当前群组的限制\n"
            "• /limit setmode <shared|individual> - 设置群组使用模式（共享/独立）\n"
            "• /limit getmode - 查看当前群组使用模式\n"
            "• /limit exempt <用户ID> - 将用户添加到豁免列表\n"
            "• /limit unexempt <用户ID> - 将用户从豁免列表移除\n"
            "• /limit list_user - 列出所有用户特定限制\n"
            "• /limit list_group - 列出所有群组特定限制\n"
            "• /limit stats - 查看插件使用统计信息\n"
            "• /limit history [用户ID] [天数] - 查询使用历史记录\n"
            "• /limit analytics [日期] - 多维度统计分析\n"
            "• /limit top [数量] - 查看使用次数排行榜\n"
            "• /limit status - 检查插件状态和健康状态\n"
            "• /limit reset <用户ID|all> - 重置用户使用次数\n\n"
            "💡 说明：\n"
            "- 默认限制：所有用户每日调用次数\n"
            "- 群组限制：可针对特定群组设置不同限制\n"
            "- 用户限制：可针对特定用户设置不同限制\n"
            "- 豁免用户：不受限制的用户列表\n"
            "- 群组模式：支持共享模式（群组共享次数）和独立模式（成员独立次数）\n"
            "- 剩余次数提醒：当剩余1、3、5次时会自动提醒\n"
            "- 使用记录：自动记录每次调用，支持历史查询\n"
            "- 统计分析：提供多维度使用数据分析"
        )

        event.set_result(MessageEventResult().message(help_msg))

    @filter.command_group("limit")
    def limit_command_group(self):
        """限制命令组"""
        pass

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("help")
    async def limit_help(self, event: AstrMessageEvent):
        """显示帮助信息（仅管理员）"""
        help_msg = (
            "日调用限制插件使用说明：\n"
            "- /limit_status：用户查看当前使用状态\n"
            "\n管理员命令：\n"
            "- /limit help：显示此帮助信息\n"
            "- /limit set <用户ID> <次数>：设置特定用户的限制\n"
            "- /limit setgroup <次数>：设置当前群组的限制\n"
            "- /limit setmode <shared|individual>：设置当前群组使用模式（共享/独立）\n"
            "- /limit getmode：查看当前群组使用模式\n"
            "- /limit exempt <用户ID>：将用户添加到豁免列表\n"
            "- /limit unexempt <用户ID>：将用户从豁免列表移除\n"
            "- /limit list_user：列出所有用户特定限制\n"
            "- /limit list_group：列出所有群组特定限制\n"
            "- /limit stats：查看插件使用统计信息\n"
            "- /limit history [用户ID] [天数]：查询使用历史记录\n"
            "- /limit analytics [日期]：多维度统计分析\n"
            "- /limit top [数量]：查看使用次数排行榜\n"
            "- /limit status：检查插件状态和健康状态\n"
            "- /limit reset <用户ID|all>：重置使用次数\n"
            "\n使用模式说明：\n"
            "- 共享模式：群组内所有成员共享使用次数\n"
            "- 独立模式：群组内每个成员有独立的使用次数\n"
        )

        event.set_result(MessageEventResult().message(help_msg))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("set")
    async def limit_set(self, event: AstrMessageEvent, user_id: str = None, limit: int = None):
        """设置特定用户的限制（仅管理员）"""

        if user_id is None or limit is None:
            event.set_result(MessageEventResult().message("用法: /limit set <用户ID> <次数>"))
            return

        try:
            limit = int(limit)
            if limit < 0:
                event.set_result(MessageEventResult().message("限制次数必须大于或等于0"))
                return

            self.user_limits[user_id] = limit
            self._save_user_limit(user_id, limit)

            event.set_result(MessageEventResult().message(f"已设置用户 {user_id} 的每日调用限制为 {limit} 次"))
        except ValueError:
            event.set_result(MessageEventResult().message("限制次数必须为整数"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("setgroup")
    async def limit_setgroup(self, event: AstrMessageEvent, limit: int = None):
        """设置当前群组的限制（仅管理员）"""

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            event.set_result(MessageEventResult().message("此命令只能在群聊中使用"))
            return

        if limit is None:
            event.set_result(MessageEventResult().message("用法: /limit setgroup <次数>"))
            return

        try:
            limit = int(limit)
            if limit < 0:
                event.set_result(MessageEventResult().message("限制次数必须大于或等于0"))
                return

            group_id = event.get_group_id()
            self.group_limits[group_id] = limit
            self._save_group_limit(group_id, limit)

            event.set_result(MessageEventResult().message(f"已设置当前群组的每日调用限制为 {limit} 次"))
        except ValueError:
            event.set_result(MessageEventResult().message("限制次数必须为整数"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("setmode")
    async def limit_setmode(self, event: AstrMessageEvent, mode: str = None):
        """设置当前群组的使用模式（仅管理员）"""

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            event.set_result(MessageEventResult().message("此命令只能在群聊中使用"))
            return

        if mode is None:
            event.set_result(MessageEventResult().message("用法: /limit setmode <shared|individual>"))
            return

        if mode not in ["shared", "individual"]:
            event.set_result(MessageEventResult().message("模式必须是 'shared'（共享）或 'individual'（独立）"))
            return

        group_id = event.get_group_id()
        self.group_modes[group_id] = mode
        self._save_group_mode(group_id, mode)
        mode_text = "共享" if mode == "shared" else "独立"
        event.set_result(MessageEventResult().message(f"已设置当前群组的使用模式为 {mode_text} 模式"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("getmode")
    async def limit_getmode(self, event: AstrMessageEvent):
        """查看当前群组的使用模式（仅管理员）"""

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            event.set_result(MessageEventResult().message("此命令只能在群聊中使用"))
            return

        group_id = event.get_group_id()
        mode = self._get_group_mode(group_id)
        mode_text = "共享" if mode == "shared" else "独立"
        event.set_result(MessageEventResult().message(f"当前群组的使用模式为 {mode_text} 模式"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("exempt")
    async def limit_exempt(self, event: AstrMessageEvent, user_id: str = None):
        """将用户添加到豁免列表（仅管理员）"""

        if user_id is None:
            event.set_result(MessageEventResult().message("用法: /limit exempt <用户ID>"))
            return

        if user_id not in self.config["limits"]["exempt_users"]:
            self.config["limits"]["exempt_users"].append(user_id)
            self.config.save_config()

            event.set_result(MessageEventResult().message(f"已将用户 {user_id} 添加到豁免列表"))
        else:
            event.set_result(MessageEventResult().message(f"用户 {user_id} 已在豁免列表中"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("unexempt")
    async def limit_unexempt(self, event: AstrMessageEvent, user_id: str = None):
        """将用户从豁免列表移除（仅管理员）"""

        if user_id is None:
            event.set_result(MessageEventResult().message("用法: /limit unexempt <用户ID>"))
            return

        if user_id in self.config["limits"]["exempt_users"]:
            self.config["limits"]["exempt_users"].remove(user_id)
            self.config.save_config()

            event.set_result(MessageEventResult().message(f"已将用户 {user_id} 从豁免列表移除"))
        else:
            event.set_result(MessageEventResult().message(f"用户 {user_id} 不在豁免列表中"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("list_user")
    async def limit_list_user(self, event: AstrMessageEvent):
        """列出所有用户特定限制（仅管理员）"""
        if not self.user_limits:
            event.set_result(MessageEventResult().message("当前没有设置任何用户特定限制"))
            return

        user_limits_str = "用户特定限制列表：\n"
        for user_id, limit in self.user_limits.items():
            user_limits_str += f"- 用户 {user_id}: {limit} 次/天\n"

        event.set_result(MessageEventResult().message(user_limits_str))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("list_group")
    async def limit_list_group(self, event: AstrMessageEvent):
        """列出所有群组特定限制（仅管理员）"""
        if not self.group_limits:
            event.set_result(MessageEventResult().message("当前没有设置任何群组特定限制"))
            return

        group_limits_str = "群组特定限制列表：\n"
        for group_id, limit in self.group_limits.items():
            group_limits_str += f"- 群组 {group_id}: {limit} 次/天\n"

        event.set_result(MessageEventResult().message(group_limits_str))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("stats")
    async def limit_stats(self, event: AstrMessageEvent):
        """显示插件使用统计信息（仅管理员）"""
        if not self.redis:
            event.set_result(MessageEventResult().message("Redis未连接，无法获取统计信息"))
            return

        try:
            # 获取今日所有用户的调用统计
            today_key = self._get_today_key()
            pattern = f"{today_key}:*"
            keys = []
            async for key in self.redis.scan_iter(pattern):
                keys.append(key)
            
            total_calls = 0
            active_users = 0
            
            for key in keys:
                usage = await self.redis.get(key)
                if usage:
                    total_calls += int(usage)
                    active_users += 1
            
            stats_msg = (
                f"📊 今日统计信息：\n"
                f"• 活跃用户数: {active_users}\n"
                f"• 总调用次数: {total_calls}\n"
                f"• 用户特定限制数: {len(self.user_limits)}\n"
                f"• 群组特定限制数: {len(self.group_limits)}\n"
                f"• 豁免用户数: {len(self.config['limits']['exempt_users'])}"
            )
            
            event.set_result(MessageEventResult().message(stats_msg))
        except Exception as e:
            logger.error(f"获取统计信息失败: {str(e)}")
            event.set_result(MessageEventResult().message("获取统计信息失败"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("history")
    async def limit_history(self, event: AstrMessageEvent, user_id: str = None, days: int = 7):
        """查询使用历史记录（仅管理员）"""
        if not self.redis:
            event.set_result(MessageEventResult().message("Redis未连接，无法获取历史记录"))
            return

        try:
            if days < 1 or days > 30:
                event.set_result(MessageEventResult().message("查询天数应在1-30之间"))
                return

            # 获取最近days天的使用记录
            date_list = []
            for i in range(days):
                date = datetime.datetime.now() - datetime.timedelta(days=i)
                date_list.append(date.strftime("%Y-%m-%d"))

            if user_id:
                # 查询特定用户的历史记录
                user_records = {}
                for date_str in date_list:
                    # 查询个人聊天记录
                    private_key = self._get_usage_record_key(user_id, None, date_str)
                    private_records = await self.redis.lrange(private_key, 0, -1)
                    
                    # 查询群组记录
                    group_pattern = f"astrbot:usage_record:{date_str}:*:{user_id}"
                    group_keys = []
                    async for key in self.redis.scan_iter(group_pattern):
                        group_keys.append(key)
                    
                    daily_total = len(private_records)
                    
                    for key in group_keys:
                        group_records = await self.redis.lrange(key, 0, -1)
                        daily_total += len(group_records)
                    
                    if daily_total > 0:
                        user_records[date_str] = daily_total
                
                if not user_records:
                    event.set_result(MessageEventResult().message(f"用户 {user_id} 在最近{days}天内没有使用记录"))
                    return
                
                history_msg = f"📊 用户 {user_id} 最近{days}天使用历史：\n"
                for date_str, count in sorted(user_records.items(), reverse=True):
                    history_msg += f"• {date_str}: {count}次\n"
                
                event.set_result(MessageEventResult().message(history_msg))
            else:
                # 查询全局历史记录
                global_stats = {}
                for date_str in date_list:
                    stats_key = self._get_usage_stats_key(date_str)
                    global_key = f"{stats_key}:global"
                    
                    total_requests = await self.redis.hget(global_key, "total_requests")
                    if total_requests:
                        global_stats[date_str] = int(total_requests)
                
                if not global_stats:
                    event.set_result(MessageEventResult().message(f"最近{days}天内没有使用记录"))
                    return
                
                history_msg = f"📊 最近{days}天全局使用统计：\n"
                for date_str, count in sorted(global_stats.items(), reverse=True):
                    history_msg += f"• {date_str}: {count}次\n"
                
                event.set_result(MessageEventResult().message(history_msg))
                
        except Exception as e:
            logger.error(f"查询历史记录失败: {str(e)}")
            event.set_result(MessageEventResult().message("查询历史记录失败"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("analytics")
    async def limit_analytics(self, event: AstrMessageEvent, date_str: str = None):
        """多维度统计分析（仅管理员）"""
        if not self.redis:
            event.set_result(MessageEventResult().message("Redis未连接，无法获取分析数据"))
            return

        try:
            if date_str is None:
                date_str = datetime.datetime.now().strftime("%Y-%m-%d")
            
            stats_key = self._get_usage_stats_key(date_str)
            
            # 获取全局统计
            global_key = f"{stats_key}:global"
            total_requests = await self.redis.hget(global_key, "total_requests")
            
            # 获取用户统计
            user_pattern = f"{stats_key}:user:*"
            user_keys = []
            async for key in self.redis.scan_iter(user_pattern):
                user_keys.append(key)
            
            # 获取群组统计
            group_pattern = f"{stats_key}:group:*"
            group_keys = []
            async for key in self.redis.scan_iter(group_pattern):
                group_keys.append(key)
            
            analytics_msg = f"📈 {date_str} 多维度统计分析：\n\n"
            
            # 全局统计
            if total_requests:
                analytics_msg += f"🌍 全局统计：\n"
                analytics_msg += f"• 总调用次数: {int(total_requests)}次\n"
            
            # 用户统计
            if user_keys:
                analytics_msg += f"\n👤 用户统计：\n"
                analytics_msg += f"• 活跃用户数: {len(user_keys)}人\n"
                
                # 计算用户平均使用次数
                user_total = 0
                for key in user_keys:
                    usage = await self.redis.hget(key, "total_usage")
                    if usage:
                        user_total += int(usage)
                
                if len(user_keys) > 0:
                    avg_usage = user_total / len(user_keys)
                    analytics_msg += f"• 用户平均使用次数: {avg_usage:.1f}次\n"
            
            # 群组统计
            if group_keys:
                analytics_msg += f"\n👥 群组统计：\n"
                analytics_msg += f"• 活跃群组数: {len(group_keys)}个\n"
                
                # 计算群组平均使用次数
                group_total = 0
                for key in group_keys:
                    usage = await self.redis.hget(key, "total_usage")
                    if usage:
                        group_total += int(usage)
                
                if len(group_keys) > 0:
                    avg_group_usage = group_total / len(group_keys)
                    analytics_msg += f"• 群组平均使用次数: {avg_group_usage:.1f}次\n"
            
            # 使用分布分析
            if user_keys:
                analytics_msg += f"\n📊 使用分布：\n"
                
                # 统计不同使用频次的用户数量
                usage_levels = {"低(1-5次)": 0, "中(6-20次)": 0, "高(21+次)": 0}
                
                for key in user_keys:
                    usage = await self.redis.hget(key, "total_usage")
                    if usage:
                        usage_count = int(usage)
                        if usage_count <= 5:
                            usage_levels["低(1-5次)"] += 1
                        elif usage_count <= 20:
                            usage_levels["中(6-20次)"] += 1
                        else:
                            usage_levels["高(21+次)"] += 1
                
                for level, count in usage_levels.items():
                    if count > 0:
                        percentage = (count / len(user_keys)) * 100
                        analytics_msg += f"• {level}: {count}人 ({percentage:.1f}%)\n"
            
            event.set_result(MessageEventResult().message(analytics_msg))
            
        except Exception as e:
            logger.error(f"获取分析数据失败: {str(e)}")
            event.set_result(MessageEventResult().message("获取分析数据失败"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("status")
    async def limit_status_admin(self, event: AstrMessageEvent):
        """检查插件状态和健康状态（仅管理员）"""
        try:
            # 检查Redis连接状态
            redis_status = "✅ 正常" if self.redis else "❌ 未连接"
            
            # 检查Redis连接是否可用
            redis_available = False
            if self.redis:
                try:
                    await self.redis.ping()
                    redis_available = True
                except:
                    redis_available = False
            
            redis_available_status = "✅ 可用" if redis_available else "❌ 不可用"
            
            # 获取配置信息
            default_limit = self.config["limits"]["default_daily_limit"]
            exempt_users_count = len(self.config["limits"]["exempt_users"])
            group_limits_count = len(self.group_limits)
            user_limits_count = len(self.user_limits)
            
            # 获取今日统计
            today_stats = "无法获取"
            if self.redis and redis_available:
                try:
                    today_key = self._get_today_key()
                    pattern = f"{today_key}:*"
                    keys = []
                    async for key in self.redis.scan_iter(pattern):
                        keys.append(key)
                    
                    total_calls = 0
                    active_users = 0
                    
                    for key in keys:
                        usage = await self.redis.get(key)
                        if usage:
                            total_calls += int(usage)
                            active_users += 1
                    
                    today_stats = f"活跃用户: {active_users}, 总调用: {total_calls}"
                except:
                    today_stats = "获取失败"
            
            # 构建状态报告
            status_msg = (
                "🔍 插件状态监控报告\n\n"
                f"📊 Redis连接状态: {redis_status}\n"
                f"🔌 Redis可用性: {redis_available_status}\n\n"
                f"⚙️ 配置信息:\n"
                f"• 默认限制: {default_limit} 次/天\n"
                f"• 豁免用户数: {exempt_users_count} 个\n"
                f"• 群组限制数: {group_limits_count} 个\n"
                f"• 用户限制数: {user_limits_count} 个\n\n"
                f"📈 今日统计: {today_stats}\n\n"
                f"💡 健康状态: {'✅ 健康' if self.redis and redis_available else '⚠️ 需要检查'}"
            )
            
            await event.send(MessageChain().message(status_msg))
            
        except Exception as e:
            logger.error(f"检查插件状态失败: {str(e)}")
            await event.send(MessageChain().message("❌ 检查插件状态失败"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("top")
    async def limit_top(self, event: AstrMessageEvent, count: int = 10):
        """显示使用次数排行榜"""
        if not self.redis:
            await event.send(MessageChain().message("❌ Redis未连接，无法获取排行榜"))
            return

        # 验证参数
        if count < 1 or count > 20:
            await event.send(MessageChain().message("❌ 排行榜数量应在1-20之间"))
            return

        try:
            # 获取今日的键模式 - 同时获取个人和群组键
            pattern = f"{self._get_today_key()}:*"

            keys = []
            async for key in self.redis.scan_iter(pattern):
                keys.append(key)
            
            if not keys:
                await event.send(MessageChain().message("📊 今日暂无使用记录"))
                return

            # 获取所有键对应的使用次数，区分个人和群组
            user_usage_data = []
            group_usage_data = []
            
            for key in keys:
                usage = await self.redis.get(key)
                if usage:
                    # 从键名中提取信息 - 使用更健壮的解析方法
                    parts = key.split(":")
                    
                    # 检查键格式是否有效
                    if len(parts) < 4:
                        continue  # 跳过格式不正确的键
                    
                    # 提取日期部分（第3部分）
                    date_part = parts[2] if len(parts) > 2 else ""
                    
                    # 检查是否是群组键（包含"group"关键字）
                    if "group" in key:
                        # 群组键格式: astrbot:daily_limit:2025-01-23:group:群组ID
                        # 查找"group"的位置
                        try:
                            group_index = parts.index("group")
                            if group_index + 1 < len(parts):
                                group_id = parts[group_index + 1]
                                group_usage_data.append({
                                    "group_id": group_id,
                                    "usage": int(usage),
                                    "type": "group",
                                    "date": date_part
                                })
                        except ValueError:
                            # 如果找不到"group"，跳过这个键
                            continue
                    else:
                        # 个人键格式: astrbot:daily_limit:2025-01-23:群组ID:用户ID
                        # 确保有足够的组成部分
                        if len(parts) >= 5:
                            group_id = parts[-2]
                            user_id = parts[-1]
                            
                            # 验证群组ID和用户ID格式（应该是数字）
                            if group_id.isdigit() and user_id.isdigit():
                                user_usage_data.append({
                                    "user_id": user_id,
                                    "group_id": group_id,
                                    "usage": int(usage),
                                    "type": "user",
                                    "date": date_part
                                })
                        elif len(parts) == 4:
                            # 可能是私聊键格式: astrbot:daily_limit:2025-01-23:private_chat:用户ID
                            if parts[-2] == "private_chat":
                                user_id = parts[-1]
                                if user_id.isdigit():
                                    user_usage_data.append({
                                        "user_id": user_id,
                                        "group_id": None,
                                        "usage": int(usage),
                                        "type": "user",
                                        "date": date_part
                                    })

            # 合并数据并按使用次数排序
            all_usage_data = user_usage_data + group_usage_data
            all_usage_data.sort(key=lambda x: x["usage"], reverse=True)
            
            # 取前count名
            top_entries = all_usage_data[:count]
            
            if not top_entries:
                await event.send(MessageChain().message("📊 今日暂无使用记录"))
                return

            # 构建排行榜消息
            leaderboard_msg = f"🏆 今日使用次数排行榜（前{len(top_entries)}名）\n\n"
            
            for i, entry_data in enumerate(top_entries, 1):
                if entry_data["type"] == "group":
                    # 群组条目
                    group_id = entry_data["group_id"]
                    usage = entry_data["usage"]
                    
                    # 获取群组限制
                    limit = self._get_user_limit("dummy_user", group_id)  # 使用虚拟用户ID获取群组限制
                    
                    if limit == float('inf'):
                        limit_text = "无限制"
                    else:
                        limit_text = f"{limit}次"
                    
                    leaderboard_msg += f"{i}. 群组 {group_id} - {usage}次 (限制: {limit_text})\n"
                else:
                    # 个人条目
                    user_id = entry_data["user_id"]
                    usage = entry_data["usage"]
                    group_id = entry_data["group_id"]
                    
                    # 获取用户限制
                    limit = self._get_user_limit(user_id, group_id)
                    
                    if limit == float('inf'):
                        limit_text = "无限制"
                    else:
                        limit_text = f"{limit}次"
                    
                    # 区分群组用户和私聊用户
                    if group_id is None:
                        leaderboard_msg += f"{i}. 私聊用户 {user_id} - {usage}次 (限制: {limit_text})\n"
                    else:
                        leaderboard_msg += f"{i}. 用户 {user_id} - {usage}次 (限制: {limit_text})\n"

            await event.send(MessageChain().message(leaderboard_msg))

        except Exception as e:
            logger.error(f"获取排行榜失败: {str(e)}")
            await event.send(MessageChain().message("❌ 获取排行榜失败，请稍后重试"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("period")
    async def limit_period(self, event: AstrMessageEvent):
        """显示当前时间段限制状态"""
        try:
            # 获取当前时间段限制值
            current_period_limit_value = self._get_current_time_period_limit()
            
            # 获取时间段限制配置
            time_period_limits = self.config.get("limits", {}).get("time_period_limits", [])
            
            if not time_period_limits:
                await event.send(MessageChain().message("📊 当前未配置时间段限制"))
                return
            
            # 构建时间段限制状态消息
            period_msg = "🕐 时间段限制配置状态\n\n"
            
            # 查找当前生效的时间段配置
            current_period_name = None
            for period_config in time_period_limits:
                name = period_config.get("name", "未命名")
                start_time = period_config.get("start_time", "00:00")
                end_time = period_config.get("end_time", "23:59")
                limit = period_config.get("limit", 0)
                enabled = period_config.get("enabled", False)
                
                # 检查是否为当前时间段
                is_current = False
                if current_period_limit_value is not None and current_period_limit_value == limit:
                    # 检查时间段是否匹配当前时间
                    current_time = datetime.datetime.now()
                    current_hour_minute = current_time.strftime("%H:%M")
                    is_weekend = current_time.weekday() >= 5
                    
                    # 检查时间段名称是否包含"周末"，如果是则只在周末生效
                    period_name_lower = name.lower()
                    if "周末" in period_name_lower and not is_weekend:
                        continue
                    elif "周末" not in period_name_lower and is_weekend:
                        continue
                    
                    # 处理跨天的时间段
                    if start_time > end_time:
                        # 跨天时间段：当前时间在开始时间之后或结束时间之前
                        if current_hour_minute >= start_time or current_hour_minute <= end_time:
                            is_current = True
                            current_period_name = name
                    else:
                        # 正常时间段：当前时间在开始时间和结束时间之间
                        if start_time <= current_hour_minute <= end_time:
                            is_current = True
                            current_period_name = name
                
                status_icon = "✅" if enabled else "❌"
                current_icon = "🔵" if is_current else "⚪"
                
                period_msg += f"{status_icon} {current_icon} {name}\n"
                period_msg += f"   时间: {start_time} - {end_time}\n"
                period_msg += f"   限制: {limit} 次\n"
                period_msg += f"   状态: {'已启用' if enabled else '已禁用'}\n"
                
                if is_current:
                    period_msg += f"   当前: 🔥 正在生效\n"
                
                period_msg += "\n"
            
            if current_period_name:
                period_msg += f"💡 当前生效限制: {current_period_name} - {current_period_limit_value} 次"
            else:
                period_msg += "💡 当前无生效的时间段限制"
            
            await event.send(MessageChain().message(period_msg))
            
        except Exception as e:
            logger.error(f"显示时间段限制状态失败: {str(e)}")
            await event.send(MessageChain().message("❌ 显示时间段限制状态失败"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("period_stats")
    async def limit_period_stats(self, event: AstrMessageEvent, days: int = 7):
        """显示时间段限制使用统计"""
        if not self.redis:
            await event.send(MessageChain().message("❌ Redis未连接，无法获取统计信息"))
            return

        # 验证参数
        if days < 1 or days > 30:
            await event.send(MessageChain().message("❌ 统计天数应在1-30之间"))
            return

        try:
            # 获取时间段限制配置
            time_period_limits = self.config.get("limits", {}).get("time_period_limits", [])
            
            if not time_period_limits:
                await event.send(MessageChain().message("📊 当前未配置时间段限制"))
                return

            # 获取最近days天的使用记录
            stats_msg = f"📊 时间段限制使用统计（最近{days}天）\n\n"
            
            for period_config in time_period_limits:
                name = period_config.get("name", "未命名")
                start_time = period_config.get("start_time", "00:00")
                end_time = period_config.get("end_time", "23:59")
                limit = period_config.get("limit", 0)
                enabled = period_config.get("enabled", False)
                
                if not enabled:
                    continue
                
                # 统计该时间段的使用情况
                total_usage = 0
                period_days = 0
                
                for i in range(days):
                    date = (datetime.datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                    
                    # 检查该日期是否在时间段内（处理周末判断）
                    target_date = datetime.datetime.now() - timedelta(days=i)
                    is_weekend = target_date.weekday() >= 5
                    
                    # 检查时间段是否适用于该日期
                    period_applies = True
                    if "周末" in name and not is_weekend:
                        period_applies = False
                    elif "工作日" in name and is_weekend:
                        period_applies = False
                    
                    if period_applies:
                        period_days += 1
                        
                        # 获取该日期的使用统计（简化处理，实际应该更精确）
                        date_key = f"astrbot:daily_limit:{date}"
                        pattern = f"{date_key}:*"
                        
                        keys = []
                        async for key in self.redis.scan_iter(pattern):
                            keys.append(key)
                        for key in keys:
                            usage = await self.redis.get(key)
                            if usage:
                                total_usage += int(usage)
                
                avg_usage = total_usage / period_days if period_days > 0 else 0
                utilization = (avg_usage / limit * 100) if limit > 0 else 0
                
                stats_msg += f"🕐 {name}\n"
                stats_msg += f"   时间: {start_time} - {end_time}\n"
                stats_msg += f"   限制: {limit} 次\n"
                stats_msg += f"   总使用: {total_usage} 次\n"
                stats_msg += f"   适用天数: {period_days} 天\n"
                stats_msg += f"   日均使用: {avg_usage:.1f} 次\n"
                stats_msg += f"   利用率: {utilization:.1f}%\n\n"
            
            await event.send(MessageChain().message(stats_msg))
            
        except Exception as e:
            logger.error(f"获取时间段限制统计失败: {str(e)}")
            await event.send(MessageChain().message("❌ 获取时间段限制统计失败"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("reset")
    async def limit_reset(self, event: AstrMessageEvent, user_id: str = None):
        """重置使用次数（仅管理员）"""
        if not self.redis:
            event.set_result(MessageEventResult().message("Redis未连接，无法重置使用次数"))
            return

        try:
            if user_id is None:
                # 显示重置帮助信息
                help_msg = (
                    "🔄 重置使用次数命令用法：\n"
                    "• /limit reset all - 重置所有使用记录（包括个人和群组）\n"
                    "• /limit reset <用户ID> - 重置特定用户的使用次数\n"
                    "• /limit reset group <群组ID> - 重置特定群组的使用次数\n"
                    "示例：\n"
                    "• /limit reset all - 重置所有使用记录\n"
                    "• /limit reset 123456 - 重置用户123456的使用次数\n"
                    "• /limit reset group 789012 - 重置群组789012的使用次数"
                )
                event.set_result(MessageEventResult().message(help_msg))
                return

            if user_id.lower() == "all":
                # 重置所有使用记录
                today_key = self._get_today_key()
                pattern = f"{today_key}:*"
                
                keys = []
                async for key in self.redis.scan_iter(pattern):
                    keys.append(key)
                
                if not keys:
                    event.set_result(MessageEventResult().message("✅ 当前没有使用记录需要重置"))
                    return
                
                deleted_count = 0
                for key in keys:
                    await self.redis.delete(key)
                    deleted_count += 1
                
                event.set_result(MessageEventResult().message(f"✅ 已重置所有使用记录，共清理 {deleted_count} 条记录"))
                
            elif user_id.lower().startswith("group "):
                # 重置特定群组
                group_id = user_id[6:].strip()  # 移除"group "前缀
                
                # 验证群组ID格式
                if not group_id.isdigit():
                    event.set_result(MessageEventResult().message("❌ 群组ID格式错误，请输入数字ID"))
                    return

                # 查找并删除该群组的所有使用记录
                today_key = self._get_today_key()
                
                # 删除群组共享记录
                group_key = self._get_group_key(group_id)
                group_deleted = 0
                if await self.redis.exists(group_key):
                    await self.redis.delete(group_key)
                    group_deleted += 1
                
                # 删除该群组下所有用户的个人记录
                pattern = f"{today_key}:{group_id}:*"
                user_keys = []
                async for key in self.redis.scan_iter(pattern):
                    user_keys.append(key)
                user_deleted = 0
                for key in user_keys:
                    await self.redis.delete(key)
                    user_deleted += 1
                
                total_deleted = group_deleted + user_deleted
                
                if total_deleted == 0:
                    event.set_result(MessageEventResult().message(f"❌ 未找到群组 {group_id} 的使用记录"))
                else:
                    event.set_result(MessageEventResult().message(f"✅ 已重置群组 {group_id} 的使用次数，共清理 {total_deleted} 条记录（群组: {group_deleted}, 用户: {user_deleted}）"))
                
            else:
                # 重置特定用户
                # 验证用户ID格式
                if not user_id.isdigit():
                    event.set_result(MessageEventResult().message("❌ 用户ID格式错误，请输入数字ID"))
                    return

                # 查找并删除该用户的所有使用记录
                today_key = self._get_today_key()
                pattern = f"{today_key}:*:{user_id}"
                
                keys = []
                async for key in self.redis.scan_iter(pattern):
                    keys.append(key)
                
                if not keys:
                    event.set_result(MessageEventResult().message(f"❌ 未找到用户 {user_id} 的使用记录"))
                    return
                
                deleted_count = 0
                for key in keys:
                    await self.redis.delete(key)
                    deleted_count += 1
                
                event.set_result(MessageEventResult().message(f"✅ 已重置用户 {user_id} 的使用次数，共清理 {deleted_count} 条记录"))
                
        except Exception as e:
            logger.error(f"重置使用次数失败: {str(e)}")
            event.set_result(MessageEventResult().message("重置使用次数失败，请检查Redis连接"))

    async def terminate(self):
        """插件终止时的清理工作"""
        logger.info("日调用限制插件已终止")
