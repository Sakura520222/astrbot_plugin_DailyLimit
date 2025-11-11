import json
import redis
import redis.exceptions
import datetime
import threading
import sys
import os
import asyncio
import aiohttp
import time
from typing import Optional  # type: ignore
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
WebServer = None
try:
    # 添加当前目录到Python路径
    import sys
    import os
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    
    from web_server import WebServer
except ImportError as e:
    # 注意：在模块级别不能使用self，这里只是定义变量
    WebServer = None
    # 实际的日志记录将在插件初始化后进行


@star.register(
    name="daily_limit",
    desc="限制用户每日调用大模型的次数",
    author="left666 & Sakura520222",
    version="v2.7.4",
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
        self.skip_patterns = []  # 忽略处理的模式列表
        self.web_server = None  # Web服务器实例
        self.web_server_thread = None  # Web服务器线程
        
        # 版本检查相关变量
        self.version_check_task = None  # 版本检查异步任务
        self.last_checked_version = None  # 上次检查的版本号
        self.last_notified_version = None  # 上次通知的版本号

        # 安全增强相关变量
        self.abuse_records = {}  # 异常行为记录 {"user_id": {"timestamp": count}}
        self.blocked_users = {}  # 被限制的用户 {"user_id": "block_until_timestamp"}
        self.abuse_stats = {}  # 异常统计 {"user_id": {"total_abuse_count": count, "last_abuse_time": timestamp}}

        # 加载群组和用户特定限制
        self._load_limits_from_config()

        # 初始化Redis连接
        self._init_redis()

        # 检查Web服务器模块是否成功导入
        if WebServer is None:
            self._log_warning("Web服务器模块导入失败，Web管理界面功能将不可用")
        else:
            # 初始化Web服务器
            self._init_web_server()
        
        # 初始化版本检查功能
        self._init_version_check()

    def _load_limits_from_config(self):
        """
        从配置文件加载群组和用户特定限制
        
        从插件的配置文件中加载所有限制相关设置，包括：
        - 群组限制配置
        - 用户限制配置  
        - 群组模式配置
        - 时间段限制配置
        - 忽略模式配置
        - 每日重置时间验证
        
        返回：
            bool: 加载成功返回True，失败返回False
        """
        self._parse_group_limits()
        self._parse_user_limits()
        self._parse_group_modes()
        self._parse_time_period_limits()
        self._load_skip_patterns()
        self._validate_daily_reset_time()
        
        self._log_info("已加载 {} 个群组限制、{} 个用户限制、{} 个群组模式配置、{} 个时间段限制和{} 个忽略模式", 
                          len(self.group_limits), len(self.user_limits), len(self.group_modes), 
                          len(self.time_period_limits), len(self.skip_patterns))
        
        # 加载安全配置
        self._load_security_config()

    def _parse_limits_config(self, config_key: str, limits_dict: dict, limit_type: str) -> None:
        """
        通用限制配置解析方法
        
        Args:
            config_key: 配置键名
            limits_dict: 目标限制字典
            limit_type: 限制类型描述
        """
        config_text = self.config["limits"].get(config_key, "")
        for line in config_text.strip().split('\n'):
            self._parse_limit_line(line, limits_dict, limit_type)

    def _parse_group_limits(self):
        """解析群组特定限制配置"""
        self._parse_limits_config("group_limits", self.group_limits, "群组")

    def _parse_user_limits(self):
        """解析用户特定限制配置"""
        self._parse_limits_config("user_limits", self.user_limits, "用户")

    def _parse_config_lines(self, config_text, parser_func):
        """通用配置行解析器"""
        if not config_text:
            return
        for line in config_text.strip().split('\n'):
            line = line.strip()
            if line:
                parser_func(line)

    def _log(self, level: str, message: str, *args) -> None:
        """
        统一的日志记录方法
        
        Args:
            level: 日志级别 ('info', 'warning', 'error')
            message: 日志消息模板
            *args: 格式化参数
        """
        log_func = getattr(logger, level, logger.info)
        if args:
            log_func(message.format(*args))
        else:
            log_func(message)

    def _log_warning(self, message, *args):
        """警告日志记录"""
        self._log('warning', message, *args)

    def _log_error(self, message, *args):
        """错误日志记录"""
        self._log('error', message, *args)

    def _log_info(self, message, *args):
        """信息日志记录"""
        self._log('info', message, *args)

    def _handle_error(self, error: Exception, context: str = "", user_message: str = None) -> None:
        """
        统一的错误处理方法
        
        提供统一的错误处理机制，包括：
        - 错误日志记录
        - 错误上下文追踪
        - 详细错误信息记录（包含堆栈跟踪）
        
        参数：
            error: 异常对象
            context: 错误上下文描述
            user_message: 返回给用户的友好错误消息（可选）
        """
        error_context = f"{context}: " if context else ""
        self._log_error("{}发生错误: {}", error_context, str(error))
        
        # 记录详细的错误信息用于调试
        if hasattr(error, '__traceback__'):
            import traceback
            error_details = traceback.format_exc()
            self._log_error("{}详细错误信息:\n{}", error_context, error_details)

    def _safe_execute(self, func, *args, context: str = "", default_return=None, **kwargs):
        """
        安全执行函数，捕获异常并记录
        
        Args:
            func: 要执行的函数
            *args: 函数参数
            context: 执行上下文描述
            default_return: 异常时的默认返回值
            **kwargs: 函数关键字参数
            
        Returns:
            函数执行结果或默认返回值
        """
        try:
            return func(*args, **kwargs)
        except Exception as e:
            self._handle_error(e, context)
            return default_return

    def _validate_redis_connection(self) -> bool:
        """
        验证Redis连接状态
        
        检查Redis连接是否可用，包括连接状态和响应能力。
        
        返回：
            bool: Redis连接是否可用
        """
        if not self.redis:
            self._log_error("Redis连接未初始化")
            return False
        
        try:
            # 发送ping命令验证连接
            response = self.redis.ping()
            if response != True:
                self._log_warning("Redis ping响应异常: {}", response)
                return False
                
            return True
            
        except redis.exceptions.ConnectionError as e:
            self._log_error("Redis连接错误: {}", str(e))
            return False
        except redis.exceptions.TimeoutError as e:
            self._log_error("Redis连接超时: {}", str(e))
            return False
        except Exception as e:
            self._handle_error(e, "Redis连接验证")
            return False
    
    def get_redis_status(self):
        """
        获取Redis连接状态信息
        
        返回：
            dict: Redis连接状态信息字典
        """
        if not self.redis:
            return {
                "connected": False,
                "status": "未初始化",
                "error": "Redis连接未初始化"
            }
        
        try:
            # 检查连接状态
            response = self.redis.ping()
            
            # 获取Redis服务器信息
            info = self.redis.info()
            
            return {
                "connected": True,
                "status": "正常",
                "response_time": "正常",
                "server_version": info.get("redis_version", "未知"),
                "used_memory": info.get("used_memory_human", "未知"),
                "connected_clients": info.get("connected_clients", 0)
            }
            
        except Exception as e:
            return {
                "connected": False,
                "status": "异常",
                "error": str(e)
            }
    
    def _reconnect_redis(self):
        """
        重新连接Redis
        
        当Redis连接断开时，尝试重新建立连接。
        
        返回：
            bool: 重连成功返回True，失败返回False
        """
        if not self.redis:
            self._log_error("Redis连接未初始化，无法重连")
            return False
        
        try:
            # 关闭现有连接
            if hasattr(self.redis, 'connection_pool') and self.redis.connection_pool:
                self.redis.connection_pool.disconnect()
            
            # 重新连接
            self.redis.connection_pool.reset()
            
            # 验证新连接
            if self._validate_redis_connection():
                self._log_info("Redis重连成功")
                return True
            else:
                self._log_error("Redis重连失败")
                return False
                
        except Exception as e:
            self._log_error("Redis重连过程中出错: {}", str(e))
            return False

    def _validate_config_structure(self) -> bool:
        """
        验证配置结构完整性
        
        Returns:
            bool: 配置结构是否完整
        """
        required_sections = ["limits", "redis"]
        required_limits_fields = ["default_daily_limit", "exempt_users", "group_limits", "user_limits"]
        
        try:
            # 检查必需配置段
            for section in required_sections:
                if section not in self.config:
                    self._log_error("配置缺少必需段: {}", section)
                    return False
            
            # 检查limits段必需字段
            for field in required_limits_fields:
                if field not in self.config["limits"]:
                    self._log_error("limits配置缺少必需字段: {}", field)
                    return False
            
            return True
        except Exception as e:
            self._handle_error(e, "配置结构验证")
            return False

    def _safe_parse_int(self, value_str, default=None):
        """安全解析整数，避免重复的异常处理"""
        try:
            return int(value_str)
        except (ValueError, TypeError):
            return default

    def _validate_config_line(self, line, required_separator=':', min_parts=2):
        """验证配置行格式"""
        line = line.strip()
        if not line or required_separator not in line:
            return None
            
        parts = line.split(required_separator, min_parts - 1)
        if len(parts) < min_parts:
            return None
            
        return parts

    def _parse_limit_line(self, line, limits_dict, limit_type):
        """解析单行限制配置"""
        parts = self._validate_config_line(line)
        if not parts:
            return
            
        entity_id = parts[0].strip()
        limit_str = parts[1].strip()
        
        limit = self._safe_parse_int(limit_str)
        if entity_id and limit and limit > 0:
            limits_dict[entity_id] = limit
        else:
            self._log_warning("{}限制配置格式错误: {}", limit_type, line)

    def _parse_group_modes(self):
        """解析群组模式配置"""
        group_mode_text = self.config["limits"].get("group_mode_settings", "")
        self._parse_config_lines(group_mode_text, self._parse_group_mode_line)

    def _parse_group_mode_line(self, line):
        """解析单行群组模式配置"""
        parts = self._validate_config_line(line)
        if not parts:
            return
            
        group_id = parts[0].strip()
        mode = parts[1].strip()
        
        if group_id and mode in ["shared", "individual"]:
            self.group_modes[group_id] = mode
        else:
            self._log_warning("群组模式配置格式错误: {}", line)

    def _parse_time_period_limits(self):
        """解析时间段限制配置"""
        time_period_text = self.config["limits"].get("time_period_limits", "")
        for line in time_period_text.strip().split('\n'):
            self._parse_time_period_line(line)

    def _parse_time_period_line(self, line):
        """解析单行时间段限制配置"""
        # 解析时间范围部分
        time_range_data = self._parse_time_range_from_line(line)
        if not time_range_data:
            return
            
        # 解析限制次数
        limit_data = self._parse_limit_from_line(line)
        if not limit_data:
            return
            
        # 解析启用标志
        enabled = self._parse_enabled_flag_from_line(line)
        
        # 如果启用，则添加到限制列表
        if enabled:
            self.time_period_limits.append({
                "start_time": time_range_data["start_time"],
                "end_time": time_range_data["end_time"],
                "limit": limit_data
            })
    
    def _parse_time_range_from_line(self, line):
        """从配置行中解析时间范围"""
        parts = self._validate_config_line(line, ':', 2)
        if not parts:
            return None
            
        time_range = parts[0].strip()
        time_parts = self._validate_config_line(time_range, '-', 2)
        if not time_parts:
            return None
            
        start_time = time_parts[0].strip()
        end_time = time_parts[1].strip()
        
        # 验证时间格式
        if not self._validate_time_format(start_time) or not self._validate_time_format(end_time):
            self._log_warning("时间段限制时间格式错误: {}", line)
            return None
            
        return {"start_time": start_time, "end_time": end_time}
    
    def _parse_limit_from_line(self, line):
        """从配置行中解析限制次数"""
        parts = self._validate_config_line(line, ':', 2)
        if not parts:
            return None
            
        limit = self._safe_parse_int(parts[1].strip())
        if limit and limit > 0:
            return limit
        else:
            self._log_warning("时间段限制次数格式错误: {}", line)
            return None
    
    def _parse_enabled_flag_from_line(self, line):
        """从配置行中解析启用标志"""
        line = line.strip()
        parts = line.split(':', 2)
        
        if len(parts) >= 3:
            return self._parse_enabled_flag(parts[2])
        return True

    def _validate_time_format(self, time_str):
        """验证时间格式"""
        try:
            datetime.datetime.strptime(time_str, "%H:%M")
            return True
        except ValueError:
            return False

    def _parse_enabled_flag(self, enabled_str):
        """解析启用标志"""
        if enabled_str is None:
            return True
            
        enabled_str = enabled_str.strip().lower()
        return enabled_str in ['true', '1', 'yes', 'y']

    def _load_skip_patterns(self):
        """加载忽略模式配置"""
        self.skip_patterns = self.config["limits"].get("skip_patterns", ["#", "*"])

    def _load_security_config(self):
        """加载安全配置"""
        try:
            security_config = self.config.get("security", {})
            
            # 加载基础配置
            self._load_basic_security_config(security_config)
            
            # 加载检测阈值配置
            self._load_detection_thresholds(security_config)
            
            # 加载自动限制配置
            self._load_auto_block_config(security_config)
            
            # 加载通知配置
            self._load_notification_config(security_config)
            
            # 初始化通知记录
            self._init_notification_records()
            
            self._log_info("安全配置加载完成，防刷机制{}", "已启用" if self.anti_abuse_enabled else "未启用")
            
        except Exception as e:
            self._log_error("加载安全配置失败: {}", str(e))
            # 使用默认值
            self._set_default_security_config()
    
    def _load_basic_security_config(self, security_config):
        """加载基础安全配置"""
        self.anti_abuse_enabled = security_config.get("anti_abuse_enabled", False)
    
    def _load_detection_thresholds(self, security_config):
        """加载检测阈值配置"""
        self.rapid_request_threshold = security_config.get("rapid_request_threshold", 10)  # 10秒内请求次数
        self.rapid_request_window = security_config.get("rapid_request_window", 10)  # 时间窗口（秒）
        self.consecutive_request_threshold = security_config.get("consecutive_request_threshold", 5)  # 连续请求次数
        self.consecutive_request_window = security_config.get("consecutive_request_window", 30)  # 时间窗口（秒）
    
    def _load_auto_block_config(self, security_config):
        """加载自动限制配置"""
        self.auto_block_duration = security_config.get("auto_block_duration", 300)  # 自动限制时长（秒）
        self.block_notification_template = security_config.get("block_notification_template", 
            "检测到异常使用行为，您已被临时限制使用{auto_block_duration}秒")
    
    def _load_notification_config(self, security_config):
        """加载通知配置"""
        self.admin_notification_enabled = security_config.get("admin_notification_enabled", True)
        admin_users_text = security_config.get("admin_users", "").strip()
        self.admin_users = admin_users_text.split("\n") if admin_users_text else []
        self.notification_cooldown = security_config.get("notification_cooldown", 300)  # 通知冷却时间（秒）
    
    def _init_notification_records(self):
        """初始化通知记录"""
        self.notified_users = {}  # 已通知用户记录 {"user_id": "last_notification_time"}
        self.notified_admins = {}  # 已通知管理员记录 {"user_id": "last_admin_notification_time"}
    
    def _set_default_security_config(self):
        """设置默认安全配置"""
        self.anti_abuse_enabled = False
        self.rapid_request_threshold = 10
        self.rapid_request_window = 10
        self.consecutive_request_threshold = 5
        self.consecutive_request_window = 30
        self.auto_block_duration = 300
        self.block_notification_template = "检测到异常使用行为，您已被临时限制使用{auto_block_duration}秒"
        self.admin_notification_enabled = True
        self.admin_users = []
        self.notification_cooldown = 300
        self.notified_users = {}
        self.notified_admins = {}

    def _detect_abuse_behavior(self, user_id, timestamp):
        """检测异常使用行为
        
        这是异常检测的主入口函数，负责协调整个检测流程。
        
        参数:
            user_id: 用户ID（字符串或数字）
            timestamp: 当前时间戳（可选，默认使用当前时间）
            
        返回:
            dict: 检测结果，包含以下字段：
                - is_abuse (bool): 是否检测到异常行为
                - reason (str): 检测结果描述
                - type (str, 可选): 异常类型（如"rapid_request", "consecutive_request"）
                - count (int, 可选): 异常请求次数
                - block_until (float, 可选): 限制结束时间戳
                - original_reason (str, 可选): 原始限制原因
        """
        if not self.anti_abuse_enabled:
            return {"is_abuse": False, "reason": "防刷机制未启用"}
        
        try:
            return self._execute_abuse_detection_pipeline(user_id, timestamp)
        except Exception as e:
            self._log_error("检测异常使用行为失败: {}", str(e))
            return {"is_abuse": False, "reason": "检测失败"}
    
    def _execute_abuse_detection_pipeline(self, user_id, timestamp):
        """执行异常检测流水线
        
        这是异常检测的核心流程，按顺序执行以下步骤：
        1. 清理过期通知记录
        2. 检查用户限制状态
        3. 初始化用户记录
        4. 记录当前请求并清理过期记录
        5. 执行异常检测规则
        6. 更新用户统计信息
        
        参数:
            user_id: 用户ID（字符串）
            timestamp: 当前时间戳（可选）
            
        返回:
            dict: 检测结果，格式与 _detect_abuse_behavior 相同
        """
        user_id = str(user_id)
        current_time = timestamp or time.time()
        
        try:
            # 执行异常检测流程
            return self._run_abuse_detection_flow(user_id, current_time)
            
        except Exception as e:
            self._log_error("异常检测流水线执行失败 - 用户 {}: {}", user_id, str(e))
            # 在检测过程中发生异常时，返回安全结果，避免误判
            return {"is_abuse": False, "reason": "检测过程异常，允许使用"}
    
    def _run_abuse_detection_flow(self, user_id, current_time):
        """执行异常检测流程
        
        参数:
            user_id: 用户ID（字符串）
            current_time: 当前时间戳
            
        返回:
            dict: 检测结果
        """
        # 步骤1: 清理过期通知记录
        self._cleanup_expired_notifications(current_time)
        
        # 步骤2: 检查用户是否已被限制
        block_check_result = self._check_user_block_status(user_id, current_time)
        if block_check_result["is_abuse"]:
            return block_check_result
        
        # 步骤3: 初始化用户记录
        self._init_user_records(user_id)
        
        # 步骤4: 记录用户请求并清理过期记录
        self._record_user_request(user_id, current_time)
        
        # 步骤5: 执行异常检测规则
        abuse_result = self._execute_abuse_detection_rules(user_id, current_time)
        if abuse_result["is_abuse"]:
            return abuse_result
        
        # 步骤6: 更新用户统计信息
        self._update_user_stats(user_id, current_time)
        
        return {"is_abuse": False, "reason": "正常使用"}
    
    def _execute_abuse_detection_rules(self, user_id, current_time):
        """执行异常检测规则
        
        按顺序执行以下检测规则：
        1. 快速请求检测：检查用户在短时间内是否发送过多请求
        2. 连续请求检测：检查用户是否连续发送请求（间隔时间过短）
        
        参数:
            user_id: 用户ID（字符串）
            current_time: 当前时间戳
            
        返回:
            dict: 检测结果，如果任一规则检测到异常则立即返回
        """
        try:
            # 检测快速请求异常
            rapid_request_result = self._detect_rapid_requests(user_id, current_time)
            if rapid_request_result["is_abuse"]:
                return rapid_request_result
            
            # 检测连续请求异常
            consecutive_request_result = self._detect_consecutive_requests(user_id, current_time)
            if consecutive_request_result["is_abuse"]:
                return consecutive_request_result
            
            return {"is_abuse": False, "reason": "所有检测规则通过"}
            
        except Exception as e:
            self._log_error("异常检测规则执行失败 - 用户 {}: {}", user_id, str(e))
            # 在规则检测过程中发生异常时，返回安全结果
            return {"is_abuse": False, "reason": "规则检测异常，允许使用"}
    
    def _cleanup_expired_notifications(self, current_time):
        """清理过期通知记录（保留最近24小时的数据）"""
        try:
            notification_cutoff_time = current_time - 86400  # 24小时
            if hasattr(self, 'notified_users'):
                self.notified_users = {uid: time for uid, time in self.notified_users.items() 
                                     if time > notification_cutoff_time}
            if hasattr(self, 'notified_admins'):
                self.notified_admins = {uid: time for uid, time in self.notified_admins.items() 
                                      if time > notification_cutoff_time}
        except Exception as e:
            self._log_error("清理过期通知记录失败: {}", str(e))
            # 清理失败不影响主要功能，继续执行
    
    def _check_user_block_status(self, user_id, current_time):
        """检查用户是否已被限制"""
        try:
            if hasattr(self, 'blocked_users') and user_id in self.blocked_users:
                block_info = self.blocked_users[user_id]
                if current_time < block_info["block_until"]:
                    return {
                        "is_abuse": True, 
                        "reason": "用户已被限制", 
                        "block_until": block_info["block_until"],
                        "original_reason": block_info["reason"]
                    }
                else:
                    # 限制已过期，移除记录
                    self._cleanup_expired_block(user_id)
            return {"is_abuse": False, "reason": "用户未被限制"}
        except Exception as e:
            self._log_error("检查用户限制状态失败 - 用户 {}: {}", user_id, str(e))
            # 检查失败时返回安全结果
            return {"is_abuse": False, "reason": "限制状态检查异常，允许使用"}
    
    def _cleanup_expired_block(self, user_id):
        """清理过期的用户限制记录"""
        del self.blocked_users[user_id]
        if user_id in self.abuse_records:
            del self.abuse_records[user_id]
        if user_id in self.abuse_stats:
            del self.abuse_stats[user_id]
    
    def _init_user_records(self, user_id):
        """初始化用户记录"""
        try:
            if not hasattr(self, 'abuse_records'):
                self.abuse_records = {}
            if not hasattr(self, 'abuse_stats'):
                self.abuse_stats = {}
                
            if user_id not in self.abuse_records:
                self.abuse_records[user_id] = []
            if user_id not in self.abuse_stats:
                self.abuse_stats[user_id] = {
                    "last_request_time": 0,
                    "consecutive_count": 0,
                    "rapid_count": 0
                }
        except Exception as e:
            self._log_error("初始化用户记录失败 - 用户 {}: {}", user_id, str(e))
            # 初始化失败不影响主要功能，继续执行
    
    def _record_user_request(self, user_id, current_time):
        """记录用户请求并清理过期记录"""
        try:
            # 确保记录字典存在
            if not hasattr(self, 'abuse_records') or user_id not in self.abuse_records:
                self._init_user_records(user_id)
            
            # 记录当前请求
            self.abuse_records[user_id].append(current_time)
            
            # 清理过期记录（保留最近1小时的数据）
            cutoff_time = current_time - 3600
            self.abuse_records[user_id] = [t for t in self.abuse_records[user_id] if t > cutoff_time]
            
        except Exception as e:
            self._log_error("记录用户请求失败 - 用户 {}: {}", user_id, str(e))
            # 记录失败不影响主要功能，继续执行
    
    def _detect_rapid_requests(self, user_id, current_time):
        """检测快速请求异常"""
        recent_requests = [t for t in self.abuse_records[user_id] 
                          if t > current_time - self.rapid_request_window]
        
        if len(recent_requests) >= self.rapid_request_threshold:
            return {
                "is_abuse": True,
                "reason": f"快速请求异常：{len(recent_requests)}次/{self.rapid_request_window}秒",
                "type": "rapid_request",
                "count": len(recent_requests)
            }
        return {"is_abuse": False, "reason": "快速请求正常"}
    
    def _detect_consecutive_requests(self, user_id, current_time):
        """检测连续请求异常"""
        stats = self.abuse_stats[user_id]
        time_since_last = current_time - stats["last_request_time"] if stats["last_request_time"] > 0 else float('inf')
        
        if time_since_last <= self.consecutive_request_window:
            stats["consecutive_count"] += 1
            if stats["consecutive_count"] >= self.consecutive_request_threshold:
                return {
                    "is_abuse": True,
                    "reason": f"连续请求异常：{stats['consecutive_count']}次连续请求",
                    "type": "consecutive_request",
                    "count": stats["consecutive_count"]
                }
        else:
            stats["consecutive_count"] = 1
        
        return {"is_abuse": False, "reason": "连续请求正常"}
    
    def _update_user_stats(self, user_id, current_time):
        """更新用户统计信息"""
        self.abuse_stats[user_id]["last_request_time"] = current_time

    async def _block_user_for_abuse(self, user_id, reason, duration=None):
        """限制用户使用
        
        参数:
            user_id: 用户ID
            reason: 限制原因
            duration: 限制时长（秒），默认使用配置值
            
        返回:
            dict: 包含限制信息的字典
        """
        try:
            user_id = str(user_id)
            block_duration = duration or self.auto_block_duration
            block_until = time.time() + block_duration
            
            block_info = {
                "block_until": block_until,
                "reason": reason,
                "blocked_at": time.time(),
                "duration": block_duration
            }
            
            self.blocked_users[user_id] = block_info
            
            self._log_warning("用户 {} 因 {} 被限制使用 {} 秒", user_id, reason, block_duration)
            
            # 注意：管理员通知现在由 _handle_abuse_detected 方法统一处理
            # 避免重复发送通知
            
            return block_info
                
        except Exception as e:
            self._log_error("限制用户失败: {}", str(e))
            # 返回一个默认的block_info，避免后续代码出错
            return {
                "block_until": time.time() + 300,  # 默认5分钟
                "reason": reason,
                "blocked_at": time.time(),
                "duration": 300
            }

    async def _notify_admins_about_block(self, user_id, reason, duration):
        """通知管理员关于用户限制"""
        try:
            from astrbot.api.event import MessageChain
            
            message = f"🛡️ 防刷机制通知\n" \
                     f"══════════\n" \
                     f"• 用户ID：{user_id}\n" \
                     f"• 限制原因：{reason}\n" \
                     f"• 限制时长：{duration}秒\n" \
                     f"• 限制时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}\n" \
                     f"\n💡 如需解除限制，请使用命令：\n" \
                     f"/limit security unblock {user_id}"
            
            # 为每个管理员用户发送通知
            for admin_user in self.admin_users:
                try:
                    # 构建管理员用户的会话标识
                    # 格式：platform_name:message_type:session_id
                    # 根据实际UMO格式：QQ:FriendMessage:123456789
                    admin_umo = f"QQ:FriendMessage:{admin_user}"  # QQ平台私聊格式
                    
                    # 创建消息链
                    message_chain = MessageChain().message(message)
                    
                    # 发送主动消息
                    await self.context.send_message(admin_umo, message_chain)
                    self._log_info("已向管理员 {} 发送限制通知", admin_user)
                    
                except Exception as admin_error:
                    self._log_error("向管理员 {} 发送通知失败: {}", admin_user, str(admin_error))
            
            self._log_info("管理员通知发送完成")
            
        except Exception as e:
            self._log_error("发送管理员通知失败: {}", str(e))

    def _validate_daily_reset_time(self):
        """验证每日重置时间配置"""
        reset_time_str = self.config["limits"].get("daily_reset_time", "00:00")
        
        # 验证重置时间格式
        try:
            reset_hour, reset_minute = map(int, reset_time_str.split(':'))
            if not (0 <= reset_hour <= 23 and 0 <= reset_minute <= 59):
                raise ValueError("重置时间格式错误")
            self._log_info("重置时间配置验证通过: {}", reset_time_str)
        except (ValueError, AttributeError) as e:
            # 如果配置格式错误，记录警告并使用默认值
            self._log_warning("重置时间配置格式错误: {}，错误: {}，使用默认值00:00", reset_time_str, e)
            # 自动修复为默认值
            self.config["limits"]["daily_reset_time"] = "00:00"
            try:
                self.config.save_config()
                self._log_info("已自动修复重置时间配置为默认值00:00")
            except Exception as save_error:
                self._log_error("保存重置时间配置失败: {}", save_error)

    def _save_group_limit(self, group_id, limit):
        """保存群组特定限制到配置文件（新格式：群组ID:限制次数）"""
        group_id = str(group_id)
        
        # 获取当前配置文本
        current_text = self.config["limits"].get("group_limits", "").strip()
        lines = current_text.split('\n') if current_text else []
        
        # 查找并更新现有行，或添加新行
        updated = False
        new_lines = []
        for line in lines:
            line = line.strip()
            if line and ':' in line:
                parts = line.split(':', 1)
                if len(parts) == 2 and parts[0].strip() == group_id:
                    # 更新现有行
                    new_lines.append(f"{group_id}:{limit}")
                    updated = True
                else:
                    # 保留其他行
                    new_lines.append(line)
        
        # 如果没有找到现有行，添加新行
        if not updated:
            new_lines.append(f"{group_id}:{limit}")
        
        # 更新配置并保存
        self.config["limits"]["group_limits"] = '\n'.join(new_lines)
        self.config.save_config()

    def _save_user_limit(self, user_id, limit):
        """保存用户特定限制到配置文件（新格式：用户ID:限制次数）"""
        user_id = str(user_id)
        
        # 获取当前配置文本
        current_text = self.config["limits"].get("user_limits", "").strip()
        lines = current_text.split('\n') if current_text else []
        
        # 查找并更新现有行，或添加新行
        updated = False
        new_lines = []
        for line in lines:
            line = line.strip()
            if line and ':' in line:
                parts = line.split(':', 1)
                if len(parts) == 2 and parts[0].strip() == user_id:
                    # 更新现有行
                    new_lines.append(f"{user_id}:{limit}")
                    updated = True
                else:
                    # 保留其他行
                    new_lines.append(line)
        
        # 如果没有找到现有行，添加新行
        if not updated:
            new_lines.append(f"{user_id}:{limit}")
        
        # 更新配置并保存
        self.config["limits"]["user_limits"] = '\n'.join(new_lines)
        self.config.save_config()

    def _save_group_mode(self, group_id, mode):
        """保存群组模式配置到配置文件（新格式：群组ID:模式）"""
        group_id = str(group_id)
        
        # 获取当前配置文本
        current_text = self.config["limits"].get("group_mode_settings", "").strip()
        lines = current_text.split('\n') if current_text else []
        
        # 查找并更新现有行，或添加新行
        updated = False
        new_lines = []
        for line in lines:
            line = line.strip()
            if line and ':' in line:
                parts = line.split(':', 1)
                if len(parts) == 2 and parts[0].strip() == group_id:
                    # 更新现有行
                    new_lines.append(f"{group_id}:{mode}")
                    updated = True
                else:
                    # 保留其他行
                    new_lines.append(line)
        
        # 如果没有找到现有行，添加新行
        if not updated:
            new_lines.append(f"{group_id}:{mode}")
        
        # 更新配置并保存
        self.config["limits"]["group_mode_settings"] = '\n'.join(new_lines)
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
            self._log_info("Redis连接成功")
        except Exception as e:
            self._log_error("Redis连接失败: {}", str(e))
            self.redis = None

    def _init_web_server(self):
        """
        初始化Web服务器
        
        创建并启动Web服务器实例，提供状态管理和错误处理。
        
        返回：
            bool: 初始化成功返回True，失败返回False
        """
        if WebServer is None:
            self._log_warning("Web服务器模块不可用，跳过Web服务器初始化")
            return False

        try:
            # 检查Web服务器是否已经在运行
            if self._is_web_server_running():
                self._log_info("Web服务器已经在运行中")
                return True
            
            # 获取Web服务器配置并创建实例
            self.web_server = self._create_web_server_instance()
            
            # 启动Web服务器
            success = self._start_web_server()
            
            if success:
                self._handle_web_server_start_success()
            else:
                self._handle_web_server_start_failure()
            
            return success
            
        except Exception as e:
            self._handle_web_server_init_error(e)
            return False

    def _create_web_server_instance(self):
        """创建Web服务器实例"""
        web_config = self.config.get("web_server", {})
        host = web_config.get("host", "127.0.0.1")
        port = web_config.get("port", 8080)
        domain = web_config.get("domain", "")
        
        return WebServer(self, host=host, port=port, domain=domain)

    def _start_web_server(self):
        """启动Web服务器"""
        return self.web_server.start_async()

    def _handle_web_server_start_success(self):
        """处理Web服务器启动成功的情况"""
        # 更新线程引用
        self.web_server_thread = self.web_server._server_thread
        
        # 记录访问地址
        self._log_web_server_access_url()
        
        # 记录服务器状态
        self._log_info("Web服务器状态: {}", self.get_web_server_status())

    def _log_web_server_access_url(self):
        """记录Web服务器访问地址"""
        web_config = self.config.get("web_server", {})
        domain = web_config.get("domain", "")
        
        if domain:
            access_url = self.web_server.get_access_url()
            self._log_info("Web管理界面已启动，访问地址: {}", access_url)
        else:
            actual_port = self.web_server.port
            host = web_config.get("host", "127.0.0.1")
            self._log_info("Web管理界面已启动，访问地址: http://{}:{}", host, actual_port)

    def _handle_web_server_start_failure(self):
        """处理Web服务器启动失败的情况"""
        error_msg = "Web服务器启动失败"
        if self.web_server._last_error:
            error_msg += f": {self.web_server._last_error}"
        self._log_error(error_msg)
        self.web_server = None

    def _handle_web_server_init_error(self, error):
        """处理Web服务器初始化错误"""
        error_msg = f"Web服务器初始化失败: {str(error)}"
        self._log_error(error_msg)
        self.web_server = None
    
    def _is_web_server_running(self):
        """
        检查Web服务器是否正在运行
        
        返回：
            bool: Web服务器是否正在运行
        """
        if hasattr(self, 'web_server') and self.web_server:
            return self.web_server.is_running()
        return False
    
    def get_web_server_status(self):
        """
        获取Web服务器状态信息
        
        返回：
            dict: Web服务器状态信息字典，如果未启动则返回None
        """
        if hasattr(self, 'web_server') and self.web_server:
            return self.web_server.get_status()
        return None



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

    def _get_trend_stats_key(self, period_type, period_value):
        """获取趋势统计Redis键
        
        参数：
            period_type: 统计周期类型 ('daily', 'weekly', 'monthly')
            period_value: 周期值 (日期字符串、周数、月份)
        """
        return f"astrbot:trend_stats:{period_type}:{period_value}"

    def _get_week_number(self, date_obj=None):
        """获取日期对应的周数"""
        if date_obj is None:
            date_obj = datetime.datetime.now()
        return date_obj.isocalendar()[1]  # 返回周数

    def _get_month_key(self, date_obj=None):
        """获取月份键（格式：YYYY-MM）"""
        if date_obj is None:
            date_obj = datetime.datetime.now()
        return date_obj.strftime("%Y-%m")

    def _record_trend_data(self, user_id, group_id=None, usage_type="llm_request"):
        """记录趋势分析数据
        
        记录日、周、月三个维度的使用趋势数据
        """
        if not self.redis:
            return False
            
        try:
            current_time = datetime.datetime.now()
            
            # 记录日趋势数据
            daily_key = self._get_trend_stats_key("daily", current_time.strftime("%Y-%m-%d"))
            self._update_trend_stats(daily_key, user_id, group_id, usage_type)
            
            # 记录周趋势数据
            week_number = self._get_week_number(current_time)
            year = current_time.year
            weekly_key = self._get_trend_stats_key("weekly", f"{year}-W{week_number}")
            self._update_trend_stats(weekly_key, user_id, group_id, usage_type)
            
            # 记录月趋势数据
            month_key = self._get_trend_stats_key("monthly", self._get_month_key(current_time))
            self._update_trend_stats(month_key, user_id, group_id, usage_type)
            
            return True
        except Exception as e:
            self._log_error("记录趋势数据失败: {}", str(e))
            return False

    def _update_trend_stats(self, trend_key, user_id, group_id, usage_type):
        """更新趋势统计数据"""
        # 使用Redis哈希存储趋势数据
        pipe = self.redis.pipeline()
        
        # 更新总请求数
        pipe.hincrby(trend_key, "total_requests", 1)
        
        # 更新用户统计
        user_stats_key = f"user:{user_id}"
        pipe.hincrby(trend_key, user_stats_key, 1)
        
        # 更新群组统计（如果有群组）
        if group_id:
            group_stats_key = f"group:{group_id}"
            pipe.hincrby(trend_key, group_stats_key, 1)
        
        # 更新使用类型统计
        usage_stats_key = f"usage_type:{usage_type}"
        pipe.hincrby(trend_key, usage_stats_key, 1)
        
        # 设置过期时间（月数据保留6个月，周数据保留12周，日数据保留30天）
        if "monthly" in trend_key:
            pipe.expire(trend_key, 180 * 24 * 3600)  # 6个月
        elif "weekly" in trend_key:
            pipe.expire(trend_key, 84 * 24 * 3600)   # 12周
        else:  # daily
            pipe.expire(trend_key, 30 * 24 * 3600)   # 30天
        
        pipe.execute()

    def _should_skip_message(self, message_str):
        """检查消息是否应该忽略处理"""
        if not message_str or not self.skip_patterns:
            return False
        
        # 检查消息是否以任何忽略模式开头
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

    def _parse_time_string(self, time_str):
        """解析时间字符串为时间对象"""
        try:
            return datetime.datetime.strptime(time_str, "%H:%M").time()
        except ValueError:
            return None

    def _is_in_time_period(self, current_time_str, start_time_str, end_time_str):
        """检查当前时间是否在指定时间段内"""
        current_time = self._parse_time_string(current_time_str)
        start_time = self._parse_time_string(start_time_str)
        end_time = self._parse_time_string(end_time_str)
        
        if not all([current_time, start_time, end_time]):
            return False
        
        # 处理跨天的时间段（如 22:00 - 06:00）
        if start_time <= end_time:
            # 不跨天的时间段
            return start_time <= current_time <= end_time
        else:
            # 跨天的时间段
            return current_time >= start_time or current_time <= end_time

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
        
        # 设置过期时间到下次重置时间
        seconds_until_tomorrow = self._get_seconds_until_tomorrow()
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

    def _get_usage_by_type(self, user_id=None, group_id=None):
        """通用使用次数获取函数"""
        if not self.redis:
            return 0

        try:
            # 检查时间段限制（优先级最高）
            time_period_limit = self._get_current_time_period_limit()
            if time_period_limit is not None:
                # 有时间段限制时，使用时间段内的使用次数
                return self._get_time_period_usage(user_id, group_id)

            # 没有时间段限制时，使用日使用次数
            if user_id is None:
                key = self._get_group_key(group_id)
            else:
                key = self._get_user_key(user_id, group_id)
            
            usage = self.redis.get(key)
            return int(usage) if usage else 0
        except Exception as e:
            self._log_error("获取使用次数失败 (用户: {}, 群组: {}): {}", user_id, group_id, str(e))
            return 0

    def _get_user_usage(self, user_id, group_id=None):
        """获取用户已使用次数（兼容旧版本）"""
        return self._get_usage_by_type(user_id=user_id, group_id=group_id)

    def _get_group_usage(self, group_id):
        """获取群组共享使用次数"""
        return self._get_usage_by_type(group_id=group_id)

    def _increment_usage_by_type(self, user_id=None, group_id=None):
        """通用使用次数增加函数"""
        if not self.redis:
            return False

        try:
            # 检查时间段限制（优先级最高）
            time_period_limit = self._get_current_time_period_limit()
            if time_period_limit is not None:
                # 有时间段限制时，增加时间段使用次数
                if self._increment_time_period_usage(user_id, group_id):
                    return True

            # 没有时间段限制时，增加日使用次数
            if user_id is None:
                key = self._get_group_key(group_id)
            else:
                key = self._get_user_key(user_id, group_id)
            
            # 增加计数并设置过期时间
            pipe = self.redis.pipeline()
            pipe.incr(key)

            # 设置过期时间到下次重置时间
            seconds_until_tomorrow = self._get_seconds_until_tomorrow()
            pipe.expire(key, seconds_until_tomorrow)

            pipe.execute()
            return True
        except Exception as e:
            self._log_error("增加使用次数失败 (用户: {}, 群组: {}): {}", user_id, group_id, str(e))
            return False

    def _increment_user_usage(self, user_id, group_id=None):
        """增加用户使用次数（兼容旧版本）"""
        return self._increment_usage_by_type(user_id=user_id, group_id=group_id)

    def _increment_group_usage(self, group_id):
        """增加群组共享使用次数"""
        return self._increment_usage_by_type(group_id=group_id)

    def _record_usage(self, user_id, group_id=None, usage_type="llm_request"):
        """
        记录使用情况
        
        记录用户或群组的使用情况到Redis中，包括：
        - 使用记录（按日期和时间）
        - 使用统计更新
        - 趋势数据分析
        - 过期时间设置
        
        参数：
            user_id: 用户ID
            group_id: 群组ID（可选）
            usage_type: 使用类型，默认为"llm_request"
            
        返回：
            bool: 记录成功返回True，失败返回False
        """
        if not self.redis:
            return False
            
        try:
            # 记录详细使用信息
            self._record_usage_details(user_id, group_id, usage_type)
            
            # 更新统计信息
            self._update_usage_stats(user_id, group_id)
            
            # 记录趋势分析数据
            self._record_trend_data(user_id, group_id, usage_type)
            
            return True
        except Exception as e:
            self._log_error("记录使用记录失败 (用户: {}, 群组: {}): {}", user_id, group_id, str(e))
            return False

    def _record_usage_details(self, user_id, group_id, usage_type):
        """记录详细使用信息"""
        timestamp = datetime.datetime.now().isoformat()
        record_key = self._get_usage_record_key(user_id, group_id)
        
        # 创建使用记录数据
        record_data = self._create_usage_record_data(user_id, group_id, usage_type, timestamp)
        
        # 使用Redis列表存储使用记录
        self.redis.rpush(record_key, json.dumps(record_data))
        
        # 设置过期时间到下次重置时间
        self._set_usage_record_expiry(record_key)

    def _create_usage_record_data(self, user_id, group_id, usage_type, timestamp):
        """创建使用记录数据"""
        return {
            "timestamp": timestamp,
            "user_id": user_id,
            "group_id": group_id,
            "usage_type": usage_type,
            "date": datetime.datetime.now().strftime("%Y-%m-%d")
        }

    def _set_usage_record_expiry(self, record_key):
        """设置使用记录过期时间"""
        seconds_until_tomorrow = self._get_seconds_until_tomorrow()
        self.redis.expire(record_key, seconds_until_tomorrow)

    def _update_usage_stats(self, user_id, group_id=None):
        """
        更新使用统计信息
        
        更新用户和群组的使用统计信息，包括：
        - 活跃用户统计
        - 活跃群组统计  
        - 总请求数统计
        
        参数：
            user_id: 用户ID
            group_id: 群组ID（可选）
            
        返回：
            bool: 更新成功返回True，失败返回False
        """
        if not self.redis:
            return False
            
        try:
            date_str = datetime.datetime.now().strftime("%Y-%m-%d")
            stats_key = self._get_usage_stats_key(date_str)
            
            # 收集需要更新的统计键
            keys_to_update = self._collect_stats_keys(stats_key, user_id, group_id)
            
            # 更新所有统计
            self._update_all_stats(keys_to_update)
            
            # 设置过期时间
            self._set_expiry_for_stats_keys(keys_to_update)
            
            return True
        except Exception as e:
            self._log_error("更新使用统计失败 (用户: {}, 群组: {}): {}", user_id, group_id, str(e))
            return False

    def _collect_stats_keys(self, stats_key, user_id, group_id):
        """收集需要更新的统计键"""
        keys_to_update = {
            "user_stats": f"{stats_key}:user:{user_id}",
            "global_stats": f"{stats_key}:global"
        }
        
        if group_id:
            keys_to_update["group_stats"] = f"{stats_key}:group:{group_id}"
            keys_to_update["group_user_stats"] = f"{stats_key}:group:{group_id}:user:{user_id}"
        
        return keys_to_update

    def _update_all_stats(self, keys_to_update):
        """更新所有统计信息"""
        # 更新用户统计
        self.redis.hincrby(keys_to_update["user_stats"], "total_usage", 1)
        
        # 更新全局统计
        self.redis.hincrby(keys_to_update["global_stats"], "total_requests", 1)

    def _get_daily_trend_data(self, days: int, current_time: datetime.datetime) -> dict:
        """获取日趋势数据
        
        参数：
            days: 查询天数
            current_time: 当前时间
            
        返回：
            dict: 日趋势数据
        """
        trend_data = {}
        for i in range(days):
            date_obj = current_time - datetime.timedelta(days=i)
            date_key = date_obj.strftime("%Y-%m-%d")
            trend_key = self._get_trend_stats_key("daily", date_key)
            
            data = self._get_trend_stats_by_key(trend_key)
            if data:
                trend_data[date_key] = data
        return trend_data

    def _get_weekly_trend_data(self, weeks: int, current_time: datetime.datetime) -> dict:
        """获取周趋势数据
        
        参数：
            weeks: 查询周数
            current_time: 当前时间
            
        返回：
            dict: 周趋势数据
        """
        trend_data = {}
        for i in range(weeks):
            date_obj = current_time - datetime.timedelta(weeks=i)
            week_number = self._get_week_number(date_obj)
            year = date_obj.year
            week_key = f"{year}-W{week_number}"
            trend_key = self._get_trend_stats_key("weekly", week_key)
            
            data = self._get_trend_stats_by_key(trend_key)
            if data:
                trend_data[week_key] = data
        return trend_data

    def _get_monthly_trend_data(self, months: int, current_time: datetime.datetime) -> dict:
        """获取月趋势数据
        
        参数：
            months: 查询月数
            current_time: 当前时间
            
        返回：
            dict: 月趋势数据
        """
        trend_data = {}
        for i in range(months):
            date_obj = current_time - datetime.timedelta(days=30*i)
            month_key = self._get_month_key(date_obj)
            trend_key = self._get_trend_stats_key("monthly", month_key)
            
            data = self._get_trend_stats_by_key(trend_key)
            if data:
                trend_data[month_key] = data
        return trend_data

    def _get_trend_data(self, period_type, days=7):
        """获取趋势数据
        
        参数：
            period_type: 统计周期类型 ('daily', 'weekly', 'monthly')
            days: 查询天数（仅对daily类型有效）
        """
        if not self.redis:
            return {}
            
        try:
            current_time = datetime.datetime.now()
            
            if period_type == "daily":
                return self._get_daily_trend_data(days, current_time)
            elif period_type == "weekly":
                return self._get_weekly_trend_data(4, current_time)
            elif period_type == "monthly":
                return self._get_monthly_trend_data(6, current_time)
            else:
                return {}
                
        except Exception as e:
            self._log_error("获取趋势数据失败: {}", str(e))
            return {}

    def _get_trend_stats_by_key(self, trend_key):
        """根据趋势键获取统计数据"""
        try:
            data = self.redis.hgetall(trend_key)
            if not data:
                return None
                
            # 解析统计数据
            stats = {
                "total_requests": int(data.get("total_requests", 0)),
                "active_users": 0,
                "active_groups": 0,
                "usage_types": {}
            }
            
            # 统计活跃用户和群组
            user_set = set()
            group_set = set()
            
            for key, value in data.items():
                if key.startswith("user:"):
                    user_id = key.split(":")[1]
                    user_set.add(user_id)
                    stats["active_users"] = len(user_set)
                elif key.startswith("group:"):
                    group_id = key.split(":")[1]
                    group_set.add(group_id)
                    stats["active_groups"] = len(group_set)
                elif key.startswith("usage_type:"):
                    usage_type = key.split(":")[1]
                    stats["usage_types"][usage_type] = int(value)
                    
            return stats
            
        except Exception as e:
            self._log_error("解析趋势统计数据失败: {}", str(e))
            return None

    def _extract_trend_metrics(self, trend_data):
        """从趋势数据中提取关键指标
        
        参数：
            trend_data: 趋势数据字典
            
        返回：
            tuple: (total_requests, active_users, active_groups, dates)
        """
        total_requests = []
        active_users = []
        active_groups = []
        dates = list(trend_data.keys())
        
        for date in dates:
            data = trend_data[date]
            total_requests.append(data.get("total_requests", 0))
            active_users.append(data.get("active_users", 0))
            active_groups.append(data.get("active_groups", 0))
        
        return total_requests, active_users, active_groups, dates

    def _calculate_growth_rate(self, total_requests):
        """计算增长率
        
        参数：
            total_requests: 总请求数列表
            
        返回：
            float: 增长率百分比
        """
        if len(total_requests) > 1:
            current_total = total_requests[-1]
            previous_total = total_requests[-2]
            if previous_total > 0:
                return ((current_total - previous_total) / previous_total) * 100
            else:
                return 100 if current_total > 0 else 0
        else:
            return 0

    def _generate_summary_section(self, total_requests, active_users, active_groups, growth_rate):
        """生成趋势报告摘要部分
        
        参数：
            total_requests: 总请求数列表
            active_users: 活跃用户数列表
            active_groups: 活跃群组数列表
            growth_rate: 增长率
            
        返回：
            str: 摘要部分文本
        """
        summary = "📈 使用趋势分析报告\n"
        summary += "═══════════════\n\n"
        
        summary += f"📊 总请求数趋势: {total_requests[-1]} 次\n"
        summary += f"📈 增长率: {growth_rate:+.1f}%\n"
        summary += f"👤 活跃用户数: {active_users[-1]} 人\n"
        summary += f"👥 活跃群组数: {active_groups[-1]} 个\n\n"
        
        return summary

    def _generate_detailed_section(self, trend_data, dates):
        """生成详细趋势数据部分
        
        参数：
            trend_data: 趋势数据字典
            dates: 日期列表
            
        返回：
            str: 详细数据部分文本
        """
        detailed = "📅 详细趋势数据:\n"
        for i, date in enumerate(dates):
            data = trend_data[date]
            detailed += f"• {date}: {data.get('total_requests', 0)} 次请求, {data.get('active_users', 0)} 活跃用户\n"
        
        return detailed

    def _analyze_trends(self, trend_data):
        """分析趋势数据，生成趋势报告"""
        if not trend_data:
            return "暂无趋势数据"
            
        try:
            # 提取关键指标
            total_requests, active_users, active_groups, dates = self._extract_trend_metrics(trend_data)
            
            # 计算增长率
            growth_rate = self._calculate_growth_rate(total_requests)
            
            # 生成报告各部分
            summary = self._generate_summary_section(total_requests, active_users, active_groups, growth_rate)
            detailed = self._generate_detailed_section(trend_data, dates)
            
            # 组合完整报告
            trend_report = summary + detailed
            
            return trend_report
            
        except Exception as e:
            self._log_error("分析趋势数据失败: {}", str(e))
            return "趋势分析失败，请稍后重试"
        
        # 更新群组统计（如果有）
        if "group_stats" in keys_to_update:
            self.redis.hincrby(keys_to_update["group_stats"], "total_usage", 1)
            self.redis.hincrby(keys_to_update["group_user_stats"], "usage_count", 1)

    def _set_expiry_for_stats_keys(self, keys_to_update):
        """为统计键设置过期时间"""
        # 计算到明天凌晨的秒数
        seconds_until_tomorrow = self._get_seconds_until_tomorrow()
        
        # 为所有存在的键设置过期时间
        for key in keys_to_update.values():
            if self.redis.exists(key):
                self.redis.expire(key, seconds_until_tomorrow)

    def _get_seconds_until_tomorrow(self):
        """获取到下次重置时间的秒数"""
        # 获取配置的重置时间
        reset_time_str = self.config["limits"].get("daily_reset_time", "00:00")
        
        # 解析重置时间
        try:
            reset_hour, reset_minute = map(int, reset_time_str.split(':'))
            if not (0 <= reset_hour <= 23 and 0 <= reset_minute <= 59):
                raise ValueError("重置时间格式错误")
        except (ValueError, AttributeError):
            # 如果配置格式错误，使用默认的00:00
            reset_hour, reset_minute = 0, 0
            self._log_warning("重置时间配置格式错误: {}，使用默认值00:00", reset_time_str)
        
        now = datetime.datetime.now()
        
        # 计算今天的重置时间
        reset_today = now.replace(hour=reset_hour, minute=reset_minute, second=0, microsecond=0)
        
        # 如果当前时间已经过了今天的重置时间，则计算明天的重置时间
        if now >= reset_today:
            reset_time = reset_today + datetime.timedelta(days=1)
        else:
            reset_time = reset_today
        
        return int((reset_time - now).total_seconds())

    def _should_process_request(self, event: AstrMessageEvent, req: ProviderRequest) -> bool:
        """检查是否应该处理请求"""
        if not self._validate_redis_connection():
            event.stop_event()
            return False
        
        if not req.prompt.strip() or self._should_skip_message(event.message_str):
            event.stop_event()
            return False
            
        return True

    def _is_exempt_user(self, user_id: int) -> bool:
        """检查用户是否为豁免用户"""
        return str(user_id) in self.config["limits"]["exempt_users"]

    def _get_usage_info(self, user_id: int, group_id: Optional[int]) -> tuple:
        """
        获取使用信息（使用次数和限制）
        
        根据用户ID和群组ID获取当前的使用情况信息，包括：
        - 当前使用次数
        - 限制次数
        - 使用类型（个人/群组共享/个人独立）
        
        参数：
            user_id: 用户ID
            group_id: 群组ID（可选）
            
        返回：
            tuple: (使用次数, 限制次数, 使用类型描述)
        """
        limit = self._get_user_limit(user_id, group_id)
        
        if group_id is not None:
            group_mode = self._get_group_mode(group_id)
            if group_mode == "shared":
                usage = self._get_group_usage(group_id)
                usage_type = "群组共享"
            else:
                usage = self._get_user_usage(user_id, group_id)
                usage_type = "个人独立"
        else:
            usage = self._get_user_usage(user_id, group_id)
            usage_type = "个人"
            
        return usage, limit, usage_type

    async def _handle_abuse_detected(self, event: AstrMessageEvent, user_id: int, abuse_result: dict):
        """处理检测到的异常使用行为"""
        try:
            user_id_str = str(user_id)
            current_time = time.time()
            
            # 检查是否在冷却时间内（避免重复通知）
            if user_id_str in self.notified_users:
                last_notification_time = self.notified_users[user_id_str]
                if current_time - last_notification_time < self.notification_cooldown:
                    self._log_info("用户 {} 在冷却时间内，跳过重复通知", user_id_str)
                    event.stop_event()
                    return
            
            # 自动限制用户
            block_info = await self._block_user_for_abuse(user_id_str, abuse_result)
            
            # 发送限制通知（如果不在冷却时间内）
            user_name = event.get_sender_name()
            block_message = self._format_block_notification(user_name, abuse_result, block_info)
            
            if event.get_message_type() == MessageType.GROUP_MESSAGE:
                await event.send(
                    MessageChain().at(user_name, user_id).message(block_message)
                )
            else:
                await event.send(MessageChain().message(block_message))
            
            # 记录通知时间
            self.notified_users[user_id_str] = current_time
            
            # 记录异常检测日志
            self._log_warning("检测到用户 {} 异常使用行为：{}，已自动限制 {} 秒", 
                            user_id_str, abuse_result["reason"], block_info["duration"])
            
            # 通知管理员（如果启用且不在冷却时间内）
            if self.admin_notification_enabled:
                if user_id_str in self.notified_admins:
                    last_admin_notification_time = self.notified_admins[user_id_str]
                    if current_time - last_admin_notification_time < self.notification_cooldown:
                        self._log_info("管理员通知在冷却时间内，跳过重复通知")
                    else:
                        await self._notify_admins_about_block(user_id_str, abuse_result["reason"], block_info["duration"])
                        self.notified_admins[user_id_str] = current_time
                else:
                    await self._notify_admins_about_block(user_id_str, abuse_result["reason"], block_info["duration"])
                    self.notified_admins[user_id_str] = current_time
            
            event.stop_event()
            
        except Exception as e:
            self._log_error("处理异常使用行为失败: {}", str(e))
            # 即使处理失败，也要阻止请求继续
            event.stop_event()

    def _format_block_notification(self, user_name: str, abuse_result: dict, block_info: dict) -> str:
        """格式化限制通知消息"""
        template = self.block_notification_template
        
        # 替换模板变量
        message = template.replace("{user_name}", user_name)
        message = message.replace("{reason}", abuse_result["reason"])
        message = message.replace("{auto_block_duration}", str(block_info["duration"]))
        message = message.replace("{duration}", str(block_info["duration"]))  # 兼容两种占位符
        
        # 计算剩余时间
        remaining_time = max(0, block_info["block_until"] - time.time())
        minutes = int(remaining_time // 60)
        seconds = int(remaining_time % 60)
        message = message.replace("{remaining_time}", f"{minutes}分{seconds}秒")
        
        return message

    async def _handle_limit_exceeded(self, event: AstrMessageEvent, user_id: int, 
                                   group_id: Optional[int], usage: int, limit: int):
        """处理超过限制的情况"""
        self._log_info("用户 {} 在群 {} 中已达到调用限制 {}", user_id, group_id, limit)
        
        if group_id is not None:
            user_name = event.get_sender_name()
            group_name = event.get_group_name() or "群组"
            group_mode = self._get_group_mode(group_id)
            
            custom_message = self._get_custom_zero_usage_message(
                usage, limit, user_name, group_name, group_mode
            )
            
            await event.send(
                MessageChain().at(user_name, user_id).message(custom_message)
            )
        else:
            user_name = event.get_sender_name()
            custom_message = self._get_custom_zero_usage_message(
                usage, limit, user_name, None, None
            )
            await event.send(MessageChain().message(custom_message))
            
        event.stop_event()

    async def _send_reminder(self, event: AstrMessageEvent, user_id: int, 
                           group_id: Optional[int], remaining: int):
        """发送剩余次数提醒"""
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
            await event.send(MessageChain().message(reminder_msg))

    def _increment_usage(self, user_id: int, group_id: Optional[int]):
        """
        增加使用次数
        
        根据群组模式智能增加使用次数：
        - 共享模式：增加群组使用次数
        - 独立模式：增加用户在该群组的使用次数
        - 私聊：增加用户个人使用次数
        
        参数：
            user_id: 用户ID
            group_id: 群组ID（可选，为None时表示私聊）
        """
        if group_id is not None:
            group_mode = self._get_group_mode(group_id)
            if group_mode == "shared":
                self._increment_group_usage(group_id)
            else:
                self._increment_user_usage(user_id, group_id)
        else:
            self._increment_user_usage(user_id, group_id)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        处理LLM请求事件
        
        这是插件的核心事件处理函数，负责：
        - 验证请求是否应该处理
        - 检查用户权限和限制
        - 检测异常使用行为（防刷机制）
        - 记录使用情况
        - 处理超过限制的情况
        - 发送提醒消息
        
        参数：
            event: AstrMessageEvent对象，包含消息事件信息
            req: ProviderRequest对象，包含LLM请求信息
            
        返回：
            bool: 是否允许继续处理请求
        """
        # 基础检查
        if not self._should_process_request(event, req):
            return False

        user_id = event.get_sender_id()
        
        # 豁免用户检查
        if self._is_exempt_user(user_id):
            return True

        # 防刷机制检测（如果启用）
        if self.anti_abuse_enabled:
            abuse_result = self._detect_abuse_behavior(user_id, time.time())
            if abuse_result["is_abuse"]:
                # 检测到异常使用行为，自动限制用户
                await self._handle_abuse_detected(event, user_id, abuse_result)
                return False

        # 获取群组信息
        group_id = None
        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            group_id = event.get_group_id()

        # 获取使用信息
        usage, limit, usage_type = self._get_usage_info(user_id, group_id)

        # 检查限制
        if usage >= limit:
            await self._handle_limit_exceeded(event, user_id, group_id, usage, limit)
            return False

        # 发送提醒
        remaining = limit - usage
        if remaining in [1, 3, 5]:
            await self._send_reminder(event, user_id, group_id, remaining)

        # 增加使用次数
        self._increment_usage(user_id, group_id)
        self._record_usage(user_id, group_id, "llm_request")
        
        return True

    def _generate_progress_bar(self, usage, limit, bar_length=10):
        """生成进度条"""
        if limit <= 0:
            return ""
        
        percentage = (usage / limit) * 100
        filled_length = int(bar_length * usage // limit)
        bar = "█" * filled_length + "░" * (bar_length - filled_length)
        
        return f"[{bar}] {percentage:.1f}%"

    def _get_custom_zero_usage_message(self, usage, limit, user_name, group_name, group_mode=None):
        """获取自定义的使用次数为0时的提醒消息"""
        # 获取自定义消息配置
        custom_messages = self.config["limits"].get("custom_messages", {})
        
        # 计算剩余次数
        remaining = limit - usage
        
        # 根据不同的场景选择不同的消息模板
        if group_mode is not None:
            # 群组消息
            if group_mode == "shared":
                # 群组共享模式
                message_template = custom_messages.get("zero_usage_group_shared_message", 
                    "本群组AI访问次数已达上限（{usage}/{limit}），请稍后再试或联系管理员提升限额。")
            else:
                # 群组独立模式
                message_template = custom_messages.get("zero_usage_group_individual_message", 
                    "您在本群组的AI访问次数已达上限（{usage}/{limit}），请稍后再试或联系管理员提升限额。")
        else:
            # 私聊消息
            message_template = custom_messages.get("zero_usage_message", 
                "您的AI访问次数已达上限（{usage}/{limit}），请稍后再试或联系管理员提升限额。")
        
        # 替换模板中的变量
        message = message_template.format(
            usage=usage,
            limit=limit,
            remaining=remaining,
            user_name=user_name or "用户",
            group_name=group_name or "群组"
        )
        
        return message

    def _get_reset_time(self):
        """获取每日重置时间"""
        # 获取配置的重置时间
        reset_time_str = self.config["limits"].get("daily_reset_time", "00:00")
        
        # 验证重置时间格式
        try:
            reset_hour, reset_minute = map(int, reset_time_str.split(':'))
            if not (0 <= reset_hour <= 23 and 0 <= reset_minute <= 59):
                raise ValueError("重置时间格式错误")
            # 返回格式化的重置时间
            return f"{reset_hour:02d}:{reset_minute:02d}"
        except (ValueError, AttributeError):
            # 如果配置格式错误，使用默认的00:00
            self._log_warning("重置时间配置格式错误: {}，使用默认值00:00", reset_time_str)
            return "00:00"

    def _get_custom_message(self, message_type, default_message, **kwargs):
        """获取自定义消息模板
        
        Args:
            message_type: 消息类型
            default_message: 默认消息模板
            **kwargs: 模板变量
        
        Returns:
            str: 格式化后的消息
        """
        # 获取自定义消息配置
        custom_messages = self.config["limits"].get("custom_messages", {})
        
        # 如果配置了自定义消息，则使用自定义消息，否则使用默认消息
        template = custom_messages.get(message_type, default_message)
        
        # 格式化消息模板
        try:
            return template.format(**kwargs)
        except KeyError as e:
            self._log_warning("消息模板变量错误: {}，使用默认消息", e)
            return default_message.format(**kwargs)
        except Exception as e:
            self._log_error("消息模板格式化错误: {}", e)
            return default_message

    def _get_usage_tip(self, remaining, limit):
        """根据剩余次数生成使用提示"""
        if remaining <= 0:
            return "⚠️ 今日次数已用完，请明天再试"
        elif remaining <= limit * 0.2:  # 剩余20%以下
            return "⚠️ 剩余次数较少，请谨慎使用"
        elif remaining <= limit * 0.5:  # 剩余50%以下
            return "💡 剩余次数适中，可继续使用"
        else:
            return "✅ 剩余次数充足，可放心使用"

    def _get_limit_type(self, user_id, group_id):
        """获取限制类型描述"""
        if str(user_id) in self.user_limits:
            return "特定限制"
        elif group_id and str(group_id) in self.group_limits:
            return "群组限制"
        else:
            return "默认限制"

    def _get_current_time_period_info(self, current_time_str):
        """获取当前时间段信息"""
        for period in self.time_period_limits:
            if self._is_in_time_period(current_time_str, period["start_time"], period["end_time"]):
                return period
        return None

    def _build_exempt_user_status(self, user_id, group_id, time_period_limit, current_time_str):
        """构建豁免用户状态消息"""
        group_context = "在本群组" if group_id is not None else ""
        
        status_msg = self._get_custom_message(
            "limit_status_exempt_message",
            "🎉 您{group_context}没有调用次数限制（豁免用户）",
            group_context=group_context
        )
        
        # 添加时间段限制信息（即使豁免用户也显示）
        if time_period_limit is not None:
            current_period_info = self._get_current_time_period_info(current_time_str)
            if current_period_info:
                time_period_msg = self._get_custom_message(
                    "limit_status_time_period_message",
                    "\n\n⏰ 当前处于时间段限制：{start_time}-{end_time}\n📋 时间段限制：{time_period_limit} 次",
                    start_time=current_period_info['start_time'],
                    end_time=current_period_info['end_time'],
                    time_period_limit=time_period_limit
                )
                status_msg += time_period_msg
        
        return status_msg

    def _build_shared_group_status(self, user_id, group_id, limit, reset_time):
        """构建群组共享模式状态消息"""
        usage = self._get_group_usage(group_id)
        remaining = limit - usage
        progress_bar = self._generate_progress_bar(usage, limit)
        usage_tip = self._get_usage_tip(remaining, limit)
        limit_type = "特定限制" if str(group_id) in self.group_limits else "默认限制"
        
        return self._get_custom_message(
            "limit_status_group_shared_message",
            "👥 群组共享模式 - {limit_type}\n📊 今日已使用：{usage}/{limit} 次\n📈 {progress_bar}\n🎯 剩余次数：{remaining} 次\n\n💡 使用提示：{usage_tip}\n🔄 每日重置时间：{reset_time}",
            limit_type=limit_type,
            usage=usage,
            limit=limit,
            progress_bar=progress_bar,
            remaining=remaining,
            usage_tip=usage_tip,
            reset_time=reset_time
        )

    def _build_individual_group_status(self, user_id, group_id, limit, reset_time):
        """构建群组独立模式状态消息"""
        usage = self._get_user_usage(user_id, group_id)
        remaining = limit - usage
        progress_bar = self._generate_progress_bar(usage, limit)
        usage_tip = self._get_usage_tip(remaining, limit)
        limit_type = self._get_limit_type(user_id, group_id)
        
        return self._get_custom_message(
            "limit_status_group_individual_message",
            "👤 个人独立模式 - {limit_type}\n📊 今日已使用：{usage}/{limit} 次\n📈 {progress_bar}\n🎯 剩余次数：{remaining} 次\n\n💡 使用提示：{usage_tip}\n🔄 每日重置时间：{reset_time}",
            limit_type=limit_type,
            usage=usage,
            limit=limit,
            progress_bar=progress_bar,
            remaining=remaining,
            usage_tip=usage_tip,
            reset_time=reset_time
        )

    def _build_private_status(self, user_id, group_id, limit, reset_time):
        """构建私聊状态消息"""
        usage = self._get_user_usage(user_id, group_id)
        remaining = limit - usage
        progress_bar = self._generate_progress_bar(usage, limit)
        usage_tip = self._get_usage_tip(remaining, limit)
        
        return self._get_custom_message(
            "limit_status_private_message",
            "👤 个人使用状态\n📊 今日已使用：{usage}/{limit} 次\n📈 {progress_bar}\n🎯 剩余次数：{remaining} 次\n\n💡 使用提示：{usage_tip}\n🔄 每日重置时间：{reset_time}",
            usage=usage,
            limit=limit,
            progress_bar=progress_bar,
            remaining=remaining,
            usage_tip=usage_tip,
            reset_time=reset_time
        )

    def _add_time_period_info(self, status_msg, user_id, group_id, time_period_limit, current_time_str):
        """添加时间段限制信息到状态消息"""
        if time_period_limit is not None:
            current_period_info = self._get_current_time_period_info(current_time_str)
            if current_period_info:
                time_period_usage = self._get_time_period_usage(user_id, group_id)
                time_period_remaining = time_period_limit - time_period_usage
                time_period_progress = self._generate_progress_bar(time_period_usage, time_period_limit)
                
                time_period_msg = self._get_custom_message(
                    "limit_status_time_period_message",
                    "\n\n⏰ 当前处于时间段限制：{start_time}-{end_time}\n📋 时间段限制：{time_period_limit} 次\n📊 时间段内已使用：{time_period_usage}/{time_period_limit} 次\n📈 {time_period_progress}\n🎯 时间段内剩余：{time_period_remaining} 次",
                    start_time=current_period_info['start_time'],
                    end_time=current_period_info['end_time'],
                    time_period_limit=time_period_limit,
                    time_period_usage=time_period_usage,
                    time_period_progress=time_period_progress,
                    time_period_remaining=time_period_remaining
                )
                status_msg += time_period_msg
        
        return status_msg

    @filter.command("limit_status")
    async def limit_status(self, event: AstrMessageEvent):
        """用户查看当前使用状态"""
        user_id = event.get_sender_id()
        group_id = event.get_group_id() if event.get_message_type() == MessageType.GROUP_MESSAGE else None

        # 检查使用状态
        limit = self._get_user_limit(user_id, group_id)
        time_period_limit = self._get_current_time_period_limit()
        current_time_str = datetime.datetime.now().strftime("%H:%M")
        
        # 首先检查用户是否被豁免（优先级最高）
        if str(user_id) in self.config["limits"]["exempt_users"]:
            status_msg = self._build_exempt_user_status(user_id, group_id, time_period_limit, current_time_str)
        else:
            reset_time = self._get_reset_time()
            
            # 根据群组模式显示正确的状态信息
            if group_id is not None:
                group_mode = self._get_group_mode(group_id)
                if group_mode == "shared":
                    status_msg = self._build_shared_group_status(user_id, group_id, limit, reset_time)
                else:
                    status_msg = self._build_individual_group_status(user_id, group_id, limit, reset_time)
            else:
                status_msg = self._build_private_status(user_id, group_id, limit, reset_time)
            
            # 添加时间段限制信息
            status_msg = self._add_time_period_info(status_msg, user_id, group_id, time_period_limit, current_time_str)

        event.set_result(MessageEventResult().message(status_msg))

    @filter.command("限制帮助")
    async def limit_help_all(self, event: AstrMessageEvent):
        """显示本插件所有指令及其帮助信息"""
        help_msg = (
            "🚀 日调用限制插件 v2.7.4 - 完整指令帮助\n"
            "═════════════════════════\n\n"
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
            "└── /limit skip_patterns - 管理忽略处理的模式配置\n\n"
            "⏰ 时间段限制命令：\n"
            "├── /limit timeperiod list - 列出所有时间段限制配置\n"
            "├── /limit timeperiod add <开始时间> <结束时间> <次数> - 添加时间段限制\n"
            "├── /limit timeperiod remove <索引> - 删除时间段限制\n"
            "├── /limit timeperiod enable <索引> - 启用时间段限制\n"
            "└── /limit timeperiod disable <索引> - 禁用时间段限制\n\n"
            "\n🕐 重置时间管理命令：\n"
            "├── /limit resettime get - 查看当前重置时间\n"
            "├── /limit resettime set <时间> - 设置每日重置时间\n"
            "│   示例：/limit resettime set 06:00 - 设置为早上6点重置\n"
            "└── /limit resettime reset - 重置为默认时间（00:00）\n"
            "🔧 忽略模式管理命令：\n"
            "├── /limit skip_patterns list - 查看当前忽略模式\n"
            "├── /limit skip_patterns add <模式> - 添加忽略模式\n"
            "├── /limit skip_patterns remove <模式> - 移除忽略模式\n"
            "└── /limit skip_patterns reset - 重置为默认模式\n\n"
            "💡 核心功能特性：\n"
            "✅ 智能限制系统：多级权限管理，支持用户、群组、豁免用户三级体系\n"
            "✅ 时间段限制：支持按时间段设置不同的调用限制（优先级最高）\n"
            "✅ 群组协作模式：支持共享模式（群组共享次数）和独立模式（成员独立次数）\n"
            "✅ 数据监控分析：实时监控、使用统计、排行榜和状态监控\n"
            "✅ 使用记录：详细记录每次调用，支持历史查询和统计分析\n"
            "✅ 自定义忽略模式：可配置需要忽略处理的消息前缀\n\n"
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
            "• 默认忽略模式：#、*（可自定义添加）\n\n"
            "📝 版本信息：v2.7.4 | 作者：left666 | 改进：Sakura520222\n"
            "═════════════════════════"
        )

        event.set_result(MessageEventResult().message(help_msg))

    @filter.command_group("limit")
    def limit_command_group(self):
        """限制命令组"""
        pass

    def _get_default_messages(self) -> dict:
        """获取默认消息配置"""
        return {
            "zero_usage_message": "您的AI访问次数已达上限（{usage}/{limit}），请稍后再试或联系管理员提升限额。",
            "zero_usage_group_shared_message": "本群组AI访问次数已达上限（{usage}/{limit}），请稍后再试或联系管理员提升限额。",
            "zero_usage_group_individual_message": "您在本群组的AI访问次数已达上限（{usage}/{limit}），请稍后再试或联系管理员提升限额。",
            "limit_status_private_message": "👤 个人使用状态\n📊 今日已使用：{usage}/{limit} 次\n📈 {progress_bar}\n🎯 剩余次数：{remaining} 次\n\n💡 使用提示：{usage_tip}\n🔄 每日重置时间：{reset_time}",
            "limit_status_group_shared_message": "👥 群组共享模式 - {limit_type}\n📊 今日已使用：{usage}/{limit} 次\n📈 {progress_bar}\n🎯 剩余次数：{remaining} 次\n\n💡 使用提示：{usage_tip}\n🔄 每日重置时间：{reset_time}",
            "limit_status_group_individual_message": "👤 个人独立模式 - {limit_type}\n📊 今日已使用：{usage}/{limit} 次\n📈 {progress_bar}\n🎯 剩余次数：{remaining} 次\n\n💡 使用提示：{usage_tip}\n🔄 每日重置时间：{reset_time}",
            "limit_status_exempt_message": "🎉 您{group_context}没有调用次数限制（豁免用户）",
            "limit_status_time_period_message": "\n\n⏰ 当前处于时间段限制：{start_time}-{end_time}\n📋 时间段限制：{time_period_limit} 次\n📊 时间段内已使用：{time_period_usage}/{time_period_limit} 次\n📈 {time_period_progress}\n🎯 时间段内剩余：{time_period_remaining} 次"
        }

    def _get_valid_message_types(self) -> list:
        """获取有效的消息类型列表"""
        return [
            "zero_usage_message", "zero_usage_group_shared_message", "zero_usage_group_individual_message",
            "limit_status_private_message", "limit_status_group_shared_message", "limit_status_group_individual_message",
            "limit_status_exempt_message", "limit_status_time_period_message"
        ]

    def _validate_message_content(self, msg_type: str, msg_content: str) -> bool:
        """验证消息内容格式"""
        if msg_type.startswith("zero_usage") and ("{usage}" not in msg_content or "{limit}" not in msg_content):
            return False
        return True

    async def _handle_messages_help(self, event: AstrMessageEvent) -> None:
        """处理消息配置帮助命令"""
        custom_messages = self.config["limits"].get("custom_messages", {})
        
        help_msg = "📝 自定义提醒消息配置\n"
        help_msg += "═══════════════════\n\n"
        
        # 显示当前配置
        if custom_messages:
            help_msg += "当前配置：\n"
            for msg_type, msg_content in custom_messages.items():
                help_msg += f"• {msg_type}: {msg_content}\n"
            help_msg += "\n"
        else:
            help_msg += "当前使用默认消息配置\n\n"
        
        help_msg += "使用方式：\n"
        help_msg += "/limit messages list - 查看当前消息配置\n"
        help_msg += "/limit messages set <类型> <消息内容> - 设置自定义消息\n"
        help_msg += "/limit messages reset <类型> - 重置指定类型的消息为默认值\n"
        help_msg += "/limit messages reset_all - 重置所有消息为默认值\n\n"
        
        help_msg += "可用消息类型：\n"
        help_msg += "• zero_usage_message - 私聊使用次数为0时的消息\n"
        help_msg += "• zero_usage_group_shared_message - 群组共享模式使用次数为0时的消息\n"
        help_msg += "• zero_usage_group_individual_message - 群组独立模式使用次数为0时的消息\n"
        help_msg += "• limit_status_private_message - /limit_status 私聊状态消息\n"
        help_msg += "• limit_status_group_shared_message - /limit_status 群组共享模式状态消息\n"
        help_msg += "• limit_status_group_individual_message - /limit_status 群组独立模式状态消息\n"
        help_msg += "• limit_status_exempt_message - /limit_status 豁免用户状态消息\n"
        help_msg += "• limit_status_time_period_message - /limit_status 时间段限制状态消息\n\n"
        
        help_msg += "支持变量：\n"
        help_msg += "• {usage} - 已使用次数\n"
        help_msg += "• {limit} - 限制次数\n"
        help_msg += "• {remaining} - 剩余次数\n"
        help_msg += "• {user_name} - 用户名\n"
        help_msg += "• {group_name} - 群组名\n"
        help_msg += "• {progress_bar} - 进度条\n"
        help_msg += "• {usage_tip} - 使用提示\n"
        help_msg += "• {reset_time} - 重置时间\n"
        help_msg += "• {limit_type} - 限制类型（特定/默认/群组）\n"
        help_msg += "• {group_context} - 群组上下文\n"
        help_msg += "• {start_time} - 时间段开始时间\n"
        help_msg += "• {end_time} - 时间段结束时间\n"
        help_msg += "• {time_period_limit} - 时间段限制次数\n"
        help_msg += "• {time_period_usage} - 时间段内已使用次数\n"
        help_msg += "• {time_period_progress} - 时间段进度条\n"
        help_msg += "• {time_period_remaining} - 时间段内剩余次数"
        
        event.set_result(MessageEventResult().message(help_msg))

    async def _handle_messages_list(self, event: AstrMessageEvent) -> None:
        """处理消息列表命令"""
        custom_messages = self.config["limits"].get("custom_messages", {})
        
        if not custom_messages:
            event.set_result(MessageEventResult().message("当前使用默认消息配置"))
            return
        
        msg_list = "📝 当前自定义消息配置：\n"
        msg_list += "═══════════════════\n\n"
        
        for msg_type, msg_content in custom_messages.items():
            msg_list += f"🔹 {msg_type}:\n"
            msg_list += f"   {msg_content}\n\n"
        
        event.set_result(MessageEventResult().message(msg_list))

    async def _handle_messages_set(self, event: AstrMessageEvent, args: list) -> None:
        """处理消息设置命令"""
        msg_type = args[3]
        msg_content = " ".join(args[4:])
        
        valid_types = self._get_valid_message_types()
        if msg_type not in valid_types:
            event.set_result(MessageEventResult().message(f"无效的消息类型，可用类型：{', '.join(valid_types)}"))
            return
        
        if not self._validate_message_content(msg_type, msg_content):
            event.set_result(MessageEventResult().message("zero_usage消息类型必须包含 {usage} 和 {limit} 变量"))
            return
        
        # 保存自定义消息配置
        if "custom_messages" not in self.config["limits"]:
            self.config["limits"]["custom_messages"] = {}
        
        self.config["limits"]["custom_messages"][msg_type] = msg_content
        self.config.save_config()
        
        event.set_result(MessageEventResult().message(f"✅ 已设置 {msg_type} 的自定义消息\n\n新消息内容：\n{msg_content}"))

    async def _handle_messages_reset(self, event: AstrMessageEvent, args: list) -> None:
        """处理消息重置命令"""
        msg_type = args[3]
        
        valid_types = self._get_valid_message_types()
        if msg_type not in valid_types:
            event.set_result(MessageEventResult().message(f"无效的消息类型，可用类型：{', '.join(valid_types)}"))
            return
        
        default_messages = self._get_default_messages()
        
        # 如果存在自定义配置，则删除该类型
        if "custom_messages" in self.config["limits"] and msg_type in self.config["limits"]["custom_messages"]:
            del self.config["limits"]["custom_messages"][msg_type]
            # 如果自定义配置为空，则删除整个配置节
            if not self.config["limits"]["custom_messages"]:
                del self.config["limits"]["custom_messages"]
            self.config.save_config()
        
        event.set_result(MessageEventResult().message(f"✅ 已重置 {msg_type} 为默认消息\n\n默认消息内容：\n{default_messages[msg_type]}"))

    async def _handle_messages_reset_all(self, event: AstrMessageEvent) -> None:
        """处理重置所有消息命令"""
        if "custom_messages" in self.config["limits"]:
            del self.config["limits"]["custom_messages"]
            self.config.save_config()
        
        event.set_result(MessageEventResult().message("✅ 已重置所有消息为默认值"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("messages")
    async def limit_messages(self, event: AstrMessageEvent):
        """管理自定义提醒消息配置（仅管理员）"""
        args = event.message_str.strip().split()
        
        # 检查命令格式：/limit messages [action] [type] [message]
        if len(args) < 3:
            await self._handle_messages_help(event)
            return
        
        action = args[2]
        
        if action == "list":
            await self._handle_messages_list(event)
        elif action == "set" and len(args) > 4:
            await self._handle_messages_set(event, args)
        elif action == "reset" and len(args) > 3:
            await self._handle_messages_reset(event, args)
        elif action == "reset_all":
            await self._handle_messages_reset_all(event)
        else:
            event.set_result(MessageEventResult().message("无效的命令格式，请使用 /limit messages 查看帮助"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("skip_patterns")
    async def limit_skip_patterns(self, event: AstrMessageEvent):
        """管理忽略模式配置（仅管理员）"""
        args = event.message_str.strip().split()
        
        # 检查命令格式：/limit skip_patterns [action] [pattern]
        if len(args) < 3:
            # 显示当前忽略模式和帮助信息
            patterns_str = ", ".join([f'"{pattern}"' for pattern in self.skip_patterns])
            event.set_result(MessageEventResult().message(
                f"当前忽略模式：{patterns_str}\n"
                f"使用方式：/limit skip_patterns list - 查看当前模式\n"
                f"使用方式：/limit skip_patterns add <模式> - 添加忽略模式\n"
                f"使用方式：/limit skip_patterns remove <模式> - 移除忽略模式\n"
                f"使用方式：/limit skip_patterns reset - 重置为默认模式"
            ))
            return
        
        action = args[2]
        
        if action == "list":
            # 显示当前忽略模式
            patterns_str = ", ".join([f'"{pattern}"' for pattern in self.skip_patterns])
            event.set_result(MessageEventResult().message(f"当前忽略模式：{patterns_str}"))
            
        elif action == "add" and len(args) > 3:
            # 添加忽略模式
            pattern = args[3]
            if pattern in self.skip_patterns:
                event.set_result(MessageEventResult().message(f"忽略模式 '{pattern}' 已存在"))
            else:
                self.skip_patterns.append(pattern)
                # 保存到配置文件
                self.config["limits"]["skip_patterns"] = self.skip_patterns
                self.config.save_config()
                event.set_result(MessageEventResult().message(f"已添加忽略模式：'{pattern}'"))
                
        elif action == "remove" and len(args) > 3:
            # 移除忽略模式
            pattern = args[3]
            if pattern in self.skip_patterns:
                self.skip_patterns.remove(pattern)
                # 保存到配置文件
                self.config["limits"]["skip_patterns"] = self.skip_patterns
                self.config.save_config()
                event.set_result(MessageEventResult().message(f"已移除忽略模式：'{pattern}'"))
            else:
                event.set_result(MessageEventResult().message(f"忽略模式 '{pattern}' 不存在"))
                
        elif action == "reset":
            # 重置为默认模式
            self.skip_patterns = ["@所有人", "#"]
            # 保存到配置文件
            self.config["limits"]["skip_patterns"] = self.skip_patterns
            self.config.save_config()
            event.set_result(MessageEventResult().message("已重置忽略模式为默认值：'@所有人', '#'"))
            
        else:
            event.set_result(MessageEventResult().message("无效的命令格式，请使用 /limit skip_patterns 查看帮助"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("resettime")
    async def limit_resettime(self, event: AstrMessageEvent):
        """管理每日重置时间配置（仅管理员）"""
        args = event.message_str.strip().split()
        
        # 检查命令格式：/limit resettime [action] [time]
        if len(args) < 3:
            # 显示当前重置时间配置和帮助信息
            current_reset_time = self.config["limits"].get("daily_reset_time", "00:00")
            
            help_msg = "🕐 每日重置时间配置管理\n"
            help_msg += "═══════════════════\n\n"
            help_msg += f"当前重置时间：{current_reset_time}\n\n"
            help_msg += "使用方式：\n"
            help_msg += "/limit resettime get - 查看当前重置时间\n"
            help_msg += "/limit resettime set <时间> - 设置每日重置时间\n"
            help_msg += "/limit resettime reset - 重置为默认时间（00:00）\n\n"
            help_msg += "时间格式说明：\n"
            help_msg += "• 格式：HH:MM（24小时制）\n"
            help_msg += "• 示例：/limit resettime set 06:00 - 设置为早上6点重置\n"
            help_msg += "• 示例：/limit resettime set 23:59 - 设置为晚上11点59分重置\n\n"
            help_msg += "💡 功能说明：\n"
            help_msg += "• 每日重置时间决定了使用次数何时清零\n"
            help_msg += "• 默认重置时间为凌晨00:00\n"
            help_msg += "• 设置后，所有用户和群组的使用次数将在指定时间重置\n"
            
            event.set_result(MessageEventResult().message(help_msg))
            return
        
        action = args[2]
        
        if action == "get":
            # 查看当前重置时间
            current_reset_time = self.config["limits"].get("daily_reset_time", "00:00")
            next_reset_time = self._get_reset_time()
            seconds_until_reset = self._get_seconds_until_tomorrow()
            
            # 计算距离下次重置的时间
            hours_until_reset = seconds_until_reset // 3600
            minutes_until_reset = (seconds_until_reset % 3600) // 60
            
            status_msg = f"🕐 当前重置时间配置\n"
            status_msg += f"═══════════════════\n\n"
            status_msg += f"• 当前重置时间：{current_reset_time}\n"
            status_msg += f"• 下次重置时间：{next_reset_time}\n"
            status_msg += f"• 距离下次重置：{hours_until_reset}小时{minutes_until_reset}分钟\n"
            
            event.set_result(MessageEventResult().message(status_msg))
            
        elif action == "set" and len(args) > 3:
            # 设置重置时间
            new_time = args[3]
            
            # 验证时间格式
            try:
                # 使用现有的时间格式验证方法
                if not self._validate_time_format(new_time):
                    event.set_result(MessageEventResult().message(f"❌ 时间格式错误：{new_time}\n请使用 HH:MM 格式（24小时制）\n示例：06:00、23:59"))
                    return
                
                # 保存配置
                self.config["limits"]["daily_reset_time"] = new_time
                self.config.save_config()
                
                # 重新验证配置
                self._validate_daily_reset_time()
                
                event.set_result(MessageEventResult().message(f"✅ 已设置每日重置时间为 {new_time}\n\n下次重置将在 {self._get_reset_time()} 进行"))
                
            except Exception as e:
                self._log_error("设置重置时间失败: {}", str(e))
                event.set_result(MessageEventResult().message(f"❌ 设置重置时间失败：{str(e)}"))
                
        elif action == "reset":
            # 重置为默认时间
            if "daily_reset_time" in self.config["limits"]:
                del self.config["limits"]["daily_reset_time"]
                self.config.save_config()
                
                # 重新验证配置
                self._validate_daily_reset_time()
                
                event.set_result(MessageEventResult().message("✅ 已重置每日重置时间为默认值 00:00"))
            else:
                event.set_result(MessageEventResult().message("✅ 当前已使用默认重置时间 00:00"))
                
        else:
            event.set_result(MessageEventResult().message("❌ 无效的命令格式，请使用 /limit resettime 查看帮助"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("help")
    def _build_basic_management_help(self) -> str:
        """构建基础管理命令帮助信息"""
        return (
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
        )

    def _build_time_period_help(self) -> str:
        """构建时间段限制命令帮助信息"""
        return (
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
        )

    def _build_reset_time_help(self) -> str:
        """构建重置时间管理命令帮助信息"""
        return (
            "\n🕐 重置时间管理命令：\n"
            "├── /limit resettime get - 查看当前重置时间\n"
            "├── /limit resettime set <时间> - 设置每日重置时间\n"
            "│   示例：/limit resettime set 06:00 - 设置为早上6点重置\n"
            "└── /limit resettime reset - 重置为默认时间（00:00）\n"
        )

    def _build_skip_patterns_help(self) -> str:
        """构建忽略模式管理命令帮助信息"""
        return (
            "\n🔧 忽略模式管理命令：\n"
            "├── /limit skip_patterns list - 查看当前忽略模式\n"
            "├── /limit skip_patterns add <模式> - 添加忽略模式\n"
            "│   示例：/limit skip_patterns add ! - 添加!为忽略模式\n"
            "├── /limit skip_patterns remove <模式> - 移除忽略模式\n"
            "│   示例：/limit skip_patterns remove # - 移除#忽略模式\n"
            "└── /limit skip_patterns reset - 重置为默认模式\n"
            "    示例：/limit skip_patterns reset - 重置为默认模式[@所有人, #]\n"
        )

    def _build_query_stats_help(self) -> str:
        """构建查询统计命令帮助信息"""
        return (
            "\n📊 查询统计命令：\n"
            "├── /limit stats - 查看今日使用统计信息\n"
            "├── /limit history [用户ID] [天数] - 查询使用历史记录\n"
            "│   示例：/limit history 123456 7 - 查询用户123456最近7天的使用记录\n"
            "├── /limit trends [周期] - 使用趋势分析（日/周/月）\n"
            "│   示例：/limit trends week - 查看最近4周的使用趋势\n"
            "├── /limit analytics [日期] - 多维度统计分析\n"
            "│   示例：/limit analytics 2025-01-23 - 分析2025年1月23日的使用数据\n"
            "├── /limit top [数量] - 查看使用次数排行榜\n"
            "│   示例：/limit top 10 - 查看今日使用次数前10名\n"
            "├── /limit status - 检查插件状态和健康状态\n"
            "└── /limit domain - 查看Web管理界面域名配置和访问地址\n"
        )

    def _build_reset_commands_help(self) -> str:
        """构建重置命令帮助信息"""
        return (
            "\n🔄 重置命令：\n"
            "├── /limit reset all - 重置所有使用记录（包括个人和群组）\n"
            "├── /limit reset <用户ID> - 重置特定用户的使用次数\n"
            "│   示例：/limit reset 123456 - 重置用户123456的使用次数\n"
            "└── /limit reset group <群组ID> - 重置特定群组的使用次数\n"
            "    示例：/limit reset group 789012 - 重置群组789012的使用次数\n"
        )

    def _build_security_commands_help(self) -> str:
        """构建安全命令帮助信息"""
        return (
            "\n🛡️ 安全命令：\n"
            "├── /limit security status - 查看防刷机制状态和统计信息\n"
            "├── /limit security enable - 启用防刷机制\n"
            "├── /limit security disable - 禁用防刷机制\n"
            "├── /limit security config - 查看当前安全配置\n"
            "├── /limit security blocklist - 查看当前被限制的用户列表\n"
            "├── /limit security unblock <用户ID> - 解除对用户的限制\n"
            "│   示例：/limit security unblock 123456 - 解除用户123456的限制\n"
            "└── /limit security stats <用户ID> - 查看用户的异常行为统计\n"
            "    示例：/limit security stats 123456 - 查看用户123456的异常行为统计\n"
        )

    def _build_version_check_help(self) -> str:
        """构建版本检查命令帮助信息"""
        return (
            "\n🔍 版本检查命令：\n"
            "├── /limit checkupdate - 手动检查版本更新\n"
            "│   示例：/limit checkupdate - 立即检查是否有新版本\n"
            "└── /limit version - 查看当前插件版本信息\n"
            "    示例：/limit version - 显示当前版本和检查状态\n"
        )

    def _build_priority_rules_help(self) -> str:
        """构建优先级规则帮助信息"""
        return (
            "\n🎯 优先级规则（从高到低）：\n"
            "1️⃣ ⏰ 时间段限制 - 优先级最高（特定时间段内的限制）\n"
            "2️⃣ 🏆 豁免用户 - 完全不受限制（白名单用户）\n"
            "3️⃣ 👤 用户特定限制 - 针对单个用户的个性化设置\n"
            "4️⃣ 👥 群组特定限制 - 针对整个群组的统一设置\n"
            "5️⃣ ⚙️ 默认限制 - 全局默认设置（兜底规则）\n"
        )

    def _build_usage_modes_help(self) -> str:
        """构建使用模式说明帮助信息"""
        return (
            "\n📊 使用模式说明：\n"
            "• 🔄 共享模式：群组内所有成员共享使用次数（默认模式）\n"
            "   └── 适合小型团队协作，统一管理使用次数\n"
            "• 👤 独立模式：群组内每个成员有独立的使用次数\n"
            "   └── 适合大型团队，成员间互不影响\n"
        )

    def _build_features_help(self) -> str:
        """构建功能特性帮助信息"""
        return (
            "\n💡 功能特性：\n"
            "✅ 智能限制系统：多级权限管理，支持用户、群组、豁免用户三级体系\n"
            "✅ 时间段限制：支持按时间段设置不同的调用限制（优先级最高）\n"
            "✅ 自定义重置时间：支持设置每日重置时间（默认00:00）\n"
            "✅ 群组协作模式：支持共享模式（群组共享次数）和独立模式（成员独立次数）\n"
            "✅ 数据监控分析：实时监控、使用统计、排行榜和状态监控\n"
            "✅ 使用趋势分析：支持日/周/月多维度使用趋势分析\n"
            "✅ 使用记录：详细记录每次调用，支持历史查询和统计分析\n"
            "✅ 自定义忽略模式：可配置需要忽略处理的消息前缀\n"
            "✅ 智能提醒：剩余次数提醒和使用状态监控\n"
        )

    def _build_usage_tips_help(self) -> str:
        """构建使用提示帮助信息"""
        return (
            "\n📝 使用提示：\n"
            "• 所有命令都需要管理员权限才能使用\n"
            "• 时间段限制优先级最高，会覆盖其他限制规则\n"
            "• 豁免用户不受任何限制规则约束\n"
            "• 默认忽略模式：#、*（可自定义添加）\n"
            "• 重置时间设置后，所有用户和群组的使用次数将在指定时间重置\n"
        )

    def _build_version_info_help(self) -> str:
        """构建版本信息帮助信息"""
        return (
            "\n📝 版本信息：v2.7.4 | 作者：left666 | 改进：Sakura520222\n"
            "═════════════════════════"
        )

    async def limit_help(self, event: AstrMessageEvent):
        """显示详细帮助信息（仅管理员）"""
        help_msg = "🚀 日调用限制插件 v2.7.4 - 管理员详细帮助\n"
        help_msg += "═════════════════════════\n\n"
        
        # 组合所有帮助信息
        help_msg += self._build_basic_management_help()
        help_msg += self._build_time_period_help()
        help_msg += self._build_reset_time_help()
        help_msg += self._build_skip_patterns_help()
        help_msg += self._build_query_stats_help()
        help_msg += self._build_reset_commands_help()
        help_msg += self._build_security_commands_help()
        help_msg += self._build_version_check_help()
        help_msg += self._build_priority_rules_help()
        help_msg += self._build_usage_modes_help()
        help_msg += self._build_features_help()
        help_msg += self._build_usage_tips_help()
        help_msg += self._build_version_info_help()
        
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

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("security")
    async def limit_security(self, event: AstrMessageEvent):
        """防刷机制管理命令（仅管理员）"""
        args = event.message_str.strip().split()
        
        # 检查命令格式：/limit security [action] [user_id]
        if len(args) < 3:
            # 显示安全命令帮助信息
            help_msg = "🛡️ 防刷机制管理命令\n"
            help_msg += "═════════════\n\n"
            help_msg += self._build_security_commands_help()
            event.set_result(MessageEventResult().message(help_msg))
            return
        
        action = args[2]
        
        if action == "status":
            await self._handle_security_status(event)
        elif action == "enable":
            await self._handle_security_enable(event)
        elif action == "disable":
            await self._handle_security_disable(event)
        elif action == "config":
            await self._handle_security_config(event)
        elif action == "blocklist":
            await self._handle_security_blocklist(event)
        elif action == "unblock" and len(args) > 3:
            await self._handle_security_unblock(event, args[3])
        elif action == "stats" and len(args) > 3:
            await self._handle_security_stats(event, args[3])
        else:
            event.set_result(MessageEventResult().message("❌ 无效的安全命令，请使用 /limit security 查看帮助"))

    async def _handle_security_status(self, event: AstrMessageEvent):
        """处理安全状态查询"""
        try:
            status_msg = "🛡️ 防刷机制状态\n"
            status_msg += "══════════\n\n"
            
            # 防刷机制状态
            status_msg += f"• 防刷机制：{'✅ 已启用' if self.anti_abuse_enabled else '❌ 未启用'}\n"
            
            # 统计信息
            blocked_count = len(self.blocked_users)
            monitored_count = len(self.abuse_records)
            
            status_msg += f"• 当前被限制用户：{blocked_count} 个\n"
            status_msg += f"• 监控中用户：{monitored_count} 个\n"
            
            # 异常检测统计
            total_abuse_detections = sum(len(records) for records in self.abuse_records.values())
            status_msg += f"• 累计异常检测：{total_abuse_detections} 次\n"
            
            # 配置信息
            status_msg += f"\n📊 检测阈值配置：\n"
            status_msg += f"• 快速请求：{self.rapid_request_threshold}次/{self.rapid_request_window}秒\n"
            status_msg += f"• 连续请求：{self.consecutive_request_threshold}次/{self.consecutive_request_window}秒\n"
            status_msg += f"• 自动限制时长：{self.auto_block_duration}秒\n"
            
            event.set_result(MessageEventResult().message(status_msg))
            
        except Exception as e:
            self._log_error("查询安全状态失败: {}", str(e))
            event.set_result(MessageEventResult().message("❌ 查询安全状态失败"))

    async def _handle_security_enable(self, event: AstrMessageEvent):
        """启用防刷机制"""
        try:
            if self.anti_abuse_enabled:
                event.set_result(MessageEventResult().message("✅ 防刷机制已经启用"))
                return
            
            # 启用防刷机制
            self.config["security"]["anti_abuse_enabled"] = True
            self.config.save_config()
            self.anti_abuse_enabled = True
            
            event.set_result(MessageEventResult().message("✅ 防刷机制已启用"))
            
        except Exception as e:
            self._log_error("启用防刷机制失败: {}", str(e))
            event.set_result(MessageEventResult().message("❌ 启用防刷机制失败"))

    async def _handle_security_disable(self, event: AstrMessageEvent):
        """禁用防刷机制"""
        try:
            if not self.anti_abuse_enabled:
                event.set_result(MessageEventResult().message("✅ 防刷机制已经禁用"))
                return
            
            # 禁用防刷机制
            self.config["security"]["anti_abuse_enabled"] = False
            self.config.save_config()
            self.anti_abuse_enabled = False
            
            # 清除所有限制记录
            self.blocked_users.clear()
            self.abuse_records.clear()
            self.abuse_stats.clear()
            
            event.set_result(MessageEventResult().message("✅ 防刷机制已禁用，所有限制记录已清除"))
            
        except Exception as e:
            self._log_error("禁用防刷机制失败: {}", str(e))
            event.set_result(MessageEventResult().message("❌ 禁用防刷机制失败"))

    async def _handle_security_config(self, event: AstrMessageEvent):
        """查看安全配置"""
        try:
            config_msg = "⚙️ 当前安全配置\n"
            config_msg += "══════════\n\n"
            
            config_msg += f"• 防刷机制：{'✅ 已启用' if self.anti_abuse_enabled else '❌ 未启用'}\n"
            config_msg += f"• 快速请求阈值：{self.rapid_request_threshold}次/{self.rapid_request_window}秒\n"
            config_msg += f"• 连续请求阈值：{self.consecutive_request_threshold}次/{self.consecutive_request_window}秒\n"
            config_msg += f"• 自动限制时长：{self.auto_block_duration}秒\n"
            config_msg += f"• 管理员通知：{'✅ 已启用' if self.admin_notification_enabled else '❌ 未启用'}\n"
            config_msg += f"• 管理员用户数：{len(self.admin_users)} 个\n"
            
            # 显示通知模板（截取前50字符）
            template_preview = self.block_notification_template[:50]
            if len(self.block_notification_template) > 50:
                template_preview += "..."
            config_msg += f"• 限制通知模板：{template_preview}\n"
            
            config_msg += f"\n💡 配置说明：\n"
            config_msg += f"• 快速请求：检测短时间内的大量请求\n"
            config_msg += f"• 连续请求：检测连续不间断的请求\n"
            config_msg += f"• 自动限制：检测到异常后自动限制用户\n"
            
            event.set_result(MessageEventResult().message(config_msg))
            
        except Exception as e:
            self._log_error("查看安全配置失败: {}", str(e))
            event.set_result(MessageEventResult().message("❌ 查看安全配置失败"))

    async def _handle_security_blocklist(self, event: AstrMessageEvent):
        """查看被限制用户列表"""
        try:
            if not self.blocked_users:
                event.set_result(MessageEventResult().message("📋 当前没有被限制的用户"))
                return
            
            blocklist_msg = "🚫 被限制用户列表\n"
            blocklist_msg += "═══════════\n\n"
            
            current_time = time.time()
            for user_id, block_info in self.blocked_users.items():
                remaining_time = max(0, block_info["block_until"] - current_time)
                minutes = int(remaining_time // 60)
                seconds = int(remaining_time % 60)
                
                blocklist_msg += f"• 用户 {user_id}\n"
                blocklist_msg += f"  原因：{block_info['reason']}\n"
                blocklist_msg += f"  剩余时间：{minutes}分{seconds}秒\n"
                blocklist_msg += f"  限制时长：{block_info['duration']}秒\n\n"
            
            event.set_result(MessageEventResult().message(blocklist_msg))
            
        except Exception as e:
            self._log_error("查看限制列表失败: {}", str(e))
            event.set_result(MessageEventResult().message("❌ 查看限制列表失败"))

    async def _handle_security_unblock(self, event: AstrMessageEvent, user_id: str):
        """解除用户限制"""
        try:
            user_id = str(user_id)
            
            if user_id not in self.blocked_users:
                event.set_result(MessageEventResult().message(f"✅ 用户 {user_id} 没有被限制"))
                return
            
            # 解除限制
            del self.blocked_users[user_id]
            
            # 清除异常记录
            if user_id in self.abuse_records:
                del self.abuse_records[user_id]
            if user_id in self.abuse_stats:
                del self.abuse_stats[user_id]
            
            event.set_result(MessageEventResult().message(f"✅ 已解除用户 {user_id} 的限制"))
            
        except Exception as e:
            self._log_error("解除用户限制失败: {}", str(e))
            event.set_result(MessageEventResult().message("❌ 解除用户限制失败"))

    async def _handle_security_stats(self, event: AstrMessageEvent, user_id: str):
        """查看用户异常行为统计"""
        try:
            user_id = str(user_id)
            
            stats_msg = f"📊 用户 {user_id} 异常行为统计\n"
            stats_msg += "═════════════\n\n"
            
            # 检查是否被限制
            if user_id in self.blocked_users:
                block_info = self.blocked_users[user_id]
                remaining_time = max(0, block_info["block_until"] - time.time())
                minutes = int(remaining_time // 60)
                seconds = int(remaining_time % 60)
                
                stats_msg += f"🚫 当前状态：被限制\n"
                stats_msg += f"• 限制原因：{block_info['reason']}\n"
                stats_msg += f"• 剩余限制时间：{minutes}分{seconds}秒\n"
                stats_msg += f"• 限制开始时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(block_info['blocked_at']))}\n\n"
            else:
                stats_msg += f"✅ 当前状态：正常\n\n"
            
            # 异常记录统计
            if user_id in self.abuse_records:
                records = self.abuse_records[user_id]
                current_time = time.time()
                
                # 最近1小时内的记录
                recent_records = [t for t in records if t > current_time - 3600]
                
                stats_msg += f"📈 最近1小时请求统计：\n"
                stats_msg += f"• 总请求次数：{len(recent_records)} 次\n"
                
                if recent_records:
                    # 计算请求频率
                    time_range = max(recent_records) - min(recent_records) if len(recent_records) > 1 else 1
                    frequency = len(recent_records) / max(time_range, 1)
                    stats_msg += f"• 平均频率：{frequency:.2f} 次/秒\n"
                    
                    # 最近请求时间
                    last_request = max(recent_records)
                    time_since_last = current_time - last_request
                    stats_msg += f"• 最后请求：{int(time_since_last)} 秒前\n"
                
                # 异常检测统计
                if user_id in self.abuse_stats:
                    user_stats = self.abuse_stats[user_id]
                    stats_msg += f"\n⚠️ 异常检测统计：\n"
                    stats_msg += f"• 连续请求计数：{user_stats['consecutive_count']} 次\n"
                    stats_msg += f"• 最后请求时间：{time.strftime('%H:%M:%S', time.localtime(user_stats['last_request_time']))}\n"
                
            else:
                stats_msg += f"📊 该用户暂无异常行为记录\n"
            
            event.set_result(MessageEventResult().message(stats_msg))
            
        except Exception as e:
            self._log_error("查看用户统计失败: {}", str(e))
            event.set_result(MessageEventResult().message("❌ 查看用户统计失败"))
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
            self._log_error("获取统计信息失败: {}", str(e))
            event.set_result(MessageEventResult().message("获取统计信息失败"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("history")
    async def limit_history(self, event: AstrMessageEvent, user_id: str = None, days: int = 7):
        """查询使用历史记录（仅管理员）"""
        if not self._validate_redis_connection():
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
                    private_records = self._safe_execute(
                        lambda: self.redis.lrange(private_key, 0, -1),
                        context=f"查询用户{user_id}在{date_str}的个人记录",
                        default_return=[]
                    )
                    
                    # 查询群组记录
                    group_pattern = f"astrbot:usage_record:{date_str}:*:{user_id}"
                    group_keys = self._safe_execute(
                        lambda: self.redis.keys(group_pattern),
                        context=f"查询用户{user_id}在{date_str}的群组记录键",
                        default_return=[]
                    )
                    
                    daily_total = len(private_records)
                    
                    for key in group_keys:
                        group_records = self._safe_execute(
                            lambda k: self.redis.lrange(k, 0, -1),
                            key,
                            context=f"查询用户{user_id}在群组键{key}的记录",
                            default_return=[]
                        )
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
                    
                    total_requests = self._safe_execute(
                        lambda: self.redis.hget(global_key, "total_requests"),
                        context=f"查询{date_str}全局统计",
                        default_return=None
                    )
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
            self._handle_error(e, "历史记录查询", "查询历史记录时发生错误，请稍后重试")
            event.set_result(MessageEventResult().message("查询历史记录失败，请稍后重试"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("trends")
    async def limit_trends(self, event: AstrMessageEvent, period: str = "day"):
        """使用趋势分析（仅管理员）
        
        Args:
            period: 分析周期，支持 day/week/month
        """
        if not self._validate_redis_connection():
            event.set_result(MessageEventResult().message("Redis未连接，无法获取趋势数据"))
            return

        try:
            # 验证周期参数
            valid_periods = ["day", "week", "month"]
            if period not in valid_periods:
                event.set_result(MessageEventResult().message(
                    f"无效的分析周期，支持：{', '.join(valid_periods)}"
                ))
                return
            
            # 映射周期参数到内部类型
            period_mapping = {
                "day": "daily",
                "week": "weekly", 
                "month": "monthly"
            }
            period_type = period_mapping.get(period, "daily")
            
            # 获取趋势数据
            trend_data = self._get_trend_data(period_type)
            
            if not trend_data:
                event.set_result(MessageEventResult().message(
                    f"暂无{period}周期的趋势数据"
                ))
                return
            
            # 分析趋势数据
            trend_report = self._analyze_trends(trend_data)
            
            # 构建趋势分析消息
            trend_msg = f"📈 {period.capitalize()}使用趋势分析：\n\n"
            trend_msg += trend_report
            
            event.set_result(MessageEventResult().message(trend_msg))
            
        except Exception as e:
            self._handle_error(e, "趋势分析", "获取趋势数据时发生错误，请稍后重试")
            event.set_result(MessageEventResult().message("获取趋势数据失败，请稍后重试"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("trends_api")
    async def limit_trends_api(self, event: AstrMessageEvent, period: str = "week"):
        """获取趋势分析API数据（仅管理员）
        
        Args:
            period: 分析周期，支持 day/week/month
        """
        if not self._validate_redis_connection():
            event.set_result(MessageEventResult().message("Redis未连接，无法获取趋势数据"))
            return

        try:
            # 验证周期参数
            valid_periods = ["day", "week", "month"]
            if period not in valid_periods:
                event.set_result(MessageEventResult().message(
                    f"无效的分析周期，支持：{', '.join(valid_periods)}"
                ))
                return
            
            # 映射周期参数到内部类型
            period_mapping = {
                "day": "daily",
                "week": "weekly", 
                "month": "monthly"
            }
            period_type = period_mapping.get(period, "weekly")
            
            # 获取趋势数据
            trend_data = self._get_trend_data(period_type)
            
            if not trend_data:
                event.set_result(MessageEventResult().message(
                    f"暂无{period}周期的趋势数据"
                ))
                return
            
            # 格式化API响应数据
            api_response = {
                "success": True,
                "period": period,
                "data": trend_data,
                "summary": {
                    "total_periods": len(trend_data),
                    "total_requests": sum([data.get("total_requests", 0) for data in trend_data.values()]),
                    "max_active_users": max([data.get("active_users", 0) for data in trend_data.values()]) if trend_data else 0,
                    "max_active_groups": max([data.get("active_groups", 0) for data in trend_data.values()]) if trend_data else 0
                }
            }
            
            # 返回JSON格式的API数据
            event.set_result(MessageEventResult().message(
                f"📊 {period.capitalize()}趋势分析API数据：\n```json\n{json.dumps(api_response, indent=2, ensure_ascii=False)}\n```"
            ))
            
        except Exception as e:
            self._handle_error(e, "趋势分析API", "获取趋势数据时发生错误，请稍后重试")
            event.set_result(MessageEventResult().message("获取趋势数据失败，请稍后重试"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("analytics")
    async def limit_analytics(self, event: AstrMessageEvent, date_str: str = None):
        """多维度统计分析（仅管理员）"""
        if not self._validate_redis_connection():
            event.set_result(MessageEventResult().message("Redis未连接，无法获取分析数据"))
            return

        try:
            if date_str is None:
                date_str = datetime.datetime.now().strftime("%Y-%m-%d")
            
            stats_key = self._get_usage_stats_key(date_str)
            
            # 获取全局统计
            global_key = f"{stats_key}:global"
            total_requests = self._safe_execute(
                lambda: self.redis.hget(global_key, "total_requests"),
                context=f"获取{date_str}全局统计",
                default_return=None
            )
            
            # 获取用户统计
            user_pattern = f"{stats_key}:user:*"
            user_keys = self._safe_execute(
                lambda: self.redis.keys(user_pattern),
                context=f"获取{date_str}用户统计键",
                default_return=[]
            )
            
            # 获取群组统计
            group_pattern = f"{stats_key}:group:*"
            group_keys = self._safe_execute(
                lambda: self.redis.keys(group_pattern),
                context=f"获取{date_str}群组统计键",
                default_return=[]
            )
            
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
                    usage = self._safe_execute(
                        lambda k: self.redis.hget(k, "total_usage"),
                        key,
                        context=f"获取用户键{key}的使用统计",
                        default_return=None
                    )
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
                    usage = self._safe_execute(
                        lambda k: self.redis.hget(k, "total_usage"),
                        key,
                        context=f"获取群组键{key}的使用统计",
                        default_return=None
                    )
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
                    usage = self._safe_execute(
                        lambda k: self.redis.hget(k, "total_usage"),
                        key,
                        context=f"获取用户键{key}的使用分布",
                        default_return=None
                    )
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
            self._handle_error(e, "统计分析", "获取分析数据时发生错误，请稍后重试")
            event.set_result(MessageEventResult().message("获取分析数据失败，请稍后重试"))

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
            self._log_error("检查插件状态失败: {}", str(e))
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
            domain_msg += "═════════════\n"
            
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
            self._log_error("获取域名配置失败: {}", str(e))
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
            self._log_error("获取排行榜失败: {}", str(e))
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
            self._log_error("重置使用次数失败: {}", str(e))
            event.set_result(MessageEventResult().message("重置使用次数失败，请检查Redis连接"))

    async def terminate(self):
        """
        插件终止时的清理工作
        
        停止Web服务器并清理所有相关资源，确保状态正确清理。
        """
        # 记录终止前的Web服务器状态
        web_server_status = self.get_web_server_status()
        
        # 停止Web服务器
        if self.web_server:
            try:
                self._log_info("正在停止Web服务器...")
                
                # 记录停止前的状态
                previous_status = self.web_server.get_status()
                
                # 停止Web服务器
                success = self.web_server.stop()
                
                if success:
                    self._log_info("Web服务器已停止")
                    # 记录停止后的状态
                    final_status = self.web_server.get_status()
                    self._log_info("Web服务器终止状态: {}", final_status)
                else:
                    self._log_warning("Web服务器停止失败")
                    
            except Exception as e:
                error_msg = f"停止Web服务器失败: {str(e)}"
                self._log_error(error_msg)
        
        # 清理Web服务器线程
        if self.web_server_thread and self.web_server_thread.is_alive():
            try:
                self._log_info("等待Web服务器线程结束...")
                self.web_server_thread.join(timeout=3)  # 最多等待3秒
                if self.web_server_thread.is_alive():
                    self._log_warning("Web服务器线程未在3秒内结束")
                else:
                    self._log_info("Web服务器线程已结束")
            except Exception as e:
                error_msg = f"等待Web服务器线程结束时出错: {str(e)}"
                self._log_error(error_msg)
        
        # 清理Web服务器实例和线程引用
        self.web_server = None
        self.web_server_thread = None
        
        self._log_info("日调用限制插件已终止")
    
    def _terminate_web_server(self):
        """
        专门用于停止Web服务器的方法
        
        返回：
            bool: 停止成功返回True，失败返回False
        """
        if not self._is_web_server_running():
            self._log_info("Web服务器未运行，无需停止")
            return True
            
        try:
            self._log_info("正在停止Web服务器...")
            
            # 记录停止前的状态
            previous_status = self.get_web_server_status()
            
            # 停止Web服务器
            success = self.web_server.stop()
            
            if success:
                self._log_info("Web服务器已停止")
                # 清理引用
                self.web_server = None
                self.web_server_thread = None
                return True
            else:
                self._log_warning("Web服务器停止失败")
                return False
                
        except Exception as e:
            error_msg = f"停止Web服务器失败: {str(e)}"
            self._log_error(error_msg)
            return False

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
        """保存时间段限制配置到配置文件（新格式：开始时间-结束时间:限制次数:是否启用）"""
        try:
            # 构建新的文本格式配置
            lines = []
            for period in self.time_period_limits:
                line = f"{period['start_time']}-{period['end_time']}:{period['limit']}:{str(period['enabled']).lower()}"
                lines.append(line)
            
            # 更新配置对象
            self.config["limits"]["time_period_limits"] = '\n'.join(lines)
            # 保存到配置文件
            self.config.save_config()
            self._log_info("已保存时间段限制配置，共 {} 个时间段", len(self.time_period_limits))
        except Exception as e:
            self._log_error("保存时间段限制配置失败: {}", str(e))

    def _init_version_check(self):
        """初始化版本检查功能"""
        try:
            # 检查是否启用版本检查功能
            if not self.config["version_check"].get("enabled", True):
                self._log_info("版本检查功能已禁用")
                return
            
            # 启动版本检查异步任务
            self.version_check_task = asyncio.create_task(self._version_check_loop())
            self._log_info("版本检查功能已启动，检查间隔：{} 分钟", 
                          self.config["version_check"].get("check_interval", 60))
            
        except Exception as e:
            self._handle_error(e, "初始化版本检查功能")

    async def _version_check_loop(self):
        """版本检查循环任务"""
        while True:
            try:
                # 获取检查间隔（分钟）
                check_interval = self.config["version_check"].get("check_interval", 60)
                
                # 执行版本检查
                await self._check_version_update()
                
                # 等待指定时间后再次检查
                await asyncio.sleep(check_interval * 60)
                
            except Exception as e:
                self._handle_error(e, "版本检查循环任务")
                # 出错后等待5分钟再重试
                await asyncio.sleep(300)

    async def _check_version_update(self):
        """检查版本更新"""
        try:
            # 获取版本检查URL
            check_url = self.config["version_check"].get("check_url", 
                                                         "https://box.firefly520.top/limit_update.txt")
            
            self._log_info("开始检查版本更新: {}", check_url)
            
            # 发送HTTP请求获取版本信息
            async with aiohttp.ClientSession() as session:
                async with session.get(check_url, timeout=30) as response:
                    if response.status != 200:
                        self._log_warning("版本检查请求失败，状态码: {}", response.status)
                        return
                    
                    content = await response.text()
                    
            # 解析版本信息
            version_info = self._parse_version_info(content)
            if not version_info:
                self._log_warning("版本信息解析失败")
                return
            
            self.last_checked_version = version_info["version"]
            self.last_checked_version_info = version_info  # 存储完整的版本信息
            
            # 比较版本号
            current_version = self.config.get("version", "v2.7.3")
            if self._compare_versions(version_info["version"], current_version) > 0:
                # 检测到新版本
                self._log_info("检测到新版本: {} -> {}", current_version, version_info["version"])
                
                # 检查是否需要发送通知
                # 如果配置了重复通知或版本不同，则发送通知
                repeat_notification = self.config["version_check"].get("repeat_notification", False)
                if repeat_notification or self.last_notified_version != version_info["version"]:
                    await self._send_version_notification(current_version, version_info)
                    self.last_notified_version = version_info["version"]
                else:
                    self._log_info("已发送过版本 {} 的通知，跳过重复发送", version_info["version"])
            else:
                self._log_info("当前已是最新版本: {}", current_version)
                
        except asyncio.TimeoutError:
            self._log_warning("版本检查请求超时")
        except Exception as e:
            self._handle_error(e, "检查版本更新")

    def _parse_version_info(self, content: str) -> dict:
        """解析版本信息文件内容"""
        try:
            version_info = {}
            lines = content.strip().split('\n')
            
            for line in lines:
                line = line.strip()
                if line.startswith('v：'):
                    version_info["version"] = line[2:].strip()
                elif line.startswith('c：'):
                    version_info["content"] = line[2:].strip()
            
            # 验证必需字段
            if "version" not in version_info:
                self._log_warning("版本信息文件中缺少版本号")
                return None
                
            return version_info
            
        except Exception as e:
            self._handle_error(e, "解析版本信息")
            return None

    def _compare_versions(self, version1: str, version2: str) -> int:
        """比较两个版本号
        
        Args:
            version1: 第一个版本号
            version2: 第二个版本号
            
        Returns:
            int: 1表示version1 > version2, -1表示version1 < version2, 0表示相等
        """
        try:
            # 移除版本号前缀（如"v"）
            v1 = version1.lstrip('vV')
            v2 = version2.lstrip('vV')
            
            # 分割版本号
            parts1 = v1.split('.')
            parts2 = v2.split('.')
            
            # 比较每个部分
            for i in range(max(len(parts1), len(parts2))):
                p1 = int(parts1[i]) if i < len(parts1) else 0
                p2 = int(parts2[i]) if i < len(parts2) else 0
                
                if p1 > p2:
                    return 1
                elif p1 < p2:
                    return -1
            
            return 0
            
        except Exception as e:
            self._handle_error(e, "比较版本号")
            return 0

    async def _send_version_notification(self, current_version: str, version_info: dict):
        """发送新版本通知给管理员"""
        try:
            # 获取管理员用户列表
            admin_users = self.config["version_check"].get("admin_users", [])
            if not admin_users:
                self._log_warning("未配置管理员用户，无法发送版本更新通知")
                return
            
            # 获取通知消息模板
            template = self.config["version_check"].get("notification_message", 
                                                        "🚀 检测到新版本可用！\n📦 当前版本：{current_version}\n🆕 最新版本：{new_version}\n📝 更新内容：{update_content}\n🔗 下载地址：{download_url}")
            
            # 格式化消息
            message = template.format(
                current_version=current_version,
                new_version=version_info.get("version", "未知"),
                update_content=version_info.get("content", "暂无更新说明"),
                download_url="https://github.com/left666/astrbot_plugin_daily_limit"
            )
            
            # 发送给每个管理员
            for user_id in admin_users:
                try:
                    # 创建消息链
                    message_chain = MessageChain().message(message)
                    
                    # 构建会话唯一标识（格式：平台:消息类型:会话ID）
                    # 对于私聊消息，格式为：QQ:FriendMessage:用户ID
                    unified_msg_origin = f"QQ:FriendMessage:{user_id}"
                    
                    # 发送主动消息给管理员
                    await self.context.send_message(unified_msg_origin, message_chain)
                    self._log_info("已发送新版本通知给管理员: {}", user_id)
                    
                except Exception as e:
                    self._handle_error(e, f"发送版本通知给管理员 {user_id}")
                    
        except Exception as e:
            self._handle_error(e, "发送版本通知")

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("checkupdate")
    async def limit_checkupdate(self, event: AstrMessageEvent):
        """手动检查版本更新（仅管理员）"""
        try:
            # 检查版本检查功能是否启用
            if not self.config["version_check"].get("enabled", True):
                event.set_result(MessageEventResult().message("❌ 版本检查功能已禁用，请在配置中启用"))
                return
            
            # 发送检查开始消息
            event.set_result(MessageEventResult().message("🔍 正在检查版本更新..."))
            
            # 执行版本检查
            await self._check_version_update()
            
            # 检查是否有新版本
            current_version = self.config.get("version", "v2.7.3")
            if self.last_checked_version:
                if self._compare_versions(self.last_checked_version, current_version) > 0:
                    # 有新版本
                    update_content = self.last_checked_version_info.get("content", "暂无更新说明") if hasattr(self, 'last_checked_version_info') else "暂无更新说明"
                    event.set_result(MessageEventResult().message(
                        f"AstrBot-日限制插件 Limit\n\n🎉 检测到新版本可用！\n"
                        f"📦 当前版本：{current_version}\n"
                        f"🆕 最新版本：{self.last_checked_version}\n"
                        f"📝 更新内容：{update_content}\n"
                        f"🔗 下载地址：https://github.com/left666/astrbot_plugin_daily_limit"
                    ))
                else:
                    # 已是最新版本
                    event.set_result(MessageEventResult().message(
                        f"✅ 当前已是最新版本：{current_version}\n"
                        f"📅 最后检查时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    ))
            else:
                # 检查失败
                event.set_result(MessageEventResult().message("❌ 版本检查失败，请稍后重试"))
                
        except Exception as e:
            self._handle_error(e, "手动检查版本更新")
            event.set_result(MessageEventResult().message("❌ 版本检查过程中出现错误，请稍后重试"))

    @filter.permission_type(PermissionType.ADMIN)
    @limit_command_group.command("version")
    async def limit_version(self, event: AstrMessageEvent):
        """查看当前插件版本信息（仅管理员）"""
        try:
            current_version = self.config.get("version", "v2.7.3")
            
            # 构建版本信息消息
            version_msg = f"📦 日调用限制插件版本信息\n"
            version_msg += f"══════════════\n\n"
            version_msg += f"• 当前版本：{current_version}\n"
            version_msg += f"• 作者：left666\n"
            version_msg += f"• 改进：Sakura520222\n\n"
            
            # 添加版本检查状态
            if not self.config["version_check"].get("enabled", True):
                version_msg += "🔴 版本检查功能：已禁用\n"
            else:
                check_interval = self.config["version_check"].get("check_interval", 60)
                version_msg += f"🟢 版本检查功能：已启用（每 {check_interval} 分钟检查一次）\n"
                
                if self.last_checked_version:
                    if self._compare_versions(self.last_checked_version, current_version) > 0:
                        version_msg += f"🆕 检测到新版本：{self.last_checked_version}\n"
                    else:
                        version_msg += f"✅ 当前已是最新版本\n"
                else:
                    version_msg += f"⏳ 尚未进行版本检查\n"
            
            version_msg += f"\n💡 使用 /limit checkupdate 手动检查更新"
            
            event.set_result(MessageEventResult().message(version_msg))
            
        except Exception as e:
            self._handle_error(e, "查看版本信息")
            event.set_result(MessageEventResult().message("❌ 获取版本信息失败"))

    # ASCII艺术字
    ASCII_ART = """
 ██████████     █████████   █████ █████       █████ █████    █████       █████ ██████   ██████ █████ ███████████
░░███░░░░███   ███░░░░░███ ░░███ ░░███       ░░███ ░░███    ░░███       ░░███ ░░██████ ██████ ░░███ ░█░░░███░░░█
 ░███   ░░███ ░███    ░███  ░███  ░███        ░░███ ███      ░███        ░███  ░███░█████░███  ░███ ░   ░███  ░ 
 ░███    ░███ ░███████████  ░███  ░███         ░░█████       ░███        ░███  ░███░░███ ░███  ░███     ░███    
 ░███    ░███ ░███░░░░░███  ░███  ░███          ░░███        ░███        ░███  ░███ ░░░  ░███  ░███     ░███    
 ░███    ███  ░███    ░███  ░███  ░███      █    ░███        ░███      █ ░███  ░███      ░███  ░███     ░███    
 ██████████   █████   █████ █████ ███████████    █████       ███████████ █████ █████     █████ █████    █████   
░░░░░░░░░░   ░░░░░   ░░░░░ ░░░░░ ░░░░░░░░░░░    ░░░░░       ░░░░░░░░░░░ ░░░░░ ░░░░░     ░░░░░ ░░░░░    ░░░░░    
                                                                                                                
                                                                                                                                                                                                      
                                       每日调用限制插件 v2.7.4                       
                                  作者: left666 & Sakura520222                  
    """

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot初始化完成时触发"""
        self._log_info("{}", self.ASCII_ART)

    async def terminate(self):
        """插件终止时清理资源"""
        try:
            # 停止Web服务器
            if hasattr(self, 'web_server') and self.web_server:
                self._log_info("正在停止Web服务器...")
                try:
                    # 显式停止Web服务器
                    stop_result = self.web_server.stop()
                    if stop_result:
                        self._log_info("Web服务器已成功停止")
                    else:
                        self._log_warning("Web服务器停止可能未完全成功")
                except Exception as e:
                    self._log_error("停止Web服务器时发生错误: {}", str(e))
                
                # 清理Web服务器引用
                self.web_server = None
                self.web_server_thread = None
            
            # 停止版本检查任务
            if self.version_check_task and not self.version_check_task.done():
                self.version_check_task.cancel()
                try:
                    await self.version_check_task
                except asyncio.CancelledError:
                    pass
                self._log_info("版本检查任务已停止")
            
        except Exception as e:
            self._handle_error(e, "停止版本检查任务")
        
        # 调用父类的terminate方法
        await super().terminate()
