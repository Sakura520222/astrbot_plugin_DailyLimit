import redis
import datetime
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
    name="daily_limit",
    desc="限制人员每日调用大模型的次数",
    author="left666",
    version="v1.0.1",
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

        # 加载群组和用户特定限制
        self._load_limits_from_config()

        # 初始化Redis连接
        self._init_redis()

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

        logger.info(f"已加载 {len(self.group_limits)} 个群组限制和 {len(self.user_limits)} 个用户限制")

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

    def _get_user_limit(self, user_id, group_id=None):
        """获取用户的调用限制次数"""
        # 检查用户是否豁免
        if str(user_id) in self.config["limits"]["exempt_users"]:
            return float('inf')  # 无限制

        # 检查用户特定限制
        if str(user_id) in self.user_limits:
            return self.user_limits[str(user_id)]

        # 检查群组特定限制
        if group_id and str(group_id) in self.group_limits:
            return self.group_limits[str(group_id)]

        # 返回默认限制
        return self.config["limits"]["default_daily_limit"]

    def _get_user_usage(self, user_id, group_id=None):
        """获取用户今日已使用次数"""
        if not self.redis:
            return 0

        key = self._get_user_key(user_id, group_id)
        usage = self.redis.get(key)
        return int(usage) if usage else 0

    def _increment_user_usage(self, user_id, group_id=None):
        """增加用户使用次数"""
        if not self.redis:
            return False

        key = self._get_user_key(user_id, group_id)
        # 增加计数并设置过期时间（确保第二天自动重置）
        pipe = self.redis.pipeline()
        pipe.incr(key)

        # 设置过期时间到明天凌晨
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_tomorrow = int((tomorrow - datetime.datetime.now()).total_seconds())
        pipe.expire(key, seconds_until_tomorrow)

        pipe.execute()
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

        # 获取用户限制和使用情况
        limit = self._get_user_limit(user_id, group_id)
        usage = self._get_user_usage(user_id, group_id)

        # 检查是否超过限制
        if usage >= limit:
            logger.info(f"用户 {user_id} 在群 {group_id} 中已达到今日调用限制 {limit}")
            if group_id is not None:
                user_name = event.get_sender_name()
                await event.send(
                    MessageChain().at(user_name, user_id).message(f"您今日的AI访问次数已达上限({limit}次)，"
                                                                  f"请明天再试或联系管理员提升限额。")
                )
            else:
                await event.send(
                    MessageChain().message(f"您今日的AI访问次数已达上限({limit}次)，"
                                           f"请明天再试或联系管理员提升限额。")
                )
            event.stop_event()  # 终止事件传播

            return False

        # 增加用户使用次数
        self._increment_user_usage(user_id, group_id)
        return True  # 允许继续处理

    @filter.command("limit_status")
    async def limit_status(self, event: AstrMessageEvent):
        """用户查看当前使用状态"""
        user_id = event.get_sender_id()
        group_id = None
        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            group_id = event.get_group_id()

        limit = self._get_user_limit(user_id, group_id)
        usage = self._get_user_usage(user_id, group_id)

        if limit == float('inf'):
            status_msg = "您没有调用次数限制"
        else:
            status_msg = f"您今日已使用 {usage}/{limit} 次AI"

        event.set_result(MessageEventResult().message(status_msg))

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
            "- /limit exempt <用户ID>：将用户添加到豁免列表\n"
            "- /limit unexempt <用户ID>：将用户从豁免列表移除\n"
            "- /limit list_user：列出所有用户特定限制\n"
            "- /limit list_group：列出所有群组特定限制\n"
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

    async def terminate(self):
        """插件终止时的清理工作"""
        logger.info("日调用限制插件已终止")
