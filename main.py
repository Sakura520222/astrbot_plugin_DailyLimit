import json
import redis
import datetime
import threading
import sys
import os
import astrbot.api.star as star  # type: ignore
from astrbot.api.event import (filter,  # type: ignore
                               AstrMessageEvent,
                               MessageEventResult,
                               MessageChain,
                               EventResultType)
from astrbot.api.platform import MessageType  # type: ignore
from astrbot.api.event.filter import PermissionType  # type: ignore
from astrbot.api import AstrBotConfig  # type: ignore
from astrbot.api.provider import ProviderRequest  # type: ignore
from astrbot.api import logger  # type: ignore

# Web服务器导入
try:
    # 添加当前目录到Python路径
    import sys
    import os
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    
    from web_server import WebServer
except ImportError as e:
    WebServer = None
    logger.warning(f"Web服务器模块导入失败，Web管理界面功能将不可用: {e}")


@star.register(
    name="daily_limit",
    desc="限制用户每日调用大模型的次数",
    author="left666 & Sakura520222",
    version="v2.4.3",
    repo="https://github.com/left666/astrbot_plugin_daily_limit"
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
        self.time_period_limits = []  # 时间段限制配置
        self.usage_records = {}  # 使用记录 {"user_id": {"date": count}}
        self.skip_patterns = []  # 跳过处理的模式列表
        self.web_server = None  # Web服务器实例
        self.web_server_thread = None  # Web服务器线程

        # 加载群组和用户特定限制
        self._load_limits_from_config()

        # 初始化Redis连接
        self._init_redis()

        # 初始化Web服务器
        self._init_web_server()

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

        # 加载时间段限制配置
        time_period_limits = self.config["limits"].get("time_period_limits", [])
        for time_limit in time_period_limits:
            start_time = time_limit.get("start_time")
            end_time = time_limit.get("end_time")
            limit = time_limit.get("limit")
            enabled = time_limit.get("enabled", True)
            
            if start_time and end_time and limit is not None and enabled:
                # 验证时间格式
                try:
                    datetime.datetime.strptime(start_time, "%H:%M")
                    datetime.datetime.strptime(end_time, "%H:%M")
                    self.time_period_limits.append({
                        "start_time": start_time,
                        "end_time": end_time,
                        "limit": limit
                    })
                except ValueError:
                    logger.warning(f"时间段限制配置格式错误: {start_time} - {end_time}")

        # 加载跳过模式配置
        self.skip_patterns = self.config["limits"].get("skip_patterns", ["@所有人", "#"])
        
        logger.info(f"已加载 {len(self.group_limits)} 个群组限制、{len(self.user_limits)} 个用户限制、{len(self.group_modes)} 个群组模式配置、{len(self.time_period_limits)} 个时间段限制和{len(self.skip_patterns)} 个跳过模式")

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

    def _init_redis(self):
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
            self.redis.ping()
            logger.info("Redis连接成功")
        except Exception as e:
            logger.error(f"Redis连接失败: {str(e)}")
            self.redis = None

    def _init_web_server(self):
        """初始化Web服务器"""
        if WebServer is None:
            logger.warning("Web服务器模块不可用，跳过Web服务器初始化")
            return

        try:
            # 获取Web服务器配置
            web_config = self.config.get("web_server", {})
            host = web_config.get("host", "127.0.0.1")
            port = web_config.get("port", 8080)
            debug = web_config.get("debug", False)
            domain = web_config.get("domain", "")

            # 创建Web服务器实例
            self.web_server = WebServer(self, host=host, port=port, domain=domain)
            
            # 启动Web服务器线程
            self.web_server_thread = threading.Thread(target=self.web_server.start_async, daemon=True)
            self.web_server_thread.start()
            
            # 根据是否有域名显示不同的访问地址
            if domain:
                access_url = self.web_server.get_access_url()
                logger.info(f"Web管理界面已启动，访问地址: {access_url}")
            else:
                logger.info(f"Web管理界面已启动，访问地址: http://{host}:{port}")
            
        except Exception as e:
            logger.error(f"Web服务器初始化失败: {str(e)}")
            self.web_server = None



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

    def _should_skip_message(self, message_str):
        """检查消息是否应该跳过处理"""
        if not message_str or not self.skip_patterns:
            return False
        
        # 检查消息是否以任何跳过模式开头
        for pattern in self.skip_patterns:
            if message_str.startswith(pattern):
                return True
        
        return False

    def _get_group_mode(self, group_id):
        """获取群组的模式配置"""
        if not group_id:
            return "individual"  # 私聊默认为独立模式
        
        # 检查是否有特定群组模式配置
        if str(group_id) in self.group_modes:
            return self.group_modes[str(group_id)]
        
        # 默认使用共享模式（保持向后兼容性）
        return "shared"

    def _is_in_time_period(self, current_time_str, start_time_str, end_time_str):
        """检查当前时间是否在指定时间段内"""
        try:
            current_time = datetime.datetime.strptime(current_time_str, "%H:%M").time()
            start_time = datetime.datetime.strptime(start_time_str, "%H:%M").time()
            end_time = datetime.datetime.strptime(end_time_str, "%H:%M").time()
            
            # 处理跨天的时间段（如 22:00 - 06:00）
            if start_time <= end_time:
                # 不跨天的时间段
                return start_time <= current_time <= end_time
            else:
                # 跨天的时间段
                return current_time >= start_time or current_time <= end_time
        except ValueError:
            return False

    def _get_current_time_period_limit(self):
        """获取当前时间段适用的限制"""
        current_time_str = datetime.datetime.now().strftime("%H:%M")
        
        for time_limit in self.time_period_limits:
            if self._is_in_time_period(current_time_str, time_limit["start_time"], time_limit["end_time"]):
                return time_limit["limit"]
        
        return None  # 没有匹配的时间段限制

    def _get_time_period_usage_key(self, user_id, group_id=None, time_period_id=None):
        """获取时间段使用次数的Redis键"""
        if time_period_id is None:
            # 如果没有指定时间段ID，使用当前时间段
            current_time_str = datetime.datetime.now().strftime("%H:%M")
            for i, time_limit in enumerate(self.time_period_limits):
                if self._is_in_time_period(current_time_str, time_limit["start_time"], time_limit["end_time"]):
                    time_period_id = i
                    break
            
            if time_period_id is None:
                return None
        
        if group_id is None:
            group_id = "private_chat"
        
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        return f"astrbot:time_period_limit:{date_str}:{time_period_id}:{group_id}:{user_id}"

    def _get_time_period_usage(self, user_id, group_id=None):
        """获取用户在时间段内的使用次数"""
        if not self.redis:
            return 0
        
        key = self._get_time_period_usage_key(user_id, group_id)
        if key is None:
            return 0
        
        usage = self.redis.get(key)
        return int(usage) if usage else 0

    def _increment_time_period_usage(self, user_id, group_id=None):
        """增加用户在时间段内的使用次数"""
        if not self.redis:
            return False
        
        key = self._get_time_period_usage_key(user_id, group_id)
        if key is None:
            return False
        
        # 增加计数并设置过期时间
        pipe = self.redis.pipeline()
        pipe.incr(key)
        
        # 设置过期时间到明天凌晨
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_tomorrow = int((tomorrow - datetime.datetime.now()).total_seconds())
        pipe.expire(key, seconds_until_tomorrow)
        
        pipe.execute()
        return True

    def _get_user_limit(self, user_id, group_id=None):
        """获取用户的调用限制次数"""
        # 检查用户是否豁免（优先级最高）
        if str(user_id) in self.config["limits"]["exempt_users"]:
            return float('inf')  # 无限制

        # 检查时间段限制（优先级第二）
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

    def _get_user_usage(self, user_id, group_id=None):
        """获取用户已使用次数（兼容旧版本）"""
        if not self.redis:
            return 0

        # 检查时间段限制（优先级最高）
        time_period_limit = self._get_current_time_period_limit()
        if time_period_limit is not None:
            # 有时间段限制时，使用时间段内的使用次数
            time_period_usage = self._get_time_period_usage(user_id, group_id)
            return time_period_usage

        # 没有时间段限制时，使用日使用次数
        key = self._get_user_key(user_id, group_id)
        usage = self.redis.get(key)
        return int(usage) if usage else 0

    def _get_group_usage(self, group_id):
        """获取群组共享使用次数"""
        if not self.redis:
            return 0

        # 检查时间段限制（优先级最高）
        time_period_limit = self._get_current_time_period_limit()
        if time_period_limit is not None:
            # 有时间段限制时，使用时间段内的使用次数
            time_period_usage = self._get_time_period_usage(None, group_id)
            return time_period_usage

        # 没有时间段限制时，使用日使用次数
        key = self._get_group_key(group_id)
        usage = self.redis.get(key)
        return int(usage) if usage else 0

    def _increment_user_usage(self, user_id, group_id=None):
        """增加用户使用次数（兼容旧版本）"""
        if not self.redis:
            return False

        # 检查时间段限制（优先级最高）
        time_period_limit = self._get_current_time_period_limit()
        if time_period_limit is not None:
            # 有时间段限制时，增加时间段使用次数
            if self._increment_time_period_usage(user_id, group_id):
                return True

        # 没有时间段限制时，增加日使用次数
        key = self._get_user_key(user_id, group_id)
        # 增加计数并设置过期时间
        pipe = self.redis.pipeline()
        pipe.incr(key)

        # 设置过期时间到明天凌晨
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_tomorrow = int((tomorrow - datetime.datetime.now()).total_seconds())
        pipe.expire(key, seconds_until_tomorrow)

        pipe.execute()
        return True

    def _increment_group_usage(self, group_id):
        """增加群组共享使用次数"""
        if not self.redis:
            return False

        # 检查时间段限制（优先级最高）
        time_period_limit = self._get_current_time_period_limit()
        if time_period_limit is not None:
            # 有时间段限制时，增加时间段使用次数
            if self._increment_time_period_usage(None, group_id):
                return True

        # 没有时间段限制时，增加日使用次数
        key = self._get_group_key(group_id)
        # 增加计数并设置过期时间
        pipe = self.redis.pipeline()
        pipe.incr(key)

        # 设置过期时间到明天凌晨
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_tomorrow = int((tomorrow - datetime.datetime.now()).total_seconds())
        pipe.expire(key, seconds_until_tomorrow)

        pipe.execute()
        return True

    def _record_usage(self, user_id, group_id=None, usage_type="llm_request"):
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
        self.redis.rpush(record_key, json.dumps(record_data))
        
        # 设置过期时间到明天凌晨
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_tomorrow = int((tomorrow - datetime.datetime.now()).total_seconds())
        self.redis.expire(record_key, seconds_until_tomorrow)
        
        # 更新统计信息
        self._update_usage_stats(user_id, group_id)
        
        return True

    def _update_usage_stats(self, user_id, group_id=None):
        """更新使用统计信息"""
        if not self.redis:
            return False
            
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        stats_key = self._get_usage_stats_key(date_str)
        
        # 更新用户统计
        user_stats_key = f"{stats_key}:user:{user_id}"
        self.redis.hincrby(user_stats_key, "total_usage", 1)
        
        # 更新全局统计
        global_stats_key = f"{stats_key}:global"
        self.redis.hincrby(global_stats_key, "total_requests", 1)
        
        # 需要设置过期时间的键列表
        keys_to_expire = [user_stats_key, global_stats_key]
        
        # 更新群组统计（如果有群组）
        if group_id:
            group_stats_key = f"{stats_key}:group:{group_id}"
            self.redis.hincrby(group_stats_key, "total_usage", 1)
            
            # 更新群组用户统计
            group_user_stats_key = f"{stats_key}:group:{group_id}:user:{user_id}"
            self.redis.hincrby(group_user_stats_key, "usage_count", 1)
            
            # 添加群组相关的键到过期列表
            keys_to_expire.extend([group_stats_key, group_user_stats_key])
        
        # 设置过期时间到明天凌晨
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_tomorrow = int((tomorrow - datetime.datetime.now()).total_seconds())
        
        # 为所有统计键设置过期时间
        for key in keys_to_expire:
            if self.redis.exists(key):
                self.redis.expire(key, seconds_until_tomorrow)
        
        return True

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """处理LLM请求事件"""
        # 检查Redis连接状态，如果未连接则阻止处理
        if not self.redis:
            logger.error("Redis未连接，阻止处理LLM请求")
            event.stop_event()
            return False
        
        # 检查请求是否有效：空提示或匹配跳过模式的消息不处理
        if not req.prompt.strip() or self._should_skip_message(event.message_str):
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
                usage = self._get_group_usage(group_id)
                usage_type = "群组共享"
            else:
                # 独立模式：使用用户个人使用次数
                usage = self._get_user_usage(user_id, group_id)
                usage_type = "个人独立"
        else:
            # 私聊消息：使用个人使用次数
            usage = self._get_user_usage(user_id, group_id)
            usage_type = "个人"

        # 检查是否超过限制
        if usage >= limit:
            logger.info(f"用户 {user_id} 在群 {group_id} 中已达到调用限制 {limit}")
            if group_id is not None:
                user_name = event.get_sender_name()
                if self._get_group_mode(group_id) == "shared":
                    await event.send(
                        MessageChain().at(user_name, user_id).message(f"本群组AI访问次数已达上限（{usage}/{limit}），"
                                                                      f"请稍后再试或联系管理员提升限额。")
                    )
                else:
                    await event.send(
                        MessageChain().at(user_name, user_id).message(f"您在本群组的AI访问次数已达上限（{usage}/{limit}），"
                                                                      f"请稍后再试或联系管理员提升限额。")
                    )
            else:
                await event.send(
                    MessageChain().message(f"您的AI访问次数已达上限（{usage}/{limit}），"
                                           f"请稍后再试或联系管理员提升限额。")
                )
            event.stop_event()  # 终止事件传播
            return False

        # 检查是否需要提醒剩余次数（当剩余次数为1、3、5时提醒）
        remaining = limit - usage
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

        # 增加使用次数
        if group_id is not None:
            group_mode = self._get_group_mode(group_id)
            if group_mode == "shared":
                self._increment_group_usage(group_id)
            else:
                self._increment_user_usage(user_id, group_id)
        else:
            self._increment_user_usage(user_id, group_id)
        
        # 记录使用记录
        self._record_usage(user_id, group_id, "llm_request")
        
        return True  # 允许继续处理

    def _generate_progress_bar(self, usage, limit, bar_length=10):
        """生成进度条"""
        if limit <= 0:
            return ""
        
        percentage = (usage / limit) * 100
        filled_length = int(bar_length * usage // limit)
        bar = "█" * filled_length + "░" * (bar_length - filled_length)
        
        return f"[{bar}] {percentage:.1f}%"

    def _get_reset_time(self):
        """获取每日重置时间"""
        return "00:00:00"

    @filter.command("limit_status")
    async def limit_status(self, event: AstrMessageEvent):
        """用户查看当前使用状态"""
        user_id = event.get_sender_id()
        group_id = None
        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            group_id = event.get_group_id()

        # 检查使用状态
        limit = self._get_user_limit(user_id, group_id)
        
        # 检查当前是否处于时间段限制中
        time_period_limit = self._get_current_time_period_limit()
        current_time_str = datetime.datetime.now().strftime("%H:%M")
        
        # 首先检查用户是否被豁免（优先级最高）
        if str(user_id) in self.config["limits"]["exempt_users"]:
            # 用户被豁免，显示个人豁免状态
            if group_id is not None:
                status_msg = "🎉 您在本群组没有调用次数限制（豁免用户）"
            else:
                status_msg = "🎉 您没有调用次数限制（豁免用户）"
            
            # 添加时间段限制信息（即使豁免用户也显示）
            if time_period_limit is not None:
                # 查找当前时间段的具体信息
                current_period_info = None
                for period in self.time_period_limits:
                    if self._is_in_time_period(current_time_str, period["start_time"], period["end_time"]):
                        current_period_info = period
                        break
                
                if current_period_info:
                    status_msg += f"\n\n⏰ 当前时间段限制：{current_period_info['start_time']}-{current_period_info['end_time']} ({time_period_limit}次)"
        else:
            # 根据群组模式显示正确的状态信息
            if group_id is not None:
                group_mode = self._get_group_mode(group_id)
                if group_mode == "shared":
                    # 共享模式：显示群组共享状态
                    usage = self._get_group_usage(group_id)
                    remaining = limit - usage
                    
                    # 生成进度条
                    progress_bar = self._generate_progress_bar(usage, limit)
                    
                    # 检查群组是否设置了特定限制
                    if str(group_id) in self.group_limits:
                        # 群组有特定限制
                        status_msg = f"👥 群组共享模式 - 特定限制\n" \
                                   f"📊 今日已使用：{usage}/{limit} 次\n" \
                                   f"📈 {progress_bar}\n" \
                                   f"🎯 剩余次数：{remaining} 次"
                    else:
                        # 群组使用默认限制
                        status_msg = f"👥 群组共享模式 - 默认限制\n" \
                                   f"📊 今日已使用：{usage}/{limit} 次\n" \
                                   f"📈 {progress_bar}\n" \
                                   f"🎯 剩余次数：{remaining} 次"
                else:
                    # 独立模式：显示用户个人状态
                    usage = self._get_user_usage(user_id, group_id)
                    remaining = limit - usage
                    
                    # 生成进度条
                    progress_bar = self._generate_progress_bar(usage, limit)
                    
                    # 检查用户是否设置了特定限制
                    if str(user_id) in self.user_limits:
                        # 用户有特定限制
                        status_msg = f"👤 个人独立模式 - 特定限制\n" \
                                   f"📊 今日已使用：{usage}/{limit} 次\n" \
                                   f"📈 {progress_bar}\n" \
                                   f"🎯 剩余次数：{remaining} 次"
                    # 检查群组是否设置了特定限制
                    elif str(group_id) in self.group_limits:
                        # 群组有特定限制
                        status_msg = f"👤 个人独立模式 - 群组限制\n" \
                                   f"📊 今日已使用：{usage}/{limit} 次\n" \
                                   f"📈 {progress_bar}\n" \
                                   f"🎯 剩余次数：{remaining} 次"
                    else:
                        # 使用默认限制
                        status_msg = f"👤 个人独立模式 - 默认限制\n" \
                                   f"📊 今日已使用：{usage}/{limit} 次\n" \
                                   f"📈 {progress_bar}\n" \
                                   f"🎯 剩余次数：{remaining} 次"
            else:
                # 私聊消息：显示个人状态
                usage = self._get_user_usage(user_id, group_id)
                remaining = limit - usage
                
                # 生成进度条
                progress_bar = self._generate_progress_bar(usage, limit)
                
                status_msg = f"👤 个人使用状态\n" \
                           f"📊 今日已使用：{usage}/{limit} 次\n" \
                           f"📈 {progress_bar}\n" \
                           f"🎯 剩余次数：{remaining} 次"
            
            # 添加时间段限制信息
            if time_period_limit is not None:
                # 查找当前时间段的具体信息
                current_period_info = None
                for period in self.time_period_limits:
                    if self._is_in_time_period(current_time_str, period["start_time"], period["end_time"]):
                        current_period_info = period
                        break
                
                if current_period_info:
                    status_msg += f"\n\n⏰ 当前处于时间段限制：{current_period_info['start_time']}-{current_period_info['end_time']}"
                    status_msg += f"\n📋 时间段限制：{time_period_limit} 次"
                    
                    # 显示时间段内的使用情况
                    time_period_usage = self._get_time_period_usage(user_id, group_id)
                    time_period_remaining = time_period_limit - time_period_usage
                    
                    # 生成时间段进度条
                    time_period_progress = self._generate_progress_bar(time_period_usage, time_period_limit)
                    
                    status_msg += f"\n📊 时间段内已使用：{time_period_usage}/{time_period_limit} 次"
                    status_msg += f"\n📈 {time_period_progress}"
                    status_msg += f"\n🎯 时间段内剩余：{time_period_remaining} 次"

        # 添加使用建议和提示信息
        if not str(user_id) in self.config["limits"]["exempt_users"]:
            status_msg += "\n\n💡 使用提示："
            
            # 根据剩余次数给出建议
            if remaining <= 0:
                status_msg += "\n⚠️ 今日次数已用完，请明天再试"
            elif remaining <= limit * 0.2:  # 剩余20%以下
                status_msg += "\n⚠️ 剩余次数较少，请谨慎使用"
            elif remaining <= limit * 0.5:  # 剩余50%以下
                status_msg += "\n💡 剩余次数适中，可继续使用"
            else:
                status_msg += "\n✅ 剩余次数充足，可放心使用"
            
            # 添加时间段限制提示
            if time_period_limit is not None:
                if time_period_remaining <= 0:
                    status_msg += "\n⏰ 当前时间段次数已用完"
                elif time_period_remaining <= time_period_limit * 0.3:  # 剩余30%以下
                    status_msg += "\n⏰ 当前时间段剩余次数较少"
            
            # 添加通用提示
            status_msg += "\n📝 使用 /限制帮助 查看详细说明"
            
            # 重置时间提示
            reset_time = self._get_reset_time()
            status_msg += f"\n🔄 每日重置时间：{reset_time}"

        event.set_result(MessageEventResult().message(status_msg))

    @filter.command("限制帮助")
    async def limit_help_all(self, event: AstrMessageEvent):
        """显示本插件所有指令及其帮助信息"""
        help_msg = (
            "🚀 日调用限制插件 v2.4.3 - 完整指令帮助\n"
            "════════════════════════════\n\n"
            "👤 用户指令（所有人可用）：\n"
            "├── /limit_status - 查看您今日的使用状态和剩余次数\n"
            "└── /限制帮助 - 显示本帮助信息\n\n"
            "👨‍💼 管理员指令（仅管理员可用）：\n"
            "├── /limit help - 显示详细管理员帮助信息\n"
            "├── /limit set <用户ID> <次数> - 设置特定用户的每日限制次数\n"
            "├── /limit setgroup <次数> - 设置当前群组的每日限制次数\n"
            "├── /limit setmode <shared|individual> - 设置群组使用模式（共享/独立）\n"
            "├── /limit getmode - 查看当前群组使用模式\n"
            "├── /limit exempt <用户ID> - 将用户添加到豁免列表（不受限制）\n"
            "├── /limit unexempt <用户ID> - 将用户从豁免列表移除\n"
            "├── /limit list_user - 列出所有用户特定限制\n"
            "├── /limit list_group - 列出所有群组特定限制\n"
            "├── /limit stats - 查看今日使用统计信息\n"
            "├── /limit history [用户ID] [天数] - 查询使用历史记录\n"
            "├── /limit analytics [日期] - 多维度统计分析\n"
            "├── /limit top [数量] - 查看使用次数排行榜\n"
            "├── /limit status - 检查插件状态和健康状态\n"
            "├── /limit reset <用户ID|all> - 重置用户使用次数\n"
            "└── /limit skip_patterns - 管理跳过处理的模式配置\n\n"
            "⏰ 时间段限制命令：\n"
            "├── /limit timeperiod list - 列出所有时间段限制配置\n"
            "├── /limit timeperiod add <开始时间> <结束时间> <次数> - 添加时间段限制\n"
            "├── /limit timeperiod remove <索引> - 删除时间段限制\n"
            "├── /limit timeperiod enable <索引> - 启用时间段限制\n"
            "└── /limit timeperiod disable <索引> - 禁用时间段限制\n\n"
            "🔧 跳过模式管理命令：\n"
            "├── /limit skip_patterns list - 查看当前跳过模式\n"
            "├── /limit skip_patterns add <模式> - 添加跳过模式\n"
            "├── /limit skip_patterns remove <模式> - 移除跳过模式\n"
            "└── /limit skip_patterns reset - 重置为默认模式\n\n"
            "💡 核心功能特性：\n"
            "✅ 智能限制系统：多级权限管理，支持用户、群组、豁免用户三级体系\n"
            "✅ 时间段限制：支持按时间段设置不同的调用限制（优先级最高）\n"
            "✅ 群组协作模式：支持共享模式（群组共享次数）和独立模式（成员独立次数）\n"
            "✅ 数据监控分析：实时监控、使用统计、排行榜和状态监控\n"
            "✅ 使用记录：详细记录每次调用，支持历史查询和统计分析\n"
            "✅ 自定义跳过模式：可配置需要跳过处理的消息前缀\n\n"
            "🎯 优先级规则（从高到低）：\n"
            "1️⃣ ⏰ 时间段限制 - 优先级最高（特定时间段内的限制）\n"
            "2️⃣ 🏆 豁免用户 - 完全不受限制（白名单用户）\n"
            "3️⃣ 👤 用户特定限制 - 针对单个用户的个性化设置\n"
            "4️⃣ 👥 群组特定限制 - 针对整个群组的统一设置\n"
            "5️⃣ ⚙️ 默认限制 - 全局默认设置（兜底规则）\n\n"
            "📊 使用模式说明：\n"
            "• 🔄 共享模式：群组内所有成员共享使用次数（默认模式）\n"
            "   └── 适合小型团队协作，统一管理使用次数\n"
            "• 👤 独立模式：群组内每个成员有独立的使用次数\n"
            "   └── 适合大型团队，成员间互不影响\n\n"
            "🔔 智能提醒：\n"
            "• 📢 剩余次数提醒：当剩余1、3、5次时会自动提醒\n"
            "• 📊 使用状态监控：实时监控使用情况，防止滥用\n\n"
            "📝 使用提示：\n"
            "• 普通用户可使用 /limit_status 查看自己的使用状态\n"
            "• 管理员可使用 /limit help 查看详细管理命令\n"
            "• 时间段限制优先级最高，会覆盖其他限制规则\n"
            "• 默认跳过模式：@所有人、#（可自定义添加）\n\n"
            "📝 版本信息：v2.4.3 | 作者：left666 | 改进：Sakura520222\n"
            "════════════════════════════"
        )

        event.set_result(MessageEventResult().message(help_msg))

    @filter.command_group("limit")
    def limit_command_group(self):
        """限制命令组"""
        pass

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("skip_patterns")
    async def limit_skip_patterns(self, event: AstrMessageEvent):
        """管理跳过模式配置（仅管理员）"""
        args = event.message_str.strip().split()
        
        # 检查命令格式：/limit skip_patterns [action] [pattern]
        if len(args) < 3:
            # 显示当前跳过模式和帮助信息
            patterns_str = ", ".join([f'"{pattern}"' for pattern in self.skip_patterns])
            event.set_result(MessageEventResult().message(
                f"当前跳过模式：{patterns_str}\n"
                f"使用方式：/limit skip_patterns list - 查看当前模式\n"
                f"使用方式：/limit skip_patterns add <模式> - 添加跳过模式\n"
                f"使用方式：/limit skip_patterns remove <模式> - 移除跳过模式\n"
                f"使用方式：/limit skip_patterns reset - 重置为默认模式"
            ))
            return
        
        action = args[2]
        
        if action == "list":
            # 显示当前跳过模式
            patterns_str = ", ".join([f'"{pattern}"' for pattern in self.skip_patterns])
            event.set_result(MessageEventResult().message(f"当前跳过模式：{patterns_str}"))
            
        elif action == "add" and len(args) > 3:
            # 添加跳过模式
            pattern = args[3]
            if pattern in self.skip_patterns:
                event.set_result(MessageEventResult().message(f"跳过模式 '{pattern}' 已存在"))
            else:
                self.skip_patterns.append(pattern)
                # 保存到配置文件
                self.config["limits"]["skip_patterns"] = self.skip_patterns
                self.config.save_config()
                event.set_result(MessageEventResult().message(f"已添加跳过模式：'{pattern}'"))
                
        elif action == "remove" and len(args) > 3:
            # 移除跳过模式
            pattern = args[3]
            if pattern in self.skip_patterns:
                self.skip_patterns.remove(pattern)
                # 保存到配置文件
                self.config["limits"]["skip_patterns"] = self.skip_patterns
                self.config.save_config()
                event.set_result(MessageEventResult().message(f"已移除跳过模式：'{pattern}'"))
            else:
                event.set_result(MessageEventResult().message(f"跳过模式 '{pattern}' 不存在"))
                
        elif action == "reset":
            # 重置为默认模式
            self.skip_patterns = ["@所有人", "#"]
            # 保存到配置文件
            self.config["limits"]["skip_patterns"] = self.skip_patterns
            self.config.save_config()
            event.set_result(MessageEventResult().message("已重置跳过模式为默认值：'@所有人', '#'"))
            
        else:
            event.set_result(MessageEventResult().message("无效的命令格式，请使用 /limit skip_patterns 查看帮助"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("help")
    async def limit_help(self, event: AstrMessageEvent):
        """显示详细帮助信息（仅管理员）"""
        help_msg = (
            "🚀 日调用限制插件 v2.4.3 - 管理员详细帮助\n"
            "════════════════════════════\n\n"
            "📋 基础管理命令：\n"
            "├── /limit help - 显示此帮助信息\n"
            "├── /limit set <用户ID> <次数> - 设置特定用户的每日限制次数\n"
            "│   示例：/limit set 123456 50 - 设置用户123456的每日限制为50次\n"
            "├── /limit setgroup <次数> - 设置当前群组的每日限制次数\n"
            "│   示例：/limit setgroup 30 - 设置当前群组的每日限制为30次\n"
            "├── /limit setmode <shared|individual> - 设置当前群组使用模式\n"
            "│   示例：/limit setmode shared - 设置为共享模式\n"
            "├── /limit getmode - 查看当前群组使用模式\n"
            "├── /limit exempt <用户ID> - 将用户添加到豁免列表（不受限制）\n"
            "│   示例：/limit exempt 123456 - 豁免用户123456\n"
            "├── /limit unexempt <用户ID> - 将用户从豁免列表移除\n"
            "│   示例：/limit unexempt 123456 - 取消用户123456的豁免\n"
            "├── /limit list_user - 列出所有用户特定限制\n"
            "└── /limit list_group - 列出所有群组特定限制\n"
            "\n⏰ 时间段限制命令：\n"
            "├── /limit timeperiod list - 列出所有时间段限制配置\n"
            "├── /limit timeperiod add <开始时间> <结束时间> <限制次数> - 添加时间段限制\n"
            "│   示例：/limit timeperiod add 09:00 18:00 10 - 添加9:00-18:00时间段限制10次\n"
            "├── /limit timeperiod remove <索引> - 删除时间段限制\n"
            "│   示例：/limit timeperiod remove 1 - 删除第1个时间段限制\n"
            "├── /limit timeperiod enable <索引> - 启用时间段限制\n"
            "│   示例：/limit timeperiod enable 1 - 启用第1个时间段限制\n"
            "└── /limit timeperiod disable <索引> - 禁用时间段限制\n"
            "    示例：/limit timeperiod disable 1 - 禁用第1个时间段限制\n"
            "\n🔧 跳过模式管理命令：\n"
            "├── /limit skip_patterns list - 查看当前跳过模式\n"
            "├── /limit skip_patterns add <模式> - 添加跳过模式\n"
            "│   示例：/limit skip_patterns add ! - 添加!为跳过模式\n"
            "├── /limit skip_patterns remove <模式> - 移除跳过模式\n"
            "│   示例：/limit skip_patterns remove # - 移除#跳过模式\n"
            "└── /limit skip_patterns reset - 重置为默认模式\n"
            "    示例：/limit skip_patterns reset - 重置为默认模式[@所有人, #]\n"
            "\n📊 查询统计命令：\n"
            "├── /limit stats - 查看今日使用统计信息\n"
            "├── /limit history [用户ID] [天数] - 查询使用历史记录\n"
            "│   示例：/limit history 123456 7 - 查询用户123456最近7天的使用记录\n"
            "├── /limit analytics [日期] - 多维度统计分析\n"
            "│   示例：/limit analytics 2025-01-23 - 分析2025年1月23日的使用数据\n"
            "├── /limit top [数量] - 查看使用次数排行榜\n"
            "│   示例：/limit top 10 - 查看今日使用次数前10名\n"
            "├── /limit status - 检查插件状态和健康状态\n"
            "└── /limit domain - 查看Web管理界面域名配置和访问地址\n"
            "\n🔄 重置命令：\n"
            "├── /limit reset all - 重置所有使用记录（包括个人和群组）\n"
            "├── /limit reset <用户ID> - 重置特定用户的使用次数\n"
            "│   示例：/limit reset 123456 - 重置用户123456的使用次数\n"
            "└── /limit reset group <群组ID> - 重置特定群组的使用次数\n"
            "    示例：/limit reset group 789012 - 重置群组789012的使用次数\n"
            "\n🎯 优先级规则（从高到低）：\n"
            "1️⃣ ⏰ 时间段限制 - 优先级最高（特定时间段内的限制）\n"
            "2️⃣ 🏆 豁免用户 - 完全不受限制（白名单用户）\n"
            "3️⃣ 👤 用户特定限制 - 针对单个用户的个性化设置\n"
            "4️⃣ 👥 群组特定限制 - 针对整个群组的统一设置\n"
            "5️⃣ ⚙️ 默认限制 - 全局默认设置（兜底规则）\n"
            "\n📊 使用模式说明：\n"
            "• 🔄 共享模式：群组内所有成员共享使用次数（默认模式）\n"
            "   └── 适合小型团队协作，统一管理使用次数\n"
            "• 👤 独立模式：群组内每个成员有独立的使用次数\n"
            "   └── 适合大型团队，成员间互不影响\n"
            "\n💡 功能特性：\n"
            "✅ 智能限制系统：多级权限管理，支持用户、群组、豁免用户三级体系\n"
            "✅ 时间段限制：支持按时间段设置不同的调用限制（优先级最高）\n"
            "✅ 群组协作模式：支持共享模式（群组共享次数）和独立模式（成员独立次数）\n"
            "✅ 数据监控分析：实时监控、使用统计、排行榜和状态监控\n"
            "✅ 使用记录：详细记录每次调用，支持历史查询和统计分析\n"
            "✅ 自定义跳过模式：可配置需要跳过处理的消息前缀\n"
            "✅ 智能提醒：剩余次数提醒和使用状态监控\n"
            "\n📝 使用提示：\n"
            "• 所有命令都需要管理员权限才能使用\n"
            "• 时间段限制优先级最高，会覆盖其他限制规则\n"
            "• 豁免用户不受任何限制规则约束\n"
            "• 默认跳过模式：@所有人、#（可自定义添加）\n"
            "\n📝 版本信息：v2.4.3 | 作者：left666 | 改进：Sakura520222\n"
            "════════════════════════════"
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
            keys = self.redis.keys(pattern)
            
            total_calls = 0
            active_users = 0
            
            for key in keys:
                usage = self.redis.get(key)
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
                    private_records = self.redis.lrange(private_key, 0, -1)
                    
                    # 查询群组记录
                    group_pattern = f"astrbot:usage_record:{date_str}:*:{user_id}"
                    group_keys = self.redis.keys(group_pattern)
                    
                    daily_total = len(private_records)
                    
                    for key in group_keys:
                        group_records = self.redis.lrange(key, 0, -1)
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
                    
                    total_requests = self.redis.hget(global_key, "total_requests")
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
            total_requests = self.redis.hget(global_key, "total_requests")
            
            # 获取用户统计
            user_pattern = f"{stats_key}:user:*"
            user_keys = self.redis.keys(user_pattern)
            
            # 获取群组统计
            group_pattern = f"{stats_key}:group:*"
            group_keys = self.redis.keys(group_pattern)
            
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
                    usage = self.redis.hget(key, "total_usage")
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
                    usage = self.redis.hget(key, "total_usage")
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
                    usage = self.redis.hget(key, "total_usage")
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
                    self.redis.ping()
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
                    keys = self.redis.keys(pattern)
                    
                    total_calls = 0
                    active_users = 0
                    
                    for key in keys:
                        usage = self.redis.get(key)
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
    @limit_command_group.command("domain")
    async def limit_domain(self, event: AstrMessageEvent):
        """查看配置的域名和访问地址（仅管理员）"""
        try:
            # 获取域名配置
            web_config = self.config.get("web_server", {})
            domain = web_config.get("domain", "")
            host = web_config.get("host", "127.0.0.1")
            port = web_config.get("port", 8080)
            
            domain_msg = "🌐 域名配置信息\n"
            domain_msg += "════════════════════════\n"
            
            if domain:
                domain_msg += f"✅ 已配置自定义域名: {domain}\n"
                # 获取Web服务器的访问地址
                if self.web_server:
                    access_url = self.web_server.get_access_url()
                    domain_msg += f"🔗 访问地址: {access_url}\n"
                else:
                    domain_msg += f"🔗 访问地址: https://{domain}\n"
            else:
                domain_msg += "❌ 未配置自定义域名\n"
                domain_msg += f"🔗 当前访问地址: http://{host}:{port}\n"
            
            domain_msg += "\n💡 配置说明:\n"
            domain_msg += "• 在配置文件的 web_server 部分添加 domain 字段来设置自定义域名\n"
            domain_msg += "• 例如: \"domain\": \"example.com\"\n"
            domain_msg += "• 配置域名后，Web管理界面将使用该域名生成访问链接\n"
            
            await event.send(MessageChain().message(domain_msg))
            
        except Exception as e:
            logger.error(f"获取域名配置失败: {str(e)}")
            await event.send(MessageChain().message("❌ 获取域名配置失败，请检查配置文件"))

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

            keys = self.redis.keys(pattern)
            
            if not keys:
                await event.send(MessageChain().message("📊 今日暂无使用记录"))
                return

            # 获取所有键对应的使用次数，区分个人和群组
            user_usage_data = []
            group_usage_data = []
            
            for key in keys:
                usage = self.redis.get(key)
                if usage:
                    # 从键名中提取信息
                    parts = key.split(":")
                    if len(parts) >= 5:
                        # 判断是个人键还是群组键
                        if parts[-2] == "group":
                            # 群组键格式: astrbot:daily_limit:2025-01-23:group:群组ID
                            group_id = parts[-1]
                            group_usage_data.append({
                                "group_id": group_id,
                                "usage": int(usage),
                                "type": "group"
                            })
                        else:
                            # 个人键格式: astrbot:daily_limit:2025-01-23:群组ID:用户ID
                            group_id = parts[-2]
                            user_id = parts[-1]
                            user_usage_data.append({
                                "user_id": user_id,
                                "group_id": group_id,
                                "usage": int(usage),
                                "type": "user"
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
                    
                    leaderboard_msg += f"{i}. 用户 {user_id} - {usage}次 (限制: {limit_text})\n"

            await event.send(MessageChain().message(leaderboard_msg))

        except Exception as e:
            logger.error(f"获取排行榜失败: {str(e)}")
            await event.send(MessageChain().message("❌ 获取排行榜失败，请稍后重试"))

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
                
                keys = self.redis.keys(pattern)
                
                if not keys:
                    event.set_result(MessageEventResult().message("✅ 当前没有使用记录需要重置"))
                    return
                
                deleted_count = 0
                for key in keys:
                    self.redis.delete(key)
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
                if self.redis.exists(group_key):
                    self.redis.delete(group_key)
                    group_deleted += 1
                
                # 删除该群组下所有用户的个人记录
                pattern = f"{today_key}:{group_id}:*"
                user_keys = self.redis.keys(pattern)
                user_deleted = 0
                for key in user_keys:
                    self.redis.delete(key)
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
                
                keys = self.redis.keys(pattern)
                
                if not keys:
                    event.set_result(MessageEventResult().message(f"❌ 未找到用户 {user_id} 的使用记录"))
                    return
                
                deleted_count = 0
                for key in keys:
                    self.redis.delete(key)
                    deleted_count += 1
                
                event.set_result(MessageEventResult().message(f"✅ 已重置用户 {user_id} 的使用次数，共清理 {deleted_count} 条记录"))
                
        except Exception as e:
            logger.error(f"重置使用次数失败: {str(e)}")
            event.set_result(MessageEventResult().message("重置使用次数失败，请检查Redis连接"))

    async def terminate(self):
        """插件终止时的清理工作"""
        # 停止Web服务器
        if self.web_server:
            try:
                self.web_server.stop()
                logger.info("Web服务器已停止")
            except Exception as e:
                logger.error(f"停止Web服务器失败: {str(e)}")
        
        logger.info("日调用限制插件已终止")

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("timeperiod", "list")
    async def limit_timeperiod_list(self, event: AstrMessageEvent):
        """列出所有时间段限制配置（仅管理员）"""
        if not self.time_period_limits:
            event.set_result(MessageEventResult().message("当前没有设置任何时间段限制"))
            return

        timeperiod_msg = "⏰ 时间段限制配置列表：\n"
        for i, period in enumerate(self.time_period_limits, 1):
            status = "✅ 启用" if period["enabled"] else "❌ 禁用"
            timeperiod_msg += f"{i}. {period['start_time']} - {period['end_time']}: {period['limit']} 次 ({status})\n"

        event.set_result(MessageEventResult().message(timeperiod_msg))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("timeperiod", "add")
    async def limit_timeperiod_add(self, event: AstrMessageEvent, start_time: str = None, end_time: str = None, limit: int = None):
        """添加时间段限制（仅管理员）"""
        if not all([start_time, end_time, limit]):
            event.set_result(MessageEventResult().message("用法: /limit timeperiod add <开始时间> <结束时间> <限制次数>"))
            return

        try:
            # 验证时间格式
            datetime.datetime.strptime(start_time, "%H:%M")
            datetime.datetime.strptime(end_time, "%H:%M")
            
            # 验证限制次数
            limit = int(limit)
            if limit < 1:
                event.set_result(MessageEventResult().message("限制次数必须大于0"))
                return

            # 添加时间段限制
            new_period = {
                "start_time": start_time,
                "end_time": end_time,
                "limit": limit,
                "enabled": True
            }
            
            self.time_period_limits.append(new_period)
            self._save_time_period_limits()
            
            event.set_result(MessageEventResult().message(f"✅ 已添加时间段限制: {start_time} - {end_time}: {limit} 次"))
            
        except ValueError as e:
            if "does not match format" in str(e):
                event.set_result(MessageEventResult().message("时间格式错误，请使用 HH:MM 格式（如 09:00）"))
            else:
                event.set_result(MessageEventResult().message("限制次数必须为整数"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("timeperiod", "remove")
    async def limit_timeperiod_remove(self, event: AstrMessageEvent, index: int = None):
        """删除时间段限制（仅管理员）"""
        if index is None:
            event.set_result(MessageEventResult().message("用法: /limit timeperiod remove <索引>"))
            return

        try:
            index = int(index) - 1  # 转换为0-based索引
            
            if index < 0 or index >= len(self.time_period_limits):
                event.set_result(MessageEventResult().message(f"索引无效，请使用 1-{len(self.time_period_limits)} 之间的数字"))
                return

            removed_period = self.time_period_limits.pop(index)
            self._save_time_period_limits()
            
            event.set_result(MessageEventResult().message(f"✅ 已删除时间段限制: {removed_period['start_time']} - {removed_period['end_time']}"))
            
        except ValueError:
            event.set_result(MessageEventResult().message("索引必须为整数"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("timeperiod", "enable")
    async def limit_timeperiod_enable(self, event: AstrMessageEvent, index: int = None):
        """启用时间段限制（仅管理员）"""
        if index is None:
            event.set_result(MessageEventResult().message("用法: /limit timeperiod enable <索引>"))
            return

        try:
            index = int(index) - 1  # 转换为0-based索引
            
            if index < 0 or index >= len(self.time_period_limits):
                event.set_result(MessageEventResult().message(f"索引无效，请使用 1-{len(self.time_period_limits)} 之间的数字"))
                return

            self.time_period_limits[index]["enabled"] = True
            self._save_time_period_limits()
            
            period = self.time_period_limits[index]
            event.set_result(MessageEventResult().message(f"✅ 已启用时间段限制: {period['start_time']} - {period['end_time']}"))
            
        except ValueError:
            event.set_result(MessageEventResult().message("索引必须为整数"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("timeperiod", "disable")
    async def limit_timeperiod_disable(self, event: AstrMessageEvent, index: int = None):
        """禁用时间段限制（仅管理员）"""
        if index is None:
            event.set_result(MessageEventResult().message("用法: /limit timeperiod disable <索引>"))
            return

        try:
            index = int(index) - 1  # 转换为0-based索引
            
            if index < 0 or index >= len(self.time_period_limits):
                event.set_result(MessageEventResult().message(f"索引无效，请使用 1-{len(self.time_period_limits)} 之间的数字"))
                return

            self.time_period_limits[index]["enabled"] = False
            self._save_time_period_limits()
            
            period = self.time_period_limits[index]
            event.set_result(MessageEventResult().message(f"✅ 已禁用时间段限制: {period['start_time']} - {period['end_time']}"))
            
        except ValueError:
            event.set_result(MessageEventResult().message("索引必须为整数"))

    def _save_time_period_limits(self):
        """保存时间段限制配置到配置文件"""
        try:
            # 确保time_period_limits字段存在
            if "time_period_limits" not in self.config["limits"]:
                self.config["limits"]["time_period_limits"] = []
            
            # 更新配置对象
            self.config["limits"]["time_period_limits"] = self.time_period_limits
            # 保存到配置文件
            self.config.save_config()
            logger.info(f"已保存时间段限制配置，共 {len(self.time_period_limits)} 个时间段")
        except Exception as e:
            logger.error(f"保存时间段限制配置失败: {str(e)}")
