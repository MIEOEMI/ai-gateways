# 运维

## 宿主机重启后网关不通

**症状**：重启后网关连不上，`curl` 返回 `000`（连不上，**不是** 502）。

实测遇到过**两种**坏法，都真实发生：

| 故障 | 现象 | 检测条件 |
|---|---|---|
| **A. 容器没在跑** | `docker ps` 里看不到，`Exited (137)` | `.State.Running != true` |
| **B. 端口绑定丢失** | 容器在跑但 `docker port` 为空 | `PortBindings` 有值 且 `NetworkSettings.Ports == {}` |

故障 A 的实测时间线（证伪 `restart: unless-stopped` 的可靠性）：

```
容器上次退出   11:24:48
宿主机开机     11:57:19    ← 退出早于开机
容器 StartedAt 仍停留在上一次
```

**不要假设 `restart: unless-stopped` 一定可靠**（实测 docker 29.1.3）。

立即恢复：

```bash
docker compose up -d --force-recreate trae2api-hub workbuddy2api gateway-dashboard
```

### 根治：gateway-watchdog

compose 里的 `gateway-watchdog` sidecar 每 30s 通过 docker socket 检查上面两个条件，
发现问题就**只重建那一个容器**。

```bash
docker logs gateway-watchdog     # 看它处理过什么
```

实测：停掉容器 → **10s 内自动拉起**；注入端口丢失 → **25s 内恢复**。

### 四个实现细节（改之前必读，全踩过）

1. **Compose 会插值 `$` 变量** —— shell 变量必须写成 `$$c`。写漏一个（比如
   `[ "$act" = "{}" ]` 里的 `$act`）就会变成 `[ "" = "{}" ]`，恒为假、
   该分支静默失效。**改完必须 `docker inspect -f '{{json .Config.Cmd}}'` 核对实际命令。**
2. **不能全项目 `--force-recreate`** —— 那会把看门狗自己也销毁，执行到一半自己
   被 SIGKILL，结果所有容器卡在 `Created` 状态没人拉起。必须逐个容器重建。
3. **sidecar 里的路径要用宿主真实路径** —— `PROJECT_DIR` 必须是宿主绝对路径，且
   挂到 sidecar 内的**同名路径**。挂到别的路径会让 compose 把相对路径按容器内路径
   解析，volume 源变成不存在的宿主路径，容器起来后账号目录是空的
   （症状：`503 no CN account available`）。
4. **重建后要自检** —— `up -d` 返回 0 不等于容器在跑，脚本里补一次
   `.State.Running` 检查并打印警告，否则失败也是静默的。

## 完整重建

```bash
./scripts/build.sh --clean --no-cache    # 重拉上游 + 重打补丁 + 重建镜像
docker compose up -d --force-recreate
```

## 数据在哪

```
data/
├── trae/
│   ├── accounts/          # 账号凭据（settings.json 含面板签名密钥）
│   └── usage/             # 用量日志、排程器状态、模型健康
└── workbuddy/
    ├── workbuddy2api/accounts.json
    ├── usage.jsonl
    └── converter.log
```

`data/` 在 `.gitignore` 里 —— **账号凭据绝不要提交**。

### 备份

只需要备份 `data/`：

```bash
tar czf gateways-$(date +%F).tar.gz data/
```

## 上游更新后补丁冲突

`scripts/build.sh` 会先把上游拉到**固定基线提交**再打补丁。如果上游已前进，
脚本会按基线拉取，所以正常情况不会冲突。

真正需要处理的是「想跟进上游新版本」：基线一改，补丁就要重新生成。

```bash
# 在新基线上重打
cd .build/trae2api-hub && git apply --check ../../patches/trae2api-hub.patch
```

冲突时 `git apply --reject` 会生成 `.rej` 文件，逐个人工合。

## 常见误判

| 看到 | 别急着当故障 |
|---|---|
| 面板里失败率很高 | 先看「主动断开」列 —— 长上下文被客户端切断不是网关故障 |
| `client disconnected` | 客户端超时/重发，网关侧无异常 |
| 面板 `(历史数据)` 很多 | 来源追踪上线前的行，正常 |
| 容器 `Exited (0)` | 正常退出也可能是关机；看 `StartedAt`/`FinishedAt` 与开机时间的关系 |
