# 群活动助手

Windows 桌面工具：接收所选 QQ 群的志愿活动通知，提取活动地点、时间、报名窗口和奖励；用户选择活动及参与人后，在开放时间自动填写和提交 WPS 表单。

使用 Python、Tkinter、SQLite、自写 QQ 接收适配器和 Playwright。群消息在本机解析，档案和浏览器登录状态保存在本机；新通知不会自动为所有人报名。

## 功能

- 活动名称和地点搜索，默认活动时间倒序，可切换有名额优先。
- 截止、取消、未开始、可报名状态及最近检查的名额。
- 取消、暂停、延期通知唯一匹配时停止排队；有歧义时保留变更通知。
- 多人档案，专业、班级和奖励选项按真实 WPS 表单读取；界面使用“学院积分”。
- 独立 QQ、WPS 在线状态；启动自动核验 WPS，完成登录后自动恢复后台。
- 顶部“刷新”和 F5 更新全部页面，保留搜索、当前页面和未保存输入。
- 持久化报名队列，提交前复查，结果不明时停止重试。

## 环境要求

- Windows 10/11 x64，Python 3.13 x64（含 Tkinter），Microsoft Edge。
- [uv](https://docs.astral.sh/uv/) 用于安装锁定的 Python 依赖。
- QQ 接收目前只支持 Windows x64 QQ **9.9.35-52892**，并校验安装文件 SHA-256；其它版本拒绝接入。
- 构建原生组件需要 Windows x64 [Zig](https://ziglang.org/download/) C 编译器，已验证 0.17.0。源码仓库不预置编译后的 DLL。
- Node.js 22 或以上用于接收器测试；正常接收使用 QQ 自带的 Node/Electron 环境。

## 从源码启动

在源码目录打开 PowerShell：

```powershell
uv sync --locked
.\native\build.ps1 -Zig C:\path\to\zig.exe
uv run python app.py
```

将 Zig 路径替换为实际安装位置，完成后应存在 `native/qq_activity_boot.dll`。也可双击 `运行.cmd` 启动；首次使用需先安装 uv 和构建原生组件。

1. 在“连接与设置”填写接收群号并保存。仓库不预置真实群号或个人资料。
2. 选择 QQ 安装目录，点击“配置接收启动器”。保存聊天、通话、文件传输进度后，通过工具的“重启 QQ”加载接收模块，需要时正常登录 QQ。
3. 打开 WPS 登录窗口，在独立 Edge 窗口完成登录。程序每 2 秒核验，确认后切回后台再次检查；也可点击“我已完成登录”手动结束登录窗口。
4. 在“个人档案”选择活动作为选项来源，读取专业、班级题目后填写资料。
5. 双击活动核对时间、限制和参与人，加入队列。保持电脑唤醒、联网及工具运行。

活动和报名时间始终显示为**北京时间（UTC+8）**。未配置群号时显示一次可关闭引导；已配置后不会因暂时离线反复弹窗。

## 状态与刷新

QQ 在线表示最近心跳报告正在监听；WPS 在线表示官方账户页明确确认了专用浏览器的登录状态。两者分别核验，在线不等于报名成功。

WPS 启动自动检查，正常状态每 4 分钟重查，无法识别或网络错误时 30 秒重试。登录窗口每 2 秒自动检查，不打断扫码或验证码。缺少证据时显示“未确认”，不会只凭 Cookie 或跳转就显示在线。

“刷新”和 F5 重读活动、队列、档案及 QQ 状态，异步重查 WPS 登录和未截止活动名额。请求合并；到期报名优先，登录核验优先于一般预检。刷新本身不新增或提交报名。

## 表单限制

登录、一次填写、收集上限及开放时间按每张表单当前设置处理。开启一次填写限制时，多人共用账号的任务会暂停；不能据此推断全部 WPS 表单每账号只能填一份。

专业和班级是选项题时使用原选项，是文本题时显示填写说明。未知必填题、验证码、手机验证、受限渠道或无法识别的页面停止自动处理，原因显示在任务中。

点击提交后只有明确的可见成功确认才记为成功；断网或不明结果记为“结果待核实”，不盲目重试。提交成功不等于录取，录取以组织者审核或邮件为准。当前版本尚未通过真实志愿报名提交验证端到端成功。

## 本机数据

默认保存在 `%LOCALAPPDATA%\QQActivityAssistant`：活动数据库、个人资料、接收配置、独立 Edge 登录资料。自定义隔离目录：

```powershell
uv run python app.py --data-dir .\local-data
```

不要上传数据库、`bridge.json`、`receiver/launch.json`、浏览器资料、日志或私人截图。`.gitignore` 排除常见路径，提交前仍需核对。备份应关闭工具后复制数据目录并妥善保管。

关闭助手不会退出已启动的 QQ，报名执行随助手关闭而停止；重启后将中断时正在执行的任务标为待核实。普通 QQ 快捷方式不加载本接收器；恢复接收配置后普通启动即可。

## 检查与开发

```powershell
uv sync --locked
uv run python -m compileall -q qq_activity tests
uv run python -m unittest discover -s tests -v
node --check receiver/preload.cjs
node --test receiver/contract.test.cjs
```

浏览器和 Tk 测试需要 Windows 图形会话及系统 Edge。测试使用临时数据库、模拟组件和路由拦截的可控表单，不重启真实 QQ，不访问真实活动提交接口。Windows CI 执行同一组检查。QQ 实机仍需匹配版本及用户登录，模拟检查不代替实机验证。

见 [架构与边界](docs/ARCHITECTURE.md)、[贡献说明](CONTRIBUTING.md)、[安全报告](SECURITY.md)。

## 可执行程序状态

当前推荐源码启动。旧版 PyInstaller EXE 曾被 Defender 以 `Trojan:Win32/Bearfoos.B!ml` 隔离，原因未确认；没有恢复隔离文件、关闭防护或加入排除。GitHub 准备版本不包含该 EXE 或运行依赖包，源码和发布候选目录另行扫描。

开发者如需自行研究构建，先构建 DLL，再执行 `打包.cmd --experimental-exe`。该操作不能证明生成 EXE 通过了防护检测，分发前需另行验证。配置包含图标、原生组件、许可及说明，禁用 UPX。

## 许可证

原创代码与资源采用 **GNU Affero General Public License v3.0 only（AGPL-3.0-only）**，完整文本见 [LICENSE](LICENSE)。按现状提供，不作任何担保。使用、修改、分发以及通过网络向用户提供修改版本时，请遵守源码提供等要求，以 [GNU 官方文本](https://www.gnu.org/licenses/agpl-3.0.html) 为准。

第三方依赖保留原许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)、`LICENSES/`。QQ、WPS、Edge 不随仓库分发，本项目不代表其官方项目。
