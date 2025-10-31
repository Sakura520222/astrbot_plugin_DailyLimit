"""
Web管理界面服务器
提供可视化界面查看使用统计和配置管理
"""
import json
import datetime
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import redis

class WebServer:
    def __init__(self, daily_limit_plugin, host='127.0.0.1', port=8080, domain=''):
        self.plugin = daily_limit_plugin
        self.host = host
        self.port = port
        self.domain = domain
        self.app = Flask(__name__)
        CORS(self.app)
        
        # 设置模板和静态文件目录
        self.app.template_folder = 'templates'
        self.app.static_folder = 'static'
        
        self._setup_routes()
    
    def _setup_routes(self):
        """设置路由"""
        
        @self.app.route('/')
        def index():
            """主页面"""
            return render_template('index.html')
        
        @self.app.route('/api/stats')
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
        stats_key = f"astrbot:usage_stats:{today}:global"
        
        stats = {
            'total_requests': 0,
            'active_users': 0,
            'active_groups': 0,
            'date': today
        }
        
        # 获取全局统计
        if self.plugin.redis.exists(stats_key):
            global_stats = self.plugin.redis.hgetall(stats_key)
            stats['total_requests'] = int(global_stats.get('total_requests', 0))
        
        # 获取活跃用户数
        user_pattern = f"astrbot:usage_stats:{today}:user:*"
        user_keys = self.plugin.redis.keys(user_pattern)
        stats['active_users'] = len(user_keys)
        
        # 获取活跃群组数
        group_pattern = f"astrbot:usage_stats:{today}:group:*"
        group_keys = self.plugin.redis.keys(group_pattern)
        stats['active_groups'] = len(group_keys)
        
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
        user_pattern = f"astrbot:usage_stats:{today}:user:*"
        user_keys = self.plugin.redis.keys(user_pattern)
        
        users_data = []
        for key in user_keys:
            # 从key中提取用户ID
            user_id = key.split(':')[-1]
            user_stats = self.plugin.redis.hgetall(key)
            
            # 获取用户限制
            user_limit = self.plugin._get_user_limit(user_id)
            
            users_data.append({
                'user_id': user_id,
                'usage_count': int(user_stats.get('total_usage', 0)),
                'limit': user_limit,
                'remaining': max(0, user_limit - int(user_stats.get('total_usage', 0)))
            })
        
        # 按使用量排序
        users_data.sort(key=lambda x: x['usage_count'], reverse=True)
        return users_data
    
    def _get_groups_data(self):
        """获取群组使用数据"""
        if not self.plugin.redis:
            return []
        
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        group_pattern = f"astrbot:usage_stats:{today}:group:*"
        group_keys = self.plugin.redis.keys(group_pattern)
        
        groups_data = []
        for key in group_keys:
            # 从key中提取群组ID
            group_id = key.split(':')[-1]
            group_stats = self.plugin.redis.hgetall(key)
            
            # 获取群组限制
            group_limit = self.plugin._get_group_limit(group_id)
            
            groups_data.append({
                'group_id': group_id,
                'usage_count': int(group_stats.get('total_usage', 0)),
                'limit': group_limit,
                'remaining': max(0, group_limit - int(group_stats.get('total_usage', 0)))
            })
        
        # 按使用量排序
        groups_data.sort(key=lambda x: x['usage_count'], reverse=True)
        return groups_data
    
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
            self.app.run(host=self.host, port=self.port, debug=False)
            return True
        except Exception as e:
            print(f"Web服务器启动失败: {e}")
            return False
    
    def start_async(self):
        """异步启动Web服务器"""
        import threading
        
        def run_server():
            self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)
        
        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        return True

if __name__ == "__main__":
    # 测试用
    server = WebServer(None)
    server.start()