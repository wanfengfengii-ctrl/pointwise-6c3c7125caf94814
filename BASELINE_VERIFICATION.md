# 初始基线核验记录

本记录仅描述出题阶段实际完成的检查，不代表目标运维能力已实现。

已读取原始业务源码、安装脚手架、契约、样例及原verification.json；未读取最终答案。pipeline.py、localio.py、manual.py、checks.py、ops.py、agentcheck.py和releasecheck.py沿用原始非答案实现。样例仅压缩排版，JSON内容不变。OPERATIONS.md替换为本题契约，volumes.py替换为原位挂卸实验工具；工具没有实现生产卷绑定或恢复逻辑。

实际执行：

1. env PYTHONDONTWRITEBYTECODE=1 python3 checks.py normal
   退出码0，stdout：NORMAL_REGRESSION_OK: revisions, tombstones, shared payloads, resume, receipts
2. env PYTHONDONTWRITEBYTECODE=1 python3 checks.py demo
   退出码0；a结果为s1/-1/sum7/count1、s2/1/sum-30/count2，b结果为s1/0/sum100/count1；checkpoints为空，保留载荷对象9个。
3. 在内存调用ops.bundle两次，断言字节相同，检查ZIP完整性并编译归档内全部源码。
   输出IN_MEMORY_PACKAGE_OK，包SHA-256为bac84ebdbc698efcaaa96e197257b4748182c2123bd7e770b4bf639ba94f594c。
4. 对本题volumes.py完整源码执行compile，输出MOUNT_LAB_SYNTAX_OK: hardware operations not executed。

当前工作区只读。解释器启动时向stderr报告无法创建xcrun缓存，但上述成功检查退出码均为0，业务断言通过。未执行磁盘文件回归、安装写入、磁盘映像创建挂卸或真实LaunchAgent；不将历史记录或语法检查当作这些项目的通过证据。最终交付须在OPERATIONS.md公开的可写本机登录环境完成安装产物及真实上线回归。
