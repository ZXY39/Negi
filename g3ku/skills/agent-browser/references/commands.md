# 常用命令映射

- 打开网页：`command="open"`
- 交互快照：`command="snapshot"`, `args=["-i"]`
- 点击：`command="click"`, `args=["@e1"]`
- 填写：`command="fill"`, `args=["@e2", "示例文本"]`
- 键入：`command="type"`, `args=["@e2", "示例文本"]`
- 读取文本：`command="get"`, `args=["text", "@e1"]`
- 等待稳定：`command="wait"`, `args=["--load", "networkidle"]`
- cookies：`command="cookies"`
- localStorage：`command="storage"`, `args=["local"]`
- state 保存：`command="state"`, `args=["save", "auth.json"]`
- state 加载：`command="state"`, `args=["load", "auth.json"]`
- 关闭浏览器：`command="close"`

