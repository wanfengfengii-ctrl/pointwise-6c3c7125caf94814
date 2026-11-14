# 采样分析作业：外置载荷卷的掉线重连发布

## 已有项目和真实运行条件

Python 3.9+标准库与SQLite，macOS 13+普通登录用户。运维提供可执行Python绝对路径，不捆绑解释器。无需管理员权限、在线账号或外部数据服务，不使用Docker或Compose。验收使用可写的本机目录、系统hdiutil/diskutil和真实GUI登录会话。路径可能含空格与中文，运行工作目录与源码目录不同；发布后源码会移走。

pipeline.py的validate、normalize、merge、summarize、encoded、digest为既有业务核，不改变其行为。Engine和LocalBlobs的持久编排可调整，可增加运维元数据。jobs现有列、检查点形状与<sha256>.json载荷内容保持兼容。数据库始终在ROOT/state.sqlite；安装目录和ROOT均在内置卷上，只有载荷目录在一个外置本地卷上。manual.py保留已有本地手工核账能力；外置卷的正式操作通过安装app.pyz进行。

本轮仅发布原卷绑定及掉线恢复，不实现换卷复制、载荷清理、备份、运行中版本升级、数据库迁移、多卷或远程存储，不处理断电和文件系统损坏。仅有一个工作者和一个提交者；可在任意已有观察点终止并重开。实验工具不被并发调用，也不要求实验工具自身的崩溃恢复。

## 已有输入、结果与回执

输入文件为UTF-8 JSON对象：
{"batch":"批次ID","segments":[[{"id":"样本ID","rev":1,"sensor":"站点","ts":61,"value":120,"deleted":false}]]}

每批1～16段，每段0～2000行；字符串非空，rev为正整数，ts与value为非布尔整数，deleted为布尔。每个样本取最高修订，允许换站、改时、撤销和复活；同修订异内容整批拒绝。未删除样本按(sensor,ts//60)汇总sum/count，保留负值和零。相同批次ID、相同内容重投返回原接纳回执，异内容报BATCH_CONFLICT，不改原账。

samples/a.json的rows为：
[{"sensor":"s1","minute":-1,"sum":7,"count":1},{"sensor":"s2","minute":1,"sum":-30,"count":2}]
samples/b.json的rows为：
[{"sensor":"s1","minute":0,"sum":100,"count":1}]

read返回按batch排序的列表，每项含batch、rows、receipt。接纳回执为{"batch":ID,"sha256":规范输入摘要}；完成回执为{"batch":ID,"sha256":规范输入摘要,"groups":分组数,"samples":未删除样本数}。结果与完成回执一起提交，未完成批次不出现在read中。重复提交不重复入账，完成后重投仍返回原接纳回执。

## 已有发布和业务入口

源码入口为python3 ops.py，安装入口为ABS DEST/app.pyz，其中ABS为Python解释器绝对路径。每条成功命令输出一个JSON值；错误非零退出并向stderr说明原因。以下路径作为独立argv元素传入，不依赖shell激活、当前目录或PYTHONPATH。

- build OUT：生成OUT/app.pyz与manifest.json，清单为{"sha256":"app.pyz的SHA-256"}，返回{"package":OUT绝对路径}。
- install PACKAGE DEST --python ABS [--no-load]：校验后安装，返回app、root、plist、label四个绝对路径或标签字段，其中root为DEST/data。
- submit ROOT INPUT：读取输入文件并返回接纳回执。
- run ROOT --once：执行一次Engine.step，返回{"progressed":布尔}；run ROOT为常驻循环。
- read ROOT：返回完成结果。
- status ROOT：返回pending、completed、required、inventory；inventory为已发布载荷摘要到字节数的映射。

同源码重复构建字节相同，坏包不得改变已安装程序和启动配置。worker.lock拒绝第二个工作者，提交者不占该长驻锁。新增生产模块须纳入ops.py的CORE清单。

默认安装在用户Library/LaunchAgents写入RunAtLoad、KeepAlive配置并bootstrap gui/<uid>；ProgramArguments是[ABS,安装app.pyz绝对路径,"run",ROOT绝对路径]，日志留在DEST。--no-load只生成DEST/launch-agent.plist。agentcheck.py可检查配置，--live还检查真实launchctl状态。安装时无作业运行，本轮不要求在线升级已有安装。

releasecheck.py当前验证构建、校验安装、源码移除、工作者互斥及样例核账，只生成自启动配置，不包含真实LaunchAgent或卷重连验收。其历史注释中的relocation不构成本题要求。

## 本轮公开运行契约

install新增--payload-dir DIR，启用外置卷模式；省略该参数时保留已有本地回归模式。DIR为原卷内已经存在的可写目录，首次安装时原卷已经挂载。绑定安装时该卷的VolumeUUID及约定载荷位置，绑定信息在ROOT持久保存；生产代码自行查询卷身份，不读取实验工具的volumes.json。重新挂载后设备号可能改变，卷名、目录名称和路径存在均不能替代卷身份。无需支持更换绑定卷或重新安装到同一DEST。

外置卷模式的status保留原字段，增加volume_state，值为ready、volume_unavailable或wrong_volume。原挂载点未挂载原卷、路径不存在或原卷访问因掉线失效时为volume_unavailable；约定位置属于另一已挂载数据卷时为wrong_volume。两种不可用状态下inventory返回{}，不得访问错卷载荷来填充。read和status仍能从内置卷上的SQLite返回既有结果和待办，不得因构造LocalBlobs而创建替代载荷目录。

缺卷或错卷时，新批次submit非零退出，stderr分别含VOLUME_UNAVAILABLE或WRONG_VOLUME，调用者可在原卷恢复后重试；不得新增该批接纳记录。已存在批次的同内容重放仍可返回原接纳回执。run --once不可用时也按相应错误非零退出，不推进依赖不可用载荷的检查点。常驻run在卷恢复后自动继续；允许受控等待或由KeepAlive重启，但不能将待办标成成功或要求人工补文件。

身份检查后真实卸载或错卷挂到同一路径，已打开的提交者、工作者仍须安全处理。临时文件创建、对象发布与清理均不得落到系统卷的裸挂载目录或错卷；不能仅在进程启动时检查身份。SQLite中提交的新载荷引用必须由原卷上完整、持久发布且摘要正确的对象支撑。掉线造成的未完成步骤可重试，已持久接纳的批次不得丢失，完成结果和原回执不变。

原卷挂回约定位置后无需人工改库、改绑定或补文件，待办自动完成，并能接纳新批。原卷身份正确且可访问时，缺失载荷、摘要不符或非法内容属于数据故障，应明确报错；摘要不符沿用PAYLOAD_HASH，不得把它们误报成缺卷并无限重试。本题不要求自动修复坏载荷。

## 磁盘映像与确定性交错设施

volumes.py仅负责实验磁盘，不包含生产身份守卫、重连逻辑或检查点恢复答案。所有命令返回JSON：

- python3 volumes.py open AREA：AREA须不存在。创建original和foreign两张64MiB稀疏磁盘映像，分别预建payload目录，最后将original挂到AREA/载荷挂载点。返回mount、payload、device、mounted、images、uuids，并保存AREA/volumes.json。
- python3 volumes.py offline AREA：用hdiutil detach -force真实卸载当前实验卷，模拟运行中掉线，保留裸挂载目录。
- python3 volumes.py mount AREA foreign：当前必须没有实验卷挂载，将foreign挂到同一位置。
- python3 volumes.py mount AREA original：同样要求先卸载，将原映像原位重挂。
- python3 volumes.py close AREA：正常卸载当前实验卷；先停止实验进程，再清理AREA。

安装使用返回的payload作为--payload-dir，DEST不放在实验挂载点内。错卷阶段可预置哨兵文件并记录完整文件快照，确认提交、工作者恢复和临时文件清理没有修改它。缺卷阶段检查裸挂载目录，不能只检查数据库。实验脚本在挂载点非空时拒绝覆盖；若错误实现写入裸挂载目录，应保留失败证据后再清理，不能以重新挂载掩盖误写。

Gate由POINTWISE_PAUSE指定一次性事件、POINTWISE_GATE指定观察目录；命中写ready.json，等待continue文件。已有事件为before_object_publish、object_ready、before_accept、after_accept、checkpoint_loaded、before_checkpoint、after_checkpoint、before_finish、after_finish。Gate目录必须位于内置卷。

为确定性验证新增volume_checked事件：位于一次载荷读或写完成身份核验后、首次访问该次载荷文件前，detail含operation，值为get或put。这只是观察点，不限定守卫方案。使用该事件以及已有发布、引用提交观察点，暂停已打开进程，真实卸载并在原位挂错卷，再释放或终止进程，随后挂回原卷并重开，检查没有误写及无依据引用。仅通过重命名普通目录或伪造UUID不能替代真实挂卸实验。

## 交付与上线回归

保留checks.py全部原业务断言。扩展releasecheck.py，使python3 releasecheck.py经安装产物核对原业务，以及缺卷启动、错卷占位、身份核验后掉线、发布或引用提交前掉线、进程终止重开和原卷恢复。覆盖已打开的提交者和工作者，核对待办、完整结果、接纳及完成回执、同批重投；另以原卷上的损坏载荷核对明确失败。保留可重跑的命令、实际输出及未通过项目，不能只打印成功。

真实上线部分由默认安装加载用户LaunchAgent：先在原卷存在时安装绑定并接纳待办，再缺卷启动或重启服务，检查不可用状态；随后经历错卷占位和原卷重挂，由自启动进程独立完成待办及恢复后新批次。移走构建源码并从其他工作目录运行，保留launchctl print输出和安装入口read的实际核账结果。结束时bootout本次标签并删除本次plist，避免遗留服务。没有GUI登录会话时须记为未执行，不能以--no-load或手工run冒充上线验证。

交付简短操作说明，写清首次绑定、缺卷与错卷的识别、原位重挂、数据故障处置以及服务卸载。将.pointwise-runtime.json的verify更新为["python3","releasecheck.py"]，demo更新为["python3","releasecheck.py","demo"]；demo展示安装后的真实结果、卷状态变化及重连核账，不只输出成功标记。基线清单目前只核对已有业务，不代表新部署要求已实现。
