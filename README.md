# Hindsight配置目录

## 文件说明

### 核心配置文件
- **start-fixed.sh** - 正确的启动脚本（唯一使用的脚本）
- **start.sh** -> start-fixed.sh（软链接，方便调用）
- **config.json** - Hindsight配置文件
- **doubao-online.toml** - 豆包在线配置（备用）
- **CONFIG_COMPLETE.md** - 完整配置记录文档

### 配置文件（已清理）
所有其他启动脚本已删除，只保留正确配置的start-fixed.sh

## 快速使用

```bash
# 启动Hindsight
bash ~/.hermes/hindsight/start-fixed.sh
# 或
bash ~/.hermes/hindsight/start.sh

# 停止Hindsight
pkill -9 -f "hindsight-api"

# 查看状态
ps aux | grep "hindsight-api.*9177" | grep -v grep
curl -s http://127.0.0.1:9177/health

# 查看日志
tail -f ~/.hindsight/daemon.log
```

## 配置摘要

- **LLM**: doubao-seed-2.0-lite (豆包线上)
- **Embedding**: BAAI/bge-small-en-v1.5 (本地, 384维)
- **Database**: 内嵌PostgreSQL (5434端口)
- **Port**: 9177

详细配置请查看 **CONFIG_COMPLETE.md**

## 开机自启

Hindsight已配置开机自动启动（通过cron @reboot）

验证方法：
```bash
crontab -l | grep hindsight
```

## 故障排查

遇到问题请先查看：
1. CONFIG_COMPLETE.md（完整配置文档）
2. ~/.hindsight/daemon.log（运行日志）
3. 使用 `curl -s http://127.0.0.1:9177/health` 检查服务状态

## 维护记录

**最后配置时间**: 2026-04-18 22:40
**配置状态**: ✅ 正常运行
**服务端口**: 9177
