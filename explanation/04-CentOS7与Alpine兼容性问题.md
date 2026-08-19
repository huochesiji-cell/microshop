# Day 1：CentOS 7 老内核与新版 Alpine 镜像的兼容性问题

## 故障现象

```bash
docker run -d --name nginx-bridge -p 8888:80 nginx:alpine
docker ps -a
# STATUS: Exited (1)

docker logs nginx-bridge
# [crit] 1#1: pwrite() "/run/nginx.pid" failed (1: Operation not permitted)
```

容器启动后 1 秒就退出，日志里最后一行是 `pwrite() failed`。

## 根因分析

```
CentOS 7                  Alpine (nginx:alpine)
─────────                 ──────────────────────
内核: 3.10.0-1160       musl libc 1.2.x
glibc 2.17              nginx 1.31.3
                        (2026年最新版)
                            │
                            │  pwrite() 到 /run/nginx.pid
                            ▼
                    CentOS 7 3.10 内核
                        │
                        │  ❌ Operation not permitted
                        ▼
                    容器退出
```

### 为什么 pwrite() 失败？

先说结论：这是**「新基础镜像 + 老宿主机内核」的兼容性问题**，不是 nginx 自己的配置错误。核心原理是 **容器共享宿主机内核**：

```
新版 nginx:alpine（2026 年）
    │ 自带新版 musl libc（1.2.x）
    │ 新版 libc 会用一些「只有新内核才支持」的系统调用
    ▼
CentOS 7 的 3.10 内核（2013 年发布）
    │ 这些新 syscall 在老内核上不存在或行为不同
    ▼
nginx 写 /run/nginx.pid 时，系统调用被拒绝
    │
    ▼
pwrite() failed (Operation not permitted) → 容器退出
```

> ⚠️ 老实说，具体是哪一个系统调用出问题，我当时没有深挖到内核层面。但**现象和规律是确定的**：很新的基础镜像（新版 musl/glibc 都可能）跑在 CentOS 7 这种 3.10 老内核上，就容易踩这类兼容坑——"镜像很新"不等于"一定能跑"。

> Docker 的「容器共享内核」特性在这里是劣势：无论镜像多新，容器都只能用宿主机的 3.10 内核，新 libc 里的新功能可能不受支持。

## 通用规律

| 基础镜像 | libc | 与 CentOS 7（3.10 内核）兼容性 |
|------|------|------|
| **Debian/Ubuntu** | glibc | ✅ 最好，老内核适配充分 |
| Alpine（较旧版本）| musl | ✅ 基本 OK |
| Alpine（较新版本 / edge）| musl | ⚠️ 容易出现兼容问题 |

## 两种解决方法

### 方法 1：换 Debian 基础镜像（推荐）

```bash
docker rm nginx-bridge
docker run -d --name nginx-bridge -p 8888:80 nginx:stable
```

- `nginx:stable` 基于 Debian，glibc 和 CentOS 7 内核天然兼容
- 镜像稍大（~140MB vs Alpine ~40MB），但稳定

### 方法 2：给容器独立的 /run tmpfs

```bash
docker rm nginx-bridge
docker run -d --name nginx-bridge -p 8888:80 --tmpfs /run nginx:alpine
```

- `--tmpfs /run`：给 `/run` 目录挂一个内存临时文件系统
- 原理：tmpfs 上 certain file operations behave differently，可能绕开内核限制
- 不稳定，不同软件可能碰到不同的 syscall 问题

