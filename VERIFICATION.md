# 本轮交付核验记录

记录在本运行环境（Linux 容器，无挂载权限、无 macOS GUI）中实际执行的核验，
以及因环境限制未执行的项目。基线业务断言见 BASELINE_VERIFICATION.md，
`checks.py` 的全部原断言保持通过且未修改。

## 实际执行（真实通过）

1. `env PYTHONDONTWRITEBYTECODE=1 python3 checks.py normal`
   → `NORMAL_REGRESSION_OK: revisions, tombstones, shared payloads, resume, receipts`
2. `env PYTHONDONTWRITEBYTECODE=1 python3 checks.py disk` → `DISK_RESUME_OK`
3. `env PYTHONDONTWRITEBYTECODE=1 python3 releasecheck.py`
   → 退出码 0；`RELEASECHECK_OK: 5 passed, 11 not executed`
   - `installed_baseline` PASS：字节级可复现构建、清单校验、坏包不改已装程序、
     源码移走后经安装产物完成提交/断点续跑/核账、工作者互斥、回执一致。
   - `external_ready_state` PASS：以 `--payload-dir` 绑定真实挂载卷（本机
     `/dev/shm` tmpfs，身份 `D135:I1`）安装，绑定写入 `ROOT/volume.json`；
     提交 a/b、完成核账（a：s1/-1/sum7/count1、s2/1/sum-30/count2；
     b：s1/0/sum100/count1）、`volume_state=ready`、载荷清单 8 个对象、
     同批重投回原回执、异内容 `BATCH_CONFLICT`、`ROOT/blobs` 未创建。
   - `external_kill_restart` PASS：在 `volume_checked` 与 `before_object_publish`
     观察点 SIGKILL 工作者，重开后自动续跑，结果正确且不重复入账。
   - `external_daemon` PASS：常驻 run 自动完成新批次；第二个工作者
     `WORKER_BUSY`；SIGKILL 后结果不变。
   - `external_corrupt_payload` PASS：篡改载荷后 `run --once` 两次均确定性
     报 `PAYLOAD_HASH`（非无限重试），`volume_state` 仍为 `ready`；删除载荷
     后报 `PAYLOAD_MISSING`；已完成结果仍可读。
4. `env PYTHONDONTWRITEBYTECODE=1 python3 releasecheck.py demo`
   → 退出码 0；stdout 为 JSON 证据（summary、逐项 checks、含真实 read
   核账输出、卷状态、绑定与回执的 evidence）。
5. 绑定校验边界（手工执行，均非零退出且不改动 DEST）：
   缺失目录 `PAYLOAD_DIR_MISSING`；非卷目录 `PAYLOAD_DIR_NOT_ON_VOLUME`；
   本地模式（省略 `--payload-dir`）端到端行为与旧版一致，status 无
   `volume_state` 字段；`manual.py` 本地核账能力保留。

## 未执行（环境限制，非跳过伪造）

- `matrix_*`（真实挂卸矩阵，10 项）：本容器 seccomp 禁止命名空间、无
  CAP_SYS_ADMIN、无 FUSE，`volumes.py open` 真实报错
  `mount: must be superuser to use mount`（退出码 32），证据见 demo JSON 的
  `volume_lab_unavailable`。在 macOS（hdiutil 磁盘映像）或有挂载权限的
  Linux（bind 后端）上，`python3 releasecheck.py` 会真实执行：缺卷启动、
  错卷占位与哨兵快照、身份核验后掉线、发布/引用提交前掉线、提交者中途
  掉线、常驻进程受控等待与恢复、损坏载荷、重连核账。
- `live_launch_agent`：本环境无 macOS GUI 登录会话（`launchctl` 不存在），
  按契约记为未执行，未以 `--no-load` 或手工 run 冒充。

## 矩阵代码的附加验证（说明）

为在不具备挂载权限的本环境中排除矩阵流程代码缺陷，曾在 /tmp 下用私有
一次性线束（仅把挂载表查询重定向到模拟表，载荷 I/O、fd 钉住、检查点、
回执均为真实；不写入仓库、不作为验收替代）完整跑通矩阵：15 passed,
1 not executed（仅 live_launch_agent）。真实挂卸验收仍以
`python3 releasecheck.py` 在 macOS/可挂载 Linux 上的输出为准。
