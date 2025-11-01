# 🚀 AstrBot 日调用限制插件 v2.4.3

<div align="center">

![Version](https://img.shields.io/badge/版本-v2.4.3-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-3.5.1%2B-green)
![Python](https://img.shields.io/badge/Python-3.10%2B-yellow)
![License](https://img.shields.io/badge/License-MIT-orange)
---
<div align="center">


![GitHub Stars](https://img.shields.io/github/stars/left666/astrbot_plugin_daily_limit?style=for-the-badge&logo=github&label=Stars&color=yellow)
![GitHub Forks](https://img.shields.io/github/forks/left666/astrbot_plugin_daily_limit?style=for-the-badge&logo=github&label=Forks&color=blue)
![GitHub Issues](https://img.shields.io/github/issues/left666/astrbot_plugin_daily_limit?style=for-the-badge&logo=github&label=Issues&color=green)

![GitHub Last Commit](https://img.shields.io/github/last-commit/left666/astrbot_plugin_daily_limit?style=for-the-badge&logo=git&label=最后提交)
![GitHub Release](https://img.shields.io/github/v/release/left666/astrbot_plugin_daily_limit?style=for-the-badge&logo=github&label=最新版本)

</div>


**智能管理AI资源使用，防止滥用，提升用户体验**
</div>

## 📖 简介

AstrBot 日调用限制插件是一个功能强大的AI资源管理工具，专为AstrBot设计。通过智能的每日调用限制机制，有效防止大模型API的滥用，确保AI服务的稳定性和公平性。

## ✨ 核心特性

### 🛡️ 智能限制系统
- **多级权限管理**：用户、群组、豁免用户三级权限体系
- **时间段限制**：支持按时间段设置不同的调用限制
- **优先级规则**：豁免用户 > 时间段限制 > 用户限制 > 群组限制 > 默认限制

### 👥 群组协作模式
- **共享模式**：群组成员共享使用次数（默认）
- **独立模式**：群组内每个成员独立计数（v2.2新增）
- **智能切换**：自动识别消息类型，无缝切换计数模式

### 📊 数据监控分析
- **实时监控**：使用统计、排行榜和状态监控
- **使用记录**：详细记录每次调用，支持历史查询
- **多维度分析**：用户、群组、全局等多维度统计分析

### 🔄 灵活管理
- **重置机制**：支持用户、群组或全部记录的重置
- **配置管理**：灵活的配置系统，支持个性化设置
- **Redis支持**：基于Redis的高性能数据存储

### 🔧 自定义跳过模式 (v2.4新增)
- **智能消息过滤**：支持自定义跳过处理的消息前缀
- **动态配置**：支持通过配置文件或管理员命令动态管理跳过模式
- **向后兼容**：默认保持与原有硬编码逻辑的兼容性

## 🚀 快速开始

### 系统要求
- **AstrBot版本**: v3.5.1+
- **Python版本**: 3.10+
- **Redis服务器**: 必须配置

### 安装步骤

1. **安装Redis依赖**
   ```bash
   pip install redis>=4.5.0
   ```

2. **配置Redis服务器**
   - 确保Redis服务正常运行
   - 记录Redis连接信息（主机、端口、密码等）

3. **安装插件**
   - 将插件文件放置到AstrBot插件目录
   - 重启AstrBot服务

### 基础配置

创建配置文件 `astrbot_plugin_dailylimit_config.json`：

```json
{
  "redis": {
    "host": "localhost",
    "port": 6379,
    "db": 0,
    "password": ""
  },
  "limits": {
    "default_daily_limit": 20,
    "exempt_users": [],
    "group_limits": [],
    "user_limits": [],
    "group_mode_settings": [],
    "time_period_limits": []
  },
  "web_server": {
    "host": "127.0.0.1",
    "port": 8080,
    "debug": false,
    "domain": ""
  }
}
```

## ⚙️ 详细配置

### Redis配置
```json
"redis": {
    "host": "localhost",
    "port": 6379,
    "db": 0,
    "password": ""
}
```

### 限制配置

#### 默认限制
- `default_daily_limit`: 默认每日调用次数（默认：20次）

#### 豁免用户
- `exempt_users`: 不受限制的用户ID列表

#### 用户限制
```json
{
    "user_id": "用户ID",
    "limit": 10
}
```

#### 群组限制
```json
{
    "group_id": "群组ID",
    "limit": 15
}
```

#### 群组模式配置
```json
{
    "group_id": "群组ID",
    "mode": "shared"  // shared: 共享模式, individual: 独立模式
}
```

#### 时间段限制
```json
{
    "start_time": "09:00",
    "end_time": "18:00",
    "limit": 10,
    "enabled": true
}
```

#### 跳过模式配置 (v2.4新增)
```json
"skip_patterns": ["#", "*"]
```
- **功能**：定义需要跳过处理的消息前缀
- **默认值**：`["#", "*"]`（保持向后兼容）
- **示例**：设置`["!", "/"]`可跳过以!或/开头的消息

#### Web服务器配置 (v2.4.4新增)
```json
"web_server": {
    "host": "127.0.0.1",
    "port": 8080,
    "debug": false,
    "domain": ""
}
```
- **host**：Web服务器绑定的主机地址（默认：127.0.0.1）
- **port**：Web服务器端口（默认：8080）
- **debug**：调试模式开关（默认：false）
- **domain**：自定义域名（用于生成访问链接，默认：空字符串）
- **示例**：设置`"domain": "example.com"`可生成`https://example.com/`访问链接

### 优先级规则

1. ⏰ **时间段限制** - 优先级最高
2. 🏆 **豁免用户** - 完全不受限制
3. 👤 **用户特定限制** - 针对单个用户
4. 👥 **群组特定限制** - 针对整个群组
5. ⚙️ **默认限制** - 全局默认设置

## 💡 使用指南

### 👤 用户命令

| 命令 | 功能 | 示例 |
|------|------|------|
| `/limit_status` | 查看个人使用情况 | `/limit_status` |
| `/限制帮助` | 显示所有可用命令 | `/限制帮助` |

### 👨‍💼 管理员命令

#### 🔧 基础管理
| 命令 | 功能 | 示例 |
|------|------|------|
| `/limit help` | 显示详细帮助 | `/limit help` |
| `/limit set <用户ID> <次数>` | 设置用户限制 | `/limit set 123456 50` |
| `/limit setgroup <次数>` | 设置群组限制 | `/limit setgroup 30` |
| `/limit setmode <shared\|individual>` | 设置群组模式 | `/limit setmode shared` |
| `/limit getmode` | 查看群组模式 | `/limit getmode` |

#### ⏰ 时间段管理
| 命令 | 功能 | 示例 |
|------|------|------|
| `/limit timeperiod list` | 列出时间段限制 | `/limit timeperiod list` |
| `/limit timeperiod add <开始> <结束> <次数>` | 添加时间段 | `/limit timeperiod add 09:00 18:00 10` |
| `/limit timeperiod remove <索引>` | 删除时间段 | `/limit timeperiod remove 1` |
| `/limit timeperiod enable <索引>` | 启用时间段 | `/limit timeperiod enable 1` |
| `/limit timeperiod disable <索引>` | 禁用时间段 | `/limit timeperiod disable 1` |

#### 🛡️ 豁免管理
| 命令 | 功能 | 示例 |
|------|------|------|
| `/limit exempt <用户ID>` | 添加豁免用户 | `/limit exempt 123456` |
| `/limit unexempt <用户ID>` | 移除豁免用户 | `/limit unexempt 123456` |

#### 📊 查询功能
| 命令 | 功能 | 示例 |
|------|------|------|
| `/limit list_user` | 列出用户限制 | `/limit list_user` |
| `/limit list_group` | 列出群组限制 | `/limit list_group` |
| `/limit stats` | 查看今日统计 | `/limit stats` |
| `/limit history [用户ID] [天数]` | 查询使用历史 | `/limit history 123456 7` |
| `/limit analytics [日期]` | 多维度分析 | `/limit analytics 2025-01-23` |
| `/limit top [数量]` | 显示排行榜 | `/limit top 5` |
| `/limit status` | 查看插件状态 | `/limit status` |
| `/limit domain` | 查看Web管理界面域名配置 | `/limit domain` |

#### 🔄 重置功能
| 命令 | 功能 | 示例 |
|------|------|------|
| `/limit reset all` | 重置所有记录 | `/limit reset all` |
| `/limit reset <用户ID>` | 重置特定用户 | `/limit reset 123456` |
| `/limit reset group <群组ID>` | 重置特定群组 | `/limit reset group 789012` |

#### 🔧 跳过模式管理 (v2.4新增)
| 命令 | 功能 | 示例 |
|------|------|------|
| `/limit skip_patterns` | 查看当前跳过模式 | `/limit skip_patterns` |
| `/limit skip_patterns add <模式>` | 添加跳过模式 | `/limit skip_patterns add !` |
| `/limit skip_patterns remove <模式>` | 移除跳过模式 | `/limit skip_patterns remove #` |
| `/limit skip_patterns reset` | 重置为默认模式 | `/limit skip_patterns reset` |

## 🔄 版本更新

### v2.4.4 (2025-11-01)
- ✅ **自定义域名配置功能** - 支持在配置文件中添加自定义绑定的域名
- ✅ **域名查看管理员指令** - 新增/limit domain命令，快速查看域名配置和访问地址
- ✅ **智能访问链接生成** - 根据域名配置自动生成合适的访问链接格式
- ✅ **Web管理界面优化** - 支持通过自定义域名访问Web管理界面

### v2.4.3 (2025-10-22)
- ✅ **全面优化/limit_status指令返回内容** - 大幅改进状态显示格式和用户体验
- ✅ **添加emoji和视觉分隔符** - 使用🎉、👥、👤、📊、🎯等emoji增强视觉效果
- ✅ **新增时间段限制状态显示** - 显示当前时间段限制、已使用次数和剩余次数
- ✅ **添加百分比进度条** - 为今日使用状态和时间段使用状态添加📈进度条显示
- ✅ **完善使用建议和提示信息** - 基于剩余次数提供分级提示和实用建议

### v2.4.2 (2025-10-21)
- ✅ **修复豁免用户状态显示问题** - 修复豁免用户在群组中发送/limit_status指令时返回错误消息的问题
- ✅ **优化状态显示逻辑** - 确保豁免用户在任何模式下都能正确显示个人豁免状态

### v2.4 & 2.4.1 (2025-10-20)
- ✅ **自定义跳过模式功能** - 支持用户自定义需要跳过处理的消息前缀
- ✅ **动态配置管理** - 支持通过配置文件或管理员命令动态管理跳过模式
- ✅ **向后兼容设计** - 默认保持与原有硬编码逻辑的兼容性
- ✅ **跳过模式管理命令** - 新增/limit skip_patterns系列命令

### v2.3 (2025-10-18)
- ✅ **时间段限制功能** - 支持按时间段设置不同的调用限制
- ✅ **时间段优先级** - 时间段限制优先级最高
- ✅ **时间段管理命令** - 新增/limit timeperiod系列命令

### v2.2 (2025-10-17)
- ✅ **群组独立模式** - 支持群组内成员独立使用次数
- ✅ **模式切换配置** - 支持为特定群组单独配置使用模式

### v2.1 (2025-10-17)
- ✅ **使用记录功能** - 自动记录每次调用，支持历史查询
- ✅ **多维度统计分析** - 提供用户、群组、全局等多维度分析

### v2.0 (2025-10-16)
- ✅ **群组共享机制** - 群组成员共享使用次数
- ✅ **智能计数切换** - 自动识别消息类型

## 🤝 贡献指南

我们欢迎社区贡献！请遵循以下指南：

### 🚀 如何贡献
1. **Fork 仓库** - fork 这个仓库到您的账户
2. **创建分支** - 为功能或修复创建新分支
3. **提交更改** - 提交清晰的提交信息
4. **创建 Pull Request** - 提交PR并描述更改

### 📋 贡献规范
- 确保代码符合项目编码风格
- 添加适当的测试用例
- 更新相关文档
- 确保所有测试通过

## 📞 技术支持

### 🐛 问题反馈
如遇到任何问题，请通过以下方式联系：
- 📧 Telegram: [@TamakiSakura520](https://t.me/TamakiSakura520)
- 💬 Issues: [GitHub Issues](https://github.com/left666/astrbot_plugin_daily_limit/issues)

## 📄 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情。

---

<div align="center">

**🌟 如果这个插件对你有帮助，请给个Star支持一下！**

</div>

## 👥 贡献者

感谢所有为这个项目做出贡献的开发者！

### 🏆 项目作者
- [left666](https://github.com/left666)

### 🤝 主要贡献者
- [Sakura520222](https://github.com/Sakura520222)

*感谢所有参与测试、反馈和贡献的社区成员！*

---

<div align="center">

**💫 您的每一个Star都是对我们最大的支持！**

[![Star History Chart](https://api.star-history.com/svg?repos=left666/astrbot_plugin_daily_limit&type=Date)](https://star-history.com/#left666/astrbot_plugin_daily_limit&Date)

</div>
