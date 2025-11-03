"""
Web管理界面服务器
提供可视化界面查看使用统计和配置管理
"""
import json
import datetime
import threading
import socket
import random
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from flask_cors import CORS
import redis
import os
import signal

class WebServer:
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
        
        # 检查端口占用并自动切换
        self._check_and_adjust_port()
        
        self._setup_routes()
    
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
        
        @self.app.route('/')
        @require_auth
        def index():
            """主页面"""
            return render_template('index.html')
        
        @self.app.route('/api/stats')
        @require_auth
        def get_stats():
            """获取统计信息"""
            try:
                stats = self._get_usage_stats()
                return jsonify({
                    'success': True,
                    'data': stats
                })
            except Exception as e:
                return jsonify({
                    'success': False,
                    'error': str(e)
                }), 500
        
        @self.app.route('/api/config')
        @require_auth
        def get_config():
            """获取配置信息"""
            try:
                config_data = self._get_config_data()
                return jsonify({
                    'success': True,
                    'data': config_data
                })
            except Exception as e:
                return jsonify({
                    'success': False,
                    'error': str(e)
                }), 500
        
        @self.app.route('/api/config', methods=['POST'])
        @require_auth
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
        
        @self.app.route('/api/users')
        @require_auth
        def get_users():
            """获取用户使用情况"""
            try:
                users_data = self._get_users_data()
                return jsonify({
                    'success': True,
                    'data': users_data
                })
            except Exception as e:
                return jsonify({
                    'success': False,
                    'error': str(e)
                }), 500
        
        @self.app.route('/api/groups')
        @require_auth
        def get_groups():
            """获取群组使用情况"""
            try:
                groups_data = self._get_groups_data()
                return jsonify({
                    'success': True,
                    'data': groups_data
                })
            except Exception as e:
                return jsonify({
                    'success': False,
                    'error': str(e)
                }), 500
        

    
    def _get_usage_stats(self):
        """获取使用统计信息"""
        if not self.plugin.redis:
            return {}
        
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        
        stats = {
            'total_requests': 0,
            'active_users': 0,
            'active_groups': 0,
            'date': today
        }
        
        # 获取活跃用户数 - 使用主插件的键格式
        user_pattern = f"astrbot:daily_limit:{today}:*:*"
        user_keys = self.plugin.redis.keys(user_pattern)
        stats['active_users'] = len(user_keys)
        
        # 获取活跃群组数 - 使用主插件的键格式
        group_pattern = f"astrbot:daily_limit:{today}:group:*"
        group_keys = self.plugin.redis.keys(group_pattern)
        stats['active_groups'] = len(group_keys)
        
        # 计算总请求数
        total_requests = 0
        for key in user_keys:
            usage = self.plugin.redis.get(key)
            if usage:
                total_requests += int(usage)
        
        stats['total_requests'] = total_requests
        
        return stats
    
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
    
    def _update_config(self, config_data):
        """更新配置"""
        # 这里需要实现配置更新的逻辑
        # 由于配置更新涉及文件操作，需要谨慎处理
        # 暂时返回成功状态
        return {'message': '配置更新功能待实现'}
    
    def _get_users_data(self):
        """获取用户使用数据"""
        if not self.plugin.redis:
            return []
        
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        # 使用主插件的键格式：astrbot:daily_limit:{today}:{group_id}:{user_id}
        user_pattern = f"astrbot:daily_limit:{today}:*:*"
        user_keys = self.plugin.redis.keys(user_pattern)
        
        users_data = []
        for key in user_keys:
            # 从key中提取用户ID和群组ID
            parts = key.split(':')
            if len(parts) >= 5:
                user_id = parts[-1]
                group_id = parts[-2]
                
                # 跳过群组键（群组键格式不同）
                if group_id == 'group':
                    continue
                    
                # 获取使用次数
                usage = self.plugin.redis.get(key)
                if not usage:
                    continue
                    
                # 获取用户限制
                user_limit = self.plugin._get_user_limit(user_id, group_id)
                
                users_data.append({
                    'user_id': user_id,
                    'group_id': group_id,
                    'usage_count': int(usage),
                    'limit': user_limit,
                    'remaining': max(0, user_limit - int(usage))
                })
        
        # 按使用量排序
        users_data.sort(key=lambda x: x['usage_count'], reverse=True)
        return users_data
    
    def _get_groups_data(self):
        """获取群组使用数据"""
        if not self.plugin.redis:
            return []
        
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        # 使用主插件的键格式：astrbot:daily_limit:{today}:group:{group_id}
        group_pattern = f"astrbot:daily_limit:{today}:group:*"
        group_keys = self.plugin.redis.keys(group_pattern)
        
        groups_data = []
        for key in group_keys:
            # 从key中提取群组ID
            parts = key.split(':')
            if len(parts) >= 5:
                group_id = parts[-1]
                
                # 获取使用次数
                usage = self.plugin.redis.get(key)
                if not usage:
                    continue
                    
                # 获取群组限制 - 使用虚拟用户ID来获取群组限制
                group_limit = self.plugin._get_user_limit("dummy_user", group_id)
                
                # 获取群组模式
                group_mode = self.plugin._get_group_mode(group_id)
                
                groups_data.append({
                    'group_id': group_id,
                    'usage_count': int(usage),
                    'limit': group_limit,
                    'remaining': max(0, group_limit - int(usage)),
                    'mode': group_mode  # 添加群组模式字段
                })
        
        # 按使用量排序
        groups_data.sort(key=lambda x: x['usage_count'], reverse=True)
        return groups_data
    
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
        """启动Web服务器"""
        try:
            self._server_running = True
            self.app.run(host=self.host, port=self.port, debug=False)
            return True
        except Exception as e:
            print(f"Web服务器启动失败: {e}")
            return False
    
    def start_async(self):
        """异步启动Web服务器"""
        def run_server():
            try:
                self._server_running = True
                from werkzeug.serving import make_server
                self._server_instance = make_server(self.host, self.port, self.app)
                self._server_instance.serve_forever()
            except Exception as e:
                print(f"Web服务器运行错误: {e}")
                self._server_running = False
        
        # 使用非守护线程，确保插件重载时能正确停止
        self._server_thread = threading.Thread(target=run_server, daemon=False)
        self._server_thread.start()
        return True
    
    def stop(self):
        """停止Web服务器"""
        if self._server_instance:
            try:
                self._server_instance.shutdown()
                self._server_instance = None
            except Exception as e:
                print(f"停止Web服务器时出错: {e}")
        
        if self._server_thread and self._server_thread.is_alive():
            try:
                # 等待线程结束，最多等待5秒
                self._server_thread.join(timeout=5)
                if self._server_thread.is_alive():
                    print("Web服务器线程未在5秒内结束，强制终止")
            except Exception as e:
                print(f"等待Web服务器线程结束时出错: {e}")
        
        self._server_running = False
        print("Web服务器已停止")

if __name__ == "__main__":
    # 测试用
    server = WebServer(None)
    server.start()