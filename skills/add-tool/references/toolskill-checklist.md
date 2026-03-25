# Toolskill 编写检查清单

## 一、工具模式

- [ ] 已明确这是 `internal` 还是 `external`
- [ ] `external` 未再放置 `main/`
- [ ] `internal` 才包含 `main/tool.py`

## 二、`resource.yaml`

- [ ] 已填写 `tool_type`
- [ ] `external` 已填写 `install_dir`
- [ ] `external.install_dir` 位于工作区根目录的 `externaltools/<tool_id>/`
- [ ] `external` 的下载 / 缓存 / 解压中转目录约定为工作区根目录的 `temp/<tool_id>/`
- [ ] `internal` 未填写 `install_dir`
- [ ] `external` 未填写 `source.vendor_dir`
- [ ] 已填写 `source`
- [ ] 已填写 `current_version`
- [ ] 已填写 `governance`

## 三、主 toolskill

- [ ] 已说明包装层契约或注册契约
- [ ] 已说明上游知识入口
- [ ] 外置工具包含 `何时使用 / 安装 / 更新 / 使用` 四段
- [ ] 内置工具已明确说明无需独立安装
- [ ] 已说明如何定位真实执行入口或仓库内实现位置
- [ ] 已明确写出 `tools/<tool_id>/` 只负责注册，`externaltools/<tool_id>/` 才是真实安装目录
- [ ] 已明确写出 `temp/<tool_id>/` 用于下载 / 缓存 / 解压中转

## 四、安装与更新信息

- [ ] 已说明安装目录如何确定
- [ ] 已说明安装命令
- [ ] 已说明更新命令
- [ ] 已说明常见失败与回退

## 五、异步与非阻塞

- [ ] 工具执行路径不会阻塞事件循环
- [ ] 阻塞 I/O（网络、子进程、压缩解压、大文件复制、重扫描等）已异步化或移到后台线程 / 子进程
- [ ] 外部命令与网络请求已设置明确超时
- [ ] 长任务有阶段性进度反馈或后台化方案
- [ ] 单个工具失败或卡顿不会把 Web、资源管理页或通信配置一起拖死

## 六、强取消

- [ ] 工具支持统一 cancellation token 或等价取消信号
- [ ] 子进程句柄已保存，暂停 / 取消时会主动 terminate / kill
- [ ] 长任务阶段会主动检查取消状态并尽快退出
- [ ] 暂停 / 取消时会给用户“正在安全停止...”之类的中间反馈
- [ ] 外置工具的真实执行入口也实现了强取消，而不只是文档里提到

## 七、最终验收

- [ ] 工具可被资源发现
- [ ] `external` 不进入 callable tool 列表
- [ ] `load_tool_context` 能返回正确 `install_dir`
- [ ] 删除 `tools/<tool_id>/` 后，系统仍可启动
