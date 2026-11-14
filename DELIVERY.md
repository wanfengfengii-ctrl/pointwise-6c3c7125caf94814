# 外置载荷卷作业：部署与恢复操作说明

适用于已绑定外置载荷卷的安装实例。业务口径、检查点、回执与数据库位置
（`ROOT/state.sqlite`）均未改变；只有载荷目录位于外置卷上。

## 首次绑定（安装）

```sh
python3 ops.py build OUT                       # 生成 OUT/app.pyz 与 manifest.json
ABS=/usr/bin/python3                            # 运维提供的解释器绝对路径
python3 ops.py install OUT DEST --python "$ABS" --payload-dir PAYLOAD
```

- `PAYLOAD` 必须是原卷内**已经存在**的可写目录，且安装时原卷已挂载。
- 安装把该卷的卷身份（macOS 为 VolumeUUID）与约定挂载位置写入
  `DEST/data/volume.json`；之后不靠卷名、目录名或路径存在判断身份。
- 省略 `--payload-dir` 时为本地回归模式（载荷在 `ROOT/blobs`），行为与旧版一致。
- 默认安装同时写入并 bootstrap 用户 LaunchAgent（`RunAtLoad`+`KeepAlive`）；
  `--no-load` 只生成 `DEST/launch-agent.plist`。

## 缺卷与错卷的识别

```sh
ABS DEST/app.pyz status ROOT
```

外置卷模式新增 `volume_state` 字段：

- `ready`：原卷在约定位置且身份一致，可正常收发。
- `volume_unavailable`：约定位置未挂载原卷、路径不存在或访问失效。
  `inventory` 为 `{}`；`read`/`status` 仍从内置卷 SQLite 返回既有结果与待办。
- `wrong_volume`：约定位置被另一数据卷占用。不会读取错卷载荷。

命令级表现（stderr 含对应标记，退出码非零）：

- 新批次 `submit`：`VOLUME_UNAVAILABLE` 或 `WRONG_VOLUME`，不产生该批接纳记录；
  原卷恢复后重投即可。已存在批次的同内容重投仍返回原接纳回执。
- `run --once`：同样按 `VOLUME_UNAVAILABLE`/`WRONG_VOLUME` 失败，不推进检查点。
- 常驻 `run`：不退出，stderr 记录一次状态后受控等待，原卷挂回自动续跑。

## 原位重挂与恢复

1. 卸载错卷（若有），把**原卷**挂回**原约定挂载位置**。
2. 无需改库、改绑定或补文件：`status` 恢复 `ready`，常驻工作者自动完成待办，
   新批次可正常接纳；已完成结果与回执不变，重复提交不重复入账。

## 数据故障处置

原卷身份正确且可访问时，以下属于**数据故障**，明确报错、不会当作缺卷无限重试：

- `PAYLOAD_HASH`：载荷摘要与内容不符。
- `PAYLOAD_MISSING`：检查点引用的载荷在卷上缺失。
- `PAYLOAD_JSON`：载荷内容非法。

处置：从备份或上游恢复该载荷文件后重跑 `run --once`；本系统不自动修复坏载荷。

## 服务卸载

```sh
launchctl bootout gui/$(id -u)/LABEL     # LABEL 见 install 返回的 label
rm ~/Library/LaunchAgents/LABEL.plist
rm -rf DEST                               # 含 app.pyz、data、plist 与日志
```

## 上线回归

```sh
python3 releasecheck.py        # 全量核对；环境不支持的项目记为 NOT_EXECUTED
python3 releasecheck.py demo   # JSON 证据：真实核账输出、卷状态与回执
```

macOS 真实 GUI 会话下会额外执行真实磁盘映像挂卸矩阵与 LaunchAgent 上线
（含 bootout 清理）；无 GUI 会话时该项记为未执行，不以 `--no-load` 冒充。
