# C++ Review Workbench

独立运行在 IDA 所在 Linux 机器上的审阅界面。左侧读取 IDA 伪代码，右侧显示 C++ 源码；顶部可筛选版本、作者、模块和状态。

## 启动

1. 将整个 review_workbench 文件夹复制到 Linux，保留 web 目录。
2. 在 IDA 中打开目标数据库，通过 File → Script file 加载本目录 bridge.py。
3. 使用 IDA 外部的 Python 环境启动：

```sh
python3 -B app.py --check
python3 -B app.py
```

桌面模式需要图形桌面、Python 3.6+、PyQt5 和 PyQtWebEngine。它们安装在外部 Python 环境，不要改动 IDA 的 Python 环境。安装版本需适配机器的 Python 和系统库。已包含编译后的界面，运行不需要 Node.js。

若外部 Python 没有 QtWebEngine，可明确选择本机浏览器模式：

```sh
python3 -B app.py --browser
```

浏览器模式中手动输入路径；关闭浏览器后，在启动终端按 Ctrl+C 结束服务。

## 设置与使用

打开 Project settings，指定 review_list 和 C++ 源码根目录，点击 Rescan 选择 IDA 数据库，再填写该数据库对应模块，例如 power2。

列表中的 0x383370:power，通过源码注释
`//0x383370:power#0x499700:power2 #Target:6509#`
定位到 IDA 地址 0x499700。每个函数独立匹配；存在多个源码候选时需要选择。

- Complete & Next：在原列表对应行追加空格和 6，成功保存后继续下一项。
- Skip：保存在工具状态中，不给原列表添加 6。
- Undo：撤销本次会话最近一次完成标记。
- Open in IDA：跳到原生 IDA 进行类型调整、重命名和交叉引用检查；返回界面时重新获取伪代码，也可手动刷新。
- Open in VS Code：要求 Linux PATH 中有 code 命令。
- 源码索引按地址注释划分展示片段，不是完整 C++ AST 解析器；源码变动后可重建索引。

保存前会检查列表、源码和伪代码变化。列表备份保存在列表旁的 .review-backups 目录中。设置保存在 ~/.config/ida-review-workbench/ 下。

bridge.py 是独立桥接脚本，不依赖 ida_type_sync.py。服务仅绑定本机回环地址；GUI 和 IDA 应使用同一 Linux 用户运行。

## 演示与验证

```sh
python3 -B app.py --demo
python3 -B -m unittest discover -s tests -v
```

演示使用隔离的临时文件和模拟 IDA，不操作真实项目。当前 17 项离线测试通过，覆盖文件写回、撤销、冲突检查、地址映射及模拟 IDA RPC。尚未完成真实 Linux IDA 和 Qt 桌面环境联调。

开发界面时，在 frontend 下执行 npm ci 和 npm run build。发布时需保留重新生成的 web 目录。
