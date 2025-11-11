"""
Web管理界面服务器
提供可视化界面查看使用统计和配置管理

本模块实现了一个基于Flask的Web服务器，用于展示AstrBot插件的使用统计信息。
主要功能包括：
- 用户和群组使用数据的可视化展示
- 实时统计信息监控
- 密码保护的安全访问

版本: v2.6.8
作者: AstrBot插件开发团队
"""
import json
import datetime
import threading
import socket
import random
import time
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from flask_cors import CORS
import redis
import os
import signal

class WebServer:
    """
    Web服务器类
    
    负责管理插件的Web界面，提供用户友好的统计信息展示和配置管理功能。
    
    主要特性：
    - 自动端口检测和调整
    - 会话管理和密码保护
    - 实时数据统计和可视化
    - 响应式Web界面设计
    - 错误处理和日志记录
    
    属性：
        host (str): 服务器监听地址
        port (int): 服务器监听端口
        plugin: 主插件实例引用
        app (Flask): Flask应用实例
        _server_running (bool): 服务器运行状态标志
        _server_thread (Thread): 服务器运行线程
    """
    def __init__(self, daily_limit_plugin, host='127.0.0.1', port=8080, domain=''):
        self.plugin = daily_limit_plugin
        self.host = host
        self.original_port = port  # 保存原始端口配置
        self.port = port
        self.domain = domain
        self.app = Flask(__name__)
        
        # 设置会话密钥
        self.app.secret_key = os.urandom(24)
        CORS(self.app)
        
        # 设置模板和静态文件目录
        self.app.template_folder = 'templates'
        self.app.static_folder = 'static'
        
        # Web服务器控制变量
        self._server_thread = None
        self._server_running = False
        self._server_instance = None
        self._last_error = None  # 记录最后一次错误信息
        self._start_time = None  # 服务器启动时间
        
        # 检查端口占用并自动切换
        self._check_and_adjust_port()
        
        self._setup_routes()
    
    def _log(self, message):
        """日志记录方法"""
        if self.plugin and hasattr(self.plugin, '_log_info'):
            self.plugin._log_info("{}", message)
        else:
            print(f"[WebServer] {message}")
    
    def _is_port_available(self, port):
        """检查端口是否可用"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((self.host, port))
                return result != 0  # 如果连接失败，说明端口可用
        except Exception:
            return False
    
    def _find_available_port(self, start_port=None):
        """查找可用的端口"""
        if start_port is None:
            start_port = self.original_port
        
        # 从原始端口开始，尝试100个端口范围
        for port in range(start_port, start_port + 100):
            if self._is_port_available(port):
                return port
        
        # 如果指定范围内没有找到，随机尝试
        for _ in range(10):
            port = random.randint(10000, 65535)
            if self._is_port_available(port):
                return port
        
        return None  # 没有找到可用端口
    
    def _check_and_adjust_port(self):
        """检查端口占用并自动调整"""
        # 检查原始端口是否可用
        if self._is_port_available(self.original_port):
            self.port = self.original_port
            print(f"Web管理界面将使用默认端口: {self.port}")
            return
        
        # 端口被占用，查找可用端口
        available_port = self._find_available_port()
        if available_port:
            self.port = available_port
            print(f"警告: 默认端口 {self.original_port} 被占用，已自动切换到端口: {self.port}")
            
            # 保存新端口到配置
            self._save_port_to_config(available_port)
        else:
            # 没有找到可用端口，使用原始端口（可能会启动失败）
            self.port = self.original_port
            print(f"警告: 无法找到可用端口，将尝试使用端口: {self.port}（可能会启动失败）")
    
    def _save_port_to_config(self, port):
        """保存端口到配置文件"""
        try:
            if self.plugin and hasattr(self.plugin, 'config'):
                # 更新配置中的端口
                if 'web_server' not in self.plugin.config:
                    self.plugin.config['web_server'] = {}
                
                self.plugin.config['web_server']['port'] = port
                
                # 保存配置
                if hasattr(self.plugin.config, 'save_config'):
                    self.plugin.config.save_config()
                    print(f"已保存新端口 {port} 到配置文件")
                else:
                    print(f"警告: 无法保存端口到配置文件，配置对象缺少save_config方法")
        except Exception as e:
            print(f"保存端口到配置时出错: {e}")
    
    def _setup_routes(self):
        """设置路由"""
        # 设置认证相关的辅助函数
        self._setup_auth_helpers()
        
        # 设置认证路由
        self._setup_auth_routes()
        
        # 设置页面路由
        self._setup_page_routes()
        
        # 设置API路由
        self._setup_api_routes()

    def _setup_auth_helpers(self):
        """设置认证相关的辅助函数"""
        def check_auth():
            """检查用户是否已登录"""
            # 如果未设置密码，则无需验证
            if not self._get_web_password():
                return True
            
            # 检查会话中是否有登录标记
            return session.get('logged_in', False)
        
        def require_auth(f):
            """需要认证的装饰器"""
            def decorated_function(*args, **kwargs):
                if not check_auth():
                    return redirect(url_for('login'))
                return f(*args, **kwargs)
            decorated_function.__name__ = f.__name__
            return decorated_function
        
        # 将装饰器保存为实例变量，供其他方法使用
        self.require_auth = require_auth

    def _setup_auth_routes(self):
        """设置认证路由"""
        @self.app.route('/login', methods=['GET', 'POST'])
        def login():
            """登录页面"""
            # 如果未设置密码，直接重定向到首页
            web_password = self._get_web_password()
            if not web_password:
                session['logged_in'] = True
                return redirect(url_for('index'))
            
            if request.method == 'POST':
                password = request.form.get('password', '')
                if password == web_password:
                    session['logged_in'] = True
                    return redirect(url_for('index'))
                else:
                    return render_template('login.html', error='密码错误')
            
            return render_template('login.html')
        
        @self.app.route('/logout')
        def logout():
            """登出"""
            session.pop('logged_in', None)
            return redirect(url_for('login'))

    def _setup_page_routes(self):
        """设置页面路由"""
        @self.app.route('/')
        @self.require_auth
        def index():
            """主页面"""
            return render_template('index.html')

    def _setup_api_routes(self):
        """设置API路由"""
        self._setup_stats_api()
        self._setup_config_api()
        self._setup_users_api()
        self._setup_groups_api()
        self._setup_trends_api()

    def _setup_stats_api(self):
        """设置统计API路由"""
        @self.app.route('/api/stats')
        @self.require_auth
        def get_stats():
            """获取统计信息"""
            return self._handle_api_request(self._get_usage_stats)

    def _setup_config_api(self):
        """设置配置API路由"""
        @self.app.route('/api/config')
        @self.require_auth
        def get_config():
            """获取配置信息"""
            return self._handle_api_request(self._get_config_data)
        
        @self.app.route('/api/config', methods=['POST'])
        @self.require_auth
        def update_config():
            """更新配置"""
            try:
                config_data = request.get_json()
                result = self._update_config(config_data)
                return jsonify({
                    'success': True,
                    'data': result
                })
            except Exception as e:
                return jsonify({
                    'success': False,
                    'error': str(e)
                }), 500

    def _setup_users_api(self):
        """设置用户API路由"""
        @self.app.route('/api/users')
        @self.require_auth
        def get_users():
            """获取用户使用情况"""
            return self._handle_api_request(self._get_users_data)

    def _setup_groups_api(self):
        """设置群组API路由"""
        @self.app.route('/api/groups')
        @self.require_auth
        def get_groups():
            """获取群组使用情况"""
            return self._handle_api_request(self._get_groups_data)

    def _setup_trends_api(self):
        """设置趋势分析API路由"""
        @self.app.route('/api/trends')
        @self.require_auth
        def get_trends():
            """获取趋势分析数据"""
            try:
                period = request.args.get('period', 'week')
                data = self._get_trends_data(period)
                return jsonify({
                    'success': True,
                    'data': data
                })
            except Exception as e:
                if self.plugin:
                    self.plugin._log_error("获取趋势分析数据失败: {}", str(e))
                else:
                    print(f"获取趋势分析数据失败: {e}")
                
                return jsonify({
                    'success': False,
                    'error': '获取趋势分析数据失败'
                }), 500

    def _handle_api_request(self, api_function):
        """处理API请求的通用方法"""
        try:
            data = api_function()
            return jsonify({
                'success': True,
                'data': data
            })
        except Exception as e:
            # 记录错误日志
            if self.plugin:
                self.plugin._log_error("Web API请求处理失败: {}", str(e))
            else:
                print(f"Web API请求处理失败: {e}")
            
            return jsonify({
                'success': False,
                'error': '服务器内部错误，请稍后重试'
            }), 500
        

    
    def _get_usage_stats(self):
        """
        获取使用统计信息
        
        从Redis中获取活跃用户数、活跃群组数和总请求数等关键统计指标。
        
        返回：
            dict: 包含统计信息的字典，格式为：
                {
                    'active_users': int,  # 活跃用户数
                    'active_groups': int, # 活跃群组数  
                    'total_requests': int # 总请求数
                }
        """
        if not self.plugin.redis:
            return {}
        
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        
        stats = self._initialize_stats_dict(today)
        
        # 获取活跃用户数
        user_keys = self._get_user_keys_for_date(today)
        stats['active_users'] = len(user_keys)
        
        # 获取活跃群组数
        group_keys = self._get_group_keys_for_date(today)
        stats['active_groups'] = len(group_keys)
        
        # 计算总请求数
        stats['total_requests'] = self._calculate_total_requests(user_keys)
        
        return stats

    def _initialize_stats_dict(self, date_str):
        """初始化统计字典"""
        return {
            'total_requests': 0,
            'active_users': 0,
            'active_groups': 0,
            'date': date_str
        }

    def _get_user_keys_for_date(self, date_str):
        """获取指定日期的用户键"""
        user_pattern = f"astrbot:daily_limit:{date_str}:*:*"
        return self.plugin.redis.keys(user_pattern)

    def _get_group_keys_for_date(self, date_str):
        """获取指定日期的群组键"""
        group_pattern = f"astrbot:daily_limit:{date_str}:group:*"
        return self.plugin.redis.keys(group_pattern)

    def _calculate_total_requests(self, user_keys):
        """计算总请求数"""
        total_requests = 0
        for key in user_keys:
            usage = self.plugin.redis.get(key)
            if usage:
                total_requests += int(usage)
        return total_requests
    
    def _get_config_data(self):
        """获取配置数据"""
        config = self.plugin.config

        return {
            'default_daily_limit': config['limits']['default_daily_limit'],
            'exempt_users': config['limits']['exempt_users'],
            'group_limits': config['limits']['group_limits'],
            'user_limits': config['limits']['user_limits'],
            'group_mode_settings': config['limits']['group_mode_settings'],
            'time_period_limits': config['limits']['time_period_limits'],
            'skip_patterns': config['limits']['skip_patterns'],
            'custom_messages': config['limits'].get('custom_messages', {}),
            'redis_config': config['redis']
        }
    
    def _get_users_data(self):
        """
        获取用户使用数据
        
        从Redis中获取所有用户的使用统计信息，包括使用次数、限制和剩余次数。
        
        返回：
            list: 用户数据列表，每个元素为字典格式：
                {
                    'user_id': str,      # 用户ID
                    'usage_count': int,  # 使用次数
                    'limit': int,       # 限制次数
                    'remaining': int,    # 剩余次数
                    'group_id': str,    # 群组ID（如果有）
                    'group_name': str   # 群组名称（如果有）
                }
        """
        if not self.plugin or not self.plugin.redis:
            return []
        
        try:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            user_keys = self._get_user_keys(today)
            
            users_data = []
            for key in user_keys:
                user_data = self._parse_user_key_data(key)
                if user_data:
                    users_data.append(user_data)
            
            return self._sort_users_data(users_data)
        except Exception as e:
            if self.plugin:
                self.plugin._log_error("获取用户数据失败: {}", str(e))
            else:
                print(f"获取用户数据失败: {e}")
            return []

    def _get_user_keys(self, date_str):
        """
        获取用户相关的Redis键
        
        根据日期模式从Redis中获取所有用户相关的键名，用于用户数据统计。
        
        参数：
            date_str (str): 日期字符串，格式为YYYY-MM-DD
            
        返回：
            list: 用户键列表，格式为 ['astrbot:daily_limit:2024-01-01:group_id:user_id', ...]
        """
        if not self.plugin or not self.plugin.redis:
            return []
        
        try:
            user_pattern = f"astrbot:daily_limit:{date_str}:*:*"
            return self.plugin.redis.keys(user_pattern)
        except Exception as e:
            if self.plugin:
                self.plugin._log_error("获取用户键列表失败: {}", str(e))
            else:
                print(f"获取用户键列表失败: {e}")
            return []

    def _parse_user_key_data(self, key):
        """解析用户键数据"""
        # 从key中提取用户ID和群组ID
        user_id, group_id = self._extract_ids_from_key(key)
        if not user_id or not group_id:
            return None
        
        # 跳过群组键（群组键格式不同）
        if group_id == 'group':
            return None
        
        # 获取使用次数
        usage = self._get_usage_from_key(key)
        if not usage:
            return None
        
        # 获取用户限制
        user_limit = self.plugin._get_user_limit(user_id, group_id)
        
        return {
            'user_id': user_id,
            'group_id': group_id,
            'usage_count': int(usage),
            'limit': user_limit,
            'remaining': max(0, user_limit - int(usage))
        }

    def _extract_ids_from_key(self, key):
        """从Redis键中提取用户ID和群组ID"""
        parts = key.split(':')
        if len(parts) >= 5:
            return parts[-1], parts[-2]
        return None, None

    def _get_usage_from_key(self, key):
        """从Redis键获取使用次数"""
        return self.plugin.redis.get(key)

    def _sort_users_data(self, users_data):
        """对用户数据进行排序"""
        users_data.sort(key=lambda x: x['usage_count'], reverse=True)
        return users_data

    def _get_period_days(self, period):
        """根据周期类型获取分析天数
        
        参数：
            period (str): 分析周期，支持 'day', 'week', 'month'
            
        返回：
            int: 分析天数
        """
        period_days_map = {
            'day': 7,    # 最近7天
            'week': 28,  # 最近4周
            'month': 90  # 最近3个月
        }
        return period_days_map.get(period, 28)  # 默认最近4周

    def _generate_trends_data_points(self, days):
        """生成趋势数据点
        
        参数：
            days (int): 分析天数
            
        返回：
            list: 趋势数据点列表
        """
        trends_data = []
        today = datetime.datetime.now()
        
        for i in range(days):
            date = today - datetime.timedelta(days=i)
            date_str = date.strftime("%Y-%m-%d")
            
            # 获取该日期的统计数据
            stats = self._get_daily_stats(date_str)
            trends_data.append({
                'date': date_str,
                'total_requests': stats['total_requests'],
                'active_users': stats['active_users'],
                'active_groups': stats['active_groups']
            })
        
        # 按日期排序（从早到晚）
        trends_data.sort(key=lambda x: x['date'])
        return trends_data

    def _get_trends_data(self, period='week'):
        """
        获取趋势分析数据
        
        参数：
            period (str): 分析周期，支持 'day', 'week', 'month'
            
        返回：
            dict: 趋势分析数据，包含日期、总请求数、活跃用户数、活跃群组数等
        """
        if not self.plugin or not self.plugin.redis:
            return {}
        
        try:
            # 根据周期确定分析天数
            days = self._get_period_days(period)
            
            # 生成趋势数据点
            trends_data = self._generate_trends_data_points(days)
            
            return {
                'period': period,
                'days': days,
                'data': trends_data
            }
            
        except Exception as e:
            if self.plugin:
                self.plugin._log_error("获取趋势分析数据失败: {}", str(e))
            else:
                print(f"获取趋势分析数据失败: {e}")
            return {}

    def _get_daily_stats(self, date_str):
        """获取指定日期的统计数据"""
        stats = self._initialize_stats_dict(date_str)
        
        # 获取活跃用户数
        user_keys = self._get_user_keys_for_date(date_str)
        stats['active_users'] = len(user_keys)
        
        # 获取活跃群组数
        group_keys = self._get_group_keys_for_date(date_str)
        stats['active_groups'] = len(group_keys)
        
        # 计算总请求数
        stats['total_requests'] = self._calculate_total_requests(user_keys)
        
        return stats
    
    def _get_groups_data(self):
        """
        获取群组使用数据
        
        从Redis中获取所有群组的使用统计信息，包括群组模式、使用次数和限制。
        
        返回：
            list: 群组数据列表，每个元素为字典格式：
                {
                    'group_id': str,      # 群组ID
                    'usage_count': int,   # 使用次数
                    'limit': int,        # 限制次数
                    'remaining': int,     # 剩余次数
                    'mode': str          # 群组模式（shared/individual）
                }
        """
        if not self.plugin or not self.plugin.redis:
            return []
        
        try:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            group_keys = self._get_group_keys_for_date(today)
            
            groups_data = self._process_group_keys(group_keys)
            
            # 按使用量排序
            groups_data.sort(key=lambda x: x['usage_count'], reverse=True)
            return groups_data
        except Exception as e:
            self._log_group_data_error("获取群组数据失败", e)
            return []

    def _process_group_keys(self, group_keys):
        """处理群组键列表，返回群组数据"""
        groups_data = []
        for key in group_keys:
            group_data = self._process_single_group_key(key)
            if group_data:
                groups_data.append(group_data)
        return groups_data

    def _process_single_group_key(self, key):
        """处理单个群组键，返回群组数据"""
        try:
            # 从key中提取群组ID
            group_id = self._extract_group_id_from_key(key)
            if not group_id:
                return None
            
            # 获取使用次数
            usage = self.plugin.redis.get(key)
            if not usage:
                return None
                
            # 获取群组限制和模式
            group_limit = self.plugin._get_user_limit("dummy_user", group_id)
            group_mode = self.plugin._get_group_mode(group_id)
            
            return {
                'group_id': group_id,
                'usage_count': int(usage),
                'limit': group_limit,
                'remaining': max(0, group_limit - int(usage)),
                'mode': group_mode
            }
        except Exception as e:
            self._log_group_data_error(f"处理群组数据失败 (键: {key})", e)
            return None

    def _extract_group_id_from_key(self, key):
        """从Redis键中提取群组ID"""
        parts = key.split(':')
        if len(parts) >= 5:
            return parts[-1]
        return None

    def _log_group_data_error(self, message, error):
        """记录群组数据错误日志"""
        if self.plugin:
            self.plugin._log_warning("{}: {}", message, str(error))
        else:
            print(f"{message}: {error}")
    
    def _get_web_password(self):
        """获取Web管理界面密码"""
        if not self.plugin or not self.plugin.config:
            return "limit"  # 默认密码
        
        # 从配置中获取密码
        web_config = self.plugin.config.get('web_server', {})
        password = web_config.get('password', 'limit')
        
        # 如果密码为空字符串，返回None表示无需密码
        if password == '':
            return None
        
        return password
    
    def get_access_url(self):
        """获取访问链接"""
        if self.domain:
            # 如果有自定义域名，使用域名
            if self.domain.startswith(('http://', 'https://')):
                return self.domain
            else:
                return f"http://{self.domain}"
        else:
            # 如果没有域名，使用IP和端口
            return f"http://{self.host}:{self.port}"
    
    def start(self):
        """
        启动Web服务器
        
        启动Flask应用并开始监听指定端口。如果端口被占用，会自动调整端口。
        
        返回：
            bool: 启动成功返回True，失败返回False
        """
        try:
            self._server_running = True
            self.app.run(host=self.host, port=self.port, debug=False)
            return True
        except Exception as e:
            error_msg = f"Web服务器启动失败: {e}"
            if self.plugin:
                self.plugin._log_error("{}", error_msg)
            else:
                print(error_msg)
            return False
    
    def start_async(self):
        """
        异步启动Web服务器
        
        启动Flask应用并返回服务器线程。
        
        返回：
            bool: 启动成功返回True，失败返回False
        """
        try:
            # 检查是否已经在运行
            if self.is_running():
                self._log("Web服务器已经在运行中")
                return True
            
            # 清理之前的错误信息
            self._last_error = None
            
            # 检查并调整端口
            self._adjust_port_if_needed()
            
            # 启动服务器线程
            self._start_server_thread()
            
            # 等待服务器启动并检查状态
            return self._wait_for_server_start()
            
        except Exception as e:
            self._handle_start_async_error(e)
            return False

    def _adjust_port_if_needed(self):
        """检查并调整端口"""
        if not self._is_port_available(self.port):
            self.port = self._find_available_port()
            self._log(f"端口被占用，自动切换到端口: {self.port}")

    def _start_server_thread(self):
        """启动服务器线程"""
        def run_server():
            try:
                self._server_running = True
                self._start_time = time.time()
                from werkzeug.serving import make_server
                self._server_instance = make_server(self.host, self.port, self.app)
                self._log(f"Web服务器启动成功: http://{self.host}:{self.port}")
                self._server_instance.serve_forever()
            except Exception as e:
                self._handle_server_thread_error(e)
        
        self._server_thread = threading.Thread(target=run_server, daemon=False)
        self._server_thread.start()

    def _handle_server_thread_error(self, error):
        """处理服务器线程错误"""
        self._server_running = False
        error_msg = f"Web服务器运行失败: {str(error)}"
        self._last_error = error_msg
        if self.plugin:
            self.plugin._log_error(error_msg)
        else:
            print(error_msg)

    def _wait_for_server_start(self):
        """等待服务器启动并检查状态"""
        for _ in range(10):  # 最多等待5秒
            time.sleep(0.5)
            if self.is_running():
                self._log(f"Web服务器启动完成，状态: {self.get_status()}")
                return True
        
        # 启动超时
        self._handle_start_timeout()
        return False

    def _handle_start_timeout(self):
        """处理启动超时"""
        error_msg = "Web服务器启动超时"
        self._last_error = error_msg
        if self.plugin:
            self.plugin._log_error(error_msg)
        else:
            print(error_msg)

    def _handle_start_async_error(self, error):
        """处理异步启动错误"""
        error_msg = f"Web服务器启动失败: {str(error)}"
        self._last_error = error_msg
        if self.plugin:
            self.plugin._log_error(error_msg)
        else:
            print(error_msg)
    
    def get_status(self):
        """
        获取Web服务器状态信息
        
        返回：
            dict: 包含服务器状态信息的字典
        """
        return {
            "running": self._server_running,
            "thread_alive": self._server_thread and self._server_thread.is_alive() if self._server_thread else False,
            "port": self.port,
            "host": self.host,
            "start_time": self._start_time,
            "last_error": self._last_error,
            "instance_exists": self._server_instance is not None
        }
    
    def is_running(self):
        """
        检查Web服务器是否正在运行
        
        返回：
            bool: 服务器是否正在运行
        """
        status = self.get_status()
        return status["running"] and status["thread_alive"]
    
    def stop(self):
        """
        停止Web服务器
        
        停止Flask应用并等待服务器线程结束。
        
        返回：
            bool: 停止成功返回True，失败返回False
        """
        try:
            # 记录停止前的状态
            previous_status = self.get_status()
            
            self._server_running = False
            
            # 优雅停止服务器实例
            if self._server_instance:
                self._server_instance.shutdown()
                
            # 等待线程结束
            if self._server_thread and self._server_thread.is_alive():
                self._server_thread.join(timeout=5)
                
            # 清理资源
            self._server_instance = None
            self._server_thread = None
            self._start_time = None
            
            self._log("Web服务器已停止")
            return True
            
        except Exception as e:
            error_msg = f"停止Web服务器失败: {str(e)}"
            self._last_error = error_msg
            if self.plugin:
                self.plugin._log_error(error_msg)
            else:
                print(error_msg)
            return False

if __name__ == "__main__":
    # 测试用
    server = WebServer(None)
    server.start()