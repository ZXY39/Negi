# Toolskill 编写检查清单

## 一、工具模式

- [ ] 已明确这是 `internal` 还是 `external`
- [ ] `external` 未再放置 `main/`
- [ ] `internal` 才包含 `main/tool.py`

## 二、`resource.yaml`

- [ ] 已填写 `tool_type`
- [ ] `external` 已填写 `install_dir`
- [ ] `external.install_dir` 位于 `tools/` 之外
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

## 四、安装与更新信息

- [ ] 已说明安装目录如何确定
- [ ] 已说明安装命令
- [ ] 已说明更新命令
- [ ] 已说明常见失败与回退

## 五、最终验收

- [ ] 工具可被资源发现
- [ ] `external` 不进入 callable tool 列表
- [ ] `load_tool_context` 能返回正确 `install_dir`
- [ ] 删除 `tools/<tool_id>/` 后，系统仍可启动
