# 数据库持久化指南

## 问题

当容器重启或重新部署时，数据丢失。这通常是因为 Docker 卷没有正确绑定。

## 解决方案

### 方案 A：使用命名卷（推荐用于 Coolify）

在 `docker-compose.yml` 中：

```yaml
volumes:
  gmgndaily_data:
    driver: local
```

**注意：** 确保在 Coolify 中配置时：
1. 在服务配置中添加卷映射：`gmgndaily_data:/data`
2. 不要删除旧卷，选择"保留卷"选项
3. 部署后验证卷是否挂载

### 方案 B：使用本地路径持久化

修改 `docker-compose.yml`：

```yaml
services:
  gmgndaily:
    volumes:
      - /data/gmgndaily:/data  # 主机的绝对路径
```

优势：
- 数据直接存储在主机文件系统
- 易于备份和恢复
- 容器销毁后数据仍然存在

### 方案 C：Coolify 特定配置

在 Coolify 的应用设置中：

1. **存储卷**：添加持久化卷
   - 容器路径：`/data`
   - 主机路径：`/data/gmgndaily`（或其他位置）
   - 勾选"持久化"

2. **部署策略**：
   - 部署前：备份现有卷
   - 部署时：使用 Docker API 保留卷
   - 部署后：验证卷挂载

## 验证数据持久化

```bash
# 进入容器
docker exec -it gmgndaily-app bash

# 检查数据库
ls -lah /data/gmgndaily.db

# 查看数据库大小
du -sh /data/

# 检查卷挂载
mount | grep gmgndaily
```

## 备份策略

建议定期备份数据库：

```bash
# 备份
docker run --rm -v gmgndaily_data:/data -v $(pwd):/backup \
  alpine tar czf /backup/gmgndaily-backup-$(date +%Y%m%d).tar.gz /data

# 恢复
docker run --rm -v gmgndaily_data:/data -v $(pwd):/backup \
  alpine tar xzf /backup/gmgndaily-backup-latest.tar.gz
```

## Coolify 最佳实践

1. **在部署前**：
   - 检查"保留卷"选项是否启用
   - 备份当前数据库

2. **部署配置**：
   ```
   Volumes: gmgndaily_data:/data
   ```

3. **监控**：
   - 定期检查容器日志
   - 验证同步任务是否正常运行
   - 监控卷的可用空间

## 问题排查

### 症状：部署后数据丢失

**原因检查**：
```bash
# 列出所有卷
docker volume ls | grep gmgndaily

# 检查卷详情
docker volume inspect gmgndaily_data

# 查看卷内容
docker run --rm -v gmgndaily_data:/data alpine ls -la /data
```

**常见原因**：
- Coolify 删除了旧卷
- 卷路径配置错误
- 容器权限问题

**解决方法**：
1. 手动创建卷：`docker volume create gmgndaily_data`
2. 恢复备份数据
3. 重新部署应用
