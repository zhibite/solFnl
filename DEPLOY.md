# Coolify 部署指南

## 部署配置

项目已针对 Coolify 部署进行了优化配置。

### 部署地址

- **域名**：fnl.bquant.com
- **端口**：8038
- **协议**：HTTPS（由 Coolify 自动处理）

### 部署步骤

1. **在 Coolify 中创建新应用**
   - 选择 "Docker Compose"
   - 指向此仓库 `docker-compose.yml`

2. **配置环境变量**（在 Coolify UI 中）
   
   必需配置：
   ```
   DEBOT_SESSION_ID=<你的beegosessionID>
   ```
   
   可选配置（使用默认值即可）：
   ```
   GMGN_PASSWORD=anywn076034
   SYNC_INTERVAL_MIN=60
   SYNC_MONTHS_BACK=2
   CHAIN=solana
   DATABASE_PATH=/data/gmgndaily.db
   ```

3. **绑定域名**
   - 在 Coolify 中配置反向代理
   - 绑定 fnl.bquant.com 到此应用
   - Coolify 会自动处理 SSL 证书（Let's Encrypt）

4. **持久化存储**
   - 数据库文件存储在 `gmgndaily_data` 卷
   - Coolify 会自动管理卷持久化

### 部署后验证

- 访问 https://fnl.bquant.com/api/health 检查健康状态
- 访问 https://fnl.bquant.com/login 登录控制台
- 默认用户名：`zengsw`，密码：`anywn076034`（可在环境变量中修改）

### 关键差异说明

- **Dockerfile**：不包含 `DATABASE_PATH` 环境变量，由 docker-compose 和 Coolify 配置
- **docker-compose.yml**：移除了本地端口映射 `ports: 8038:8038`，Coolify 自动处理反向代理
- **环境变量**：通过 `.env.docker` 模板和 Coolify UI 配置，不硬编码在代码中

### 日志和监控

- 应用日志：在 Coolify 仪表板中查看
- 健康检查：每 30 秒检查一次，失败 3 次后自动重启
- 自动重启：`restart: unless-stopped`

### 数据库管理

- SQLite 数据库位置：`/data/gmgndaily.db`
- 卷名称：`gmgndaily_data`
- 备份：从 Coolify 卷管理界面下载备份

### 常见问题

**Q: 如何更新 DEBOT_SESSION_ID？**  
A: 在 Coolify UI 中修改环境变量，然后重新部署应用即可。

**Q: 如何查看同步日志？**  
A: 登录 https://fnl.bquant.com/wallets 在"同步日志"查看。

**Q: 可以改变同步间隔吗？**  
A: 可以，在 Coolify 中修改 `SYNC_INTERVAL_MIN` 环境变量。
